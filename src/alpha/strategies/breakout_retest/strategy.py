from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from alpha.strategies._group_b import GroupBStrategyBase

OR_START_MIN = 9 * 60
OR_END_MIN = 9 * 60 + 30
ENTRY_END_MIN = 16 * 60 + 30


@dataclass(frozen=True)
class BreakoutRetestParams:
    breakout_atr: float = 0.10
    retest_tolerance_atr: float = 0.10
    stop_atr: float = 0.25
    max_wait_bars: int = 12
    target_r: float = 2.0


VARIANTS = (
    BreakoutRetestParams(),
    BreakoutRetestParams(breakout_atr=0.25),
    BreakoutRetestParams(target_r=1.5),
)


class BreakoutRetestStrategy(GroupBStrategyBase):
    """Hypothesis: outside range-like H1 regimes an opening-range breakout that holds its retest
    continues. The opening range is built only from bars that already closed."""

    strategy_id = "OPENING_RANGE_BREAKOUT_RETEST"
    strategy_version = "1.0"

    def __init__(self, frame: pd.DataFrame, params: BreakoutRetestParams = VARIANTS[0]) -> None:
        super().__init__(frame, params)

    def reset(self) -> None:
        self._day = None
        self._or_high: float | None = None
        self._or_low: float | None = None
        self._or_done = False
        self._broke: tuple[int, float, int] | None = None  # direction, level, bars waited
        self._traded = False
        self._setup: tuple[int, float] | None = None

    def regime_eligible(self, state) -> bool:
        stamp = self._stamp(state).tz_convert("Europe/Berlin")
        minute = stamp.hour * 60 + stamp.minute
        if stamp.date() != self._day:
            self._day = stamp.date()
            self._or_high = self._or_low = None
            self._or_done, self._broke, self._traded, self._setup = False, None, False, None
        bar = state.m5
        if OR_START_MIN <= minute < OR_END_MIN:
            high, low = float(bar["high"]), float(bar["low"])
            self._or_high = high if self._or_high is None else max(self._or_high, high)
            self._or_low = low if self._or_low is None else min(self._or_low, low)
            self._or_done = minute == OR_END_MIN - 5
            return False
        if not self._or_done or minute >= ENTRY_END_MIN or self._traded:
            return False
        return self._regime_at(state)["TREND_STRENGTH"] != "RANGE_LIKE"

    def setup_condition(self, state) -> bool:
        assert self._or_high is not None and self._or_low is not None
        bar, rng = state.m5, self._range(state)
        close, high, low = float(bar["close"]), float(bar["high"]), float(bar["low"])
        buffer = float(self.params["breakout_atr"]) * rng
        tolerance = float(self.params["retest_tolerance_atr"]) * rng
        self._setup = None
        if self._broke is None:
            if close > self._or_high + buffer:
                self._broke = (1, float(self._or_high), 0)
            elif close < self._or_low - buffer:
                self._broke = (-1, float(self._or_low), 0)
            return False
        direction, level, waited = self._broke
        waited += 1
        mid = (self._or_high + self._or_low) / 2.0
        lost = close < mid if direction > 0 else close > mid
        if waited > int(self.params["max_wait_bars"]) or lost:
            self._broke = None
            return False
        self._broke = (direction, level, waited)
        retested = (
            direction > 0 and low <= level + tolerance and close > level
        ) or (direction < 0 and high >= level - tolerance and close < level)
        if retested:
            self._setup = (direction, level)
        return self._setup is not None

    def trigger(self, state, signal_ts: pd.Timestamp):
        assert self._setup is not None
        direction, level = self._setup
        bar, buffer = state.m5, float(self.params["stop_atr"]) * self._range(state)
        close = float(bar["close"])
        stop = (
            min(float(bar["low"]), level - buffer)
            if direction > 0
            else max(float(bar["high"]), level + buffer)
        )
        risk = abs(close - stop)
        if risk <= 0:
            return None
        self._traded = True
        target = close + direction * float(self.params["target_r"]) * risk
        return self._candidate(
            state,
            signal_ts,
            direction=direction,
            stop=stop,
            target=target,
            metadata={
                "or_high": float(self._or_high),
                "or_low": float(self._or_low),
                "retested_level": level,
            },
        )
