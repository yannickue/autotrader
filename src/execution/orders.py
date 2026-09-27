"""Order state and reject/halt codes for the deterministic paper execution engine."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from execution.models import OrderSide, OrderType, TimeInForce

ZERO = Decimal("0")


class OrderStatus(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


TERMINAL_STATUSES = frozenset(
    {OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.FILLED}
)

# Monotonic transition table: from -> allowed next statuses.
_ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.CANCELED}),
    OrderStatus.ACCEPTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}


def is_legal_transition(current: OrderStatus, target: OrderStatus) -> bool:
    if current == target:
        return True
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


class RejectCode(StrEnum):
    NOT_READY = "NOT_READY"
    RISK_NOT_APPROVED = "RISK_NOT_APPROVED"
    RISK_MISMATCH = "RISK_MISMATCH"
    RISK_ALREADY_CONSUMED = "RISK_ALREADY_CONSUMED"
    EXCEEDS_APPROVED_SIZE = "EXCEEDS_APPROVED_SIZE"
    STALE_REQUEST = "STALE_REQUEST"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_TIF = "INVALID_TIF"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    SPREAD_GUARD = "SPREAD_GUARD"
    REDUCE_ONLY_VIOLATION = "REDUCE_ONLY_VIOLATION"
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"
    SLIPPAGE_GUARD = "SLIPPAGE_GUARD"
    CANCEL_REPLACE_EXCEEDS_REMAINING = "CANCEL_REPLACE_EXCEEDS_REMAINING"
    UNKNOWN_ORDER_NOT_FOUND = "UNKNOWN_ORDER_NOT_FOUND"


class HaltCode(StrEnum):
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    OVERFILL = "OVERFILL"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ChildRole(StrEnum):
    ENTRY = "entry"
    STOP = "stop"
    TAKE_PROFIT = "take_profit"


class TrailingStopPolicy(Protocol):
    """Deterministic trailing-stop update rule. Trailing stops may only tighten."""

    def update(
        self, current_stop: Decimal, position_side: OrderSide, trade_price: Decimal
    ) -> Decimal: ...


@dataclass(slots=True)
class FixedDistanceTrailingStop:
    """Trails the stop price a fixed absolute distance behind the trade price."""

    distance: Decimal

    def update(
        self, current_stop: Decimal, position_side: OrderSide, trade_price: Decimal
    ) -> Decimal:
        if position_side is OrderSide.BUY:
            # Protecting a long position: stop trails below price, only moves up.
            candidate = trade_price - self.distance
            return max(current_stop, candidate)
        # Protecting a short position: stop trails above price, only moves down.
        candidate = trade_price + self.distance
        return min(current_stop, candidate)


@dataclass(slots=True, kw_only=True)
class Order:
    client_order_id: str
    request_id: str
    decision_id: str
    instrument: str
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: Decimal
    limit_price: Decimal | None
    reduce_only: bool
    created_at: datetime
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: Decimal = ZERO
    avg_fill_price: Decimal = ZERO
    updated_at: datetime | None = None
    role: ChildRole = ChildRole.ENTRY
    parent_client_order_id: str | None = None
    oco_sibling_id: str | None = None
    replaces_client_order_id: str | None = None
    replaced_by_client_order_id: str | None = None
    trigger_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    trailing_policy: TrailingStopPolicy | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitResult:
    accepted: bool
    client_order_id: str
    status: OrderStatus
    reject_code: RejectCode | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelResult:
    accepted: bool
    client_order_id: str
    status: OrderStatus
    reason: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelReplaceResult:
    accepted: bool
    original_client_order_id: str
    new_client_order_id: str | None
    status: OrderStatus | None
    reject_code: RejectCode | None = None
    reason: str | None = None
