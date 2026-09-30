# ruff: noqa: E501
"""Per-candidate ``min_space_r`` is re-checked by ``simulate_fast`` against the ACTUAL fill.

The temporal kernel checks ``min_space_r`` against the decision close, but the sim fills at o[i+1] +
spread + slippage.  An optional per-candidate array on ``CandidateArrays`` (NaN / absent = no check)
makes the sim skip, under the appended label ``space_below_min_at_fill``, a structural-target
candidate whose implied R to target at the fill is below the required space.
"""

from __future__ import annotations

import numpy as np

from alpha.common.sim import COST_SCENARIOS
from alpha.fast.sim import EXIT_FIXED_R, SKIP_LABELS, CandidateArrays, MarketArrays, simulate_fast

N = 40
COST = COST_SCENARIOS["BASE"]  # spread_mult 1, slippage 0.5 -> long fill = 100 + 1 + 0.5


def _market() -> MarketArrays:
    o = np.full(N, 100.0)
    minute = 600 + 5 * np.arange(N)
    contig = np.ones(N, dtype=bool)
    contig[-1] = False
    m = MarketArrays(o, o + 0.5, o - 0.5, o.copy(), np.full(N, 1.0), minute, np.zeros(N, dtype=np.int64), contig)
    m.h[8] = 110.0  # target reachable
    return m


def _cand(direction, stop, target, min_space=None) -> CandidateArrays:
    kw = {} if min_space is None else {"min_space_r": np.array([min_space])}
    return CandidateArrays(np.array([4], dtype=np.int64), np.array([direction], dtype=np.int8), np.array([stop]),
                           np.array([target]), np.array([2.0]), np.array([EXIT_FIXED_R], dtype=np.int8), **kw)


def test_label_appended_after_existing_ones() -> None:
    assert SKIP_LABELS[9] == "target_crossed_at_fill" and SKIP_LABELS[10] == "space_below_min_at_fill"
    assert len(SKIP_LABELS) == 11


def test_space_ok_at_close_but_below_min_at_fill_is_skipped_and_counted() -> None:
    # close 100, stop 95, target 108: space at the close = 8/5 = 1.6R >= 1.5 (kernel passes).
    # fill 101.5 (spread+slip): risk 6.5, beyond 6.5 -> implied 1.0R < 1.5 -> the sim must skip.
    t = simulate_fast(_market(), _cand(1, 95.0, 108.0, min_space=1.5), COST)
    assert len(t) == 0 and t.skips["space_below_min_at_fill"] == 1 and t.skips["target_crossed_at_fill"] == 0


def test_absent_nan_and_satisfied_min_space_are_booked_identically() -> None:
    base = simulate_fast(_market(), _cand(1, 95.0, 108.0), COST)
    assert len(base) == 1 and base.skips["space_below_min_at_fill"] == 0
    for ms in (np.nan, 0.0, 1.0):  # 1.0R == implied 1.0R: not below the minimum
        t = simulate_fast(_market(), _cand(1, 95.0, 108.0, min_space=ms), COST)
        np.testing.assert_array_equal(t.as_matrix(), base.as_matrix())
        assert t.skips["space_below_min_at_fill"] == 0


def test_short_side_and_slot_not_consumed() -> None:
    t = simulate_fast(_market(), _cand(-1, 105.0, 92.0, min_space=5.0), COST)
    assert len(t) == 0 and t.skips["space_below_min_at_fill"] == 1
    two = CandidateArrays(np.array([4, 5], dtype=np.int64), np.array([1, 1], dtype=np.int8), np.array([95.0, 95.0]),
                          np.array([108.0, 108.0]), np.array([2.0, 2.0]), np.zeros(2, dtype=np.int8),
                          min_space_r=np.array([1.5, np.nan]))
    t2 = simulate_fast(_market(), two, COST)
    assert len(t2) == 1 and t2.decision_idx[0] == 5 and t2.skips["space_below_min_at_fill"] == 1


def test_min_space_length_mismatch_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        CandidateArrays(np.array([4], dtype=np.int64), np.array([1], dtype=np.int8), np.array([95.0]),
                        np.array([108.0]), np.array([2.0]), np.zeros(1, dtype=np.int8), min_space_r=np.array([1.0, 2.0]))


def test_attach_min_space_from_compiled_spec_only_for_structural_targets() -> None:
    from types import SimpleNamespace

    from alpha.discovery.temporal_evaluate import attach_min_space

    c = CandidateArrays(np.array([4, 9], dtype=np.int64), np.array([1, 1], dtype=np.int8), np.array([95.0, 95.0]),
                        np.array([108.0, np.nan]), np.array([2.0, 1.5]), np.zeros(2, dtype=np.int8))
    nxt = SimpleNamespace(target=SimpleNamespace(kind="next_structure", min_space_r=1.5))
    out = attach_min_space(c, nxt)
    assert out.min_space_r[0] == 1.5 and np.isnan(out.min_space_r[1])  # fallback (NaN target) is unchecked
    assert attach_min_space(c, SimpleNamespace(target=SimpleNamespace(kind="fixed_r", min_space_r=0.0))) is c
    assert attach_min_space(c, SimpleNamespace(target=SimpleNamespace(kind="next_structure", min_space_r=0.0))) is c
    sub = out.subset(np.array([True, False]))
    assert len(sub.decision_idx) == 1 and sub.min_space_r[0] == 1.5
