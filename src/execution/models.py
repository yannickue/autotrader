"""Validated requests accepted by the deterministic execution engine."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionRequest:
    """Risk-approved order intent; only the execution engine may consume it."""

    request_id: str
    risk_decision_id: str
    instrument: str
    timestamp: datetime
    side: OrderSide
    quantity: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    reduce_only: bool
    client_order_id: str
    time_in_force: TimeInForce
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price is required for limit orders")
