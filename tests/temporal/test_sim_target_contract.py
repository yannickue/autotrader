# ruff: noqa: E501
"""PIN: how ``simulate_fast`` treats a finite ``target`` that is already on the wrong side of the fill.

Observed (documented, sim.py is NOT changed): the candidate is neither skipped nor rejected and
raises nothing.  The trade is FILLED at o[i+1] and then exits IMMEDIATELY on the entry bar
(``exit_idx == entry_idx``) with reason TARGET at exactly ``exit_price == target``: the target test
``h[k] >= target + penetration`` (long) / ``low[k] + spread <= target - penetration`` (short) is
already true on the entry bar, so a "target" that lies on the losing side of the fill books a LOSS
labelled TARGET.  For a long target below the stop the exit price is even beyond the stop (a
worse-than-1R loss) because the stop test on the entry bar is only hit if that bar's low reaches
it.  Consequence for V2: a structural target must be rejected in the candidate stage (the oracle
does: target strictly beyond the DECISION close) AND must be checked against the o[i+1] gap; the
gap case is only pinned here, not fixed.
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


def test_long_target_below_fill_is_filled_then_exits_instantly_as_target():
    m = _market()
    t = simulate_fast(m, _cand(1, 95.0, 99.0), COST)  # fill = 100 + 1 (spread) + 0.5 = 101.5
    assert len(t) == 1, "not skipped"
    assert t.entry_idx[0] == 5 and t.exit_idx[0] == 5, "instant exit on the entry bar"
    assert t.exit_reason[0] == REASON_TARGET
    assert t.exit_price[0] == 99.0 and t.entry_price[0] > 99.0
    assert t.pnl_pts[0] < 0 and t.net_pnl_eur[0] < 0 and t.r_multiple[0] < 0  # a LOSS booked as TARGET


def test_long_target_below_stop_exits_beyond_the_stop():
    t = simulate_fast(_market(), _cand(1, 95.0, 90.0), COST)
    assert len(t) == 1 and t.exit_idx[0] == t.entry_idx[0]
    assert t.exit_reason[0] == REASON_TARGET and t.exit_price[0] == 90.0
    assert t.r_multiple[0] < -1.0  # worse than the intended 1R stop loss


def test_short_target_above_fill_is_filled_then_exits_instantly_as_target():
    # short fill = o[j] - slip = 99.5; bar high/low 100.5/99.5 (+ spread 1 on the ask side)
    t = simulate_fast(_market(), _cand(-1, 105.0, 101.0), COST)
    assert len(t) == 1
    assert t.exit_idx[0] == t.entry_idx[0] and t.exit_reason[0] == REASON_TARGET
    assert t.exit_price[0] == 101.0 and t.pnl_pts[0] < 0 and t.r_multiple[0] < 0


def test_gap_over_a_valid_decision_close_target_is_the_same_bug_class():
    """Target above the decision close (valid for the oracle) but crossed by the o[i+1] gap down:
    close[i]=100, target=100.5 is 'beyond' it for a long; the next open gaps to 102 (so the fill
    is beyond the target already) -> instant TARGET exit at a price BELOW the fill (a loss)."""
    m = _market(open_next=102.0)
    t = simulate_fast(m, _cand(1, 95.0, 100.5), COST)
    assert len(t) == 1 and t.exit_idx[0] == t.entry_idx[0]
    assert t.exit_reason[0] == REASON_TARGET and t.exit_price[0] == 100.5
    assert t.exit_price[0] < t.entry_price[0]
    assert t.pnl_pts[0] < 0
