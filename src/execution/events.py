"""Deterministic event types consumed by the paper execution engine.

Only explicit trade/mark events trigger fills or protective-order triggers; quote or
candle touches never do (docs/OPEN_QUESTIONS.md item 20).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeEvent:
    """An executed trade print used to fill resting limit/protective orders."""

    instrument: str
    timestamp: datetime
    trade_id: str
    price: Decimal
    quantity: Decimal
