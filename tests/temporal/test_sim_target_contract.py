# ruff: noqa: E501
"""PIN: ``simulate_fast`` SKIPS (and counts) a finite ``target`` that is not strictly beyond the fill.

Previously (defect found by W3) such a candidate was filled at o[i+1] and exited instantly on the
entry bar as TARGET at exactly ``target`` (a loss labelled TARGET).  Now the kernel skips it under
the appended reason ``target_crossed_at_fill`` (rule: beyond-fill distance must exceed
eps = 1e-9 * max(1, |fill|) and the implied R must be finite > 0; slot / day cap not consumed).
The gap case (target valid against the decision close but crossed by the o[i+1] gap) is covered too.
"""

from __future__ import annotations

import numpy as np

from alpha.common.sim import COST_SCENARIOS
from alpha.fast.sim import (
    EXIT_FIXED_R,
    REASON_TARGET,
    CandidateArrays,
    MarketArrays,
    simulate_fast,
)

N = 12
COST = COST_SCENARIOS["BASE"]  # spread_mult 1, slippage 0.5, no target penetration


def _market(open_next: float = 100.0) -> MarketArrays:
    o = np.full(N, 100.0)
    o[5] = open_next  # entry bar (decision bar 4)
    h = o + 0.5
    lo = o - 0.5
    c = o.copy()
    minute = 600 + 5 * np.arange(N)
    day = np.zeros(N, dtype=np.int64)
    contig = np.ones(N, dtype=bool)
    contig[-1] = False
    return MarketArrays(o, h, lo, c, np.full(N, 1.0), minute, day, contig)


def _cand(direction: int, stop: float, target: float, r: float = 2.0) -> CandidateArrays:
    return CandidateArrays(
        decision_idx=np.array([4], dtype=np.int64), direction=np.array([direction], dtype=np.int8),
        stop=np.array([stop]), target=np.array([target]), target_r=np.array([r]),
        exit_kind=np.array([EXIT_FIXED_R], dtype=np.int8),
    )


def test_baseline_nan_target_uses_r_and_is_not_instant():
    t = simulate_fast(_market(), _cand(1, 95.0, np.nan), COST)
    assert len(t) == 1
    assert t.exit_idx[0] > t.entry_idx[0] or t.exit_reason[0] != REASON_TARGET


def _skipped(t) -> bool:
    return len(t) == 0 and t.skips["target_crossed_at_fill"] == 1


def test_long_target_below_fill_is_skipped_and_counted():
    assert _skipped(simulate_fast(_market(), _cand(1, 95.0, 99.0), COST))  # fill = 101.5


def test_long_target_below_stop_is_skipped_and_counted():
    assert _skipped(simulate_fast(_market(), _cand(1, 95.0, 90.0), COST))


def test_short_target_above_fill_is_skipped_and_counted():
    assert _skipped(simulate_fast(_market(), _cand(-1, 105.0, 101.0), COST))  # short fill = 99.5


def test_gap_over_a_valid_decision_close_target_is_skipped_and_counted():
    """close[i]=100, long target 100.5 is valid vs the decision close; the next open gaps to 102."""
    assert _skipped(simulate_fast(_market(open_next=102.0), _cand(1, 95.0, 100.5), COST))
