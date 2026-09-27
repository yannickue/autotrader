"""Value objects for the deterministic paper portfolio ledger."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from execution.models import OrderSide


@dataclass(frozen=True, slots=True, kw_only=True)
class Fill:
    """A single, deduplicated execution fill applied to the portfolio."""

    fill_id: str
    instrument: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class PositionView:
    """Read-only snapshot of a single instrument's position."""

    instrument: str
    quantity: Decimal
    avg_entry_price: Decimal
    unrealized_pnl: Decimal
    mark_price: Decimal | None
