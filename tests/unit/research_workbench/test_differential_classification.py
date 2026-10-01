"""Classification engine of the FAST <-> Nautilus differential (pure, no Nautilus import).

Handcrafted trade pairs are fed to ``classify_pair`` / ``compare_trade_lists``; the contract:
every field gets a DiffClass, an unexplained difference is BUG_SUSPECTED => status FAIL, and
scenarios Nautilus cannot express are BLOCKED (never silently PASS).
"""

from __future__ import annotations

import dataclasses
import sys

import pytest

from alpha.common.sim import CostScenario
from alpha.fast.sim import EXIT_TRAIL, CandidateArrays
from research_workbench.differential import (
    DEFAULT_TOLERANCES,
    Component,
    DiffClass,
    FieldDiff,
    NormalizedTrade,
    PairContext,
    classify_pair,
    compare_trade_lists,
    run_differential,
    trade_mismatch_leg,
)
from research_workbench.golden import GOLDEN_SCENARIOS

BASE = NormalizedTrade(
    signal_ts_ns=1_000,
    direction=1,
    decision_ts_ns=1_000,
    entry_ref=100.0,
    fill_price=101.0,
    stop=95.0,
    target=113.0,
    exit_ts_ns=5_000,
    exit_price=113.0,
    exit_reason="TARGET",
    r_multiple=2.0,
    gross_pnl=107.25,
    net_pnl=99.0,
    mfe_r=2.0,
    mae_r=0.25,
    costs=8.25,
    holding_bars=4,
)
CTX = PairContext(side=1, qty=8.25)


def _by_field(diffs: list[FieldDiff]) -> dict[str, FieldDiff]:
    return {d.field: d for d in diffs}


def _cls(diffs: list[FieldDiff]) -> dict[str, DiffClass]:
    return {d.field: d.diff_class for d in diffs}


def test_identical_trades_are_all_exact_and_mfe_mae_not_applicable() -> None:
    fid = dataclasses.replace(BASE, mfe_r=None, mae_r=None)
    diffs = classify_pair(0, BASE, fid, CTX)
    assert {d.diff_class for d in diffs} == {DiffClass.EXACT_MATCH}
    assert "mfe_r" not in _by_field(diffs) and "mae_r" not in _by_field(diffs)  # n/a, not "match"
    assert trade_mismatch_leg(diffs) == "NONE"


def test_every_field_gets_a_class_and_a_leg() -> None:
    diffs = classify_pair(0, BASE, BASE, CTX)
    assert len(diffs) == 17
    assert {d.leg for d in diffs} == {"ENTRY", "EXIT", "JOINT"}
    assert _by_field(diffs)["fill_price"].leg == "ENTRY"
    assert _by_field(diffs)["exit_price"].leg == "EXIT"
    assert _by_field(diffs)["net_pnl"].leg == "JOINT"


def test_difference_within_half_tick_is_tolerance_match_with_justification() -> None:
    fid = dataclasses.replace(BASE, exit_price=113.004)
    d = _by_field(classify_pair(0, BASE, fid, CTX))["exit_price"]
    assert d.diff_class is DiffClass.TOLERANCE_MATCH
    assert "tick" in d.reason
    assert d.delta == pytest.approx(0.004)


def test_difference_beyond_tolerance_without_explanation_is_bug_suspected() -> None:
    fid = dataclasses.replace(BASE, exit_price=112.0)
    d = _by_field(classify_pair(0, BASE, fid, CTX))["exit_price"]
    assert d.diff_class is DiffClass.BUG_SUSPECTED
    assert "unexplained" in d.reason


def test_unexplained_exit_reason_is_bug_suspected() -> None:
    fid = dataclasses.replace(BASE, exit_reason="STOP")
    assert _cls(classify_pair(0, BASE, fid, CTX))["exit_reason"] is DiffClass.BUG_SUSPECTED


def test_declared_reason_alternative_is_expected_abstraction() -> None:
    ctx = PairContext(side=1, qty=8.25, reason_alternatives={("TARGET", "STOP"): "bar order"})
    fid = dataclasses.replace(BASE, exit_reason="STOP")
    d = _by_field(classify_pair(0, BASE, fid, ctx))["exit_reason"]
    assert d.diff_class is DiffClass.EXPECTED_ABSTRACTION and d.reason == "bar order"


def test_explained_component_amount_must_match_exactly() -> None:
    slip = Component(-0.01, DiffClass.BROKER_FIDELITY_DIFFERENCE, "one tick slip")
    ctx = PairContext(side=1, qty=8.25, components={"exit_price": (slip,)})
    ok = dataclasses.replace(BASE, exit_price=112.99)
    assert (
        _cls(classify_pair(0, BASE, ok, ctx))["exit_price"] is DiffClass.BROKER_FIDELITY_DIFFERENCE
    )
    off = dataclasses.replace(BASE, exit_price=112.90)  # explanation does NOT cover this delta
    assert _cls(classify_pair(0, BASE, off, ctx))["exit_price"] is DiffClass.BUG_SUSPECTED


