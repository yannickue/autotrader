"""Shared causal plumbing for candidate-only strategy families."""

from __future__ import annotations

from dataclasses import asdict

import pandas as pd

from alpha.common.sim import ExitSpec
from alpha.context import CONTEXT_LABELS
from alpha.regime import REGIME_DIMENSIONS
from alpha.signals.candidate import ENTRY_INTENT, CandidateStrategyBase, SignalCandidate
from alpha.strategies._labels import LabelLookup, frame_labels, stamp_positions
from alpha.timeframe import MtfState


class FrameCandidateStrategy(CandidateStrategyBase):
    """Base for strategies whose indicators are bound to one immutable input frame."""

    def __init__(self, frame: pd.DataFrame, params: object) -> None:
        super().__init__(asdict(params))  # type: ignore[arg-type]
        self._view, self._regime, self._context = frame_labels(frame)
        bars = self._view.m5
        previous_close = bars["close"].shift(1)
        true_range = pd.concat(
            [
                bars["high"] - bars["low"],
                (bars["high"] - previous_close).abs(),
                (bars["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        self._atr = true_range.rolling(14, min_periods=14).mean()
        self._lookup: LabelLookup | None = None
        self._view_positions: dict[int, int] | None = None

    def reset(self) -> None:
        """Strategies keep no state between causal runs."""

    def begin_run(self) -> None:
        """Snapshot the (possibly test-mutated) labels positionally for one causal run."""
        self._lookup = LabelLookup(self._regime, self._context)
        self._view_positions = stamp_positions(self._view.m5.index)

    def end_run(self) -> None:
        self._lookup = None
        self._view_positions = None

    @staticmethod
    def _stamp(state: MtfState) -> pd.Timestamp:
        return pd.Timestamp(state.m5.name)

    def _position(self, state: MtfState) -> int:
        if self._view_positions is not None:
            return self._view_positions[self._stamp(state).value]
        return int(self._view.m5.index.get_loc(self._stamp(state)))

    def _labels(self, state: MtfState) -> tuple[dict[str, str], dict[str, bool]]:
        stamp = self._stamp(state)
        if self._lookup is not None:
            return self._lookup.regime(stamp), self._lookup.context(stamp)
        regime = {name: str(self._regime.at[stamp, name]) for name in REGIME_DIMENSIONS}
        context = {name: bool(self._context.at[stamp, name]) for name in CONTEXT_LABELS}
        return regime, context

    def _candidate(
        self,
        state: MtfState,
        signal_ts: pd.Timestamp,
        *,
        direction: int,
        stop: float,
        target_r: float,
        metadata: dict[str, object],
    ) -> SignalCandidate:
        regime, context = self._labels(state)
        signal_price = float(state.m5["close"])
        risk = abs(signal_price - stop)
        target = signal_price + direction * target_r * risk
        return SignalCandidate(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            instrument="GER40",
            direction=direction,
            signal_ts=signal_ts,
            signal_price=signal_price,
            entry_intent=ENTRY_INTENT,
            stop=float(stop),
            target=float(target),
            exit_spec=ExitSpec("fixed_r", target_r),
            h1_regime=regime,
            m15_context=context,
            session_phase=state.levels.phase,
            setup_metadata=metadata,
            param_fingerprint=self.param_fingerprint,
        )

