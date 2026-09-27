"""Deterministic paper portfolio ledger: positions, realized/unrealized PnL, equity."""

from decimal import Decimal
from typing import Any

from execution.models import OrderSide
from portfolio.models import Fill, PositionView

ZERO = Decimal("0")


class _Position:
    __slots__ = ("avg_entry_price", "mark_price", "quantity")

    def __init__(self, quantity: Decimal = ZERO, avg_entry_price: Decimal = ZERO) -> None:
        self.quantity = quantity
        self.avg_entry_price = avg_entry_price
        self.mark_price: Decimal | None = None


def _is_finite_decimal(value: Decimal) -> bool:
    return value.is_finite()


class Portfolio:
    """Tracks signed positions, realized/unrealized PnL, fees, and equity.

    Deterministic and injected-clock friendly: no wall-clock reads occur here: callers
    supply fill/mark data, and this ledger never touches real time.
    """

    def __init__(self, *, starting_balance: Decimal) -> None:
        self._starting_balance = starting_balance
        self._realized_pnl = ZERO
        self._fees = ZERO
        self._positions: dict[str, _Position] = {}
        self._seen_fill_ids: set[str] = set()

    def apply_fill(self, fill: Fill) -> bool:
        """Apply a fill to the ledger. Returns False (no-op) for a duplicate fill_id."""
        if not _is_finite_decimal(fill.quantity) or fill.quantity <= ZERO:
            raise ValueError("fill quantity must be finite and positive")
        if not _is_finite_decimal(fill.price) or fill.price <= ZERO:
            raise ValueError("fill price must be finite and positive")
        if not _is_finite_decimal(fill.fee):
            raise ValueError("fill fee must be finite")

        if fill.fill_id in self._seen_fill_ids:
            return False
        self._seen_fill_ids.add(fill.fill_id)

        position = self._positions.setdefault(fill.instrument, _Position())
        signed_delta = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        current_qty = position.quantity
        new_qty = current_qty + signed_delta

        same_direction_or_flat = current_qty == ZERO or (
            (current_qty > ZERO) == (signed_delta > ZERO)
        )

        if same_direction_or_flat:
            total_cost = abs(current_qty) * position.avg_entry_price + fill.quantity * fill.price
            position.avg_entry_price = total_cost / abs(new_qty) if new_qty != ZERO else ZERO
        else:
            closing_qty = min(abs(current_qty), fill.quantity)
            if current_qty > ZERO:
                realized = (fill.price - position.avg_entry_price) * closing_qty
            else:
                realized = (position.avg_entry_price - fill.price) * closing_qty
            self._realized_pnl += realized

            remaining_fill_qty = fill.quantity - closing_qty
            if new_qty == ZERO:
                position.avg_entry_price = ZERO
            elif remaining_fill_qty > ZERO:
                # Flipped through zero: the remainder opens a fresh position at fill price.
                position.avg_entry_price = fill.price
            # else: still same sign as before, partial close, avg_entry_price unchanged.

        position.quantity = new_qty
        self._fees += fill.fee
        return True

    def mark(self, instrument: str, price: Decimal) -> None:
        position = self._positions.setdefault(instrument, _Position())
        position.mark_price = price

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized_pnl

    @property
    def fees(self) -> Decimal:
        return self._fees

    def _unrealized_pnl(self, instrument: str, position: _Position) -> Decimal:
        if position.mark_price is None or position.quantity == ZERO:
            return ZERO
        return (position.mark_price - position.avg_entry_price) * position.quantity

    @property
    def positions(self) -> dict[str, PositionView]:
        return {
            instrument: PositionView(
                instrument=instrument,
                quantity=position.quantity,
                avg_entry_price=position.avg_entry_price,
                unrealized_pnl=self._unrealized_pnl(instrument, position),
                mark_price=position.mark_price,
            )
            for instrument, position in self._positions.items()
        }

    @property
    def gross_notional(self) -> Decimal:
        total = ZERO
        for position in self._positions.values():
            if position.mark_price is None:
                continue
            total += abs(position.quantity) * position.mark_price
        return total

    @property
    def net_notional(self) -> Decimal:
        total = ZERO
        for position in self._positions.values():
            if position.mark_price is None:
                continue
            total += position.quantity * position.mark_price
        return total

    @property
    def instrument_notionals(self) -> dict[str, Decimal]:
        return {
            instrument: position.quantity * position.mark_price
            for instrument, position in self._positions.items()
            if position.mark_price is not None
        }

    @property
    def equity(self) -> Decimal:
        unrealized_total = sum(
            (
                self._unrealized_pnl(instrument, position)
                for instrument, position in self._positions.items()
            ),
            start=ZERO,
        )
        return self._starting_balance + self._realized_pnl - self._fees + unrealized_total

    def export_state(self) -> dict[str, Any]:
        """Deterministically serialize ledger state (Decimals as strings)."""
        return {
            "starting_balance": str(self._starting_balance),
            "realized_pnl": str(self._realized_pnl),
            "fees": str(self._fees),
            "seen_fill_ids": sorted(self._seen_fill_ids),
            "positions": {
                instrument: {
                    "quantity": str(position.quantity),
                    "avg_entry_price": str(position.avg_entry_price),
                    "mark_price": (
                        str(position.mark_price) if position.mark_price is not None else None
                    ),
                }
                for instrument, position in sorted(self._positions.items())
            },
        }

    def import_state(self, state: dict[str, Any]) -> None:
        """Replace this ledger's state with a previously exported checkpoint."""
        self._starting_balance = Decimal(state["starting_balance"])
        self._realized_pnl = Decimal(state["realized_pnl"])
        self._fees = Decimal(state["fees"])
        self._seen_fill_ids = set(state["seen_fill_ids"])
        self._positions = {}
        for instrument, payload in state["positions"].items():
            position = _Position(
                quantity=Decimal(payload["quantity"]),
                avg_entry_price=Decimal(payload["avg_entry_price"]),
            )
            position.mark_price = (
                Decimal(payload["mark_price"]) if payload["mark_price"] is not None else None
            )
            self._positions[instrument] = position
