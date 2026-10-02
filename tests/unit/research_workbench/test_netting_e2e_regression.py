"""Regression for the SYN_E2E defect + engine-timestamp audit (HEAVY; tier ``slow``).

The lead adds ``"tests/unit/research_workbench/test_netting_e2e_regression.py"`` to ``SLOW_FILES``.

Original failure mode (verified by mutation, see the test docstring): the replay decided inside
``on_bar`` BEFORE the exchange had matched the bar, so a candidate whose decision bar is the exit
bar of the previous trade (intrabar STOP/TARGET) found the position "still open" and was ignored,
and market orders filled against the PREVIOUS bar close.
"""

from __future__ import annotations

import numpy as np
import pytest

from alpha.common.sim import CostScenario, SimRules
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, MarketArrays, simulate_fast
from research_workbench.differential import (
    BAR_NS,
    DiffClass,
    _default_instrument,
    _write_synthetic_catalog,
    bar_open_ts_ns,
    run_differential,
)

pytest.importorskip("nautilus_trader")

N = 40
DECISIONS = (2, 5, 8, 11, 14, 17, 20, 23)  # each exits intrabar 3 bars later = next decision bar


def _chain() -> tuple[MarketArrays, CandidateArrays]:
    """Trending bars (close_k = 100 + k, open_k = close_{k-1}); every trade is a long that hits its
    explicit target 1.5 above the fill INSIDE bar i+3, which is exactly the next decision bar."""
    c = 100.0 + np.arange(N, dtype=float)
    o = np.concatenate(([99.0], c[:-1]))
    h = np.maximum(o, c) + 0.2
    low = np.minimum(o, c) - 0.2
    contig = np.ones(N, dtype=bool)
    contig[-1] = False
    market = MarketArrays(
        o, h, low, c, np.full(N, 1.0), 600 + 5 * np.arange(N), np.zeros(N, dtype=np.int64), contig
    )
    idx = np.array(DECISIONS, dtype=np.int64)
    fill = c[idx] + 1.0
    cands = CandidateArrays(
        decision_idx=idx,
        direction=np.ones(len(idx), dtype=np.int8),
        stop=fill - 6.0,
        target=fill + 1.5,
        target_r=np.full(len(idx), 2.0),
        exit_kind=np.full(len(idx), EXIT_FIXED_R, dtype=np.int8),
    )
    return market, cands


COST = CostScenario("SLIP0", spread_mult=1.0, slippage_pts=0.0)
RULES = SimRules(max_trades_per_day=20)  # the default cap of 6 would drop the last candidates


def test_fast_side_of_the_chain_is_back_to_back_at_the_previous_exit_bar() -> None:
    market, cands = _chain()
    t = simulate_fast(market, cands, COST, rules=RULES)
    assert len(t) == len(DECISIONS) >= 6
    assert [int(x) for x in t.decision_idx] == list(DECISIONS)
    assert [int(x) for x in t.exit_idx[:-1]] == list(DECISIONS[1:])  # decision == previous exit
    assert {str(x) for x in t.exit_reason_labels} == {"TARGET"}


def test_back_to_back_trades_after_intrabar_exits_are_all_replayed() -> None:
    """Fails with the old on_bar scheme (and with ALERT_DELAY_NS=0): 7 of 8 candidates ignored."""
    market, cands = _chain()
    r = run_differential(market, cands, COST, rules=RULES, window=None, scenario_id="chain")
    assert r.status == "PASS", (r.blocked_reason, r.summary.get("bug_suspected_fields"))
    assert r.summary["nautilus_ignored_candidates"] == []
    assert r.fast_trade_count == r.fidelity_trade_count == len(DECISIONS)
    assert r.summary["netting"]["same_bar_reentries"] == len(DECISIONS) - 1
    assert r.summary["netting"]["replay_ignored_active_position"] == 0
    non_exact = {
        d.field
        for d in r.field_diffs
        if d.diff_class not in (DiffClass.EXACT_MATCH, DiffClass.TOLERANCE_MATCH)
    }
    assert non_exact == set()  # fills against the decision-bar close, target fills at the target


def test_engine_timestamps_are_kept_next_to_the_modeled_ones() -> None:
    """Intrabar exits carry the bar-close ts natively; re-entries run 1 ns after the close."""
    market, cands = _chain()
    r = run_differential(market, cands, COST, rules=RULES, window=None, scenario_id="chain")
    open_ts = bar_open_ts_ns(market)
    timing = r.summary["engine_timing"]
    assert len(timing) == len(DECISIONS)
    for row, i in zip(timing, DECISIONS, strict=True):
        decision_ts = int(open_ts[i]) + BAR_NS
        assert row["signal_ts_ns"] == decision_ts
        assert row["engine_entry_ts_ns"] == decision_ts + 1  # alert order: +1 ns, after the bar
        assert row["modeled_exit_ts_ns"] == int(open_ts[i + 3]) + BAR_NS
        assert row["engine_exit_ts_ns"] == row["modeled_exit_ts_ns"]  # intrabar TARGET: native


def test_forced_exit_is_plus_one_and_same_ts_reentry_is_plus_two(tmp_path) -> None:
    """Forced exit at the close of bar 8 and a candidate deciding on that same close."""
    from nautilus_kernel.replay_backtest import ReplayCandidate, run_candidate_replay_backtest

    market, _ = _chain()
    inst = _default_instrument()
    open_ts = bar_open_ts_ns(market)
    _write_synthetic_catalog(tmp_path, inst, market, 1.0, open_ts)
    close = [int(x) + BAR_NS for x in open_ts]
    cands = [
        ReplayCandidate(close[2], 1, 90.0, 400.0, 1.0),  # never hits stop/target
        ReplayCandidate(close[8], 1, 90.0, 400.0, 1.0),  # decides on the forced-exit close
    ]
    rep = run_candidate_replay_backtest(
        catalog_path=tmp_path, instrument=inst, timeframe="5m", candidates=cands,
        forced_exits={close[8]: "SESSION_END"},
    )  # fmt: skip
    entries = [f for f in rep.fills if f.tag == "entry"]
    exits = [f for f in rep.fills if f.tag == "exit:SESSION_END"]
    assert len(entries) == 2 and len(exits) == 1 and rep.ignored == []
    assert exits[0].ts_ns == close[8] and exits[0].engine_ts_ns == close[8] + 1
    assert entries[0].ts_ns == close[2] and entries[0].engine_ts_ns == close[2] + 1
    assert entries[1].ts_ns == close[8] and entries[1].engine_ts_ns == close[8] + 2
