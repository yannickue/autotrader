from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from data.models import MarketSnapshot
from signals.models import Direction, Signal
from strategies._shared import bounded_confidence, build_signal, validate_snapshots


@dataclass(frozen=True, slots=True, kw_only=True)
class PullbackConfig:
    strategy_id: str = "pullback-v1"
    lookback: int = 3
    trend_threshold: Decimal = Decimal("0.05")
    pullback_threshold: Decimal = Decimal("0.03")
    expected_horizon: timedelta = timedelta(minutes=30)

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise ValueError("lookback must be at least 2")
        if self.trend_threshold <= 0:
            raise ValueError("trend_threshold must be positive")
        if self.pullback_threshold <= 0:
            raise ValueError("pullback_threshold must be positive")
        if self.expected_horizon <= timedelta(0):
            raise ValueError("expected_horizon must be positive")


class PullbackStrategy:
    def __init__(self, config: PullbackConfig) -> None:
        self.config = config

    def evaluate(self, snapshots: Sequence[MarketSnapshot]) -> Signal | None:
        validate_snapshots(snapshots)
        if len(snapshots) < self.config.lookback + 1:
            return None
        history = snapshots[-(self.config.lookback + 1) : -1]
        latest = snapshots[-1]
        origin = history[0].last
        if origin == 0:
            return None
        peak = max(snapshot.last for snapshot in history)
        trough = min(snapshot.last for snapshot in history)
        upward_trend = (peak - origin) / origin
        downward_trend = (origin - trough) / origin
        long_retracement = (peak - latest.last) / peak if peak else Decimal("0")
        short_retracement = (latest.last - trough) / trough if trough else Decimal("0")
        if (
            upward_trend >= self.config.trend_threshold
            and long_retracement >= self.config.pullback_threshold
            and latest.last > origin
        ):
            direction = Direction.LONG
            trend_strength = upward_trend
            retracement = long_retracement
        elif (
            downward_trend >= self.config.trend_threshold
            and short_retracement >= self.config.pullback_threshold
            and latest.last < origin
        ):
            direction = Direction.SHORT
            trend_strength = downward_trend
            retracement = short_retracement
        else:
            return None
        return build_signal(
            snapshot=latest,
            strategy_id=self.config.strategy_id,
            family="pullback",
            direction=direction,
            invalidation_level=origin,
            expected_move=trend_strength,
            expected_horizon=self.config.expected_horizon,
            confidence=bounded_confidence(retracement, self.config.pullback_threshold),
            features={
                "lookback": self.config.lookback,
                "trend_strength": str(trend_strength),
                "retracement": str(retracement),
                "trend_threshold": str(self.config.trend_threshold),
                "pullback_threshold": str(self.config.pullback_threshold),
            },
        )
