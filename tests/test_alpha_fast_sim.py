from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha.common.frame import Frame
from alpha.common.sim import COST_SCENARIOS, ExitSpec, Signals, simulate
from alpha.fast.sim import (
    EXIT_FIXED_R,
    EXIT_TRAIL,
    REASON_ENTRY_GAP_STOP,
    REASON_SESSION_END,
    REASON_STOP,
    CandidateArrays,
    MarketArrays,
    day_clustered_mean_ci,
    fast_metrics,
    simulate_fast,
    simulate_many,
)
from alpha.regime import REGIME_DIMENSIONS
from alpha.signals import SignalCandidate
from alpha.signals.evaluation import evaluate_candidates


def _frame(
    o: list[float],
    h: list[float],
    low: list[float],
    c: list[float],
    *,
    spread: float = 1.0,
    start: str = "2024-01-02 09:00",
) -> Frame:
    ts = pd.date_range(start, periods=len(o), freq="5min", tz="Europe/Berlin")
    dates = ts.tz_localize(None).normalize().to_numpy().astype("datetime64[D]")
    return Frame(
        ts=ts,
        o=np.asarray(o, dtype=float),
        h=np.asarray(h, dtype=float),
        l=np.asarray(low, dtype=float),
        c=np.asarray(c, dtype=float),
        spread=np.full(len(o), spread, dtype=float),
        minute=np.asarray(ts.hour * 60 + ts.minute, dtype=np.int64),
        day=np.zeros(len(o), dtype=np.int64),
        date=dates,
        contig_next=np.asarray([True] * (len(o) - 1) + [False]),
    )


def _market(frame: Frame) -> MarketArrays:
    return MarketArrays.from_frame(frame)


def _candidates(
    decision: list[int],
    side: list[int],
    stop: list[float],
    *,
    exit_kind: int = EXIT_FIXED_R,
    exit_r: float = 2.0,
) -> CandidateArrays:
    size = len(decision)
    return CandidateArrays(
        decision_idx=np.asarray(decision, dtype=np.int64),
        direction=np.asarray(side, dtype=np.int8),
        stop=np.asarray(stop, dtype=float),
        target=np.full(size, np.nan),
        target_r=np.full(size, exit_r),
        exit_kind=np.full(size, exit_kind, dtype=np.int8),
    )


def _reference(frame: Frame, candidates: CandidateArrays, exit_spec: ExitSpec, cost_name: str):
    side = np.zeros(len(frame), dtype=np.int8)
    stop = np.full(len(frame), np.nan)
    side[candidates.decision_idx] = candidates.direction
    stop[candidates.decision_idx] = candidates.stop
    return simulate(frame, Signals(side, stop), exit_spec, COST_SCENARIOS[cost_name])[0]


def test_fixed_r_matches_reference_for_every_cost_scenario() -> None:
    frame = _frame(
        [100, 100, 102, 103, 104, 105],
        [101, 103, 104, 106, 106, 106],
        [99, 99, 101, 102, 103, 104],
        [100, 102, 103, 105, 105, 105],
    )
    candidates = _candidates([0], [1], [94], exit_r=1.0)
    for cost_name, cost in COST_SCENARIOS.items():
        got = simulate_fast(_market(frame), candidates, cost)
        want = _reference(frame, candidates, ExitSpec("fixed_r", 1.0), cost_name)
        np.testing.assert_array_equal(got.entry_idx, want["entry_idx"].to_numpy())
        np.testing.assert_array_equal(got.exit_idx, want["exit_idx"].to_numpy())
        np.testing.assert_allclose(got.r_multiple, want["r_multiple"], rtol=0, atol=1e-9)
        np.testing.assert_allclose(got.net_pnl_eur, want["pnl_eur"], rtol=0, atol=1e-9)


def test_signal_candidate_conversion_preserves_reference_r_target_semantics() -> None:
    frame = _frame([100, 100], [101, 101], [99, 99], [100, 100])
    candidate = SignalCandidate(
        strategy_id="test",
        strategy_version="1",
        instrument="GER40",
        direction=1,
        signal_ts=frame.ts[0] + pd.Timedelta(minutes=5),  # M5 bar CLOSE of decision bar 0
        signal_price=100.0,
        entry_intent="NEXT_BAR_OPEN_MARKET",
        stop=90.0,
        target=150.0,
        exit_spec=ExitSpec("fixed_r", 2.0),
        h1_regime={name: "x" for name in REGIME_DIMENSIONS},
        m15_context={},
        session_phase="OPEN",
        setup_metadata={},
        param_fingerprint="test",
    )
    arrays = CandidateArrays.from_signal_candidates(frame, [candidate])
    assert arrays.decision_idx.tolist() == [0]
    assert np.isnan(arrays.target[0])
    assert arrays.target_r.tolist() == [2.0]


def test_stop_wins_when_stop_and_target_are_touched() -> None:
    frame = _frame([100, 100, 100], [101, 110, 101], [99, 90, 99], [100, 100, 100], spread=0)
    got = simulate_fast(
        _market(frame),
        _candidates([0], [1], [95], exit_r=1),
        COST_SCENARIOS["GROSS_REFERENCE"],
    )
    assert got.exit_reason.tolist() == [REASON_STOP]
    assert got.r_multiple.tolist() == [-1.0]


