"""Review round 1: disclosure of by-construction fields, non-circular joint fields, ERROR status.

Pure / light: no Nautilus import (the replay module is replaced by a stub where needed).
"""

from __future__ import annotations

import dataclasses
import sys
import types

import pytest

from alpha.common.sim import CostScenario
from alpha.fast.sim import MarketArrays
from research_workbench import differential as diff
from research_workbench.differential import (
    Component,
    DiffClass,
    NormalizedTrade,
    PairContext,
    classify_pair,
    compare_trade_lists,
    run_differential,
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
    qty=8.25,
)
CTX = PairContext(side=1, qty=8.25)
BY_CONSTRUCTION = ["signal_ts_ns", "direction", "stop", "target", "qty"]


def _inputs(sid: str):
    return GOLDEN_SCENARIOS[sid].inputs()


def test_by_construction_fields_are_marked_and_excluded_from_matched_statistics() -> None:
    diffs = classify_pair(0, BASE, BASE, CTX)
    marked = {d.field for d in diffs if d.by_construction}
    assert marked == set(BY_CONSTRUCTION)
    assert all("BY_CONSTRUCTION (not independent)" in d.reason for d in diffs if d.by_construction)
    assert not any(d.by_construction for d in diffs if d.field in ("fill_price", "exit_price"))
    r = compare_trade_lists("t", [BASE], [BASE], contexts={0: CTX})
    n_indep = sum(1 for d in r.field_diffs if not d.by_construction)
    assert r.summary["matched_fields"] == n_indep < len(r.field_diffs)
    assert r.summary["by_construction_fields"] == BY_CONSTRUCTION
    assert "Does NOT validate candidate generation or position sizing" in r.summary["scope"]


def test_by_construction_mismatch_still_fails_the_status() -> None:
    fid = dataclasses.replace(BASE, qty=16.5, stop=94.0)
    r = compare_trade_lists("t", [BASE], [fid], contexts={0: CTX})
    assert r.status == "FAIL"
    bad = {d.field for d in r.field_diffs if d.diff_class is DiffClass.BUG_SUSPECTED}
    assert {"qty", "stop"} <= bad


def test_coherently_wrong_fast_net_pnl_is_bug_suspected() -> None:
    wrong = dataclasses.replace(BASE, net_pnl=BASE.net_pnl * 1.1)
    r = compare_trade_lists("t", [wrong], [BASE], contexts={0: CTX})
    assert r.status == "FAIL"
    names = set(r.summary["bug_suspected_fields"])
    assert "fast_net_pnl_consistency" in names and "net_pnl" in names


def test_wrong_fast_r_is_bug_suspected_even_with_explained_price_components() -> None:
    slip = Component(-0.01, DiffClass.BROKER_FIDELITY_DIFFERENCE, "one tick slip")
    ctx = PairContext(side=1, qty=8.25, components={"exit_price": (slip,)})
    fast_wrong_r = dataclasses.replace(BASE, r_multiple=2.5)
    fid = dataclasses.replace(
        BASE,
        exit_price=112.99,
        net_pnl=99.0 - 0.0825,
        costs=8.25 + 0.0825,
        r_multiple=(99.0 - 0.0825) / (6.0 * 8.25),
    )
    r = compare_trade_lists("t", [fast_wrong_r], [fid], contexts={0: ctx})
    assert r.status == "FAIL"
    assert "fast_r_multiple_consistency" in r.summary["bug_suspected_fields"]
    assert "r_multiple" in r.summary["bug_suspected_fields"]  # expected delta ignores FAST R


def test_correct_accounting_with_explained_components_still_passes() -> None:
    slip = Component(-0.01, DiffClass.BROKER_FIDELITY_DIFFERENCE, "one tick slip")
    ctx = PairContext(side=1, qty=8.25, components={"exit_price": (slip,)})
    fid = dataclasses.replace(
        BASE,
        exit_price=112.99,
        net_pnl=99.0 - 0.0825,
        costs=8.25 + 0.0825,
        r_multiple=(99.0 - 0.0825) / (6.0 * 8.25),
    )
    assert compare_trade_lists("t", [BASE], [fid], contexts={0: ctx}).status == "PASS"


def test_both_engines_coherently_wrong_is_still_caught() -> None:
    wrong = dataclasses.replace(BASE, net_pnl=BASE.net_pnl * 1.1, r_multiple=2.2)
    r = compare_trade_lists("t", [wrong], [wrong], contexts={0: CTX})
    assert r.status == "FAIL"
    assert {"fast_net_pnl_consistency", "fidelity_net_pnl_consistency"} <= set(
        r.summary["bug_suspected_fields"]
    )


def _stub_replay_module(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("nautilus_kernel.replay_backtest")
    mod.ReplayCandidate = lambda **kw: kw  # type: ignore[attr-defined]
    mod.run_candidate_replay_backtest = lambda **kw: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nautilus_kernel.replay_backtest", mod)


def _raiser(exc: Exception):
    def f(*a, **k):
        raise exc

    return f


def test_unexpected_instrument_exception_is_error_not_blocked(monkeypatch) -> None:
    _stub_replay_module(monkeypatch)
    monkeypatch.setattr(diff, "_default_instrument", _raiser(RuntimeError("boom")))
    market, cands, cost, window = _inputs("long_normal_target")
    r = run_differential(market, cands, cost, window=window, scenario_id="x")
    assert r.status == "ERROR" and r.blocked_reason is None
    assert r.summary["error_type"] == "RuntimeError" and r.summary["error_message"] == "boom"
    assert r.summary["by_construction_fields"] and r.summary["scope"]


def test_replay_stage_exception_is_error(monkeypatch) -> None:
    _stub_replay_module(monkeypatch)
    monkeypatch.setattr(
        diff, "_default_instrument", lambda: type("I", (), {"price_increment": 0.01})()
    )
    monkeypatch.setattr(diff, "_write_synthetic_catalog", _raiser(ValueError("catalog failed")))
    market, cands, cost, window = _inputs("long_normal_target")
    r = run_differential(market, cands, cost, window=window, scenario_id="x")
    assert r.status == "ERROR" and r.summary["error_type"] == "ValueError"


def test_missing_instrument_fixture_is_blocked(monkeypatch) -> None:
    _stub_replay_module(monkeypatch)
    monkeypatch.setattr(diff, "_default_instrument", _raiser(FileNotFoundError("no fixture")))
    market, cands, cost, window = _inputs("long_normal_target")
    r = run_differential(market, cands, cost, window=window, scenario_id="x")
    assert r.status == "BLOCKED" and "instrument unavailable" in (r.blocked_reason or "")


def test_malformed_market_timestamps_are_error() -> None:
    market, cands, cost, window = _inputs("long_normal_target")
    minute = market.minute.copy()
    minute[8] = minute[7]  # not strictly increasing
    bad = MarketArrays(
        market.o,
        market.h,
        market.l,
        market.c,
        market.spread,
        minute,
        market.day,
        market.contig_next,
    )
    r = run_differential(bad, cands, cost, window=window, scenario_id="x")
    assert r.status == "ERROR" and r.summary["error_type"] == "ValueError"


def test_blocked_results_also_carry_scope_and_by_construction_fields() -> None:
    market, cands, _, window = _inputs("long_normal_target")
    cost = CostScenario("c", 1.0, 0.0, 0.0, 1.0)
    r = run_differential(market, cands, cost, window=window, scenario_id="x")
    assert r.status == "BLOCKED"
    assert r.summary["scope"] and "qty" in r.summary["by_construction_fields"]
