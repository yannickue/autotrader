"""Netting / non-flat golden scenarios: FAST hand numbers + netting audit (no Nautilus import)."""

from __future__ import annotations

import types

import numpy as np
import pytest

from alpha.fast.sim import simulate_fast
from research_workbench.differential import (
    BAR_NS,
    DiffClass,
    FieldDiff,
    _netting_diffs,
    bar_open_ts_ns,
    netting_report,
)
from research_workbench.golden_netting import NETTING_SCENARIOS

_IDS = sorted(NETTING_SCENARIOS)


@pytest.mark.parametrize("sid", _IDS)
def test_fast_side_matches_hand_calculation(sid: str) -> None:
    s = NETTING_SCENARIOS[sid]
    market, cands, cost, window = s.inputs()
    t = simulate_fast(market, cands, cost, window=window)
    exp = s.expected["trades"]
    assert len(t) == s.expected["n_trades"] == len(exp)
    for n, e in enumerate(exp):
        assert int(t.decision_idx[n]) == e["decision_idx"]
        assert int(t.entry_idx[n]) == e["entry_idx"]
        assert int(t.exit_idx[n]) == e["exit_idx"]
        assert str(t.exit_reason_labels[n]) == e["exit_reason"]
        assert t.entry_price[n] == pytest.approx(e["fill"], abs=1e-9)
        assert t.exit_price[n] == pytest.approx(e["exit_price"], abs=1e-9)
        assert t.risk_pts[n] == pytest.approx(e["risk"], abs=1e-9)
        assert t.qty[n] == pytest.approx(e["qty"], abs=1e-12)
        assert t.net_pnl_eur[n] == pytest.approx(e["net"], abs=1e-9)
        assert t.cost_eur[n] == pytest.approx(e["cost"], abs=1e-9)
        assert t.gross_pnl_eur[n] == pytest.approx(e["gross"], abs=1e-9)
        assert t.r_multiple[n] == pytest.approx(e["r"], abs=1e-9)
        assert t.mfe_r[n] == pytest.approx(e["mfe_r"], abs=1e-9)
        assert t.mae_r[n] == pytest.approx(e["mae_r"], abs=1e-9)
        assert int(t.holding_bars[n]) == e["holding_bars"]


def test_trend_scenario_is_really_non_flat() -> None:
    m = NETTING_SCENARIOS["entry_on_trending_close"].market()
    assert len(set(m.c.tolist())) == len(m.c)  # no two closes equal: lookback bugs change numbers
    assert np.all(m.o[1:] == m.c[:-1])  # contiguous, no price gaps


def test_fast_admits_decision_at_previous_exit_bar_but_not_before() -> None:
    market, cands, cost, window = NETTING_SCENARIOS["same_bar_exit_and_reentry"].inputs()
    t = simulate_fast(market, cands, cost, window=window)
    assert int(t.decision_idx[1]) == int(t.exit_idx[0]) == 6  # same-bar exit + re-entry admitted
    rep = netting_report(t, cands)
    assert rep["overlap_violations"] == 0 and rep["same_bar_reentries"] == 1

    market, cands, cost, window = NETTING_SCENARIOS["overlapping_candidate_dropped"].inputs()
    t = simulate_fast(market, cands, cost, window=window)
    assert len(t) == 1 and int(t.exit_idx[0]) == 7
    assert sum(t.skips.values()) == 0  # the drop is NOT visible in the FAST skip counters
    rep = netting_report(t, cands)
    assert rep["candidates_total"] == 2 and rep["candidates_blocked_by_open_position"] == 1
    assert rep["overlap_violations"] == 0


def test_netting_report_detects_overlapping_trades_and_flags_bug() -> None:
    t = types.SimpleNamespace(
        decision_idx=np.array([3, 5]), entry_idx=np.array([4, 6]), exit_idx=np.array([9, 12])
    )
    cands = types.SimpleNamespace(decision_idx=np.array([3, 5]))
    rep = netting_report(t, cands)
    assert rep["overlap_violations"] == 1 and rep["overlap_trade_indices"] == [1]
    diffs = _netting_diffs(rep)
    assert len(diffs) == 1 and isinstance(diffs[0], FieldDiff)
    assert diffs[0].field == "FAST_ALLOWS_OVERLAP"
    assert diffs[0].diff_class is DiffClass.BUG_SUSPECTED  # => status FAIL
    assert _netting_diffs({"overlap_violations": 0, "overlap_trade_indices": []}) == []


def test_timestamps_for_new_scenarios_increase() -> None:
    for s in NETTING_SCENARIOS.values():
        assert np.all(np.diff(bar_open_ts_ns(s.market())) == BAR_NS)
