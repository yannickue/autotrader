"""Immutable market-data messages shared across event-driven components."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class DataQuality(StrEnum):
    """Provenance state used to gate downstream trading decisions."""

    LIVE = "live"
    DELAYED = "delayed"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketSnapshot:
    """Normalized point-in-time market state emitted by a data adapter."""

    instrument: str
    timestamp: datetime
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume: Decimal
    volatility: Decimal | None
    liquidity: Decimal | None
    source: str
    quality: DataQuality
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")

