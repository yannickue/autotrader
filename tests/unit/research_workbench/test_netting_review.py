"""Review round 2 (light, no Nautilus): overlap audit over ALL intervals, duplicate candidates."""

from __future__ import annotations

import dataclasses
import types

import numpy as np

from alpha.common.sim import CostScenario
from research_workbench.differential import netting_report, run_differential
from research_workbench.golden import GOLDEN_SCENARIOS


def _trades(dec, ent, ext):
    return types.SimpleNamespace(
        decision_idx=np.array(dec), entry_idx=np.array(ent), exit_idx=np.array(ext)
    )


def _cands(idx):
    return types.SimpleNamespace(decision_idx=np.array(idx))


def test_overlap_detects_unsorted_output() -> None:
    # trade list given out of order: B (3..9) listed after C (12..15); A (5..8) overlaps B
    t = _trades([12, 3, 5], [13, 4, 6], [15, 9, 8])
    rep = netting_report(t, _cands([3, 5, 12]))
    assert rep["overlap_violations"] == 1
    assert rep["overlap_trade_indices"] == [2]  # index in the ORIGINAL (unsorted) output


def test_overlap_detects_nested_interval_not_adjacent_to_its_container() -> None:
    # container 3..20, then a short trade 5..6 nested inside, then a trade 8..9 also nested:
    # adjacent-only checks would miss the third (it is after a short one that ended early)
    t = _trades([3, 5, 8], [4, 6, 9], [20, 6, 9])
    rep = netting_report(t, _cands([3, 5, 8]))
    assert rep["overlap_trade_indices"] == [1, 2]


def test_same_bar_reentry_and_clean_sequence_are_not_violations() -> None:
    t = _trades([3, 6, 9], [4, 7, 10], [6, 9, 12])
    rep = netting_report(t, _cands([3, 6, 9]))
    assert rep["overlap_violations"] == 0 and rep["same_bar_reentries"] == 2


def test_duplicate_decision_indices_use_multiplicity_not_a_set() -> None:
    # two candidates share decision index 3, only ONE trade executed from it: the extra one is
    # not silently treated as traded (old code: set(dec)); 3 is inside no interval -> "other"
    t = _trades([3], [4], [7])
    rep = netting_report(t, _cands([3, 3, 5]))
    assert rep["duplicate_decision_indices"] == 1
    assert rep["candidates_blocked_by_open_position"] == 1  # index 5 inside 3..7
    assert rep["candidates_unfilled_other"] == 1  # the second candidate at 3


def test_run_differential_rejects_duplicate_decision_indices_as_blocked() -> None:
    market, cands, cost, window = GOLDEN_SCENARIOS["long_normal_target"].inputs()
    dup = dataclasses.replace(cands)
    for name in ("decision_idx", "direction", "stop", "target", "target_r", "exit_kind"):
        object.__setattr__(dup, name, np.repeat(getattr(cands, name), 2))
    r = run_differential(market, dup, cost, window=window, scenario_id="dup")
    assert r.status == "BLOCKED" and "duplicate" in (r.blocked_reason or "")
    assert isinstance(cost, CostScenario)