def test_joint_fields_follow_explained_price_components() -> None:
    slip = Component(-0.01, DiffClass.BROKER_FIDELITY_DIFFERENCE, "one tick slip")
    ctx = PairContext(side=1, qty=8.25, components={"exit_price": (slip,)})
    fid = dataclasses.replace(
        BASE,
        exit_price=112.99,
        net_pnl=99.0 - 0.01 * 8.25,
        costs=8.25 + 0.01 * 8.25,
        r_multiple=(99.0 - 0.01 * 8.25) / (6.0 * 8.25),
    )
    c = _cls(classify_pair(0, BASE, fid, ctx))
    assert c["exit_price"] is DiffClass.BROKER_FIDELITY_DIFFERENCE
    assert c["net_pnl"] is DiffClass.BROKER_FIDELITY_DIFFERENCE
    assert c["costs"] is DiffClass.BROKER_FIDELITY_DIFFERENCE
    assert c["r_multiple"] is DiffClass.BROKER_FIDELITY_DIFFERENCE
    assert c["gross_pnl"] is DiffClass.EXACT_MATCH  # slippage is a cost, not a basis move


def test_joint_pnl_disagreement_not_explained_by_prices_is_bug_suspected() -> None:
    slip = Component(-0.01, DiffClass.BROKER_FIDELITY_DIFFERENCE, "one tick slip")
    ctx = PairContext(side=1, qty=8.25, components={"exit_price": (slip,)})
    fid = dataclasses.replace(BASE, exit_price=112.99, net_pnl=50.0)  # PnL accounting is off
    assert _cls(classify_pair(0, BASE, fid, ctx))["net_pnl"] is DiffClass.BUG_SUSPECTED


def test_price_gap_abstraction_classifies_entry_and_marks_entry_leg() -> None:
    gap = Component(-2.0, DiffClass.EXPECTED_ABSTRACTION, "fill timing", True)
    ctx = PairContext(side=1, qty=8.25, components={"entry_ref": (gap,), "fill_price": (gap,)})
    fid = dataclasses.replace(
        BASE,
        entry_ref=98.0,
        fill_price=99.0,
        net_pnl=99.0 + 2.0 * 8.25,
        gross_pnl=107.25 + 16.5,
        costs=8.25,
        r_multiple=(99.0 + 16.5) / (4.0 * 8.25),
    )
    diffs = classify_pair(0, BASE, fid, ctx)
    c = _cls(diffs)
    assert c["entry_ref"] is DiffClass.EXPECTED_ABSTRACTION
    assert c["fill_price"] is DiffClass.EXPECTED_ABSTRACTION
    assert c["net_pnl"] is DiffClass.EXPECTED_ABSTRACTION
    assert c["gross_pnl"] is DiffClass.EXPECTED_ABSTRACTION
    assert trade_mismatch_leg(diffs) == "ENTRY_MISMATCH"


def test_mismatch_leg_exit_only_and_both() -> None:
    gap = Component(-2.0, DiffClass.EXPECTED_ABSTRACTION, "timing", True)
    exit_ctx = PairContext(side=1, qty=1.0, components={"exit_price": (gap,)})
    fid = dataclasses.replace(BASE, exit_price=111.0)
    assert trade_mismatch_leg(classify_pair(0, BASE, fid, exit_ctx)) == "EXIT_MISMATCH"
    both_ctx = PairContext(side=1, qty=1.0, components={"exit_price": (gap,), "fill_price": (gap,)})
    fid2 = dataclasses.replace(BASE, exit_price=111.0, fill_price=99.0)
    assert trade_mismatch_leg(classify_pair(0, BASE, fid2, both_ctx)) == "BOTH"
    # tolerance matches are not "material" mismatches but ARE counted by the strict variant
    tol = dataclasses.replace(BASE, exit_price=113.004)
    diffs = classify_pair(0, BASE, tol, CTX)
    assert trade_mismatch_leg(diffs) == "NONE"
    assert trade_mismatch_leg(diffs, strict=True) == "EXIT_MISMATCH"


def test_missing_values_are_bug_suspected_not_silent() -> None:
    fid = dataclasses.replace(BASE, target=None)
    assert _cls(classify_pair(0, BASE, fid, CTX))["target"] is DiffClass.BUG_SUSPECTED


# --- compare_trade_lists: status derivation ------------------------------------------------------


