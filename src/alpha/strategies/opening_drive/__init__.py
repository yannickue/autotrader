"""Berlin cash-open drive continuation candidate strategy."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from alpha.common.dataset import BERLIN
from alpha.common.protocol import stable_hash
from alpha.signals.candidate import SignalCandidate
from alpha.timeframe import MtfState

from .._common import FrameCandidateStrategy


@dataclass(frozen=True)
class OpeningDriveParams:
    minimum_impulse_atr: float = 3.0
    pullback_bars: int = 3
    max_pullback_fraction: float = 0.35
    stop_buffer_atr: float = 0.20
    target_r: float = 2.0

    def fingerprint(self) -> str:
        return stable_hash(asdict(self))


VARIANTS = (
    OpeningDriveParams(),
    OpeningDriveParams(minimum_impulse_atr=4.0, max_pullback_fraction=0.30),
)


class OpeningDriveStrategy(FrameCandidateStrategy):
    """Hypothesis: a large first-hour cash-open impulse continues after a shallow pullback."""

    strategy_id = "GROUP_A_OPENING_DRIVE"
    strategy_version = "1.0.0"

    def __init__(self, frame: pd.DataFrame, params: OpeningDriveParams = VARIANTS[0]) -> None:
        self.config = params
        super().__init__(frame, params)
        local = self._view.m5.index.tz_convert(BERLIN)
        self._local_minutes = np.asarray(local.hour * 60 + local.minute, dtype=np.int64)
        day = np.asarray(local.year * 10_000 + local.month * 100 + local.day, dtype=np.int64)
        # timestamps are strictly increasing, so each Berlin day is one contiguous block
        self._day_start = np.searchsorted(day, day, side="left")

    def _opening_range(self, state: MtfState) -> pd.DataFrame:
        # Bars of the current Berlin day known at this close (positions <= current) inside the
        # 09:00-10:00 window. Berlin-local day/minute are elementwise functions of each timestamp,
        # so they are precomputed once; the day's first position bounds the (causal) scan.
        position = self._position(state)
        start = int(self._day_start[position])
        minutes = self._local_minutes[start : position + 1]
        keep = np.flatnonzero((minutes >= 9 * 60) & (minutes < 10 * 60)) + start
        return self._view.m5.iloc[keep]

    def regime_eligible(self, state: MtfState) -> bool:
        if state.levels.phase not in {"EUROPEAN_OPEN", "MORNING"}:
            return False
        opening = self._opening_range(state)
        if len(opening) != 12:
            return False
        drive = float(opening["close"].iloc[-1] - opening["open"].iloc[0])
        direction = "UP" if drive > 0 else "DOWN"
        regime, context = self._labels(state)
        return (
            regime["DIRECTION"] == direction
            and regime["TREND_STRENGTH"] in {"TRENDING", "WEAK"}
            and (context["PULLBACK"] or context["TREND_CONTINUATION"])
        )

    def setup_condition(self, state: MtfState) -> bool:
        position = self._position(state)
        if position < self.config.pullback_bars or pd.isna(self._atr.iloc[position]):
            return False
        opening = self._opening_range(state)
        drive = float(opening["close"].iloc[-1] - opening["open"].iloc[0])
        atr = float(self._atr.iloc[position])
        if abs(drive) < self.config.minimum_impulse_atr * atr:
            return False
        pullback = self._view.m5.iloc[position - self.config.pullback_bars : position]
        if drive > 0:
            depth = float(opening["high"].max() - pullback["low"].min())
        else:
            depth = float(pullback["high"].max() - opening["low"].min())
        return bool(0 < depth <= self.config.max_pullback_fraction * abs(drive))

    def trigger(self, state: MtfState, signal_ts: pd.Timestamp) -> SignalCandidate | None:
        position = self._position(state)
        opening = self._opening_range(state)
        pullback = self._view.m5.iloc[position - self.config.pullback_bars : position]
        drive = float(opening["close"].iloc[-1] - opening["open"].iloc[0])
        close = float(state.m5["close"])
        open_ = float(state.m5["open"])
        atr = float(self._atr.iloc[position])
        if drive > 0:
            if not (close > float(pullback["high"].max()) and close > open_):
                return None
            direction = 1
            stop = float(pullback["low"].min()) - self.config.stop_buffer_atr * atr
        else:
            if not (close < float(pullback["low"].min()) and close < open_):
                return None
            direction = -1
            stop = float(pullback["high"].max()) + self.config.stop_buffer_atr * atr
        return self._candidate(
            state,
            signal_ts,
            direction=direction,
            stop=stop,
            target_r=self.config.target_r,
            metadata={
                "pattern": "opening_drive_pullback",
                "opening_range_high": float(opening["high"].max()),
                "opening_range_low": float(opening["low"].min()),
            },
        )


__all__ = ["VARIANTS", "OpeningDriveParams", "OpeningDriveStrategy"]
