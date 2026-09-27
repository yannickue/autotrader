from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import pytest

from data.models import DataQuality, MarketSnapshot
from signals.models import Direction
from strategies import (
    BreakoutConfig,
    BreakoutStrategy,
    MomentumConfig,
    MomentumStrategy,
    PullbackConfig,
    PullbackStrategy,
)


class Strategy(Protocol):
    def evaluate(self, snapshots: list[MarketSnapshot]) -> object: ...


def snapshots(*prices: str) -> tuple[MarketSnapshot, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return tuple(
        MarketSnapshot(
            instrument="BTCUSDT-PERP",
            timestamp=start + timedelta(minutes=index),
            bid=Decimal(price) - Decimal("0.1"),
            ask=Decimal(price) + Decimal("0.1"),
            last=Decimal(price),
            volume=Decimal("100"),
            volatility=Decimal("0.02"),
            liquidity=Decimal("1000000"),
            source="synthetic",
            quality=DataQuality.LIVE,
        )
        for index, price in enumerate(prices)
    )


@pytest.mark.parametrize(
    ("prices", "expected_direction"),
    [
        (("100", "101", "103"), Direction.LONG),
        (("100", "99", "97"), Direction.SHORT),
    ],
)
def test_momentum_emits_directional_signal_with_complete_attribution(
    prices: tuple[str, ...], expected_direction: Direction
) -> None:
    strategy = MomentumStrategy(
        MomentumConfig(
            strategy_id="momentum-v1",
            lookback=3,
            return_threshold=Decimal("0.02"),
            invalidation_fraction=Decimal("0.01"),
            expected_horizon=timedelta(minutes=15),
        )
    )

    signal = strategy.evaluate(snapshots(*prices))

    assert signal is not None
    assert signal.instrument == "BTCUSDT-PERP"
    assert signal.direction is expected_direction
    assert signal.timestamp == datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    assert signal.strategy_id == "momentum-v1"
    assert signal.entry_zone == (
        Decimal(prices[-1]) - Decimal("0.1"),
        Decimal(prices[-1]) + Decimal("0.1"),
    )
    assert signal.expected_horizon == timedelta(minutes=15)
    assert Decimal("0") <= signal.confidence <= Decimal("1")
    assert signal.metadata["family"] == "momentum"
    assert signal.metadata["feature_attribution"]["lookback_return"] in {"0.03", "-0.03"}
    assert strategy.evaluate(snapshots(*prices)).signal_id == signal.signal_id


def test_momentum_returns_none_below_threshold() -> None:
    strategy = MomentumStrategy(
        MomentumConfig(lookback=3, return_threshold=Decimal("0.02"))
    )

    assert strategy.evaluate(snapshots("100", "100.5", "101")) is None


@pytest.mark.parametrize(
    ("prices", "expected_direction", "breakout_level"),
    [
        (("100", "102", "101", "104"), Direction.LONG, "102"),
        (("100", "98", "99", "96"), Direction.SHORT, "98"),
    ],
)
def test_breakout_emits_only_after_buffered_range_break(
    prices: tuple[str, ...], expected_direction: Direction, breakout_level: str
) -> None:
    strategy = BreakoutStrategy(
        BreakoutConfig(
            strategy_id="breakout-v1",
            lookback=3,
            breakout_buffer=Decimal("0.01"),
            invalidation_fraction=Decimal("0.005"),
        )
    )

    signal = strategy.evaluate(snapshots(*prices))

    assert signal is not None
    assert signal.direction is expected_direction
    assert signal.metadata["family"] == "breakout"
    assert signal.metadata["feature_attribution"]["range_boundary"] == breakout_level
    assert signal.expected_move > Decimal("0")


def test_breakout_returns_none_inside_buffered_range() -> None:
    strategy = BreakoutStrategy(BreakoutConfig(lookback=3, breakout_buffer=Decimal("0.01")))

    assert strategy.evaluate(snapshots("100", "102", "101", "102.5")) is None


@pytest.mark.parametrize(
    ("prices", "expected_direction", "invalidation"),
    [
        (("100", "104", "110", "106"), Direction.LONG, Decimal("100")),
        (("100", "96", "90", "94"), Direction.SHORT, Decimal("100")),
    ],
)
def test_pullback_emits_in_trend_direction_after_retracement(
    prices: tuple[str, ...], expected_direction: Direction, invalidation: Decimal
) -> None:
    strategy = PullbackStrategy(
        PullbackConfig(
            strategy_id="pullback-v1",
            lookback=3,
            trend_threshold=Decimal("0.05"),
            pullback_threshold=Decimal("0.03"),
            expected_horizon=timedelta(minutes=30),
        )
    )

    signal = strategy.evaluate(snapshots(*prices))

    assert signal is not None
    assert signal.direction is expected_direction
    assert signal.invalidation_level == invalidation
    assert signal.expected_horizon == timedelta(minutes=30)
    assert signal.metadata["family"] == "pullback"
    assert signal.metadata["feature_attribution"]["retracement"] in {
        "0.03636363636363636363636363636",
        "0.04444444444444444444444444444",
    }


def test_pullback_returns_none_without_sufficient_trend() -> None:
    strategy = PullbackStrategy(
        PullbackConfig(
            lookback=3,
            trend_threshold=Decimal("0.05"),
            pullback_threshold=Decimal("0.03"),
        )
    )

    assert strategy.evaluate(snapshots("100", "102", "103", "99")) is None


@pytest.mark.parametrize(
    "strategy",
    [
        MomentumStrategy(MomentumConfig(lookback=3)),
        BreakoutStrategy(BreakoutConfig(lookback=3)),
        PullbackStrategy(PullbackConfig(lookback=3)),
    ],
)
def test_strategy_rejects_mixed_instruments(strategy: Strategy) -> None:
    series = list(snapshots("100", "101", "103", "104"))
    series[-1] = MarketSnapshot(
        instrument="ETHUSDT-PERP",
        timestamp=series[-1].timestamp,
        bid=series[-1].bid,
        ask=series[-1].ask,
        last=series[-1].last,
        volume=series[-1].volume,
        volatility=series[-1].volatility,
        liquidity=series[-1].liquidity,
        source=series[-1].source,
        quality=series[-1].quality,
    )

    with pytest.raises(ValueError, match="same instrument"):
        strategy.evaluate(series)
