# ruff: noqa: E501
"""OpportunityEngine: closed M5 bars (+ latest quote) -> ``(OpportunitySnapshot, Decision)`` pairs.

Causality
---------
``on_m5_close(market, now)`` reads only bars whose close is ``<= now`` and emits ONLY candidates whose
decision index is the just-closed bar. The V2 generators mask a decision when the bar AFTER it does not
exist (``entry_mask``); for the live bar that next bar cannot exist yet, so the engine appends ONE
placeholder bar (open = high = low = close = last close, tick volume 0, timestamp = last + 5 min).
The placeholder contributes calendar facts only (is the entry bar contiguous, in the same local day and
inside the spec's entry window -- the same facts the simulator checks at the actual next bar); because
its OHLC equals the last close it cannot extend any range, ATR or VWAP of bar ``i``. The
truncation-invariance and perturbation tests prove that nothing after bar ``i`` reaches the output.

This is the LIVE path: ``build_family_data`` refuses bars after 2026-08-31 (research holdout guard), so
the engine assembles ``FamilyData`` through the same ``_assemble`` step without that research-only guard.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import numpy as np
import pandas as pd

from alpha.common.market_data import align_markets
from alpha.families.data import (
    ATR_WINDOW,
    FamilyData,
    LeaderFeatures,
    _assemble,
    atr14,
    round_steps,
)
from alpha.families.registry import describe_candidate, generate_candidates
from alpha.families.spec import MarketCalendar, tod_bounds
from alpha.fast.sim import CandidateArrays
from alpha.session import local_clock
from demo.contracts import Decision, OpportunitySnapshot, Phase, TradeIntent, opportunity_id_for
from demo.opportunity.bar_source import (
    M5_SECONDS,
    BarSource,
    Quote,
    closed_bars_only,
    tick_activity_of,
    validate_frame,
)
from demo.opportunity.clock import forced_flat_utc, local_minute_of, local_of, to_utc
from demo.opportunity.policy import (
    Candidate,
    StaticDemoPolicy,
)
from demo.opportunity.production_spec import (
    ALPHA_STATUS,
    PHASE2_TAG,
    ROLE_SHADOW,
    FrozenSpec,
    ProductionSpecSet,
    load_production_spec,
    market_spec_hash,
)
from demo.opportunity.snapshot import (
    build_context,
    build_snapshot,
    build_structure,
    build_versions,
    git_commit,
    realized_vol,
)
from demo.sequence_metrics import regime_of
from markets.spec import PHASE2_CANONICALS, MarketSpec, load_market_spec

# Reason codes of a CATCH-UP decision: an opportunity found on a bar that is already older than one bar
# when it is evaluated is NEVER tradable (no chasing of stale entries); it is recorded as a terminal,
# explained non-trade and labelled counterfactually like every other rejection.
CATCHUP_MISSED = "CATCHUP_MISSED"
EXPIRED_ENTRY = "EXPIRED_ENTRY"
ALREADY_MOVED = "ALREADY_MOVED"
CATCHUP_CODES: tuple[str, ...] = (CATCHUP_MISSED, EXPIRED_ENTRY, ALREADY_MOVED)
ORIGIN_CATCHUP = "CATCHUP"
# Lane F: a SHADOW-role spec (v1.2) is evaluated, snapshotted and counterfactually labelled like any rejected opportunity,
# but an otherwise ACCEPTED decision is turned into this terminal non-trade: no intent, no broker order, ever.
SHADOW_VARIANT = "SHADOW_VARIANT"


@dataclass(frozen=True, slots=True)
class CatchupInfo:
    """Evaluate a PAST closed bar causally (``now`` = that bar's close) while knowing the real clock.

    ``live_now``   real processing time (the decision is stamped with it; the age is ``live_now - signal``);
    ``live_quote`` the current executable quote, only used to classify ``ALREADY_MOVED`` (never to trade).
    """

    live_now: datetime
    live_quote: Quote | None = None


@dataclass(frozen=True, slots=True)
class ShadowScan:
    """Lane U2: evaluate closed bars for MEASUREMENT only (never an intent, never the stack).

    ``code``          the terminal REJECT code every emitted decision carries as its primary reason
                      (``OUT_OF_WINDOW_SHADOW`` | ``SHADOW_UNIVERSE``); an otherwise ACCEPTED assessment is turned into
                      exactly this rejection, so the existing counterfactual labeller labels it like any non-trade;
    ``origin``        persisted as ``signal.origin`` (also the ``_last_bar`` idempotence namespace);
    ``relax_window``  evaluate the generators on a LIVE-ONLY copy of the market calendar whose entry window is
                      [00:00, 24:00); candidates whose entry bar lies INSIDE the real (tod-adjusted) window are
                      dropped (the normal path owns those). Frozen specs / research windows are never mutated;
    ``tags``          merged into ``signal`` (cluster, asset_class, ...);
    ``admit``         optional dedupe / cap callback ``(candidate) -> bool`` (False = not recorded).
    """

    code: str
    origin: str
    relax_window: bool = False
    tags: Mapping[str, Any] = field(default_factory=dict)
    admit: Callable[[Candidate], bool] | None = None


def relaxed_market_spec(ms: MarketSpec) -> MarketSpec:
    """Live-only copy of ``ms`` with the entry window opened to the whole local day (clock exit = end of day)."""
    cal = dataclasses.replace(ms.calendar, entry_start_min=0, entry_end_min=1440, forced_flat_min=1440)
    return dataclasses.replace(ms, calendar=cal)


DEFAULT_WINDOW_BARS = 6000  # ~3 weeks of M5: D1 ATR14 and the previous cash day are always inside
DEFAULT_MIN_HISTORY_BARS = 600


class SeenStore(Protocol):
    """Persisted dedupe set (Lane D implements the SQLite side, e.g. ``INSERT OR IGNORE``)."""

    def add_if_new(self, opportunity_id: str) -> bool:
        """Atomically record ``opportunity_id``; True if it was NEW, False if already seen."""
        ...


class InMemorySeenStore:
    def __init__(self, initial: tuple[str, ...] = ()) -> None:
        self._seen: set[str] = set(initial)

    def add_if_new(self, opportunity_id: str) -> bool:
        if opportunity_id in self._seen:
            return False
        self._seen.add(opportunity_id)
        return True

    def __len__(self) -> int:
        return len(self._seen)


# ------------------------------------------------------------------------------- data assembly
def _dt_index_ns(ts_ns: np.ndarray) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(np.asarray(ts_ns, dtype="int64").view("datetime64[ns]"), tz="UTC")


def live_leader_features(
    follower_ts_ns: np.ndarray, leader_frame: pd.DataFrame, leader_cal: MarketCalendar, name: str,
    ns: tuple[int, ...] = (1, 3, 6, 12),
) -> LeaderFeatures:
    """Same numbers as ``alpha.families.data.build_leader_features`` without the research holdout guard.

    ``leader_frame`` must contain closed bars only; each follower bar is matched to the leader's latest
    bar COMPLETED at the follower bar's close (``align_markets``, prefix stable)."""
    fol = pd.DataFrame({"ts": _dt_index_ns(follower_ts_ns)})
    al = align_markets({"F": fol, "L": leader_frame}, ref="F", max_lag_bars=1)["L"]
    lts = pd.DatetimeIndex(leader_frame["ts"]).as_unit("ns").asi8
    h, low, c = (leader_frame[k].to_numpy(float) for k in ("high", "low", "close"))
    atr = atr14(h, low, c)
    step_ns = M5_SECONDS * 10**9
    scale = np.where(atr > 0, atr, np.nan)
    ret: dict[int, np.ndarray] = {}
    for n in ns:
        r = np.full(len(c), np.nan)
        if len(c) > n:
            ok = (lts[n:] - lts[:-n]) == n * step_ns
            r[n:] = np.where(ok, (c[n:] - c[:-n]) / scale[n:], np.nan)
        ret[n] = al.take(r)
    minute, _ = local_clock(lts, leader_cal.session_calendar())
    in_cash = ((minute >= leader_cal.cash_open_min) & (minute < leader_cal.cash_close_min)).astype(float)
    return LeaderFeatures(name, ret, al.take(in_cash) > 0.5)


def assemble_live(
    market: str, mspec: MarketSpec, frame: pd.DataFrame,
    leaders: Mapping[str, tuple[pd.DataFrame, MarketCalendar]] | None = None,
) -> FamilyData:
    """FamilyData of ``frame`` (closed M5 bars) + ONE placeholder next bar (see module doc).
    The last row of the result is the placeholder; the deciding bar is ``len(result) - 2``."""
    cal = MarketCalendar.from_market_spec(mspec)
    ts = pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8.astype(np.int64)
    o, h, low, c = (frame[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    spread = frame["spread_pts"].to_numpy(float) * float(mspec.point_size)
    vol = frame["tick_volume"].to_numpy(float)
    ts_p = np.r_[ts, ts[-1] + M5_SECONDS * 10**9]
    last = c[-1]
    o_p, h_p, l_p, c_p = (np.r_[a, last] for a in (o, h, low, c))
    spread_p = np.r_[spread, spread[-1]]
    vol_p = np.r_[vol, 0.0]
    cross: dict[str, LeaderFeatures] = {}
    for name, (lf, lcal) in (leaders or {}).items():
        if len(lf) > ATR_WINDOW + 12:
            cross[name] = live_leader_features(ts_p, lf, lcal, name)
    return _assemble(
        market, cal, ts_p, o_p, h_p, l_p, c_p, spread_p, vol_p,
        round_steps(mspec.asset_class, mspec.tick_size), cross,
    )


def make_candidate(
    market: str, mspec: MarketSpec, fs: FrozenSpec, data: FamilyData, cands: CandidateArrays, k: int,
) -> Candidate:
    i = int(cands.decision_idx[k])
    open_ns = int(data.ts_ns[i])
    bar_open = datetime.fromtimestamp(open_ns // 10**9, tz=UTC)
    signal = datetime.fromtimestamp(open_ns // 10**9 + M5_SECONDS, tz=UTC)
    ms_r = float(cands.min_space_r[k]) if cands.min_space_r is not None else float("nan")
    return Candidate(
        market=market, broker_symbol=mspec.broker_symbol, family=fs.family,
        strategy_id=fs.strategy_id, spec_hash=fs.spec_hash, direction=int(cands.direction[k]),
        signal_ts=signal, bar_open_ts=bar_open, close=float(data.c[i]), atr=float(data.atr[i]),
        bar_spread=float(data.spread[i]), stop=float(cands.stop[k]), target=float(cands.target[k]),
        target_r=float(cands.target_r[k]), exit_kind=int(cands.exit_kind[k]), min_space_r=ms_r,
        window=fs.spec.effective_window(data.cal),
    )


def opportunity_id_of(c: Candidate) -> str:
    return opportunity_id_for(
        c.market, c.strategy_id, c.spec_hash, to_utc(c.signal_ts).isoformat(), c.direction
    )


def entry_in_real_window(ms: MarketSpec, spec: Any, signal_ts: datetime, bar_open_ts: datetime) -> bool:
    """True if the ENTRY bar (opens at ``signal_ts``) lies inside ``spec``'s REAL (tod-adjusted) entry window on the deciding
    bar's local day - the exact test of ``StaticDemoPolicy.assess``. Such a bar belongs to the normal (tradable) path."""
    w = spec.effective_window(MarketCalendar.from_market_spec(ms))
    lo, hi = tod_bounds(w.entry_start_min, w.entry_end_min, getattr(spec, "tod", "all"))
    sig = to_utc(signal_ts)
    return local_of(ms, sig).date() == local_of(ms, bar_open_ts).date() and lo <= local_minute_of(ms, sig) < hi


# ------------------------------------------------------------------------------- engine
class OpportunityEngine:
    def __init__(
        self,
        source: BarSource,
        *,
        production: ProductionSpecSet | None = None,
        market_specs: Mapping[str, MarketSpec] | None = None,
        policy: StaticDemoPolicy | None = None,
        phase: Phase = "DISCOVERY",
        seen_store: SeenStore | None = None,
        position_open: Callable[[str], bool] | None = None,
        window_bars: int = DEFAULT_WINDOW_BARS,
        min_history_bars: int = DEFAULT_MIN_HISTORY_BARS,
        emit_duplicates: bool = False,
        commit: str | None = None,
        sequence_tracker: Any | None = None,
    ) -> None:
        self._source = source
        self._prod = production or load_production_spec()
        names = self._prod.market_names()
        self._mspec: dict[str, MarketSpec] = (
            dict(market_specs) if market_specs is not None else {m: load_market_spec(m) for m in names}
        )
        self._policy = policy or StaticDemoPolicy()
        self._phase: Phase = phase
        self._seen: SeenStore = seen_store if seen_store is not None else InMemorySeenStore()
        self._position_open = position_open or (lambda _m: False)
        self._window = window_bars
        self._min_hist = min_history_bars
        self._emit_dup = emit_duplicates
        self._commit = commit if commit is not None else git_commit()
        self._last_bar: dict[str, pd.Timestamp] = {}
        self._intents: dict[str, TradeIntent] = {}
        self.last_intents: list[TradeIntent] = []
        self.last_candidates: list[Candidate] = []  # ACCEPTED candidates of the last call
        self.health: dict[str, str] = {}
        self.suppressed_duplicates = 0
        self.config_hash = self._policy.config.config_hash()
        self._versions_cache: dict[str, dict[str, str]] = {}
        self._seq = sequence_tracker  # Lane U2: measure-only same-zone / flip / whipsaw metrics (None = off)
        self.shadow_skipped_in_window = 0

    @property
    def strategy_hash(self) -> str:
        return self._prod.strategy_hash

    def _versions(self, market: str) -> dict[str, str]:
        v = self._versions_cache.get(market)
        if v is None:
            cfg = f"{self.config_hash}|w{self._window}|m{self._min_hist}"
            v = build_versions(
                commit=self._commit, config_hash=cfg, market_spec_hash=market_spec_hash(self._mspec[market]),
                strategy_hash=self._prod.strategy_hash, policy_id=self._policy.policy_id,
            )
            self._versions_cache[market] = v
        return v

    def _leaders(self, market: str, now: datetime) -> dict[str, tuple[pd.DataFrame, MarketCalendar]]:
        need = sorted({
            getattr(fs.spec, "leader", "") for fs in self._prod.specs_for(market)
            if fs.family == "LEADLAG"
        })
        out: dict[str, tuple[pd.DataFrame, MarketCalendar]] = {}
        for ld in need:
            if ld not in self._mspec:
                continue
            fr = closed_bars_only(self._source.m5_frame(ld, self._window), now).reset_index(drop=True)
            if len(fr):
                validate_frame(fr, ld)
                out[ld] = (fr, MarketCalendar.from_market_spec(self._mspec[ld]))
        return out

    def _catchup_decision(
        self, snap: OpportunitySnapshot, cand: Candidate, assessment, catchup: CatchupInfo,
    ) -> Decision:
        live_now = to_utc(catchup.live_now)
        dec = self._policy.decision(snap.opportunity_id, self._phase, live_now, assessment)
        if not dec.accepted:
            return dec  # an engine reject stays exactly that (its own gate codes), just decided late
        reasons = [CATCHUP_MISSED, EXPIRED_ENTRY]
        q = catchup.live_quote
        if q is not None and q.valid:
            live_exec = q.ask if cand.direction > 0 else q.bid
            tol = abs(assessment.geometry.entry_zone_hi - assessment.geometry.entry_zone_lo) / 2.0
            if abs(live_exec - cand.close) > tol:
                reasons.append(ALREADY_MOVED)
        return Decision(
            opportunity_id=dec.opportunity_id, phase=dec.phase, decided_utc=dec.decided_utc, accepted=False,
            reasons=tuple(reasons), policy_id=dec.policy_id, shadow=dec.shadow,
        )

    def intents_for(self, pairs: list[tuple[OpportunitySnapshot, Decision]]) -> list[TradeIntent]:
        return [self._intents[s.opportunity_id] for s, d in pairs
                if d.accepted and s.opportunity_id in self._intents]

    def on_m5_close(
        self, market: str, now_utc: datetime, *, catchup: CatchupInfo | None = None,
        shadow: ShadowScan | None = None,
    ) -> list[tuple[OpportunitySnapshot, Decision]]:
        """Closed-bar evaluation. ``catchup`` set: ``now_utc`` is the CLOSE of a past bar (causal
        truncation), the quote is synthesised from that bar (close + recorded bar spread: there is no
        historical executable quote), snapshots carry ``signal.origin = CATCHUP`` and an opportunity the
        policy would have accepted is recorded as a rejected ``CATCHUP_MISSED`` decision (never an intent).

        ``shadow`` (Lane U2): measurement-only scan (out-of-window relaxed window / shadow universe): see ``ShadowScan``.
        With ``shadow=None`` (the default) this method is exactly the pre-U2 code path."""
        now = to_utc(now_utc)
        self.last_intents = []
        self.last_candidates = []
        ms = self._mspec[market]
        frame = self._source.m5_frame(market, self._window)
        frame = closed_bars_only(frame, now).reset_index(drop=True)
        if len(frame) < self._min_hist:
            self.health[market] = "insufficient_history"
            return []
        validate_frame(frame, market)
        last_ts = pd.Timestamp(frame["ts"].iloc[-1])
        bar_key = market if shadow is None else f"{market}|{shadow.origin}"
        if self._last_bar.get(bar_key) == last_ts:
            return []  # this closed bar was already processed (idempotent per bar)
        self.health[market] = "ok"

        data = assemble_live(
            market, relaxed_market_spec(ms) if shadow is not None and shadow.relax_window else ms, frame,
            self._leaders(market, now),
        )
        i = len(data) - 2  # deciding bar; the last row is the placeholder
        if catchup is None:
            quote = self._source.latest_quote(market)
        else:
            last_close = float(frame["close"].iloc[-1])
            last_spread = float(frame["spread_pts"].iloc[-1]) * float(ms.point_size)
            quote = Quote(ts_utc=now, bid=last_close, ask=last_close + last_spread)
        found: list[tuple[FrozenSpec, Candidate]] = []
        for fs in self._prod.specs_for(market):
            cands = generate_candidates(data, fs.spec, fs.thr)
            hit = np.flatnonzero(cands.decision_idx == i)
            if len(hit) == 0:
                continue
            cand0 = make_candidate(market, ms, fs, data, cands, int(hit[0]))
            if shadow is not None and shadow.relax_window and self._in_real_window(ms, fs, cand0):
                self.shadow_skipped_in_window += 1  # the normal (tradable) path owns bars inside the real window
                continue
            if shadow is not None and shadow.admit is not None and not shadow.admit(cand0):
                continue  # deduped / capped by the caller (counted there)
            found.append((fs, cand0))
        if not found:
            self._last_bar[bar_key] = last_ts
            return []

        ctx = build_context(frame, found[0][1].signal_ts, ms.calendar.tz)
        rvol = realized_vol(frame["close"].to_numpy(float))
        tick = tick_activity_of(self._source, market)
        n_dir = {d: sum(1 for f, c in found if c.direction == d and f.role != ROLE_SHADOW) for d in (1, -1)}
        pairs: list[tuple[OpportunitySnapshot, Decision]] = []
        for fs, cand in found:
            oid = opportunity_id_of(cand)
            fresh = self._seen.add_if_new(oid)
            if not fresh and not self._emit_dup:
                self.suppressed_duplicates += 1
                continue
            # No one-position rule: every technically valid opportunity is emitted. ``position_open``
            # stays on the constructor for API compatibility only and is never consulted.
            assessment = self._policy.assess(cand, quote, now, ms, is_duplicate=not fresh)
            same = [f for f, c in found if c.direction == cand.direction and f.role != ROLE_SHADOW]
            signal_meta = {
                "family": fs.family, "strategy_id": fs.strategy_id, "spec_hash": fs.spec_hash,
                "spec": fs.spec.to_dict(),
                "thr": [None if not math.isfinite(v) else v for v in fs.thr_values],
                "confluence": n_dir[cand.direction],
                "independent_clusters": len({f.family for f in same}),
                "opposing_specs": n_dir[-cand.direction],
                "quality": None,
                "target_r": None if not math.isfinite(cand.target_r) else cand.target_r,
                "structural_target": math.isfinite(cand.target),
                "role": fs.role,
            }
            if shadow is not None:
                signal_meta["origin"] = shadow.origin
                signal_meta.update(shadow.tags)
                if shadow.relax_window:
                    w0 = fs.spec.effective_window(MarketCalendar.from_market_spec(ms))
                    signal_meta["window_relaxed"] = {
                        "real_entry_start_min": w0.entry_start_min, "real_entry_end_min": w0.entry_end_min,
                        "real_exit_min": w0.exit_min, "note": "live-only relaxed copy; frozen spec untouched",
                    }
            if market in PHASE2_CANONICALS:  # persisted with every Phase-2 snapshot (and so with its outcome/label join)
                signal_meta["phase"] = PHASE2_TAG
                signal_meta["alpha_status"] = ALPHA_STATUS
            levels = describe_candidate(data, fs.spec, i, cand.direction)
            if levels:
                signal_meta["structure_levels"] = levels
            if self._seq is not None:
                signal_meta["sequence"] = self._seq.observe(
                    market=market, oid=oid, signal_ts=cand.signal_ts, direction=cand.direction, family=fs.family,
                    stop=cand.stop, close=cand.close, atr=cand.atr, regime=regime_of(ctx),
                )
            if catchup is not None:
                signal_meta["origin"] = ORIGIN_CATCHUP
                signal_meta["catchup"] = {
                    "live_now": to_utc(catchup.live_now).isoformat(),
                    "age_s": (to_utc(catchup.live_now) - to_utc(cand.signal_ts)).total_seconds(),
                    "synthetic_quote": "bar_close_plus_recorded_bar_spread",
                    "engine_verdict": "ACCEPTED" if assessment.accepted else list(assessment.reasons),
                }
            ff = forced_flat_utc(ms, cand.signal_ts, cand.window.exit_min).isoformat()
            snap = build_snapshot(
                cand=cand, assessment=assessment, phase=self._phase, created_utc=now, mspec=ms,
                versions=self._versions(market), context=ctx,
                structure=build_structure(data, i, ms, cand.direction), signal_meta=signal_meta,
                tick_activity=tick, rvol=rvol, forced_flat_iso=ff,
            )
            if shadow is not None:
                base = self._policy.decision(snap.opportunity_id, self._phase, now, assessment)
                rest = tuple(r for r in base.reasons if r != "ACCEPTED" and r != shadow.code)
                pairs.append((snap, Decision(
                    opportunity_id=base.opportunity_id, phase=base.phase, decided_utc=base.decided_utc, accepted=False,
                    reasons=(shadow.code, *rest), policy_id=base.policy_id, shadow=base.shadow,
                )))
                continue  # measurement only: no intent, ever
            if catchup is None:
                dec = self._policy.decision(snap.opportunity_id, self._phase, now, assessment)
            else:
                dec = self._catchup_decision(snap, cand, assessment, catchup)
            if fs.role == ROLE_SHADOW and dec.accepted:
                dec = Decision(
                    opportunity_id=dec.opportunity_id, phase=dec.phase, decided_utc=dec.decided_utc, accepted=False,
                    reasons=(SHADOW_VARIANT,), policy_id=dec.policy_id, shadow=dec.shadow,
                )
            if dec.accepted:
                intent = self._policy.intent_for(snap, dec, ms, cand.window)
                if intent is not None:
                    self._intents[snap.opportunity_id] = intent
                    self.last_intents.append(intent)
                    self.last_candidates.append(cand)
            pairs.append((snap, dec))
        # marked processed only after the WHOLE bar was built without an exception: a failure anywhere
        # above leaves the bar retryable (the runner retries once, then records a SCAN_ERROR)
        self._last_bar[bar_key] = last_ts
        return pairs

    def _in_real_window(self, ms: MarketSpec, fs: FrozenSpec, cand: Candidate) -> bool:
        return entry_in_real_window(ms, fs.spec, cand.signal_ts, cand.bar_open_ts)

    # ---- Lane U2 read accessors (no behaviour) -------------------------------------------------
    supports_shadow_scan = True

    def market_spec(self, market: str) -> MarketSpec:
        return self._mspec[market]

    def specs_for(self, market: str) -> tuple[FrozenSpec, ...]:
        return self._prod.specs_for(market)

    def release_seen(self) -> None:
        """Drop in-flight (not yet persisted) seen ids so a bar whose persistence failed can be rebuilt.
        Persisted opportunities stay seen through their snapshot row."""
        fn = getattr(self._seen, "release_all", None)
        if fn is not None:
            fn()
