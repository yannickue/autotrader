# ruff: noqa: E501
"""Classify every hindsight move: EXECUTED / REJECTED / NEAR_MISS / OUT_OF_WINDOW / NO_SETUP (+ why)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from alpha.families.data import FamilyData
from coverage_analysis.moves import Move, MoveParams
from coverage_analysis.r2 import R2Row
from coverage_analysis.replay import ReplayResult, Trigger

EXECUTED = "EXECUTED"  # an R2 opportunity around the move start became a trade (outcome attached when closed)
REJECTED = "REJECTED"  # an R2 opportunity exists but was rejected (engine gate / stack gate / cancelled); R2 counterfactual attached
NEAR_MISS = "NEAR_MISS"  # no signal; one family condition was within tolerance (actual / required / normalized gap reported)
OUT_OF_WINDOW = "OUT_OF_WINDOW"  # bars exist (broker tradable) but no frozen spec's entry window is open: shadow classification only
NO_SETUP = "NO_SETUP"  # none of the above (closest failing family condition reported)
CLASSES = (EXECUTED, REJECTED, NEAR_MISS, OUT_OF_WINDOW, NO_SETUP)  # exactly five; they sum to the number of moves
# Data-availability metadata (``r2_data_status`` of a move), NOT a class. Rule: a move whose offline-replayed frozen
# generators WOULD have signalled while R2 holds no record (bars pre-date the live runner) is classed REJECTED
# ("a signal existed, no trade resulted"; never EXECUTED without live evidence) with r2_data_status = SIGNAL_NOT_IN_R2.
R2_AVAILABLE = "R2_AVAILABLE"  # an R2 record exists around the move start
SIGNAL_NOT_IN_R2 = "SIGNAL_NOT_IN_R2"
R2_NOT_APPLICABLE = "NOT_APPLICABLE"
NO_STRUCTURE = "NO_TRIGGER_STRUCTURE (no relaxable condition reaches a trigger within 0.75 of its scale)"


def bar_close(data: FamilyData, idx: int) -> datetime:
    return datetime.fromtimestamp(int(data.ts_ns[idx]) // 10**9, tz=UTC) + timedelta(seconds=300)


def window_open_mask(data: FamilyData, specs: Sequence[Any]) -> np.ndarray:
    """bool[n]: a decision at the close of bar i would enter (next bar) inside SOME spec's entry window on the same local day
    -- pure calendar, no price facts (the OUT_OF_WINDOW test)."""
    n = len(data)
    ok = np.zeros(n, dtype=bool)
    if n < 2:
        return ok
    m1 = data.minute[1:]
    same_day = (data.day[1:] == data.day[:-1]) & data.contig_next[:-1]
    for fs in specs:
        w = fs.spec.effective_window(data.cal)
        ok[:-1] |= same_day & (m1 >= w.entry_start_min) & (m1 < w.entry_end_min)
    return ok


class _Idx:
    """Triggers of one direction sorted by bar index (window lookup)."""

    def __init__(self, triggers: Sequence[Trigger]) -> None:
        self.by_dir: dict[int, tuple[np.ndarray, list[Trigger]]] = {}
        for d in (1, -1):
            ts = sorted((t for t in triggers if t.direction == d), key=lambda t: t.idx)
            self.by_dir[d] = (np.array([t.idx for t in ts], dtype=np.int64), ts)

    def window(self, d: int, a: int, b: int) -> list[Trigger]:
        arr, ts = self.by_dir[d]
        lo, hi = int(np.searchsorted(arr, a, "left")), int(np.searchsorted(arr, b, "right"))
        return ts[lo:hi]


def classify_moves(
    data: FamilyData, moves: Sequence[Move], rep: ReplayResult, specs: Sequence[Any], market: str,
    r2_rows: Sequence[R2Row], p: MoveParams,
) -> list[dict[str, Any]]:
    sig, near, wide = _Idx(rep.signals), _Idx(rep.near), _Idx(rep.wide)
    wopen = window_open_mask(data, specs)
    r2m = [r for r in r2_rows if r.market == market]
    out: list[dict[str, Any]] = []
    for mv in moves:
        a = max(0, mv.start - p.cover_pre)
        b = max(a, min(mv.start + p.cover_post, mv.reach - 1))
        base = {
            "market": market, "direction": mv.direction, "start_utc": bar_close(data, mv.start).isoformat(),
            "reach_utc": bar_close(data, mv.reach).isoformat(), "atr": mv.atr, "excursion_atr": mv.excursion_atr,
            "start_idx": mv.start, "r2_data_status": R2_NOT_APPLICABLE,
        }
        t0, t1 = bar_close(data, a), bar_close(data, b)
        r2_hit = [r for r in r2m if r.direction == mv.direction and t0 <= r.signal_ts <= t1]
        if r2_hit:
            base.update(klass=EXECUTED if any(r.executed for r in r2_hit) else REJECTED, r2_data_status=R2_AVAILABLE, detail=[{
                "opportunity_id": r.opportunity_id, "family": r.family, "accepted": r.accepted,
                "reasons": list(r.reasons), "intent_state": r.intent_state, "stack_reject_code": r.stack_reject_code,
                "outcome_net_r": r.outcome_net_r, "counterfactual_r": r.cf_r, "counterfactual_source": r.cf_source,
            } for r in r2_hit])
            out.append(base)
            continue
        s = sig.window(mv.direction, a, b)
        if s:
            base.update(klass=REJECTED, r2_data_status=SIGNAL_NOT_IN_R2, detail=[{"family": t.family, "mode": t.mode, "strategy_id": t.strategy_id, "bar_offset": t.idx - mv.start} for t in s[:5]])
            out.append(base)
            continue
        nm = near.window(mv.direction, a, b)
        if nm:
            t = min(nm, key=lambda x: x.ratio)
            base.update(klass=NEAR_MISS, detail={
                "family": t.family, "mode": t.mode, "failed_condition": t.condition, "actual": t.actual,
                "required": t.required, "normalized_gap": t.ratio, "gap_abs": t.gap_abs, "unit": t.unit,
                "bar_offset": t.idx - mv.start,
            })
            out.append(base)
            continue
        if not bool(wopen[a:b + 1].any()):
            base.update(klass=OUT_OF_WINDOW, detail={"note": "broker bars exist (tradable) but no frozen spec's entry window is open: SHADOW classification only, no order"})
            out.append(base)
            continue
        w = wide.window(mv.direction, a, b)
        if w:
            t = min(w, key=lambda x: x.ratio)
            base.update(klass=NO_SETUP, reason=f"{t.family}/{t.mode}:{t.condition}", detail={
                "closest_family": t.family, "mode": t.mode, "failed_condition": t.condition, "actual": t.actual,
                "required": t.required, "normalized_gap": t.ratio, "gap_abs": t.gap_abs, "unit": t.unit})
        else:
            base.update(klass=NO_SETUP, reason=NO_STRUCTURE, detail={})
        out.append(base)
    return out


def no_setup_reasons(rows: Sequence[dict[str, Any]], top: int = 5) -> list[tuple[str, int]]:
    return Counter(r["reason"] for r in rows if r["klass"] == NO_SETUP).most_common(top)