def test_identical_lists_pass() -> None:
    r = compare_trade_lists("t", [BASE], [BASE], contexts={0: CTX})
    assert r.status == "PASS" and r.blocked_reason is None
    assert r.summary["mismatch_leg_counts"] == {
        "ENTRY_MISMATCH": 0,
        "EXIT_MISMATCH": 0,
        "BOTH": 0,
        "NONE": 1,
    }
    assert {"partial_exits", "tp1", "runner", "stop_changes"} <= set(r.summary["not_applicable"])


def test_unexplained_difference_makes_status_fail() -> None:
    fid = dataclasses.replace(BASE, exit_price=100.0, exit_reason="STOP")
    r = compare_trade_lists("t", [BASE], [fid], contexts={0: CTX})
    assert r.status == "FAIL"
    assert r.summary["class_counts"]["BUG_SUSPECTED"] >= 1
    assert "exit_price" in r.summary["bug_suspected_fields"]
    assert r.summary["mismatch_leg_counts"]["EXIT_MISMATCH"] == 1


def test_trade_count_difference_fails() -> None:
    r = compare_trade_lists("t", [BASE], [], contexts={})
    assert r.status == "FAIL"
    fields = {(d.field, d.diff_class) for d in r.field_diffs}
    assert ("trade_count", DiffClass.BUG_SUSPECTED) in fields
    assert ("trade_presence", DiffClass.BUG_SUSPECTED) in fields
    r2 = compare_trade_lists("t", [], [BASE])
    assert r2.status == "FAIL"


def test_zero_trades_on_both_sides_is_an_exact_pass() -> None:
    r = compare_trade_lists("t", [], [])
    assert r.status == "PASS" and r.field_diffs[0].diff_class is DiffClass.EXACT_MATCH


def test_trades_are_matched_by_signal_timestamp_not_position() -> None:
    other = dataclasses.replace(BASE, signal_ts_ns=9_000, decision_ts_ns=9_000)
    r = compare_trade_lists(
        "t", [BASE, other], [other, BASE], contexts={0: CTX, 1: dataclasses.replace(CTX)}
    )
    assert r.status == "PASS"


def test_tolerance_table_has_a_written_justification_for_every_compared_field() -> None:
    from research_workbench.differential import FIELD_LEG

    assert set(FIELD_LEG) <= set(DEFAULT_TOLERANCES)
    for name, tol in DEFAULT_TOLERANCES.items():
        assert tol.justification.strip(), name
        assert tol.abs_tol >= 0


# --- BLOCKED paths (decided before Nautilus is touched) ------------------------------------------


def _inputs(sid: str):
    s = GOLDEN_SCENARIOS[sid]
    return s.inputs()


def test_commission_cost_is_blocked_with_reason() -> None:
    market, cands, cost, window = _inputs("long_normal_target")
    cost = CostScenario("c", 1.0, 0.0, 0.0, 1.0)
    r = run_differential(market, cands, cost, window=window, scenario_id="x")
    assert r.status == "BLOCKED" and "commission" in (r.blocked_reason or "")


def test_trailing_exit_kind_is_blocked() -> None:
    market, cands, cost, window = _inputs("long_normal_target")
    trail = CandidateArrays(
        cands.decision_idx,
        cands.direction,
        cands.stop,
        cands.target,
        cands.target_r,
        cands.exit_kind.copy() * 0 + EXIT_TRAIL,
    )
    r = run_differential(market, trail, cost, window=window, scenario_id="x")
    assert r.status == "BLOCKED" and "TRAIL" in (r.blocked_reason or "")


def test_entry_gap_stop_is_blocked() -> None:
    import numpy as np

    from alpha.fast.sim import MarketArrays

    market, cands, cost, window = _inputs("long_normal_target")
    o, h, low, c = (market.o.copy(), market.h.copy(), market.l.copy(), market.c.copy())
    o[4], h[4], low[4], c[4] = 90.0, 90.5, 89.5, 90.0  # opens far below the 95 stop
    gapped = MarketArrays(
        o, h, low, c, market.spread, market.minute, market.day, market.contig_next
    )
    cands = CandidateArrays(
        cands.decision_idx,
        cands.direction,
        cands.stop,
        np.array([np.nan]),
        cands.target_r,
        cands.exit_kind,
    )
    r = run_differential(gapped, cands, cost, window=window, scenario_id="x")
    assert r.status == "BLOCKED" and "ENTRY_GAP_STOP" in (r.blocked_reason or "")


def test_missing_nautilus_is_blocked_not_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "nautilus_kernel.replay_backtest", None)
    market, cands, cost, window = _inputs("long_normal_target")
    r = run_differential(market, cands, cost, window=window, scenario_id="x")
    assert r.status == "BLOCKED" and "nautilus_trader unavailable" in (r.blocked_reason or "")
