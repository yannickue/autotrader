from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from data.models import MarketSnapshot
from signals.models import Direction, Signal
from strategies._shared import bounded_confidence, build_signal, validate_snapshots


@dataclass(frozen=True, slots=True, kw_only=True)
class MomentumConfig:
    strategy_id: str = "momentum-v1"
    lookback: int = 3
    return_threshold: Decimal = Decimal("0.02")
    invalidation_fraction: Decimal = Decimal("0.01")
    expected_horizon: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise ValueError("lookback must be at least 2")
        if self.return_threshold <= 0:
            raise ValueError("return_threshold must be positive")
        if not Decimal("0") < self.invalidation_fraction < Decimal("1"):
            raise ValueError("invalidation_fraction must be between 0 and 1")
        if self.expected_horizon <= timedelta(0):
            raise ValueError("expected_horizon must be positive")


class MomentumStrategy:
    def __init__(self, config: MomentumConfig) -> None:
        self.config = config

    def evaluate(self, snapshots: Sequence[MarketSnapshot]) -> Signal | None:
        validate_snapshots(snapshots)
        if len(snapshots) < self.config.lookback:
            return None
        window = snapshots[-self.config.lookback :]
        start = window[0].last
        latest = window[-1]
        if start == 0:
            return None
        lookback_return = (latest.last - start) / start
        if lookback_return >= self.config.return_threshold:
            direction = Direction.LONG
            invalidation = latest.last * (Decimal("1") - self.config.invalidation_fraction)
        elif lookback_return <= -self.config.return_threshold:
            direction = Direction.SHORT
            invalidation = latest.last * (Decimal("1") + self.config.invalidation_fraction)
        else:
            return None
        strength = abs(lookback_return)
        return build_signal(
            snapshot=latest,
            strategy_id=self.config.strategy_id,
            family="momentum",
            direction=direction,
            invalidation_level=invalidation,
            expected_move=strength,
            expected_horizon=self.config.expected_horizon,
            confidence=bounded_confidence(strength, self.config.return_threshold),
            features={
                "lookback": self.config.lookback,
                "lookback_return": str(lookback_return),
                "return_threshold": str(self.config.return_threshold),
            },
        )
