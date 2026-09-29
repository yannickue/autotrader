from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from alpha.common.frame import Frame
from alpha.common.sim import CostScenario, ExitSpec, SizingSpec
from alpha.signals import (
    CandidateStrategyBase,
    SignalCandidate,
    assert_truncation_invariant,
    evaluate_candidates,
    generate_candidates,
    grouped_metrics,
    record_conflicts,
)
from alpha.timeframe import MtfView

REGIME = {
    "DIRECTION": "UP",
    "TREND_STRENGTH": "TRENDING",
    "VOLATILITY": "NORMAL",
    "VOL_STATE": "EXPANSION",
}
CONTEXT = {"BREAKOUT_SETUP": True, "PULLBACK": False}


def bars(
    closes: list[float],
    *,
    start: str = "2026-02-03 09:00",
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    count = len(closes)
    values = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "ts": pd.date_range(start, periods=count, freq="5min", tz="Europe/Berlin"),
            "open": values if opens is None else opens,
            "high": values + 1 if highs is None else highs,
            "low": values - 1 if lows is None else lows,
            "close": values,
            "spread_pts": np.zeros(count),
        }
    )


def candidate(
    signal_ts: pd.Timestamp,
    *,
    strategy_id: str = "demo",
    strategy_version: str = "1.0",
    direction: int = 1,
    signal_price: float = 100.0,
    stop: float = 90.0,
    context: dict[str, bool] | None = None,
    param_fingerprint: str = "abc123",
) -> SignalCandidate:
    return SignalCandidate(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        instrument="DAX",
        direction=direction,
        signal_ts=signal_ts,
        signal_price=signal_price,
        entry_intent="NEXT_BAR_OPEN_MARKET",
        stop=stop,
        target=None,
        exit_spec=ExitSpec("fixed_r", 1.0),
        h1_regime=REGIME,
        m15_context=CONTEXT if context is None else context,
        session_phase="OPEN",
        setup_metadata={"pattern": "test"},
        param_fingerprint=param_fingerprint,
    )


def test_candidate_is_frozen_and_validates_prices_and_json_metadata() -> None:
    item = candidate(pd.Timestamp("2026-02-03 09:05", tz="Europe/Berlin"))
    with pytest.raises((AttributeError, TypeError)):
        item.stop = 95.0  # type: ignore[misc]
    with pytest.raises(ValueError, match="stop"):
        replace(item, stop=101.0)
    with pytest.raises(ValueError, match="finite"):
        replace(item, signal_price=np.inf)
    with pytest.raises(ValueError, match="JSON"):
        replace(item, setup_metadata={"bad": object()})


class StepStrategy(CandidateStrategyBase):
    strategy_id = "steps"
    strategy_version = "2.1"

    def __init__(self) -> None:
        super().__init__({"threshold": 100.0})
        self.calls: list[str] = []
        self.emitted = 0

    def reset(self) -> None:
        self.calls.clear()
        self.emitted = 0

    def regime_eligible(self, state) -> bool:
        self.calls.append("REGIME")
        return float(state.m5["close"]) >= 100.0

    def setup_condition(self, state) -> bool:
        self.calls.append("SETUP")
        return True

    def trigger(self, state, signal_ts: pd.Timestamp) -> SignalCandidate | None:
        self.calls.append("TRIGGER")
        if self.emitted:
            return None
        self.emitted += 1
        return candidate(
            signal_ts,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            param_fingerprint=self.param_fingerprint,
        )


def test_runner_keeps_three_steps_separate_and_resets_each_run() -> None:
    strategy = StepStrategy()
    view = MtfView(bars([99.0, 100.0, 101.0]))

    first = generate_candidates(strategy, view)
    first_calls = strategy.calls.copy()
    second = generate_candidates(strategy, view)

    assert [call for call in first_calls if call == "REGIME"] == ["REGIME"] * 3
    assert first_calls[-3:] == ["REGIME", "SETUP", "TRIGGER"]
    assert first == second
    assert len(second) == 1
    assert strategy.param_fingerprint


class CausalStrategy(StepStrategy):
    def regime_eligible(self, state) -> bool:
        return float(state.m5["close"]) >= self.params["threshold"]


