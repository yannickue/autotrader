"""Durable SQLite-backed persistence/recovery layer.

Standalone module (see docs/OPEN_QUESTIONS.md item 22): stores open
positions, active orders, fills, risk reservations, execution/client order
ids, portfolio state, last-known reconciliation state, and HALT state, and
reconstructs them on restart via `SQLiteStore.recover()`. Not yet wired into
`execution.paper.PaperExecutionEngine` or `risk.engine.RiskEngine` -- that is
a later integration step.
"""

from persistence.models import (
    FillRecord,
    HaltStateRecord,
    OrderRecord,
    PortfolioStateRecord,
    PositionRecord,
    ReconciliationStateRecord,
    RecoveryResult,
    ReduceOnlyReservationRecord,
    ReservationRecord,
    StateCorruptionError,
    StateSnapshot,
)
from persistence.store import STATE_FORMAT_VERSION, SQLiteStore

__all__ = [
    "STATE_FORMAT_VERSION",
    "FillRecord",
    "HaltStateRecord",
    "OrderRecord",
    "PortfolioStateRecord",
    "PositionRecord",
    "ReconciliationStateRecord",
    "RecoveryResult",
    "ReduceOnlyReservationRecord",
    "ReservationRecord",
    "SQLiteStore",
    "StateCorruptionError",
    "StateSnapshot",
]
