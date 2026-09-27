"""Immutable market-data messages shared across event-driven components."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
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
        if not self.instrument.strip():
            raise ValueError("instrument cannot be empty")
        if not self.source.strip():
            raise ValueError("source cannot be empty")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be UTC")
        for field_name in ("bid", "ask", "last", "volume"):
            value = getattr(self, field_name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")
        for field_name in ("volatility", "liquidity"):
            value = getattr(self, field_name)
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError(f"{field_name} must be finite and non-negative")
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")

    @property
    def spread(self) -> Decimal:
        """Top-of-book spread derived without duplicating boundary state."""
        return self.ask - self.bid

