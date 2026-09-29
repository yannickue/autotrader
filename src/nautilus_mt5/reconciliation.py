"""Adapter-level reconciliation: state machine + pure venue-snapshot comparison.

Reconciliation is a comparison between BROKER truth (an actual MT5 snapshot) and
Nautilus' own view (cache). It is never granted by a connection being READY:

    connect / reconnect / restart -> NOT_RECONCILED
    reconcile() -> RECONCILING -> RECONCILED (source VENUE_SNAPSHOT)  or  MISMATCH (+ HALTED)

`PAPER_SELF_CHECK` cannot be used here: `complete_match` refuses any source but
VENUE_SNAPSHOT. `invalidate()` (connection loss, unknown order outcome, session
generation change) drops RECONCILED again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from risk.models import ReconciliationSource, ReconciliationState, RuntimeMode

ZERO = Decimal(0)


class DiscrepancyKind(StrEnum):
    UNEXPECTED_BROKER_POSITION = "UNEXPECTED_BROKER_POSITION"
    MISSING_BROKER_POSITION = "MISSING_BROKER_POSITION"
    POSITION_QTY_MISMATCH = "POSITION_QTY_MISMATCH"
    POSITION_SIDE_MISMATCH = "POSITION_SIDE_MISMATCH"
    UNKNOWN_BROKER_ORDER = "UNKNOWN_BROKER_ORDER"
    UNRESOLVED_ORDER = "UNRESOLVED_ORDER"
    EXTERNAL_ACTIVITY = "EXTERNAL_ACTIVITY"
    DEAL_INGEST_FAILED = "DEAL_INGEST_FAILED"
    UNKNOWN_SYMBOL_POSITION = "UNKNOWN_SYMBOL_POSITION"
    PROTECTION_MISSING_LOCALLY = "PROTECTION_MISSING_LOCALLY"


@dataclass(frozen=True, slots=True, kw_only=True)
class Discrepancy:
    kind: DiscrepancyKind
    detail: str


@dataclass(slots=True)
class ReconciliationTracker:
    state: ReconciliationState = ReconciliationState.NOT_RECONCILED
    source: ReconciliationSource | None = None
    runtime: RuntimeMode = RuntimeMode.STARTING
    generation: int | None = None
    discrepancies: tuple[Discrepancy, ...] = ()
    unprotected_positions: bool = False
    history: list[str] = field(default_factory=list)

    def _log(self, message: str) -> None:
        self.history.append(message)

    def on_connect(self, generation: int) -> None:
        """Every (re)connect starts unreconciled -- a reconnect NEVER implies RECONCILED."""
        self.state = ReconciliationState.NOT_RECONCILED
        self.source = None
        self.runtime = RuntimeMode.STARTING
        self.generation = generation
        self.discrepancies = ()
        self._log(f"connect(gen={generation}) -> NOT_RECONCILED")

    def begin(self) -> None:
        self.state = ReconciliationState.RECONCILING
        self.runtime = RuntimeMode.RECONCILING
        self.discrepancies = ()  # a new comparison starts from a clean slate
        self._log("begin -> RECONCILING")

    def complete_match(self, source: ReconciliationSource, *, unprotected: bool = False) -> None:
        if source is not ReconciliationSource.VENUE_SNAPSHOT:
            raise ValueError("only a VENUE_SNAPSHOT comparison can reconcile a broker account")
        self.state = ReconciliationState.RECONCILED
        self.source = source
        self.runtime = RuntimeMode.READY
        self.discrepancies = ()
        self.unprotected_positions = unprotected
        self._log("match -> RECONCILED(VENUE_SNAPSHOT)")

    def complete_mismatch(self, discrepancies: tuple[Discrepancy, ...]) -> None:
        self.state = ReconciliationState.MISMATCH
        self.source = None
        self.runtime = RuntimeMode.HALTED  # latched until a later successful reconcile()
        self.discrepancies = discrepancies
        self._log(f"mismatch -> MISMATCH+HALTED: {[d.kind.value for d in discrepancies]}")

    def invalidate(self, reason: str) -> None:
        """Connection loss / unknown outcome: authority is gone until re-reconciled."""
        if self.state is ReconciliationState.MISMATCH:
            return  # MISMATCH is stickier than NOT_RECONCILED; keep the halt
        self.state = ReconciliationState.NOT_RECONCILED
        self.source = None
        self.runtime = RuntimeMode.DEGRADED
        self._log(f"invalidate({reason}) -> NOT_RECONCILED+DEGRADED")


@dataclass(frozen=True, slots=True, kw_only=True)
class BrokerPosition:
    ticket: int
    broker_symbol: str
    signed_qty: Decimal
    price_open: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    magic: int


@dataclass(frozen=True, slots=True, kw_only=True)
class BrokerOrder:
    ticket: int
    broker_symbol: str
    comment: str
    magic: int


@dataclass(frozen=True, slots=True, kw_only=True)
class BrokerSnapshot:
    positions: tuple[BrokerPosition, ...]
    open_orders: tuple[BrokerOrder, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalView:
    """Nautilus' view, read from its cache (never owned or mutated here)."""

    positions: dict[str, Decimal]  # broker symbol -> signed qty
    protective_positions: frozenset[int]  # broker position tickets we have a protective order for
    known_order_tickets: frozenset[int]


