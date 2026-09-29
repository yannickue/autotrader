from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from alpha.strategies._group_b import GroupBStrategyBase


@dataclass(frozen=True)
class FailedBreakoutParams:
    breakout_atr: float = 0.10
    fail_window_bars: int = 6
    stop_atr: float = 0.10
    target_r: float = 2.0


VARIANTS = (FailedBreakoutParams(), FailedBreakoutParams(fail_window_bars=12))


class FailedBreakoutStrategy(GroupBStrategyBase):
    """Hypothesis: in a range-like H1 regime a CLOSE beyond the previous-day high/low that closes
    back inside within a few bars traps breakout traders; fade it.

    Distinct from the session sweep (wick-only, same bar) and the previous-day reaction: the
    breakout must first be accepted by a close, then rejected by a later close."""

    strategy_id = "FAILED_BREAKOUT_REVERSAL"
    strategy_version = "1.0"

    def __init__(self, frame: pd.DataFrame, params: FailedBreakoutParams = VARIANTS[0]) -> None:
        super().__init__(frame, params)

    def reset(self) -> None:
        # direction, level, bars waited, extreme since the breakout
        self._broke: tuple[int, float, int, float] | None = None
        self._setup: tuple[int, float, float] | None = None

    def regime_eligible(self, state) -> bool:
        return self._regime_at(state)["TREND_STRENGTH"] == "RANGE_LIKE"

    def setup_condition(self, state) -> bool:
        self._setup = None
        pdh, pdl = state.levels.previous_day_high, state.levels.previous_day_low
        if pdh is None or pdl is None:
            self._broke = None
            return False
        bar = state.m5
        close, high, low = float(bar["close"]), float(bar["high"]), float(bar["low"])
        buffer = float(self.params["breakout_atr"]) * self._range(state)
        if self._broke is None:
            if close > pdh + buffer:
                self._broke = (1, float(pdh), 0, high)
            elif close < pdl - buffer:
                self._broke = (-1, float(pdl), 0, low)
            return False
        direction, level, waited, extreme = self._broke
        waited += 1
        extreme = max(extreme, high) if direction > 0 else min(extreme, low)
        if direction > 0 and close < level:
            self._setup, self._broke = (-1, level, extreme), None
        elif direction < 0 and close > level:
            self._setup, self._broke = (1, level, extreme), None
        elif waited > int(self.params["fail_window_bars"]):
            self._broke = None
        else:
            self._broke = (direction, level, waited, extreme)
        return self._setup is not None

    def trigger(self, state, signal_ts: pd.Timestamp):
        assert self._setup is not None
        direction, level, extreme = self._setup
        close = float(state.m5["close"])
        buffer = float(self.params["stop_atr"]) * self._range(state)
        stop = extreme + buffer if direction < 0 else extreme - buffer
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
            metadata={"failed_level": level, "breakout_extreme": extreme},
        )
