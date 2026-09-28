"""Canonical data provenance types shared across data sources.

Standalone: no dependency on `src/adapters`, `src/pipeline`, or any live
broker/exchange connection.

Critical correctness rule (directive-mandated): a CFD broker's tick count is
ACTIVITY/FREQUENCY data (how often the broker's price updated), not real
executed exchange volume -- a CFD has no central order book, so
`ACTIVTRADES_MT5_CFD` ticks must always be tagged `BROKER_TICK_ACTIVITY`,
never `REAL_EXCHANGE_VOLUME`. `REAL_EXCHANGE_VOLUME` is reserved for sources
that report actual traded volume from a real order book/exchange (e.g.
`BINANCE_USDM`). `Provenance.__post_init__` hard-validates this: constructing
`Provenance(source=Source.ACTIVTRADES_MT5_CFD,
semantic_type=SemanticType.REAL_EXCHANGE_VOLUME)` raises `ValueError`,
because treating CFD tick activity as real exchange volume would silently
corrupt any downstream liquidity/volume-based analysis.
"""

from dataclasses import dataclass
from enum import StrEnum


class Source(StrEnum):
    """Where a data record originated."""

    ACTIVTRADES_MT5_CFD = "ACTIVTRADES_MT5_CFD"
    BINANCE_USDM = "BINANCE_USDM"
    EXTERNAL_REFERENCE = "EXTERNAL_REFERENCE"
    CME_REFERENCE = "CME_REFERENCE"
    EUREX_REFERENCE = "EUREX_REFERENCE"


class SemanticType(StrEnum):
    """What a data record actually represents.

    `REAL_EXCHANGE_VOLUME` means genuine traded volume reported by a real
    order-book exchange. A CFD broker's tick count is NOT this -- it is
    activity/frequency data (`BROKER_TICK_ACTIVITY`), because a CFD has no
    central order book to report real traded volume from. Never tag a CFD
    broker's tick count as `REAL_EXCHANGE_VOLUME`.
    """

    BROKER_BID_ASK = "BROKER_BID_ASK"
    BROKER_TICK = "BROKER_TICK"
    BROKER_TICK_ACTIVITY = "BROKER_TICK_ACTIVITY"
    OHLC_BAR = "OHLC_BAR"
    REAL_EXCHANGE_VOLUME = "REAL_EXCHANGE_VOLUME"
    ORDER_BOOK = "ORDER_BOOK"
    OPEN_INTEREST = "OPEN_INTEREST"
    FUNDING = "FUNDING"
    REFERENCE_PRICE = "REFERENCE_PRICE"


@dataclass(frozen=True, slots=True, kw_only=True)
class Provenance:
    """Tags one data record with where it came from and what it represents."""

    source: Source
    semantic_type: SemanticType

    def __post_init__(self) -> None:
        if (
            self.source is Source.ACTIVTRADES_MT5_CFD
            and self.semantic_type is SemanticType.REAL_EXCHANGE_VOLUME
        ):
            raise ValueError(
                "ACTIVTRADES_MT5_CFD cannot be tagged REAL_EXCHANGE_VOLUME: a CFD "
                "broker's tick count is activity/frequency data, not real exchange "
                "volume from a central order book -- use BROKER_TICK_ACTIVITY instead"
            )
