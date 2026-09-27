"""Signals emitted by strategies without any venue-order semantics."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True, slots=True, kw_only=True)
class Signal:
    """Immutable strategy opinion consumed by the risk engine."""

    signal_id: str
    instrument: str
    direction: Direction
    timestamp: datetime
    strategy_id: str
    entry_zone: tuple[Decimal, Decimal]
    invalidation_level: Decimal
    expected_move: Decimal
    expected_horizon: timedelta
    confidence: Decimal
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")