def test_adverse_entry_gap_stops_at_entry_open() -> None:
    frame = _frame([100, 90, 91], [101, 92, 92], [99, 89, 90], [100, 91, 91], spread=1)
    got = simulate_fast(_market(frame), _candidates([0], [1], [95]), COST_SCENARIOS["BASE"])
    assert got.entry_gap.tolist() == [True]
    assert got.exit_reason.tolist() == [REASON_ENTRY_GAP_STOP]
    assert got.entry_idx.tolist() == got.exit_idx.tolist() == [1]


def test_forced_flat_at_2130_open() -> None:
    frame = _frame(
        [100, 100, 101, 102], [101, 102, 103, 103], [99, 99, 100, 101], [100, 101, 102, 102],
        spread=1, start="2024-01-02 19:50",
    )
    frame.minute[:] = [19 * 60 + 50, 19 * 60 + 55, 20 * 60, 21 * 60 + 30]
    got = simulate_fast(
        _market(frame), _candidates([0], [1], [90], exit_r=10), COST_SCENARIOS["BASE"]
    )
    assert got.exit_idx.tolist() == [3]
    assert got.exit_reason.tolist() == [REASON_SESSION_END]
    assert got.holding_bars.tolist() == [2]


def test_trailing_stop_and_costs_match_reference() -> None:
    frame = _frame(
        [100, 100, 104, 103, 102],
        [101, 105, 106, 104, 103],
        [99, 99, 103, 100, 101],
        [100, 104, 104, 102, 102],
        spread=1,
    )
    candidates = _candidates([0], [1], [94], exit_kind=EXIT_TRAIL, exit_r=1)
    got = simulate_fast(_market(frame), candidates, COST_SCENARIOS["COMBINED_ADVERSE"])
    want = _reference(frame, candidates, ExitSpec("trail", 1), "COMBINED_ADVERSE")
    np.testing.assert_allclose(got.r_multiple, want["r_multiple"], rtol=0, atol=1e-9)
    np.testing.assert_allclose(got.cost_eur, want["cost_eur"], rtol=0, atol=1e-9)
    np.testing.assert_allclose(got.mfe_r, want["mfe_r"], rtol=0, atol=1e-9)
    np.testing.assert_allclose(got.mae_r, want["mae_r"], rtol=0, atol=1e-9)


def test_simulate_many_is_deterministic() -> None:
    frame = _frame(
        [100, 100, 102, 103],
        [101, 103, 104, 104],
        [99, 99, 101, 102],
        [100, 102, 103, 103],
    )
    market = _market(frame)
    inputs = [_candidates([0], [1], [94], exit_r=1), _candidates([0], [-1], [110], exit_r=1)]
    first = simulate_many(market, inputs, COST_SCENARIOS["BASE"])
    second = simulate_many(market, inputs, COST_SCENARIOS["BASE"])
    for left, right in zip(first, second, strict=True):
        np.testing.assert_array_equal(left.as_matrix(), right.as_matrix())


def test_fast_metrics_include_tail_robust_expectancy_and_clustered_ci() -> None:
    frame = _frame(
        [100, 100, 102, 103],
        [101, 103, 104, 104],
        [99, 99, 101, 102],
        [100, 102, 103, 103],
        spread=0,
    )
    trades = simulate_fast(
        _market(frame),
        _candidates([0], [1], [95], exit_r=1),
        COST_SCENARIOS["GROSS_REFERENCE"],
    )
    metrics = fast_metrics(trades, trading_days=np.asarray([0, 1, 2]))
    assert metrics["trades"] == 1
    assert metrics["zero_trade_days"] == 2
    assert metrics["expectancy_without_top_2_winners"] is None
    assert metrics["avg_holding_bars"] == trades.holding_bars.mean()
    assert day_clustered_mean_ci(
        np.asarray([1.0, -1.0]), np.asarray([0, 1]), seed=7
    ) == (None, None)


@pytest.mark.skipif(not Path("data/ar1_ger40").exists(), reason="research dataset not present")
def test_golden_candidates_match_ar1_for_all_cost_scenarios() -> None:
    from alpha.common.dataset import load_research_dataset
    from alpha.common.protocol import Partition, SplitPlan

    sys.path.insert(0, str(Path("research/runners").resolve()))
    import ar2_compare

    config = json.loads(Path("research/configs/ar2_phase2.json").read_text())
    plan = SplitPlan(**{key: Partition(key, *value) for key, value in config["splits"].items()})
    dataset = load_research_dataset("data/ar1_ger40")
    data = ar2_compare.dev_frame(dataset.frame, plan).iloc[46000:58000].reset_index(drop=True)
    frame = Frame.from_dataframe(data)
    market = MarketArrays.from_frame(frame)
    with Path("research/reference/ar2_ref12k/golden.pkl").open("rb") as handle:
        golden = pickle.load(handle)
    for candidates in golden.values():
        arrays = CandidateArrays.from_signal_candidates(frame, candidates)
        for cost in COST_SCENARIOS.values():
            got = simulate_fast(market, arrays, cost)
            want = evaluate_candidates(frame, candidates, cost=cost).trades
            if want.empty:
                assert len(got.entry_idx) == 0
                continue
            np.testing.assert_array_equal(got.entry_idx, want["entry_idx"].to_numpy())
            np.testing.assert_array_equal(got.exit_idx, want["exit_idx"].to_numpy())
            np.testing.assert_array_equal(got.exit_reason_labels, want["exit_reason"].to_numpy())
            np.testing.assert_allclose(got.r_multiple, want["r_multiple"], rtol=0, atol=1e-9)
            np.testing.assert_allclose(got.net_pnl_eur, want["pnl_eur"], rtol=0, atol=1e-9)