def compare(
    snapshot: BrokerSnapshot,
    local: LocalView,
    *,
    registered_symbols: frozenset[str],
    require_protection: bool,
) -> tuple[tuple[Discrepancy, ...], bool]:
    """Returns (blocking discrepancies, unprotected_at_broker)."""
    found: list[Discrepancy] = []
    unprotected = False
    seen_symbols: set[str] = set()
    for pos in snapshot.positions:
        if pos.broker_symbol not in registered_symbols:
            found.append(
                Discrepancy(
                    kind=DiscrepancyKind.UNKNOWN_SYMBOL_POSITION,
                    detail=f"position {pos.ticket} on unregistered symbol {pos.broker_symbol}",
                )
            )
            continue
        seen_symbols.add(pos.broker_symbol)
        mine = local.positions.get(pos.broker_symbol, ZERO)
        if mine == ZERO:
            found.append(
                Discrepancy(
                    kind=DiscrepancyKind.UNEXPECTED_BROKER_POSITION,
                    detail=f"broker position {pos.ticket} {pos.signed_qty} not in Nautilus cache",
                )
            )
        elif (mine > 0) != (pos.signed_qty > 0):
            found.append(
                Discrepancy(
                    kind=DiscrepancyKind.POSITION_SIDE_MISMATCH,
                    detail=f"{pos.broker_symbol}: local {mine} vs broker {pos.signed_qty}",
                )
            )
        elif mine != pos.signed_qty:
            found.append(
                Discrepancy(
                    kind=DiscrepancyKind.POSITION_QTY_MISMATCH,
                    detail=f"{pos.broker_symbol}: local {mine} vs broker {pos.signed_qty}",
                )
            )
        if require_protection and pos.stop_loss is None:
            unprotected = True
    for symbol, qty in local.positions.items():
        if qty != ZERO and symbol not in seen_symbols:
            found.append(
                Discrepancy(
                    kind=DiscrepancyKind.MISSING_BROKER_POSITION,
                    detail=f"Nautilus holds {symbol} {qty} but the broker has no such position",
                )
            )
    for order in snapshot.open_orders:
        if order.ticket not in local.known_order_tickets:
            found.append(
                Discrepancy(
                    kind=DiscrepancyKind.UNKNOWN_BROKER_ORDER,
                    detail=f"broker working order {order.ticket} ({order.comment!r}) is unknown",
                )
            )
    return tuple(found), unprotected


def snapshot_from_records(positions: list[Any], orders: list[Any]) -> BrokerSnapshot:
    """Build a snapshot from `PositionRecord`/`OrderRecord` objects (models.py)."""
    return BrokerSnapshot(
        positions=tuple(
            BrokerPosition(
                ticket=p.ticket,
                broker_symbol=p.symbol,
                signed_qty=p.volume if str(p.side) == "BUY" else -p.volume,
                price_open=p.price_open,
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                magic=p.magic,
            )
            for p in positions
        ),
        open_orders=tuple(
            BrokerOrder(ticket=o.ticket, broker_symbol=o.symbol, comment=o.comment, magic=o.magic)
            for o in orders
        ),
    )
