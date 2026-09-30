# ruff: noqa: E501
"""Historical replay of the opportunity engine on DEV bars -> opportunities/day and reason histograms.

Two modes over the SAME frozen production spec and the SAME static demo policy:

* ``mode="batch"`` (default, fast): ``FamilyData`` is built once for the whole range and every frozen
  spec is generated once. Because the generators are causal, the decision at bar ``i`` equals the decision
  the streaming engine takes with bars ``<= i`` only (proved by the parity tests in
  ``tests/unit/demo/opportunity/test_opp_parity.py``); the only difference is that batch mode knows whether
  the REAL next bar exists, the live engine cannot.
* ``mode="stream"`` (slow, ~50 ms per bar): drives the real ``OpportunityEngine`` bar by bar over a
  ``ReplayBarSource`` and returns full ``(OpportunitySnapshot, Decision)`` pairs.

Position gating (``ONE_POSITION_PER_INSTRUMENT``) needs to know how long an accepted trade would be open.
``SimplePositionTracker`` walks the following BID bars (stop first, then target, then the spec's clock
exit, then day end) -- a conservative flow model, NOT a P&L simulation. The result reports the flow
with and without that gating.

Nothing at or after 2026-09-01 can be replayed (``dev_frame`` refuses it).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from alpha.common.market_data import DEV_END, dev_frame, load_dev_market_frame
from alpha.families import leadlag
from alpha.families.data import build_family_data, build_leader_features
from alpha.families.registry import generate_candidates
from alpha.families.spec import MarketCalendar
from demo.contracts import Decision, OpportunitySnapshot, Phase
from demo.opportunity.bar_source import M5_SECONDS, Quote, ReplayBarSource
from demo.opportunity.engine import (
    InMemorySeenStore,
    OpportunityEngine,
    make_candidate,
    opportunity_id_of,
)
from demo.opportunity.policy import ACCEPTED, Candidate, StaticDemoPolicy
from demo.opportunity.production_spec import ProductionSpecSet, load_production_spec
from markets.spec import CANONICALS, MarketSpec, load_market_spec


@dataclass(frozen=True, slots=True)
class ReplayRow:
    signal_ts_utc: str
    family: str
    strategy_id: str
    direction: int
    accepted: bool
    reasons: tuple[str, ...]


@dataclass(slots=True)
class ReplayResult:
    market: str
    start: str
    end: str
    mode: str
    n_active_days: int
    rows: list[ReplayRow] = field(default_factory=list)
    pairs: list[tuple[OpportunitySnapshot, Decision]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        n = len(self.rows)
        acc = sum(1 for r in self.rows if r.accepted)
        no_pos = sum(
            1 for r in self.rows if r.accepted or r.reasons == ("ONE_POSITION_PER_INSTRUMENT",)
        )
        days = max(1, self.n_active_days)
        reasons: Counter[str] = Counter()
        for r in self.rows:
            reasons.update(r.reasons)
        fam: Counter[str] = Counter(r.family for r in self.rows)
        fam_acc: Counter[str] = Counter(r.family for r in self.rows if r.accepted)
        return {
            "market": self.market, "start": self.start, "end": self.end, "mode": self.mode,
            "active_days": self.n_active_days, "opportunities": n, "accepted": acc,
            "opportunities_per_day": round(n / days, 2),
            "valid_before_position_gate": no_pos,
            "valid_before_position_gate_per_day": round(no_pos / days, 2),
            "accepted_per_day": round(acc / days, 2),
            "reason_histogram": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "by_family": dict(sorted(fam.items())),
            "accepted_by_family": dict(sorted(fam_acc.items())),
        }


class SimplePositionTracker:
    """Replay-only oracle: when would an accepted intent be flat again (conservative bar walk)?"""

    def __init__(self) -> None:
        self._busy_until_open_ns: dict[str, int] = {}
        self.now_bar_open_ns: int = 0

    def is_open(self, market: str) -> bool:
        return self.now_bar_open_ns < self._busy_until_open_ns.get(market, -1)

    def register(self, market: str, data, cand: Candidate, exec_price: float, target: float | None) -> None:
        i = int(np.searchsorted(data.ts_ns, int(cand.bar_open_ts.timestamp()) * 10**9))
        n = len(data)
        d = cand.direction
        exit_min = cand.window.exit_min
        stop = cand.stop
        k = i + 1
        exit_idx = k
        while k < n:
            exit_idx = k
            if k > i + 1 and not data.contig_next[k - 1]:
                break
            if data.minute[k] >= exit_min or data.day[k] != data.day[i]:
                break
            sp = float(data.spread[k])
            if d > 0:
                if data.l[k] <= stop:
                    exit_idx = k + 1
                    break
                if target is not None and data.h[k] >= target:
                    exit_idx = k + 1
                    break
            else:
                if data.h[k] + sp >= stop:
                    exit_idx = k + 1
                    break
                if target is not None and data.l[k] + sp <= target:
                    exit_idx = k + 1
                    break
            k += 1
        else:
            exit_idx = n
        exit_idx = min(exit_idx, n - 1)
        self._busy_until_open_ns[market] = int(data.ts_ns[exit_idx])


def _active_days(frame: pd.DataFrame, mspec: MarketSpec) -> int:
    loc = pd.DatetimeIndex(frame["ts"]).tz_convert(mspec.calendar.tz)
    minute = loc.hour * 60 + loc.minute
    cal = mspec.calendar
    in_cash = (minute >= cal.cash_open_min) & (minute < cal.cash_close_min) & (loc.weekday < 5)
    counts = pd.Series(1, index=loc.normalize()[in_cash]).groupby(level=0).sum()
    return int((counts >= 12).sum())


def load_replay_frames(
    market: str, end: str, *, data_root: str | Path | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, MarketSpec]]:
    if pd.Timestamp(end) > pd.Timestamp(DEV_END):
        raise ValueError(f"end {end} is after the development end {DEV_END} (forward holdout)")
    names = sorted({market, *leadlag.PAIRS.get(market, ())})
    specs = {m: load_market_spec(m) for m in names}
    frames = {m: dev_frame(load_dev_market_frame(specs[m], data_root=data_root), end=end) for m in names}
    return frames, specs


def replay_opportunities(
    market: str,
    start: str,
    end: str,
    *,
    production: ProductionSpecSet | None = None,
    mode: str = "batch",
    policy: StaticDemoPolicy | None = None,
    phase: Phase = "DISCOVERY",
    data_root: str | Path | None = None,
    frames: dict[str, pd.DataFrame] | None = None,
    market_specs: dict[str, MarketSpec] | None = None,
) -> ReplayResult:
    """Replay dev bars of ``market`` with signal times in ``[start, end]`` (UTC).

    ``start``/``end`` are ISO dates (``end`` then includes the whole day) or ISO datetimes (``end`` exclusive).

    ``end`` may not exceed 2026-08-31. Returns the rows and the ``summary()`` statistics."""
    if market not in CANONICALS:
        raise ValueError(market)
    prod = production or load_production_spec()
    if frames is None or market_specs is None:
        frames, market_specs = load_replay_frames(market, end[:10], data_root=data_root)
    ms = market_specs[market]
    t0 = pd.Timestamp(start, tz="UTC")
    t1 = pd.Timestamp(end, tz="UTC")
    if len(end) <= 10:  # a bare date includes that whole UTC day
        t1 += pd.Timedelta(days=1)
    fr = frames[market]
    in_range = fr[(pd.DatetimeIndex(fr["ts"]) >= t0) & (pd.DatetimeIndex(fr["ts"]) < t1)]
    result = ReplayResult(market, start, end, mode, _active_days(in_range, ms))
    if mode == "stream":
        return _stream(result, market, ms, prod, frames, market_specs, policy, phase, t0, t1)
    if mode != "batch":
        raise ValueError(mode)
    return _batch(result, market, ms, prod, frames, market_specs, policy, t0, t1)


def _batch(
    result: ReplayResult, market: str, ms: MarketSpec, prod: ProductionSpecSet,
    frames: dict[str, pd.DataFrame], mspecs: dict[str, MarketSpec], policy: StaticDemoPolicy | None,
    t0: pd.Timestamp, t1: pd.Timestamp,
) -> ReplayResult:
    pol = policy or StaticDemoPolicy()
    cal = MarketCalendar.from_market_spec(ms)
    fr = frames[market]
    leaders = {
        ld: build_leader_features(fr, frames[ld], MarketCalendar.from_market_spec(mspecs[ld]), ld)
        for ld in leadlag.PAIRS.get(market, ()) if ld in frames
    }
    data = build_family_data(
        fr, cal, name=market, point_size=ms.point_size, tick_size=ms.tick_size,
        asset_class=ms.asset_class, cross=leaders,
    )
    items: list[tuple[int, int, Candidate, Any]] = []  # (bar idx, spec order, candidate, frozen)
    for order, fs in enumerate(prod.specs_for(market)):
        cands = generate_candidates(data, fs.spec, fs.thr)
        for k in range(len(cands.decision_idx)):
            i = int(cands.decision_idx[k])
            sig = pd.Timestamp(int(data.ts_ns[i]) + M5_SECONDS * 10**9, tz="UTC")
            if not (t0 <= sig < t1):
                continue
            items.append((i, order, make_candidate(market, ms, fs, data, cands, k), fs))
    items.sort(key=lambda x: (x[0], x[1]))
    tracker = SimplePositionTracker()
    seen = InMemorySeenStore()
    pending_bar = -1
    pending_accept = False
    for i, _order, cand, fs in items:
        if i != pending_bar:
            pending_bar, pending_accept = i, False
        tracker.now_bar_open_ns = int(data.ts_ns[i])
        if not seen.add_if_new(opportunity_id_of(cand)):
            continue
        quote = Quote(ts_utc=cand.signal_ts, bid=cand.close, ask=cand.close + cand.bar_spread)
        a = pol.assess(
            cand, quote, cand.signal_ts, ms,
            position_open=pending_accept or tracker.is_open(market),
        )
        if a.accepted:
            pending_accept = True
            tracker.register(market, data, cand, a.exec_price, a.geometry.target)
        result.rows.append(ReplayRow(
            cand.signal_ts.isoformat(), fs.family, fs.strategy_id, cand.direction, a.accepted, a.reasons,
        ))
    return result


def _stream(
    result: ReplayResult, market: str, ms: MarketSpec, prod: ProductionSpecSet,
    frames: dict[str, pd.DataFrame], mspecs: dict[str, MarketSpec], policy: StaticDemoPolicy | None,
    phase: Phase, t0: pd.Timestamp, t1: pd.Timestamp,
) -> ReplayResult:
    src = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    cal = MarketCalendar.from_market_spec(ms)
    fr = frames[market]
    leaders = {
        ld: build_leader_features(fr, frames[ld], MarketCalendar.from_market_spec(mspecs[ld]), ld)
        for ld in leadlag.PAIRS.get(market, ()) if ld in frames
    }
    full = build_family_data(
        fr, cal, name=market, point_size=ms.point_size, tick_size=ms.tick_size,
        asset_class=ms.asset_class, cross=leaders,
    )
    tracker = SimplePositionTracker()
    engine = OpportunityEngine(
        src, production=prod, market_specs=mspecs, policy=policy, phase=phase,
        position_open=tracker.is_open, commit="replay",
    )
    ts = pd.DatetimeIndex(fr["ts"])
    for i in np.flatnonzero((ts >= t0 - pd.Timedelta(seconds=M5_SECONDS)) & (ts < t1)):
        now = (ts[i] + pd.Timedelta(seconds=M5_SECONDS)).to_pydatetime()
        if pd.Timestamp(now) < t0:
            continue
        src.set_time(now)
        tracker.now_bar_open_ns = int(ts[i].value)
        for snap, dec in engine.on_m5_close(market, now):
            result.pairs.append((snap, dec))
            result.rows.append(ReplayRow(
                snap.signal_ts_utc, snap.signal["family"], snap.signal["strategy_id"], snap.direction,
                dec.accepted, dec.reasons,
            ))
            if dec.accepted:
                intent = engine.last_intents[-1]
                tracker.register(market, full, engine.last_candidates[-1], intent.entry_ref, intent.target)
    return result


__all__ = (
    "ACCEPTED", "ReplayResult", "ReplayRow", "SimplePositionTracker", "load_replay_frames",
    "replay_opportunities",
)
