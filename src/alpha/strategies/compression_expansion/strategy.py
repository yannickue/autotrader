from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import pandas as pd

from alpha.strategies._group_b import GroupBStrategyBase


@dataclass(frozen=True)
class CompressionExpansionParams:
    box_bars: int = 12
    expansion_mult: float = 1.8
    compression_memory_bars: int = 24
    stop_atr: float = 0.10
    target_r: float = 2.0


VARIANTS = (CompressionExpansionParams(), CompressionExpansionParams(expansion_mult=2.2))


class CompressionExpansionStrategy(GroupBStrategyBase):
    """Hypothesis: after an H1 low-volatility/compression state and recent M15 compression, an
    M5 bar with outsized range that closes beyond the prior box starts an expansion move."""

    strategy_id = "COMPRESSION_EXPANSION"
    strategy_version = "1.0"

    def __init__(
        self, frame: pd.DataFrame, params: CompressionExpansionParams = VARIANTS[0]
    ) -> None:
        super().__init__(frame, params)

    def reset(self) -> None:
        self._bars: deque[tuple[float, float]] = deque(maxlen=int(self.params["box_bars"]))
        self._since_compression: int | None = None
        self._prior: list[tuple[float, float]] = []
        self._setup: tuple[int, float, float] | None = None

    def regime_eligible(self, state) -> bool:
        bar = state.m5
        self._prior = list(self._bars)  # the prior box excludes the current bar
        self._bars.append((float(bar["high"]), float(bar["low"])))
        if self._context_at(state)["COMPRESSION"]:
            self._since_compression = 0
        elif self._since_compression is not None:
            self._since_compression += 1
        regime = self._regime_at(state)
        return regime["VOL_STATE"] == "COMPRESSION" or regime["VOLATILITY"] == "LOW"

    def setup_condition(self, state) -> bool:
        self._setup = None
        if (
            len(self._prior) < int(self.params["box_bars"])
            or self._since_compression is None
            or self._since_compression > int(self.params["compression_memory_bars"])
        ):
            return False
        box_high = max(high for high, _ in self._prior)
        box_low = min(low for _, low in self._prior)
        mean_range = sum(high - low for high, low in self._prior) / len(self._prior)
        bar = state.m5
        close = float(bar["close"])
        if mean_range <= 0 or float(bar["high"] - bar["low"]) < (
            float(self.params["expansion_mult"]) * mean_range
        ):
            return False
        if close > box_high:
            self._setup = (1, box_high, box_low)
        elif close < box_low:
            self._setup = (-1, box_high, box_low)
        return self._setup is not None

    def trigger(self, state, signal_ts: pd.Timestamp):
        assert self._setup is not None
        direction, box_high, box_low = self._setup
        close = float(state.m5["close"])
        buffer = float(self.params["stop_atr"]) * self._range(state)
        stop = box_low - buffer if direction > 0 else box_high + buffer
        risk = abs(close - stop)
        if risk <= 0:
            return None
        target = close + direction * float(self.params["target_r"]) * risk
        return self._candidate(
            state,
            signal_ts,
            direction=direction,
            stop=stop,
            target=target,
            metadata={"box_high": box_high, "box_low": box_low},
        )
