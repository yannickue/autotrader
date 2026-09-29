"""Strategy-candidate contract and deterministic causal runner."""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable

import pandas as pd

from alpha.common.protocol import stable_hash
from alpha.common.sim import ExitSpec
from alpha.regime import REGIME_DIMENSIONS
from alpha.timeframe import MtfState, MtfView

ENTRY_INTENT = "NEXT_BAR_OPEN_MARKET"


@dataclass(frozen=True)
class SignalCandidate:
    """A causal, unselected setup emitted at an M5 close."""

    strategy_id: str
    strategy_version: str
    instrument: str
    direction: int
    signal_ts: pd.Timestamp
    signal_price: float
    entry_intent: str
    stop: float
    target: float | None
    exit_spec: ExitSpec
    h1_regime: dict[str, str]
    m15_context: dict[str, bool]
    session_phase: str
    setup_metadata: dict[str, object]
    param_fingerprint: str

    def __post_init__(self) -> None:
        if self.direction not in {-1, 1}:
            raise ValueError("direction must be +1 or -1")
        stamp = pd.Timestamp(self.signal_ts)
        if stamp.tzinfo is None:
            raise ValueError("signal_ts must be timezone-aware")
        if self.entry_intent != ENTRY_INTENT:
            raise ValueError(f"entry_intent must be {ENTRY_INTENT}")
        prices = [self.signal_price, self.stop]
        if self.target is not None:
            prices.append(self.target)
        if not all(math.isfinite(float(value)) for value in prices):
            raise ValueError("signal, stop, and target prices must be finite")
        if self.direction > 0 and self.stop >= self.signal_price:
            raise ValueError("long stop must be below signal price")
        if self.direction < 0 and self.stop <= self.signal_price:
            raise ValueError("short stop must be above signal price")
        if self.target is not None:
            if self.direction > 0 and self.target <= self.signal_price:
                raise ValueError("long target must be above signal price")
            if self.direction < 0 and self.target >= self.signal_price:
                raise ValueError("short target must be below signal price")
        if set(self.h1_regime) != set(REGIME_DIMENSIONS):
            raise ValueError(f"h1_regime must contain exactly {REGIME_DIMENSIONS}")
        try:
            json.dumps(asdict(self), sort_keys=True, default=_reject_non_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("candidate fields must be JSON-serializable") from exc


def _reject_non_json(value: object) -> object:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, ExitSpec):
        return asdict(value)
    raise TypeError(type(value).__name__)


@runtime_checkable
class CandidateStrategy(Protocol):
    strategy_id: str
    strategy_version: str
    params: Mapping[str, object]
    param_fingerprint: str

    def reset(self) -> None: ...

    def regime_eligible(self, state: MtfState) -> bool: ...

    def setup_condition(self, state: MtfState) -> bool: ...

    def trigger(self, state: MtfState, signal_ts: pd.Timestamp) -> SignalCandidate | None: ...


class CandidateStrategyBase(ABC):
    """Base that owns immutable run parameters and their stable fingerprint."""

    strategy_id: str
    strategy_version: str

    def __init__(self, params: Mapping[str, object]) -> None:
        if not getattr(self, "strategy_id", "") or not getattr(self, "strategy_version", ""):
            raise ValueError("strategy_id and strategy_version must be declared")
        self.params = dict(params)
        self.param_fingerprint = stable_hash(self.params)

    @abstractmethod
    def reset(self) -> None:
        """Clear all per-run state."""

    @abstractmethod
    def regime_eligible(self, state: MtfState) -> bool:
        """Return whether the higher-timeframe regime permits evaluation."""

    @abstractmethod
    def setup_condition(self, state: MtfState) -> bool:
        """Return whether the setup exists."""

    @abstractmethod
    def trigger(self, state: MtfState, signal_ts: pd.Timestamp) -> SignalCandidate | None:
        """Return a triggered candidate, or None."""


def generate_candidates(
    strategy: CandidateStrategy, view_or_frame: MtfView | pd.DataFrame
) -> list[SignalCandidate]:
    """Run REGIME -> SETUP -> TRIGGER in order over causal MTF states."""

    view = view_or_frame if isinstance(view_or_frame, MtfView) else MtfView(view_or_frame)
    strategy.reset()
    candidates: list[SignalCandidate] = []
    for position, bar_open in enumerate(view.m5.index):
        state = view.at(position)
        if not strategy.regime_eligible(state):
            continue
        if not strategy.setup_condition(state):
            continue
        signal_ts = bar_open + pd.Timedelta(minutes=5)
        item = strategy.trigger(state, signal_ts)
        if item is None:
            continue
        if item.signal_ts != signal_ts:
            raise ValueError("candidate signal_ts must equal the current M5 bar-close time")
        if item.strategy_id != strategy.strategy_id:
            raise ValueError("candidate strategy_id differs from its strategy")
        if item.strategy_version != strategy.strategy_version:
            raise ValueError("candidate strategy_version differs from its strategy")
        if item.param_fingerprint != strategy.param_fingerprint:
            raise ValueError("candidate param_fingerprint differs from its strategy")
        candidates.append(item)
    return candidates


def assert_truncation_invariant(
    strategy_factory: Callable[[pd.DataFrame], CandidateStrategy],
    frame: pd.DataFrame,
    cutoff: int,
) -> None:
    """Assert candidates known by cutoff are unchanged when later OHLC data changes."""

    if cutoff < 0 or cutoff >= len(frame):
        raise IndexError(cutoff)
    altered = frame.copy(deep=True)
    future = altered.index > altered.index[cutoff]
    perturbations = (
        ("open", 10_000.0),
        ("high", 10_001.0),
        ("low", 9_999.0),
        ("close", 10_000.0),
    )
    for column, offset in perturbations:
        altered.loc[future, column] = altered.loc[future, column].astype(float) + offset
    cutoff_ts = pd.Timestamp(frame.iloc[cutoff]["ts"]) + pd.Timedelta(minutes=5)
    original = [
        item
        for item in generate_candidates(strategy_factory(frame), frame)
        if item.signal_ts <= cutoff_ts
    ]
    changed = [
        item
        for item in generate_candidates(strategy_factory(altered), altered)
        if item.signal_ts <= cutoff_ts
    ]
    if original != changed:
        raise AssertionError("candidate truncation invariance violated")
