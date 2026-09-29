"""H1 trend / M15 pullback / M5 resumption candidate strategy."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from alpha.common.protocol import stable_hash
from alpha.signals.candidate import SignalCandidate
from alpha.timeframe import MtfState

from .._common import FrameCandidateStrategy


@dataclass(frozen=True)
class TrendPullbackParams:
    resumption_lookback: int = 3
    structure_lookback: int = 5
    stop_buffer_atr: float = 0.20
    target_r: float = 2.0

    def fingerprint(self) -> str:
        return stable_hash(asdict(self))


VARIANTS = (
    TrendPullbackParams(),
    TrendPullbackParams(resumption_lookback=4, structure_lookback=6),
)


class TrendPullbackStrategy(FrameCandidateStrategy):
    """Hypothesis: an M5 resumption after an M15 pullback persists in a strong H1 trend."""

    strategy_id = "GROUP_A_TREND_PULLBACK"
    strategy_version = "1.0.0"

    def __init__(self, frame: pd.DataFrame, params: TrendPullbackParams = VARIANTS[0]) -> None:
        self.config = params
        super().__init__(frame, params)

    def regime_eligible(self, state: MtfState) -> bool:
        regime, context = self._labels(state)
        return (
            regime["DIRECTION"] in {"UP", "DOWN"}
            and regime["TREND_STRENGTH"] == "TRENDING"
            and context["PULLBACK"]
        )

    def setup_condition(self, state: MtfState) -> bool:
        position = self._position(state)
        needed = max(self.config.resumption_lookback + 1, self.config.structure_lookback)
        if position < needed or pd.isna(self._atr.iloc[position]):
            return False
        closes = self._view.m5["close"].iloc[position - self.config.structure_lookback : position]
        return bool((closes.diff().dropna() != 0).any())

    def trigger(self, state: MtfState, signal_ts: pd.Timestamp) -> SignalCandidate | None:
        position = self._position(state)
        bars = self._view.m5
        window = bars.iloc[position - self.config.resumption_lookback : position]
        structure = bars.iloc[position - self.config.structure_lookback : position + 1]
        regime, _ = self._labels(state)
        close = float(state.m5["close"])
        open_ = float(state.m5["open"])
        atr = float(self._atr.iloc[position])
        if regime["DIRECTION"] == "UP":
            pulled_back = bool((window["close"].diff().dropna() < 0).any())
            if not (pulled_back and close > float(window["high"].max()) and close > open_):
                return None
            stop = float(structure["low"].min()) - self.config.stop_buffer_atr * atr
            direction = 1
        else:
            pulled_back = bool((window["close"].diff().dropna() > 0).any())
            if not (pulled_back and close < float(window["low"].min()) and close < open_):
                return None
            stop = float(structure["high"].max()) + self.config.stop_buffer_atr * atr
            direction = -1
        return self._candidate(
            state,
            signal_ts,
            direction=direction,
            stop=stop,
            target_r=self.config.target_r,
            metadata={
                "pattern": "pullback_resumption",
                "structure_bars": self.config.structure_lookback,
            },
        )


__all__ = ["VARIANTS", "TrendPullbackParams", "TrendPullbackStrategy"]
