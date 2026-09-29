from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from alpha.strategies._shared import GroupCStrategyBase


@dataclass(frozen=True)
class PreviousDayLevelsParams:
    tolerance_atr: float = 0.15
    stop_atr: float = 0.25
    target_r: float = 1.5


VARIANTS = (PreviousDayLevelsParams(), PreviousDayLevelsParams(tolerance_atr=0.25))


class PreviousDayLevelsStrategy(GroupCStrategyBase):
    """Hypothesis: RANGE_EXTREME/REVERSAL_CONTEXT rejects known PDH or PDL."""

    strategy_id = "PREVIOUS_DAY_LEVELS"
    strategy_version = "1.0"

    def __init__(self, frame: pd.DataFrame, params: PreviousDayLevelsParams = VARIANTS[0]) -> None:
        super().__init__(frame, params)

    def reset(self) -> None:
        self._setup: tuple[int, float] | None = None

    def regime_eligible(self, state) -> bool:
        regime, context = self._regime_at(state), self._context_at(state)
        return regime["TREND_STRENGTH"] in {"RANGE_LIKE", "WEAK"} and (
            context["RANGE_EXTREME"] or context["REVERSAL_CONTEXT"]
        )

    def setup_condition(self, state) -> bool:
        pdh, pdl = state.levels.previous_day_high, state.levels.previous_day_low
        if pdh is None or pdl is None:
            return False
        bar, tolerance = state.m5, float(self.params["tolerance_atr"]) * self._range(state)
        if float(bar["high"]) >= pdh - tolerance:
            self._setup = (-1, float(pdh))
        elif float(bar["low"]) <= pdl + tolerance:
            self._setup = (1, float(pdl))
        else:
            self._setup = None
        return self._setup is not None

    def trigger(self, state, signal_ts: pd.Timestamp):
        assert self._setup is not None
        direction, level = self._setup
        bar = state.m5
        close, open_ = float(bar["close"]), float(bar["open"])
        rejected = (
            direction < 0 and float(bar["high"]) >= level and close < level and close < open_
        ) or (direction > 0 and float(bar["low"]) <= level and close > level and close > open_)
        if not rejected:
            return None
        buffer = float(self.params["stop_atr"]) * self._range(state)
        stop = (
            max(float(bar["high"]), level + buffer)
            if direction < 0
            else min(float(bar["low"]), level - buffer)
        )
        risk = abs(close - stop)
        target = close + direction * float(self.params["target_r"]) * risk
        return self._candidate(
            state,
            signal_ts,
            direction=direction,
            stop=stop,
            target=target,
            metadata={"level_name": "PDH" if direction < 0 else "PDL", "level": level},
        )
