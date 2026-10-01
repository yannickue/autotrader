"""End-to-end FAST <-> REAL Nautilus BacktestEngine differential on the golden scenarios.

HEAVY: imports nautilus_trader and starts a BacktestEngine per scenario (~1-3 s each, ~250 MB).
This file must be tiered ``slow`` (or at least ``integration``): ``tests/conftest.py`` assigns
tiers by path, so the lead must add

    "tests/unit/research_workbench/test_differential_e2e.py",

to ``SLOW_FILES`` in ``tests/conftest.py`` (this lane does not edit conftest).

Nautilus semantics asserted here (measured, nautilus_trader 1.231, bar_execution=True):
* a market order submitted in ``on_bar`` at the decision bar CLOSE fills at that bar's closing
  book (ask close for BUY, bid close for SELL), +/- one tick if FillModel(prob_slippage=1);
* STOP_MARKET fills at its trigger price when the bar trades through it, at the bar's open book
  when the bar gaps through it; LIMIT target fills at the limit price;
* bars are walked as ticks O, H, L, C; every tick carries the bar CLOSE ts_event.
"""

from __future__ import annotations

import pytest

from research_workbench.differential import DiffClass, run_differential
from research_workbench.golden import GOLDEN_SCENARIOS

pytest.importorskip("nautilus_trader")

# scenario -> {field: expected DiffClass} for every field that is NOT an exact match
EXPECTED_NON_EXACT: dict[str, dict[str, DiffClass]] = {
    "long_normal_target": {},
    "short_normal_target": {},
    "long_normal_stop": {},
    "short_normal_stop": {},
    "target_crossed_at_fill": {},
    "session_end": {},
    "spread_slippage_cost_stop": {},
    "short_stop_and_target_same_bar": {},
    "gap_through_stop": {"exit_ts_ns": DiffClass.EXPECTED_ABSTRACTION},
    "data_gap": {
        "exit_ts_ns": DiffClass.EXPECTED_ABSTRACTION,
        "exit_price": DiffClass.EXPECTED_ABSTRACTION,
        "net_pnl": DiffClass.EXPECTED_ABSTRACTION,
        "gross_pnl": DiffClass.EXPECTED_ABSTRACTION,
        "r_multiple": DiffClass.EXPECTED_ABSTRACTION,
    },
    "long_stop_and_target_same_bar": {
        "exit_reason": DiffClass.EXPECTED_ABSTRACTION,
        "exit_price": DiffClass.EXPECTED_ABSTRACTION,
        "net_pnl": DiffClass.EXPECTED_ABSTRACTION,
        "gross_pnl": DiffClass.EXPECTED_ABSTRACTION,
        "r_multiple": DiffClass.EXPECTED_ABSTRACTION,
    },
    "spread_slippage_cost_target": {
        "exit_price": DiffClass.BROKER_FIDELITY_DIFFERENCE,
        "net_pnl": DiffClass.BROKER_FIDELITY_DIFFERENCE,
        "costs": DiffClass.BROKER_FIDELITY_DIFFERENCE,
        "r_multiple": DiffClass.BROKER_FIDELITY_DIFFERENCE,
    },
}
_RESULTS: dict[str, object] = {}


def _run(sid: str):
    if sid not in _RESULTS:
        market, cands, cost, window = GOLDEN_SCENARIOS[sid].inputs()
        _RESULTS[sid] = run_differential(market, cands, cost, window=window, scenario_id=sid)
    return _RESULTS[sid]


def test_every_golden_scenario_has_an_expectation() -> None:
    assert set(EXPECTED_NON_EXACT) == set(GOLDEN_SCENARIOS)


@pytest.mark.parametrize("sid", sorted(EXPECTED_NON_EXACT))
def test_golden_scenario_through_both_real_engines(sid: str) -> None:
    r = _run(sid)
    exp = GOLDEN_SCENARIOS[sid].expected
    bad = [d for d in r.field_diffs if d.diff_class is DiffClass.BUG_SUSPECTED]
    assert r.status == "PASS", (r.blocked_reason, bad)
    assert r.fast_trade_count == r.fidelity_trade_count == exp["n_trades"]
    non_exact = {
        d.field: d.diff_class
        for d in r.field_diffs
        if d.diff_class not in (DiffClass.EXACT_MATCH, DiffClass.TOLERANCE_MATCH)
    }
    assert non_exact == EXPECTED_NON_EXACT[sid]
    assert r.summary["class_counts"]["BUG_SUSPECTED"] == 0
    # every non-exact diff carries a written reason
    assert all(d.reason for d in r.field_diffs if d.diff_class is not DiffClass.EXACT_MATCH)


@pytest.mark.parametrize(
    "sid", ["long_normal_target", "short_normal_stop", "gap_through_stop", "session_end"]
)
def test_nautilus_side_numbers_match_hand_calculation(sid: str) -> None:
    """The FIDELITY values are real Nautilus output: check them against hand numbers directly."""
    r = _run(sid)
    exp = GOLDEN_SCENARIOS[sid].expected
    got = {d.field: d.fidelity for d in r.field_diffs}
    assert got["fill_price"] == pytest.approx(exp["fill"], abs=1e-9)
    assert got["net_pnl"] == pytest.approx(
        {"gap_through_stop": -90.75, "session_end": -8.25}.get(sid, exp["net"]), abs=1e-6
    )
    assert got["exit_reason"] == exp["exit_reason"]


def test_mismatch_leg_counts_reported_per_leg() -> None:
    gap = _run("gap_through_stop").summary["mismatch_leg_counts"]
    assert gap == {"ENTRY_MISMATCH": 0, "EXIT_MISMATCH": 1, "BOTH": 0, "NONE": 0}
    clean = _run("long_normal_target").summary["mismatch_leg_counts"]
    assert clean == {"ENTRY_MISMATCH": 0, "EXIT_MISMATCH": 0, "BOTH": 0, "NONE": 1}
    assert "mfe_r" in _run("long_normal_target").summary["not_applicable"]


def test_nautilus_replay_is_real_engine_output_not_an_echo() -> None:
    """A corrupted candidate (stop moved) must be visible as a real difference, not a PASS."""
    import dataclasses

    from research_workbench import differential as diff

    s = GOLDEN_SCENARIOS["long_normal_stop"]
    market, cands, cost, window = s.inputs()
    orig = diff.normalize_fast

    def tampered(m, c, t):
        trades = orig(m, c, t)
        return [dataclasses.replace(x, exit_price=x.exit_price + 1.0) for x in trades]

    diff.normalize_fast = tampered
    try:
        r = run_differential(market, cands, cost, window=window, scenario_id="tampered")
    finally:
        diff.normalize_fast = orig
    assert r.status == "FAIL"
    assert "exit_price" in r.summary["bug_suspected_fields"]
