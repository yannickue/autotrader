"""Recorder-facing immutable DEMO execution events."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class Accepted:
    intent_id: str
    quantity: Decimal
    equity: Decimal | None = None
    risk_fraction: Decimal | None = None
    risk_budget: Decimal | None = None
    leverage: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Rejected:
    intent_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Fill:
    intent_id: str
    price: Decimal
    quantity: Decimal
    spread: Decimal
    slippage: Decimal
    commission: Decimal
    swap: Decimal
    broker_order_id: str
    broker_position_id: str


@dataclass(frozen=True, slots=True)
class ProtectionConfirmed:
    intent_id: str
    broker_position_id: str
    stop: Decimal
    target: Decimal | None


@dataclass(frozen=True, slots=True)
class PositionClosed:
    intent_id: str
    broker_position_id: str
    exit_reason: Literal["STOP", "TARGET", "SESSION_END", "MANUAL", "EXTERNAL"]
    exit_price: Decimal | None = None
    exit_quantity: Decimal | None = None
    closed_utc: str | None = None
    commission: Decimal | None = None  # signed broker amount, negative = cost
    swap: Decimal | None = None  # signed broker amount, negative = cost
    profit_eur: Decimal | None = None  # broker profit on closing deal(s), before costs


ExecutionEvent = Accepted | Rejected | Fill | ProtectionConfirmed | PositionClosed
