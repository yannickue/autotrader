"""Pullback/reclaim of an equal-time M5 typical-price session reference.

Every completed five-minute bar receives exactly one equal weight. This is a session TWAP,
not VWAP: ActivTrades CFD real volume is unavailable/zero and tick activity is not used.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from alpha.strategies._shared import GroupCStrategyBase


@dataclass(frozen=True)
class SessionTwapReferenceParams:
    stop_atr: float = 0.75
    target_r: float = 2.0


VARIANTS = (SessionTwapReferenceParams(), SessionTwapReferenceParams(stop_atr=1.0))


class SessionTwapReferenceStrategy(GroupCStrategyBase):
    """Hypothesis: a PULLBACK in a directional TRENDING H1 reclaims session TWAP."""

    strategy_id = "SESSION_TWAP_REFERENCE"
    strategy_version = "1.0"

    def __init__(
        self, frame: pd.DataFrame, params: SessionTwapReferenceParams = VARIANTS[0]
    ) -> None:
        super().__init__(frame, params)

    def reset(self) -> None:
        self._day = None
        self._sum = 0.0
        self._count = 0
        self._last_close = None
        self._prior_close_for_bar = None
        self._previous_reference = None
        self._reference = None
        self._direction = 0

    def regime_eligible(self, state) -> bool:
        stamp = self._stamp(state).tz_convert("Europe/Berlin")
        day = stamp.date()
        if day != self._day:
            self._day, self._sum, self._count = day, 0.0, 0
            self._last_close = self._previous_reference = None
        bar = state.m5
        typical = (float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3.0
        prior_reference = self._sum / self._count if self._count else None
        self._sum += typical
        self._count += 1
        self._reference = self._sum / self._count
        self._previous_reference = prior_reference
        self._prior_close_for_bar = self._last_close
        self._last_close = float(bar["close"])
        regime, context = self._regime_at(state), self._context_at(state)
        direction = 1 if regime["DIRECTION"] == "UP" else -1 if regime["DIRECTION"] == "DOWN" else 0
        self._direction = direction
        return regime["TREND_STRENGTH"] == "TRENDING" and direction != 0 and context["PULLBACK"]

    def setup_condition(self, state) -> bool:
        if self._prior_close_for_bar is None or self._previous_reference is None:
            return False
        close = float(state.m5["close"])
        reclaimed = (
            self._direction > 0
            and self._prior_close_for_bar <= self._previous_reference
            and close > self._reference
        ) or (
            self._direction < 0
            and self._prior_close_for_bar >= self._previous_reference
            and close < self._reference
        )
        return reclaimed

    def trigger(self, state, signal_ts: pd.Timestamp):
        bar, direction = state.m5, self._direction
        close, open_ = float(bar["close"]), float(bar["open"])
        if (direction > 0 and close <= open_) or (direction < 0 and close >= open_):
            return None
        distance = float(self.params["stop_atr"]) * self._range(state)
        stop = (
            min(float(bar["low"]), close - distance)
            if direction > 0
            else max(float(bar["high"]), close + distance)
        )
        risk = abs(close - stop)
        target = close + direction * float(self.params["target_r"]) * risk
        return self._candidate(
            state,
            signal_ts,
            direction=direction,
            stop=stop,
            target=target,
            metadata={
                "session_twap_reference": float(self._reference),
                "weighting_source": "EQUAL_TIME_M5_TYPICAL_PRICE",
            },
        )
