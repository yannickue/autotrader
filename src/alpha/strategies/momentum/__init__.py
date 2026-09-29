"""H1/M15 momentum continuation with an M5 consolidation break."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from alpha.common.protocol import stable_hash
from alpha.signals.candidate import SignalCandidate
from alpha.timeframe import MtfState

from .._common import FrameCandidateStrategy


@dataclass(frozen=True)
class MomentumParams:
    consolidation_bars: int = 4
    max_range_atr: float = 1.50
    impulse_atr: float = 1.0
    stop_buffer_atr: float = 0.20
    target_r: float = 2.0

    def fingerprint(self) -> str:
        return stable_hash(asdict(self))


VARIANTS = (
    MomentumParams(),
    MomentumParams(consolidation_bars=5, max_range_atr=1.75),
)


class MomentumContinuationStrategy(FrameCandidateStrategy):
    """Hypothesis: aligned H1/M15 momentum resumes after a short M5 pause."""

    strategy_id = "GROUP_A_MOMENTUM_CONTINUATION"
    strategy_version = "1.0.0"

    def __init__(self, frame: pd.DataFrame, params: MomentumParams = VARIANTS[0]) -> None:
        self.config = params
        super().__init__(frame, params)

    def regime_eligible(self, state: MtfState) -> bool:
        regime, context = self._labels(state)
        return (
            regime["DIRECTION"] in {"UP", "DOWN"}
            and regime["TREND_STRENGTH"] == "TRENDING"
            and context["MOMENTUM_CONTINUATION"]
        )

    def setup_condition(self, state: MtfState) -> bool:
        position = self._position(state)
        count = self.config.consolidation_bars
        if position <= count + 1 or pd.isna(self._atr.iloc[position]):
            return False
        pause = self._view.m5.iloc[position - count : position]
        atr = float(self._atr.iloc[position])
        compact = float(pause["high"].max() - pause["low"].min()) <= self.config.max_range_atr * atr
        anchor = float(self._view.m5["close"].iloc[position - count - 2])
        first = float(pause["close"].iloc[0])
        regime, _ = self._labels(state)
        impulse = first - anchor
        aligned = (
            impulse >= self.config.impulse_atr * atr
            if regime["DIRECTION"] == "UP"
            else impulse <= -self.config.impulse_atr * atr
        )
        return bool(compact and aligned)

    def trigger(self, state: MtfState, signal_ts: pd.Timestamp) -> SignalCandidate | None:
        position = self._position(state)
        pause = self._view.m5.iloc[position - self.config.consolidation_bars : position]
        regime, _ = self._labels(state)
        close = float(state.m5["close"])
        open_ = float(state.m5["open"])
        atr = float(self._atr.iloc[position])
        if regime["DIRECTION"] == "UP":
            if not (close > float(pause["high"].max()) and close > open_):
                return None
            direction = 1
            stop = float(pause["low"].min()) - self.config.stop_buffer_atr * atr
        else:
            if not (close < float(pause["low"].min()) and close < open_):
                return None
            direction = -1
            stop = float(pause["high"].max()) + self.config.stop_buffer_atr * atr
        return self._candidate(
            state,
            signal_ts,
            direction=direction,
            stop=stop,
            target_r=self.config.target_r,
            metadata={
                "pattern": "brief_consolidation_break",
                "consolidation_bars": self.config.consolidation_bars,
            },
        )


__all__ = ["VARIANTS", "MomentumContinuationStrategy", "MomentumParams"]
