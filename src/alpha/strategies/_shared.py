"""Shared causal plumbing for Group C candidate strategies."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pandas as pd

from alpha.common.sim import ExitSpec
from alpha.context import CONTEXT_LABELS, classify_context
from alpha.regime import REGIME_DIMENSIONS, classify_regime
from alpha.signals.candidate import ENTRY_INTENT, CandidateStrategyBase, SignalCandidate


class GroupCStrategyBase(CandidateStrategyBase):
    """Precompute causal labels and provide uniform candidate construction."""

    def __init__(self, frame: pd.DataFrame, params: Any) -> None:
        super().__init__(asdict(params))
        self._regime = classify_regime(frame)
        self._context = classify_context(frame)
        self.reset()

    @staticmethod
    def _stamp(state: Any) -> pd.Timestamp:
        return pd.Timestamp(state.m5.name)

    def _regime_at(self, state: Any) -> dict[str, str]:
        row = self._regime.loc[self._stamp(state)]
        return {dimension: str(row[dimension]) for dimension in REGIME_DIMENSIONS}

    def _context_at(self, state: Any) -> dict[str, bool]:
        row = self._context.loc[self._stamp(state)]
        return {label: bool(row[label]) for label in CONTEXT_LABELS}

    def _candidate(
        self,
        state: Any,
        signal_ts: pd.Timestamp,
        *,
        direction: int,
        stop: float,
        target: float,
        metadata: dict[str, object],
    ) -> SignalCandidate:
        return SignalCandidate(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            instrument="GER40",
            direction=direction,
            signal_ts=signal_ts,
            signal_price=float(state.m5["close"]),
            entry_intent=ENTRY_INTENT,
            stop=float(stop),
            target=float(target),
            exit_spec=ExitSpec("fixed_r", float(self.params["target_r"])),
            h1_regime=self._regime_at(state),
            m15_context=self._context_at(state),
            session_phase=state.levels.phase,
            setup_metadata=metadata,
            param_fingerprint=self.param_fingerprint,
        )

    @staticmethod
    def _range(state: Any) -> float:
        bars = [bar for bar in (state.m15, state.h1) if bar is not None]
        values = [float(bar["high"] - bar["low"]) for bar in bars]
        fallback = float(state.m5["high"] - state.m5["low"])
        return max([*values, fallback, 1e-9])
