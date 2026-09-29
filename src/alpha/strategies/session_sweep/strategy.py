from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from alpha.strategies._shared import GroupCStrategyBase


@dataclass(frozen=True)
class SessionSweepParams:
    sweep_atr: float = 0.05
    stop_atr: float = 0.10
    target_r: float = 2.0


VARIANTS = (SessionSweepParams(), SessionSweepParams(sweep_atr=0.10))


class SessionSweepStrategy(GroupCStrategyBase):
    """Hypothesis: a weak/range H1 reversal context rewards failed session extensions."""

    strategy_id = "SESSION_SWEEP_REVERSAL"
    strategy_version = "1.0"

    def __init__(self, frame: pd.DataFrame, params: SessionSweepParams = VARIANTS[0]) -> None:
        super().__init__(frame, params)

    def reset(self) -> None:
        self._day = None
        self._prior_high = self._prior_low = None
        self._setup: tuple[int, float] | None = None

    def regime_eligible(self, state) -> bool:
        stamp = self._stamp(state).tz_convert("Europe/Berlin")
        day = stamp.date()
        if day != self._day:
            self._day, self._prior_high, self._prior_low = day, None, None
        current_prior = (self._prior_high, self._prior_low)
        self._prior_high = state.levels.session_high
        self._prior_low = state.levels.session_low
        self._current_prior = current_prior
        regime, context = self._regime_at(state), self._context_at(state)
        return regime["TREND_STRENGTH"] in {"RANGE_LIKE", "WEAK"} and context["REVERSAL_CONTEXT"]

    def setup_condition(self, state) -> bool:
        prior_high, prior_low = self._current_prior
        if prior_high is None or prior_low is None:
            return False
        bar, buffer = state.m5, float(self.params["sweep_atr"]) * self._range(state)
        if float(bar["high"]) > prior_high + buffer and float(bar["close"]) < prior_high:
            self._setup = (-1, float(prior_high))
        elif float(bar["low"]) < prior_low - buffer and float(bar["close"]) > prior_low:
            self._setup = (1, float(prior_low))
        else:
            self._setup = None
        return self._setup is not None

    def trigger(self, state, signal_ts: pd.Timestamp):
        assert self._setup is not None
        direction, swept = self._setup
        close = float(state.m5["close"])
        buffer = float(self.params["stop_atr"]) * self._range(state)
        stop = (
            float(state.m5["high"]) + buffer if direction < 0 else float(state.m5["low"]) - buffer
        )
        risk = abs(close - stop)
        target = close + direction * float(self.params["target_r"]) * risk
        return self._candidate(
            state,
            signal_ts,
            direction=direction,
            stop=stop,
            target=target,
            metadata={"swept_level": swept, "level_source": "SESSION_RUNNING_PRIOR_BAR"},
        )
