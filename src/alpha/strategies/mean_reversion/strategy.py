from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from alpha.strategies._shared import GroupCStrategyBase


@dataclass(frozen=True)
class RangeMeanReversionParams:
    extreme_fraction: float = 0.20
    stop_buffer: float = 0.10
    target_r: float = 1.5


VARIANTS = (
    RangeMeanReversionParams(),
    RangeMeanReversionParams(extreme_fraction=0.15),
    RangeMeanReversionParams(stop_buffer=0.15),
)


class RangeMeanReversionStrategy(GroupCStrategyBase):
    """Hypothesis: RANGE_LIKE H1 + RANGE_EXTREME M15 rejects toward range mid."""

    strategy_id = "RANGE_MEAN_REVERSION"
    strategy_version = "1.0"

    def __init__(self, frame: pd.DataFrame, params: RangeMeanReversionParams = VARIANTS[0]) -> None:
        super().__init__(frame, params)

    def reset(self) -> None:
        self._setup: tuple[int, float, float] | None = None

    def regime_eligible(self, state) -> bool:
        regime, context = self._regime_at(state), self._context_at(state)
        return regime["TREND_STRENGTH"] == "RANGE_LIKE" and context["RANGE_EXTREME"]

    def setup_condition(self, state) -> bool:
        if state.m15 is None:
            return False
        row = self._context.loc[self._stamp(state)]
        low, high = float(row["range_low"]), float(row["range_high"])
        if not low < high:
            return False
        close = float(state.m5["close"])
        width = high - low
        fraction = float(self.params["extreme_fraction"])
        direction = (
            1 if close <= low + fraction * width else -1 if close >= high - fraction * width else 0
        )
        self._setup = (direction, low, high) if direction else None
        return self._setup is not None

    def trigger(self, state, signal_ts: pd.Timestamp):
        assert self._setup is not None
        direction, low, high = self._setup
        bar = state.m5
        close, open_ = float(bar["close"]), float(bar["open"])
        rejected = (
            direction > 0 and float(bar["low"]) <= low and close > low and close > open_
        ) or (direction < 0 and float(bar["high"]) >= high and close < high and close < open_)
        if not rejected:
            return None
        width, mid = high - low, (high + low) / 2.0
        buffer = float(self.params["stop_buffer"]) * width
        stop = low - buffer if direction > 0 else high + buffer
        risk = (close - stop) if direction > 0 else (stop - close)
        target = close + direction * float(self.params["target_r"]) * risk
        if (direction > 0 and target > mid) or (direction < 0 and target < mid):
            return None
        return self._candidate(
            state,
            signal_ts,
            direction=direction,
            stop=stop,
            target=target,
            metadata={"range_low": low, "range_high": high, "range_mid": mid},
        )
