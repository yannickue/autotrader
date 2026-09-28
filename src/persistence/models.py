"""Plain, engine-agnostic value objects for the durable persistence layer.

Deliberately decoupled from `execution.*` / `risk.*` types (per CLAUDE.md: keep
research and production dependencies separated, and per the task's isolation
requirement for this module). Fields that mirror an enum elsewhere in the
codebase (order status, side, ...) are plain strings here; a later integration
step is responsible for converting between the engine's real types and these
records when wiring `PaperExecutionEngine`/`RiskEngine` into this store.

All money/quantity fields are `Decimal`. All timestamps are UTC-aware
`datetime`. Every record validates itself in `__post_init__` so a caller can
never construct (and therefore never persist) an internally inconsistent
record -- corruption this layer must still detect is corruption that reaches
the *database* out-of-band (a hand-edited row, a crash mid-write, a foreign
process), which is what `store.recover()` guards against separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _require_utc(value: datetime, field_name: str) -> None:
    if not _is_utc(value):
        raise ValueError(f"{field_name} must be a UTC-aware datetime")


def _require_finite(value: Decimal, field_name: str) -> None:
    if not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")


def _require_nonempty(value: str, field_name: str) -> None:
    if not value:
        raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionRecord:
    """An open (or flat) position for one instrument, as reconstructed state."""

    instrument: str
    quantity: Decimal
    avg_entry_price: Decimal
    mark_price: Decimal | None = None
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_nonempty(self.instrument, "instrument")
        _require_finite(self.quantity, "quantity")
        _require_finite(self.avg_entry_price, "avg_entry_price")
        if self.mark_price is not None:
            _require_finite(self.mark_price, "mark_price")
        _require_utc(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderRecord:
    """An active or terminal order, as tracked by the execution engine."""

    client_order_id: str
    request_id: str
    decision_id: str
    instrument: str
    side: str
    order_type: str
    time_in_force: str
    quantity: Decimal
    limit_price: Decimal | None
    reduce_only: bool
    created_at: datetime
    status: str
    filled_quantity: Decimal
    avg_fill_price: Decimal
    role: str
    parent_client_order_id: str | None = None
    oco_sibling_id: str | None = None
    replaces_client_order_id: str | None = None
    replaced_by_client_order_id: str | None = None
    trigger_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    updated_at: datetime
    # Arbitrary engine-supplied metadata (e.g. {"reference_price": ...}) that
    # doesn't fit a typed column. Must be JSON-serializable; stored as JSON.
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty(self.client_order_id, "client_order_id")
        _require_nonempty(self.request_id, "request_id")
        _require_nonempty(self.decision_id, "decision_id")
        _require_nonempty(self.instrument, "instrument")
        _require_finite(self.quantity, "quantity")
        if self.quantity <= ZERO:
            raise ValueError("quantity must be positive")
        if self.limit_price is not None:
            _require_finite(self.limit_price, "limit_price")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        _require_finite(self.filled_quantity, "filled_quantity")
        if self.filled_quantity < ZERO:
            raise ValueError("filled_quantity must not be negative")
        if self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity must not exceed quantity")
        _require_finite(self.avg_fill_price, "avg_fill_price")
        if self.trigger_price is not None:
            _require_finite(self.trigger_price, "trigger_price")
        if self.take_profit_price is not None:
            _require_finite(self.take_profit_price, "take_profit_price")


@dataclass(frozen=True, slots=True, kw_only=True)
class FillRecord:
    """A single, deduplicated execution fill. `fill_id` is the idempotency key."""

    fill_id: str
    instrument: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal = ZERO
    timestamp: datetime
    applied_at: datetime

    def __post_init__(self) -> None:
        _require_nonempty(self.fill_id, "fill_id")
        _require_nonempty(self.instrument, "instrument")
        _require_finite(self.quantity, "quantity")
        if self.quantity <= ZERO:
            raise ValueError("quantity must be positive")
        _require_finite(self.price, "price")
        if self.price <= ZERO:
            raise ValueError("price must be positive")
        _require_finite(self.fee, "fee")
        _require_utc(self.timestamp, "timestamp")
        _require_utc(self.applied_at, "applied_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReservationRecord:
    """Mirrors `RiskEngine._reservations[decision_id] = (side, abs_notional, signed_notional)`."""

    decision_id: str
    side: str
    abs_notional: Decimal
    signed_notional: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_nonempty(self.decision_id, "decision_id")
        _require_finite(self.abs_notional, "abs_notional")
        if self.abs_notional < ZERO:
            raise ValueError("abs_notional must not be negative")
        _require_finite(self.signed_notional, "signed_notional")
        _require_utc(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReduceOnlyReservationRecord:
    """Mirrors `RiskEngine._reduce_only_reservations[decision_id] = (instrument, quantity)`."""

    decision_id: str
    instrument: str
    quantity: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_nonempty(self.decision_id, "decision_id")
        _require_nonempty(self.instrument, "instrument")
        _require_finite(self.quantity, "quantity")
        if self.quantity <= ZERO:
            raise ValueError("quantity must be positive")
        _require_utc(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class PortfolioStateRecord:
    """Singleton portfolio-level ledger state needed for full reconstruction."""

    starting_balance: Decimal
    realized_pnl: Decimal
    fees: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_finite(self.starting_balance, "starting_balance")
        _require_finite(self.realized_pnl, "realized_pnl")
        _require_finite(self.fees, "fees")
        _require_utc(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciliationStateRecord:
    """Last-known venue reconciliation outcome."""

    mode: str
    reconciled: bool
    mismatch_reason: str | None
    last_reconciled_at: datetime | None
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_nonempty(self.mode, "mode")
        if self.last_reconciled_at is not None:
            _require_utc(self.last_reconciled_at, "last_reconciled_at")
        _require_utc(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class HaltStateRecord:
    """Singleton HALT state/reason, independent of which component halted."""

    halted: bool
    reason: str | None
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_utc(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class StateSnapshot:
    """Full reconstructed state, as returned by a successful `store.recover()`."""

    positions: tuple[PositionRecord, ...] = ()
    orders: tuple[OrderRecord, ...] = ()
    fills: tuple[FillRecord, ...] = ()
    reservations: tuple[ReservationRecord, ...] = ()
    reduce_only_reservations: tuple[ReduceOnlyReservationRecord, ...] = ()
    execution_ids: tuple[tuple[str, str], ...] = ()
    portfolio_state: PortfolioStateRecord | None = None
    reconciliation_state: ReconciliationStateRecord | None = None
    halt_state: HaltStateRecord | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryResult:
    """The single outcome type for `store.recover()`.

    `ok=False` means the persisted state is missing, ambiguous, or fails a
    sanity check -- the caller must treat this as fail-closed (HALT) rather
    than proceed with `snapshot`, which is always `None` in that case.
    """

    ok: bool
    snapshot: StateSnapshot | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class StateCorruptionError(Exception):
    """Raised only for programmer-error-shaped misuse, never for ordinary
    persisted-data corruption (that is reported via `RecoveryResult.ok=False`
    so callers have one uniform, non-exception fail-closed path to check)."""
