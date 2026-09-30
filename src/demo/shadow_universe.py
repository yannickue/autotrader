# ruff: noqa: E501
"""Lane U2: OUT_OF_WINDOW forward shadow + SHADOW-UNIVERSE collection. OPT-IN, failure-isolated, measurement only.

Both mechanisms live INSIDE the main runner process (no second MT5 process: the real trader holds the MT5 global
lock) and are OFF by default. Neither can ever trade:

* every record they produce is a REJECTED decision whose primary reason is ``OUT_OF_WINDOW_SHADOW`` /
  ``SHADOW_UNIVERSE`` (engine ``ShadowScan``: no intent is ever created), it is persisted as an ordinary
  snapshot + decision (DemoStore) and labelled counterfactually by the existing ``demo.labeling`` (counterfactual_meta
  source = the code), and the funnel/report show it in separate sections;
* shadow-universe markets are not in any stack registry (hard constructor guard ``forbidden`` + runner guard).

A) ``OutOfWindowShadow``  active markets: while the broker is TRADABLE (fresh quote, not a known pause) but a frozen
   family's entry window is closed, run the SAME generators on the closed bar with a LIVE-ONLY relaxed copy of the
   calendar (``engine.relaxed_market_spec``; frozen specs / research windows untouched) and record what they WOULD
   have signalled. Deduped + capped per (market, family, direction, zone, UTC day), bounded by a per-cycle wall
   budget, exceptions contained and counted.
B) ``ShadowUniverseScanner``  markets of ``configs/markets_shadow`` (shadow_only): bars/quotes through a READ-ONLY
   ``ShadowLiveBarSource`` (the runner's own MT5 session/lane: ``copy_rates_from_pos`` + ``symbol_info_tick`` only; NO
   ``symbol_select``, Market Watch is never touched - a symbol the terminal cannot serve is counted as an error
   and retried on the next bar boundary), the fit-free STRUCT family set (see ``SHADOW_FAMILY_POLICY``), round-robin
   batches under a wall-time budget, per-symbol failure isolation, heartbeat section ``shadow_universe``.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pandas as pd

from demo.contracts import Decision, OpportunitySnapshot
from demo.execution.stack_port import StackFailClosed
from demo.opportunity.bar_source import M5_SECONDS, Quote
from demo.opportunity.engine import OpportunityEngine, ShadowScan, entry_in_real_window
from demo.opportunity.policy import OUT_OF_WINDOW_SHADOW, SHADOW_UNIVERSE, Candidate
from demo.opportunity.shadow_specs import shadow_production_set
from markets.shadow import ShadowMarketSpec, load_shadow_specs
from markets.spec import MarketSpec, SessionBucket, SessionCalendar

ORIGIN_OOW = "OUT_OF_WINDOW_SHADOW"
ORIGIN_UNIVERSE = "SHADOW_UNIVERSE"
PENDING_FLAG = "quote_freshness_unverified_market_closed_at_scan"
ALL_READY = "all-ready"

# Shadow markets that have no round-number scale in ``alpha.families.data.ROUND_TICKS`` borrow the index scale ONLY so that
# ``_assemble`` can build its (context-only) round features; no ROUND family is evaluated for any shadow market.
ASSET_CLASS_BY_CLUSTER = {
    "FX": "fx_cfd", "INDEX": "index_cfd", "METALS": "metal_cfd", "ENERGY": "energy_cfd", "CRYPTO": "crypto_cfd",
    "COMMODITY_SOFT": "index_cfd", "STOCKS": "index_cfd", "BONDS": "index_cfd", "OTHER": "index_cfd",
}
SHADOW_FAMILY_POLICY = (
    "Fit-free, session-agnostic STRUCT family set (class defaults of production spec v1.2: 1 'confirmed' + 3 "
    "'breakout/retest/fade' variants, thresholds empty, NO fitted parameter) for EVERY shadow asset class. The cash-session "
    "families (ORB, GAP, OVERNIGHT, VOLREV, ROUND, EOD, LEADLAG) are deliberately NOT applied: they need Train-fitted "
    "thresholds and/or a validated cash session / round-number scale / leader pair, none of which exists for a shadow "
    "symbol whose calendar is only PROVISIONAL (derived from observed bars). Lane F dropped ORB for BTCUSD/BRENT for the "
    "same reason (invented open)."
)
FAMILIES_BY_CLUSTER: dict[str, tuple[str, ...]] = {c: ("STRUCT",) for c in ASSET_CLASS_BY_CLUSTER}


class ShadowUniverseError(RuntimeError):
    """Configuration that would let a shadow-only market near the trading path (hard guard)."""


class ShadowReadError(RuntimeError):
    """A READ of a shadow symbol failed (contained per symbol; never a fail-closed for the trading runner)."""


# ------------------------------------------------------------------------------ spec conversion
def shadow_to_market_spec(spec: ShadowMarketSpec) -> MarketSpec:
    """Engine-compatible ``MarketSpec`` of a shadow market (NOT validated on purpose: ``MarketSpec.validate`` rejects every
    canonical outside the production universe, and a shadow market must never be loadable through that path).

    Calendar: session-agnostic 24 h UTC day (the observed provisional session is informational only), so the STRUCT family
    (which does not use a cash session) sees an always-open window."""
    cal = SessionCalendar(
        tz="UTC", cash_open_min=0, cash_close_min=1440, entry_start_min=0, entry_end_min=1440, forced_flat_min=1440,
        buckets=(SessionBucket(name="ALL", start_min=0, end_min=1440),),
        weekend_policy="not_modelled", holiday_policy="not_modelled",
        dst_notes="shadow: session-agnostic UTC day", status="provisional",
    )
    return MarketSpec(
        canonical=spec.canonical, broker_symbol=spec.broker_symbol, broker_path=spec.broker_path,
        asset_class=ASSET_CLASS_BY_CLUSTER.get(spec.cluster, "index_cfd"),
        point_size=spec.point_size, digits=spec.digits, contract_size=spec.contract_size, tick_size=spec.tick_size,
        volume_min=spec.volume_min, volume_step=spec.volume_step, volume_max=spec.volume_max,
        currency_profit=spec.currency_profit, currency_margin=spec.currency_margin,
        timezone="UTC", calendar=cal, max_entry_spread_price=spec.max_entry_spread_price,
        spread_model_notes="shadow: observed recorded spreads", max_leverage=min(30.0, spec.implied_leverage or 1.0),
        margin_notes="shadow only", history={}, data_quality_flags=spec.data_quality_flags,
        snapshot_date=spec.snapshot_date, server_tz_policy="shadow_only", research_only=True, trading_enabled=False,
    )


def select_shadow_markets(
    arg: str | Iterable[str] | None, specs: Mapping[str, ShadowMarketSpec] | None = None,
) -> tuple[dict[str, ShadowMarketSpec], set[str]]:
    """``(selected specs, pending canonicals)``. ``arg`` = ``all-ready`` (every spec of ``configs/markets_shadow``), a
    comma list / iterable of canonicals. PENDING = specs whose quote was stale at the Lane-U scan (market closed then):
    they are re-validated at runtime (fresh quote) before they are scanned."""
    allspecs = dict(specs) if specs is not None else load_shadow_specs()
    if arg is None or (isinstance(arg, str) and arg.strip() in ("", ALL_READY)):
        names = sorted(allspecs)
    else:
        names = [x.strip() for x in (arg.split(",") if isinstance(arg, str) else arg) if str(x).strip()]
        unknown = [n for n in names if n not in allspecs]
        if unknown:
            raise ShadowUniverseError(f"unknown shadow market(s) {unknown}: not in configs/markets_shadow")
    sel = {n: allspecs[n] for n in names}
    pending = {n for n, sp in sel.items() if PENDING_FLAG in sp.data_quality_flags}
    return sel, pending


# ---------------------------------------------------------------------------- read-only source
class _ShadowStackProxy:
    """Just enough of ``Mt5DemoStack`` for ``LiveBarSource._fetch/_quote`` to read SHADOW symbols. Reads go through the
    stack's own lane/session (single MT5 connection); any per-symbol read failure becomes ``ShadowReadError`` (contained),
    only a genuine stack fail-closed propagates."""

    def __init__(self, stack: Any, symbols: Mapping[str, str]) -> None:
        self._s = stack
        self._symbols = dict(symbols)

    @property
    def _cfg(self) -> Any:
        return self._s._cfg

    def _now(self) -> datetime:
        return self._s._now()

    def _session_or_fail(self) -> Any:
        return self._s._session_or_fail()

    def _market(self, market: str) -> Any:
        try:
            return SimpleNamespace(broker_symbol=self._symbols[market])
        except KeyError:
            raise ShadowReadError(f"unknown_shadow_market:{market}") from None

    def _on_lane(self, fn: Callable[..., Any], *args: Any, **_kw: Any) -> Any:
        try:
            return self._s._on_lane(fn, *args, retry_reads=False)
        except StackFailClosed:
            raise
        except ShadowReadError:
            raise
        except Exception as exc:
            raise ShadowReadError(f"{type(exc).__name__}: {exc}") from exc


def make_shadow_live_source(stack: Any, symbols: Mapping[str, str]) -> Any:
    """READ-ONLY ``BarSource`` for shadow symbols over the runner's own MT5 lane (``LiveBarSource`` with a proxy stack).
    REAL-MT5-UNVERIFIED: whether the terminal serves ``copy_rates_from_pos`` for a symbol that is not in Market Watch
    has not been observed in this lane; such a symbol shows up as ``errors`` in the heartbeat, not as a crash."""
    from demo.execution.live import LiveBarSource

    return LiveBarSource(_ShadowStackProxy(stack, symbols))  # type: ignore[arg-type]


# ------------------------------------------------------------------ A) out-of-window controller
def _zone_key(cand: Candidate) -> int | None:
    if cand.atr and cand.atr > 0 and cand.stop == cand.stop:
        return round(float(cand.stop) / (0.25 * float(cand.atr)))
    return None


@dataclass
class OutOfWindowShadow:
    """Runner-side controller of the OUT_OF_WINDOW forward shadow (flag ``out_of_window_shadow_enabled``)."""

    cap_per_zone_day: int = 2
    budget_s: float = 1.5
    clock: Callable[[], float] = time.monotonic
    counters: Counter = field(default_factory=Counter)
    last_ms: float = 0.0
    _cycle_start: float | None = None
    _cycle_ms: float = 0.0
    _seen: dict[tuple, int] = field(default_factory=dict)
    _day: str = ""
    last_error: str | None = None

    def begin_cycle(self) -> None:
        self._cycle_start = self.clock()
        self._cycle_ms = 0.0

    def _admit(self, cand: Candidate) -> bool:
        day = cand.signal_ts.astimezone(UTC).date().isoformat()
        if day != self._day:
            self._day, self._seen = day, {}
        key = (cand.market, cand.family, cand.direction, _zone_key(cand), day)
        n = self._seen.get(key, 0)
        if n >= self.cap_per_zone_day:
            self.counters["capped"] += 1
            return False
        self._seen[key] = n + 1
        return True

    def scan(
        self, engine: Any, market: str, signal_close: datetime, now: datetime, *, tradable: bool,
    ) -> list[tuple[OpportunitySnapshot, Decision]]:
        """One out-of-window pass for the live bar that closed at ``signal_close``. Never raises for an engine/data
        problem (counted in ``errors``); stack/persistence failures are not engine errors and are not caught here."""
        if not getattr(engine, "supports_shadow_scan", False):
            self.counters["skipped_engine_unsupported"] += 1
            return []
        if not tradable:
            self.counters["skipped_not_tradable"] += 1
            return []
        if self._cycle_start is not None and self.clock() - self._cycle_start >= self.budget_s:
            self.counters["skipped_budget"] += 1
            return []
        try:
            ms = engine.market_spec(market)
            bar_open = signal_close - timedelta(seconds=M5_SECONDS)
            if not any(not entry_in_real_window(ms, fs.spec, signal_close, bar_open) for fs in engine.specs_for(market)):
                self.counters["skipped_window_open"] += 1  # every spec's window is open: the normal path owns this bar
                return []
        except (StackFailClosed, OSError):
            raise
        except Exception as exc:
            self.counters["errors"] += 1
            self.last_error = f"precheck:{market}:{type(exc).__name__}: {exc}"
            return []
        t0 = self.clock()
        try:
            pairs = engine.on_m5_close(market, now, shadow=ShadowScan(
                code=OUT_OF_WINDOW_SHADOW, origin=ORIGIN_OOW, relax_window=True, admit=self._admit,
            ))
        except (StackFailClosed, OSError):
            raise
        except Exception as exc:
            self.counters["errors"] += 1
            self.last_error = f"scan:{market}:{type(exc).__name__}: {exc}"
            with _suppress():
                engine.release_seen()
            pairs = []
        dt = (self.clock() - t0) * 1000.0
        self.last_ms = dt
        self._cycle_ms += dt
        self.counters["evaluated"] += 1
        self.counters["recorded"] += len(pairs)
        return pairs

    def stats(self) -> dict[str, Any]:
        c = self.counters
        return {
            "enabled": True, "evaluated": c["evaluated"], "recorded": c["recorded"], "capped": c["capped"],
            "skipped_not_tradable": c["skipped_not_tradable"], "skipped_window_open": c["skipped_window_open"],
            "skipped_budget": c["skipped_budget"], "errors": c["errors"], "last_scan_ms": round(self.last_ms, 1),
            "last_cycle_ms": round(self._cycle_ms, 1), "cap_per_zone_day": self.cap_per_zone_day,
            "budget_s": self.budget_s, "last_error": self.last_error,
        }


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: Any) -> bool:
        return True


# --------------------------------------------------------------- B) shadow-universe scanner
def _expected_last_open(now: datetime, settle_s: float) -> datetime:
    settled = now.timestamp() - settle_s - M5_SECONDS
    return datetime.fromtimestamp((settled // M5_SECONDS) * M5_SECONDS, tz=UTC)


class ShadowUniverseScanner:
    """Bounded, round-robin, failure-isolated scan of the shadow markets on closed M5 bars.

    ``scan_cycle(now)`` returns ``[(snapshot, decision)]`` (all REJECTED ``SHADOW_UNIVERSE``); the runner persists them.
    A symbol is DUE once per M5 boundary (time based, no fetch): it is fetched only when a new closed bar should exist.
    A market that shows no fresh bar for that boundary is idle (closed) and re-checked at the next boundary."""

    def __init__(
        self,
        *,
        specs: Mapping[str, ShadowMarketSpec],
        source: Any,
        phase: str,
        seen_store: Any,
        pending: Collection[str] = (),
        forbidden: Collection[str] = (),
        max_symbols_per_cycle: int = 12,
        budget_s: float = 2.0,
        window_bars: int = 2000,
        min_history_bars: int = 600,
        max_bar_age_s: float = 360.0,
        quote_fresh_s: float = 120.0,
        settle_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        commit: str | None = None,
        sequence_tracker: Any | None = None,
    ) -> None:
        clash = sorted(set(specs) & set(forbidden)) + sorted(
            sp.broker_symbol for sp in specs.values() if sp.broker_symbol in set(forbidden)
        )
        if clash:
            raise ShadowUniverseError(f"shadow markets {clash} belong to the trading registry: refused (hard guard)")
        self._specs = dict(specs)
        self._order = sorted(self._specs)
        self._pending = set(pending) & set(self._specs)
        self._validated: set[str] = set()
        self._src = source
        self._max = max(1, int(max_symbols_per_cycle))
        self._budget = float(budget_s)
        self._window = int(window_bars)
        self._max_age = float(max_bar_age_s)
        self._quote_fresh = float(quote_fresh_s)
        self._settle = float(settle_s)
        self._clock = clock
        self._cursor = 0
        self._last_scanned_open: dict[str, datetime] = {}
        self._checked: dict[str, datetime] = {}
        self._frames: dict[str, list[tuple[datetime, float, float, float, float, float]]] = {}
        self._points = {m: sp.point_size for m, sp in self._specs.items()}
        self.clusters = {m: sp.cluster for m, sp in self._specs.items()}
        mspecs = {m: shadow_to_market_spec(sp) for m, sp in self._specs.items()}
        self.production = shadow_production_set(self._specs)
        self.engine = OpportunityEngine(
            source, production=self.production, market_specs=mspecs, phase=phase,  # type: ignore[arg-type]
            seen_store=seen_store, window_bars=self._window, min_history_bars=min_history_bars, commit=commit,
            sequence_tracker=sequence_tracker,
        )
        self.counters: Counter = Counter()
        self.errors_by_symbol: Counter = Counter()
        self.last_error: str | None = None
        self.scanned_last_cycle = 0
        self.last_cycle_ms = 0.0
        self.opportunities_recorded = 0
        self.by_cluster: Counter = Counter()

    # ---- identity ------------------------------------------------------------------------------
    @property
    def markets(self) -> tuple[str, ...]:
        return tuple(self._order)

    def owns(self, market: str) -> bool:
        return market in self._specs

    def frame_rows(self, market: str) -> list[tuple[datetime, float, float, float, float, float]]:
        """Closed bars of the last scan of ``market`` (labeller input; no MT5 read here)."""
        return list(self._frames.get(market, ()))

    def release_seen(self) -> None:
        self.engine.release_seen()

    # ---- one cycle -----------------------------------------------------------------------------
    def _tags(self, market: str) -> dict[str, Any]:
        sp = self._specs[market]
        return {
            "cluster": sp.cluster, "asset_class": ASSET_CLASS_BY_CLUSTER.get(sp.cluster, "index_cfd"),
            "shadow_only": True, "family_set": "STRUCT_FIT_FREE", "broker_symbol": sp.broker_symbol,
            "spec_pending_at_scan": market in self._pending,
        }

    def _validate_pending(self, market: str, now: datetime) -> bool:
        """A spec whose quote was stale at the Lane-U scan is scanned only once a fresh quote proves it trades."""
        if market not in self._pending or market in self._validated:
            return True
        q = self._src.latest_quote(market)
        if isinstance(q, Quote) and q.valid:
            age = (now - q.ts_utc.astimezone(UTC)).total_seconds()
            if -5.0 <= age <= self._quote_fresh:
                self._validated.add(market)
                self.counters["pending_validated"] += 1
                return True
        self.counters["pending_not_fresh"] += 1
        return False

    def scan_cycle(self, now: datetime) -> list[tuple[OpportunitySnapshot, Decision]]:
        now = now.astimezone(UTC)
        t_start = self._clock()
        out: list[tuple[OpportunitySnapshot, Decision]] = []
        expected = _expected_last_open(now, self._settle)
        n = len(self._order)
        scanned = 0
        start = self._cursor
        for k in range(n):
            if scanned >= self._max or (scanned and self._clock() - t_start >= self._budget):
                self.counters["deferred"] += n - k
                break
            idx = (start + k) % n
            m = self._order[idx]
            if self._last_scanned_open.get(m) == expected or self._checked.get(m) == expected:
                continue
            self._cursor = (idx + 1) % n  # round robin: the next cycle starts after the last symbol handled
            scanned += 1
            try:
                pairs = self._scan_symbol(m, now, expected)
            except StackFailClosed:
                raise
            except Exception as exc:
                self.counters["errors"] += 1
                self.errors_by_symbol[m] += 1
                self.last_error = f"{m}:{type(exc).__name__}: {exc}"
                self._checked[m] = expected  # retry at the next bar boundary, not every cycle
                with _suppress():
                    self.engine.release_seen()
                continue
            out.extend(pairs)
        self.scanned_last_cycle = scanned
        self.last_cycle_ms = (self._clock() - t_start) * 1000.0
        self.opportunities_recorded += len(out)
        for snap, _d in out:
            self.by_cluster[self.clusters.get(snap.market, "?")] += 1
        return out

    def _scan_symbol(self, market: str, now: datetime, expected: datetime) -> list[tuple[OpportunitySnapshot, Decision]]:
        if not self._validate_pending(market, now):
            self._checked[market] = expected
            return []
        frame = self._src.m5_frame(market, self._window)
        if len(frame) == 0:
            self.counters["idle_no_bars"] += 1
            self._checked[market] = expected
            return []
        point = self._points[market]
        last_open = pd.Timestamp(frame["ts"].iloc[-1]).to_pydatetime()
        self._frames[market] = [
            (pd.Timestamp(t).to_pydatetime(), float(o), float(h), float(lo), float(c), float(sp) * point)
            for t, o, h, lo, c, sp in zip(
                frame["ts"], frame["open"], frame["high"], frame["low"], frame["close"], frame["spread_pts"], strict=True,
            )
        ]
        age = (now - (last_open + timedelta(seconds=M5_SECONDS))).total_seconds()
        if age > self._max_age:
            self.counters["idle_stale_bar"] += 1  # market closed / no fresh bar: nothing live to evaluate
            self._checked[market] = expected
            return []
        pairs = self.engine.on_m5_close(market, now, shadow=ShadowScan(
            code=SHADOW_UNIVERSE, origin=ORIGIN_UNIVERSE, relax_window=False, tags=self._tags(market),
        ))
        self._last_scanned_open[market] = last_open
        self._checked[market] = expected  # one evaluation per bar boundary
        self.counters["scanned"] += 1
        return pairs

    # ---- heartbeat -------------------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "symbols_total": len(self._order),
            "scanned_last_cycle": self.scanned_last_cycle,
            "errors": self.counters["errors"],
            "last_cycle_ms": round(self.last_cycle_ms, 1),
            "opportunities_recorded": self.opportunities_recorded,
            "scanned_total": self.counters["scanned"],
            "deferred_total": self.counters["deferred"],
            "idle_stale_bar": self.counters["idle_stale_bar"],
            "pending_total": len(self._pending),
            "pending_validated": len(self._validated),
            "pending_not_fresh": self.counters["pending_not_fresh"],
            "by_cluster": dict(self.by_cluster),
            "max_symbols_per_cycle": self._max, "budget_s": self._budget,
            "last_error": self.last_error,
            "symbols_with_errors": dict(self.errors_by_symbol.most_common(5)),
        }


__all__ = (
    "ALL_READY", "FAMILIES_BY_CLUSTER", "SHADOW_FAMILY_POLICY", "OutOfWindowShadow", "ShadowReadError",
    "ShadowUniverseError", "ShadowUniverseScanner", "make_shadow_live_source", "select_shadow_markets",
    "shadow_production_set", "shadow_to_market_spec",
)