class LeakyStrategy(CausalStrategy):
    def __init__(self, source: pd.DataFrame) -> None:
        super().__init__()
        self.future_close = float(source["close"].iloc[-1])

    def regime_eligible(self, state) -> bool:
        return self.future_close < 500.0 and super().regime_eligible(state)


def test_truncation_invariance_accepts_causal_and_detects_leaky_strategy() -> None:
    frame = bars([99.0, 100.0, 101.0, 102.0])
    assert_truncation_invariant(lambda _frame: CausalStrategy(), frame, cutoff=1)
    with pytest.raises(AssertionError, match="truncation"):
        assert_truncation_invariant(lambda data: LeakyStrategy(data), frame, cutoff=1)


def test_bridge_preserves_attribution_and_ar1_stop_first_and_gap_entry() -> None:
    frame = bars(
        [100.0, 100.0, 100.0, 100.0],
        opens=[100.0, 100.0, 110.0, 100.0],
        highs=[101.0, 101.0, 121.0, 101.0],
        lows=[99.0, 99.0, 89.0, 99.0],
    )
    # Known at the 09:10 close; entry is the gapped 09:10 bar-open row (index 2).
    item = candidate(pd.Timestamp("2026-02-03 09:10", tz="Europe/Berlin"))
    result = evaluate_candidates(
        Frame.from_dataframe(frame),
        [item],
        cost=CostScenario("ZERO", slippage_pts=0.0),
        sizing=SizingSpec(min_risk_pts=1.0, lot_step=0.01, min_lot=0.01),
    )

    trade = result.trades.iloc[0]
    assert trade["entry_price"] == 110.0
    assert trade["exit_reason"] == "STOP"
    assert trade["strategy_id"] == "demo"
    assert trade["h1_regime"] == REGIME
    assert trade["m15_context"] == CONTEXT
    assert trade["session_phase"] == "OPEN"
    assert trade["r_multiple"] == pytest.approx(-1.0)
    assert trade["leverage"] > 0


def test_grouped_metrics_counts_zero_days_and_marks_small_samples() -> None:
    trades = pd.DataFrame(
        [
            {
                "strategy_id": "a",
                "h1_regime": REGIME,
                "m15_context": CONTEXT,
                "date": np.datetime64("2026-02-03"),
                "r_multiple": 1.0,
                "pnl_eur": 50.0,
            },
            {
                "strategy_id": "a",
                "h1_regime": REGIME,
                "m15_context": CONTEXT,
                "date": np.datetime64("2026-02-03"),
                "r_multiple": -1.0,
                "pnl_eur": -50.0,
            },
        ]
    )
    days = np.array([np.datetime64("2026-02-03"), np.datetime64("2026-02-04")])

    metrics = grouped_metrics(trades, trading_days=days, minimum_trades=3)
    row = metrics[
        (metrics["regime_dimension"] == "DIRECTION")
        & (metrics["context"] == "BREAKOUT_SETUP")
    ].iloc[0]

    assert row["n_trades"] == 2
    assert row["n_trading_days"] == 2
    assert row["trades_per_day"] == 1.0
    assert row["median_trades_per_day"] == 1.0
    assert row["zero_trade_days"] == 1
    assert bool(row["insufficient_sample"])
    assert row["expectancy_r"] == 0.0


def test_conflict_recorder_reports_direction_relationships_and_open_overlap() -> None:
    stamp = pd.Timestamp("2026-02-03 09:05", tz="Europe/Berlin")
    candidates = [
        candidate(stamp, strategy_id="a"),
        candidate(stamp, strategy_id="b"),
        candidate(stamp, strategy_id="c", direction=-1, signal_price=100.0, stop=110.0),
    ]
    trades = pd.DataFrame(
        [
            {"strategy_id": "a", "entry_ts": stamp, "exit_ts": stamp + pd.Timedelta("15min")},
            {
                "strategy_id": "b",
                "entry_ts": stamp + pd.Timedelta("5min"),
                "exit_ts": stamp + pd.Timedelta("20min"),
            },
        ]
    )

    records = record_conflicts(candidates, trades)

    assert any(record.kind == "SAME_DIRECTION_AGREEMENT" for record in records)
    assert any(record.kind == "OPPOSITE_DIRECTION_CONFLICT" for record in records)
    assert any(record.kind == "OVERLAPPING_OPEN_TRADES" for record in records)
