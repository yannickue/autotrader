from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from data.models import MarketSnapshot
from signals.models import Direction, Signal
from strategies._shared import bounded_confidence, build_signal, validate_snapshots


@dataclass(frozen=True, slots=True, kw_only=True)
class BreakoutConfig:
    strategy_id: str = "breakout-v1"
    lookback: int = 3
    breakout_buffer: Decimal = Decimal("0.01")
    invalidation_fraction: Decimal = Decimal("0.005")
    expected_horizon: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise ValueError("lookback must be at least 2")
        if self.breakout_buffer <= 0:
            raise ValueError("breakout_buffer must be positive")
        if not Decimal("0") < self.invalidation_fraction < Decimal("1"):
            raise ValueError("invalidation_fraction must be between 0 and 1")
        if self.expected_horizon <= timedelta(0):
            raise ValueError("expected_horizon must be positive")


class BreakoutStrategy:
    def __init__(self, config: BreakoutConfig) -> None:
        self.config = config

    def evaluate(self, snapshots: Sequence[MarketSnapshot]) -> Signal | None:
        validate_snapshots(snapshots)
        if len(snapshots) < self.config.lookback + 1:
            return None
        history = snapshots[-(self.config.lookback + 1) : -1]
        latest = snapshots[-1]
        upper = max(snapshot.last for snapshot in history)
        lower = min(snapshot.last for snapshot in history)
        if upper == 0 or lower == 0:
            return None
        upper_trigger = upper * (Decimal("1") + self.config.breakout_buffer)
        lower_trigger = lower * (Decimal("1") - self.config.breakout_buffer)
        if latest.last >= upper_trigger:
            direction = Direction.LONG
            boundary = upper
            strength = (latest.last - upper) / upper
            invalidation = upper * (Decimal("1") - self.config.invalidation_fraction)
        elif latest.last <= lower_trigger:
            direction = Direction.SHORT
            boundary = lower
            strength = (lower - latest.last) / lower
            invalidation = lower * (Decimal("1") + self.config.invalidation_fraction)
        else:
            return None
        return build_signal(
            snapshot=latest,
            strategy_id=self.config.strategy_id,
            family="breakout",
            direction=direction,
            invalidation_level=invalidation,
            expected_move=strength,
            expected_horizon=self.config.expected_horizon,
            confidence=bounded_confidence(strength, self.config.breakout_buffer),
            features={
                "lookback": self.config.lookback,
                "range_boundary": str(boundary),
                "breakout_strength": str(strength),
                "breakout_buffer": str(self.config.breakout_buffer),
            },
        )
