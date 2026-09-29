"""SQLite-backed durable persistence/recovery layer.

LEGACY_RUNTIME / SHADOW_ORACLE (Nautilus convergence, see
docs/ARCHITECTURE_AUDIT_2026-09-29.md): retained as a parity oracle;
operational state is later owned by
Nautilus and only the append-only audit journal is kept. Do not add features except
confirmed safety fixes.

Resolves docs/OPEN_QUESTIONS.md item 22 (durable checkpoint/event-journal
storage backend) with SQLite, per the sprint brief's default. Standalone and
independently testable: no dependency on `execution.*` / `risk.*` -- a later
integration step converts `PaperExecutionEngine.export_checkpoint()` /
`RiskEngine`'s reservation ledger into the plain records in `persistence.models`
and calls the write methods below.

Durability model: every write commits before returning (no batched/async
commits), so "persist state" -> "process dies" -> "reopen the same file" never
loses an acknowledged write. This does not claim external exactly-once or
multi-process coordination (see OPEN_QUESTIONS #15, #22) -- only single-writer
crash-safe durability for one SQLite file, which is what stdlib `sqlite3`
gives you with default journaling.

Fail-closed recovery: `recover()` never raises for ordinary persisted-data
problems (unreadable rows, a state-format-version mismatch, a value that
fails a sanity check). It always returns a `RecoveryResult`; `ok=False` means
the caller must treat state as ambiguous and HALT rather than guess.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

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
    StateSnapshot,
)

# Bumped whenever the on-disk row shapes change in a way that would make an
# older/newer reader misinterpret data instead of failing loudly.
#
# v2 (Slice 4a): added `orders.metadata` (JSON), added `component_state`
# table, and fills are now ordered by insertion (`rowid`) rather than the
# lexicographic `fill_id` sort a v1 reader would use -- a v1-stamped database
# is rejected by `recover()` below rather than silently reinterpreted.
STATE_FORMAT_VERSION = "3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    instrument TEXT PRIMARY KEY,
    quantity TEXT NOT NULL,
    avg_entry_price TEXT NOT NULL,
    mark_price TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    time_in_force TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT,
    reduce_only INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    filled_quantity TEXT NOT NULL,
    avg_fill_price TEXT NOT NULL,
    role TEXT NOT NULL,
    parent_client_order_id TEXT,
    oco_sibling_id TEXT,
    replaces_client_order_id TEXT,
    replaced_by_client_order_id TEXT,
    trigger_price TEXT,
    take_profit_price TEXT,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    decision_id TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    abs_notional TEXT NOT NULL,
    signed_notional TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reduce_only_reservations (
    decision_id TEXT PRIMARY KEY,
    instrument TEXT NOT NULL,
    quantity TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_ids (
    kind TEXT NOT NULL,
    identifier TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (kind, identifier)
);

CREATE TABLE IF NOT EXISTS portfolio_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    starting_balance TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    fees TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL,
    reconciled INTEGER NOT NULL,
    mismatch_reason TEXT,
    last_reconciled_at TEXT,
    updated_at TEXT NOT NULL,
    reconciliation_state TEXT NOT NULL DEFAULT 'not_reconciled',
    reconciliation_source TEXT
);

CREATE TABLE IF NOT EXISTS halt_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    halted INTEGER NOT NULL,
    reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_state (
    component TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be UTC-aware")
    return value.astimezone(UTC)


def _dec(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"{field_name} is not a valid Decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} is not finite: {value!r}")
    return parsed


def _dec_opt(value: str | None, field_name: str) -> Decimal | None:
    return None if value is None else _dec(value, field_name)


def _ts(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is not a valid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} is not UTC-aware: {value!r}")
    return parsed


def _ts_opt(value: str | None, field_name: str) -> datetime | None:
    return None if value is None else _ts(value, field_name)


class SQLiteStore:
    """Durable store for open positions, orders, fills, risk reservations,
    execution/client order ids, portfolio state, reconciliation state, and
    HALT state -- everything needed to reconstruct engine state on restart.
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._add_missing_reconciliation_columns()
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'state_format_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('state_format_version', ?)",
                (STATE_FORMAT_VERSION,),
            )

    def _add_missing_reconciliation_columns(self) -> None:
        """A DB written by format version 2 lacks the reconciliation state/source
        columns. Add them (defaults: not_reconciled / NULL) so writes do not
        crash; `recover()` still refuses such a DB (state_format_version 2 !=
        3), so nothing is ever interpreted from a pre-source row."""
        existing = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(reconciliation_state)")
        }
        if "reconciliation_state" not in existing:
            self._conn.execute(
                "ALTER TABLE reconciliation_state ADD COLUMN "
                "reconciliation_state TEXT NOT NULL DEFAULT 'not_reconciled'"
            )
        if "reconciliation_source" not in existing:
            self._conn.execute(
                "ALTER TABLE reconciliation_state ADD COLUMN reconciliation_source TEXT"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transactions --------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group multiple writes into a single atomic commit.

        `BEGIN IMMEDIATE` on enter (acquires the write lock up front, so a
        concurrent writer fails fast rather than deadlocking mid-transaction),
        `COMMIT` on clean exit, `ROLLBACK` (then re-raise) on any exception.
        Nesting is not supported -- callers must not call `transaction()`
        again while already inside one.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def write_snapshot(
        self,
        *,
        positions: Iterable[PositionRecord] = (),
        orders: Iterable[OrderRecord] = (),
        reservations: Iterable[ReservationRecord] = (),
        reduce_only_reservations: Iterable[ReduceOnlyReservationRecord] = (),
        portfolio_state: PortfolioStateRecord | None = None,
        halt_state: HaltStateRecord | None = None,
        reconciliation_state: ReconciliationStateRecord | None = None,
        component_state: Mapping[str, dict[str, Any]] | None = None,
        fills: Iterable[FillRecord] = (),
    ) -> None:
        """Atomically replace the full set of mutable-state tables in ONE commit.

        This is the single-commit-point primitive a caller (e.g. the pipeline's
        crash-recovery wiring) builds on: every table that can go torn if a
        fill's several individual writes (order, reservation, position,
        portfolio state, ...) are applied one at a time and the process dies
        mid-sequence is instead replaced together here, inside one
        `transaction()`.

        `positions`/`orders`/`reservations`/`reduce_only_reservations` are
        always fully replaced (existing rows deleted first, so this is the
        new complete set, not a merge) -- pass the full current set, not a
        delta. `portfolio_state`/`halt_state`/`reconciliation_state` are
        singleton rows: passing `None` leaves that row untouched, passing a
        record replaces it. `component_state`, when given (not `None`), fully
        replaces the entire component_state table with the given mapping.

        `fills` (Slice 4b): `fills` itself stays append-only -- these rows are
        never deleted/replaced wholesale like the tables above -- but passing
        the current call's newly observed fills here records them (via
        `record_fill`'s own idempotent `INSERT OR IGNORE`) INSIDE the same
        transaction as the rest of this snapshot, so "this fill was recorded"
        and "the rest of this call's state was snapshotted" commit or roll
        back together atomically. A caller with no new fills for this call
        (the common case) passes nothing and gets exactly the pre-Slice-4b
        behavior.
        """
        with self.transaction():
            self._conn.execute("DELETE FROM positions")
            self._conn.execute("DELETE FROM orders")
            self._conn.execute("DELETE FROM reservations")
            self._conn.execute("DELETE FROM reduce_only_reservations")
            for position in positions:
                self.upsert_position(position)
            for order in orders:
                self.upsert_order(order)
            for reservation in reservations:
                self.upsert_reservation(reservation)
            for reduce_only_reservation in reduce_only_reservations:
                self.upsert_reduce_only_reservation(reduce_only_reservation)
            if portfolio_state is not None:
                self.set_portfolio_state(portfolio_state)
            if halt_state is not None:
                self.set_halt_state(halt_state)
            if reconciliation_state is not None:
                self.set_reconciliation_state(reconciliation_state)
            if component_state is not None:
                self._conn.execute("DELETE FROM component_state")
                for component, payload in component_state.items():
                    self.set_component_state(component, payload)
            for fill in fills:
                self.record_fill(fill)

    # -- writes (all idempotent: same primary key overwrites, not duplicates) --

    def upsert_position(self, position: PositionRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO positions (instrument, quantity, avg_entry_price, mark_price, updated_at)
            VALUES (:instrument, :quantity, :avg_entry_price, :mark_price, :updated_at)
            ON CONFLICT (instrument) DO UPDATE SET
                quantity = excluded.quantity,
                avg_entry_price = excluded.avg_entry_price,
                mark_price = excluded.mark_price,
                updated_at = excluded.updated_at
            """,
            {
                "instrument": position.instrument,
                "quantity": str(position.quantity),
                "avg_entry_price": str(position.avg_entry_price),
                "mark_price": None if position.mark_price is None else str(position.mark_price),
                "updated_at": _utc(position.updated_at).isoformat(),
            },
        )

    def upsert_order(self, order: OrderRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO orders (
                client_order_id, request_id, decision_id, instrument, side, order_type,
                time_in_force, quantity, limit_price, reduce_only, created_at, status,
                filled_quantity, avg_fill_price, role, parent_client_order_id,
                oco_sibling_id, replaces_client_order_id, replaced_by_client_order_id,
                trigger_price, take_profit_price, updated_at, metadata
            ) VALUES (
                :client_order_id, :request_id, :decision_id, :instrument, :side, :order_type,
                :time_in_force, :quantity, :limit_price, :reduce_only, :created_at, :status,
                :filled_quantity, :avg_fill_price, :role, :parent_client_order_id,
                :oco_sibling_id, :replaces_client_order_id, :replaced_by_client_order_id,
                :trigger_price, :take_profit_price, :updated_at, :metadata
            )
            ON CONFLICT (client_order_id) DO UPDATE SET
                request_id = excluded.request_id,
                decision_id = excluded.decision_id,
                instrument = excluded.instrument,
                side = excluded.side,
                order_type = excluded.order_type,
                time_in_force = excluded.time_in_force,
                quantity = excluded.quantity,
                limit_price = excluded.limit_price,
                reduce_only = excluded.reduce_only,
                created_at = excluded.created_at,
                status = excluded.status,
                filled_quantity = excluded.filled_quantity,
                avg_fill_price = excluded.avg_fill_price,
                role = excluded.role,
                parent_client_order_id = excluded.parent_client_order_id,
                oco_sibling_id = excluded.oco_sibling_id,
                replaces_client_order_id = excluded.replaces_client_order_id,
                replaced_by_client_order_id = excluded.replaced_by_client_order_id,
                trigger_price = excluded.trigger_price,
                take_profit_price = excluded.take_profit_price,
                updated_at = excluded.updated_at,
                metadata = excluded.metadata
            """,
            {
                "client_order_id": order.client_order_id,
                "request_id": order.request_id,
                "decision_id": order.decision_id,
                "instrument": order.instrument,
                "side": order.side,
                "order_type": order.order_type,
                "time_in_force": order.time_in_force,
                "quantity": str(order.quantity),
                "limit_price": None if order.limit_price is None else str(order.limit_price),
                "reduce_only": int(order.reduce_only),
                "created_at": _utc(order.created_at).isoformat(),
                "status": order.status,
                "filled_quantity": str(order.filled_quantity),
                "avg_fill_price": str(order.avg_fill_price),
                "role": order.role,
                "parent_client_order_id": order.parent_client_order_id,
                "oco_sibling_id": order.oco_sibling_id,
                "replaces_client_order_id": order.replaces_client_order_id,
                "replaced_by_client_order_id": order.replaced_by_client_order_id,
                "trigger_price": None if order.trigger_price is None else str(order.trigger_price),
                "take_profit_price": (
                    None if order.take_profit_price is None else str(order.take_profit_price)
                ),
                "updated_at": _utc(order.updated_at).isoformat(),
                "metadata": json.dumps(order.metadata, sort_keys=True),
            },
        )

    def has_fill(self, fill_id: str) -> bool:
        """True if `fill_id` was already durably recorded -- the idempotency
        check a caller must run (or rely on `record_fill`'s return value)
        before applying a fill/event to in-memory or ledger state, so a
        replayed duplicate can never double-apply exposure."""
        row = self._conn.execute(
            "SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)
        ).fetchone()
        return row is not None

    def record_fill(self, fill: FillRecord) -> bool:
        """Durably record a fill. Returns True if newly recorded, False if
        `fill.fill_id` was already present (a duplicate/replayed event) --
        the caller must not re-apply a fill for which this returns False."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO fills "
            "(fill_id, instrument, side, quantity, price, fee, timestamp, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.fill_id,
                fill.instrument,
                fill.side,
                str(fill.quantity),
                str(fill.price),
                str(fill.fee),
                _utc(fill.timestamp).isoformat(),
                _utc(fill.applied_at).isoformat(),
            ),
        )
        return cursor.rowcount > 0

    def upsert_reservation(self, reservation: ReservationRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO reservations (decision_id, side, abs_notional, signed_notional, updated_at)
            VALUES (:decision_id, :side, :abs_notional, :signed_notional, :updated_at)
            ON CONFLICT (decision_id) DO UPDATE SET
                side = excluded.side,
                abs_notional = excluded.abs_notional,
                signed_notional = excluded.signed_notional,
                updated_at = excluded.updated_at
            """,
            {
                "decision_id": reservation.decision_id,
                "side": reservation.side,
                "abs_notional": str(reservation.abs_notional),
                "signed_notional": str(reservation.signed_notional),
                "updated_at": _utc(reservation.updated_at).isoformat(),
            },
        )

    def release_reservation(self, decision_id: str) -> None:
        """Idempotent: removing an already-absent decision_id is a no-op."""
        self._conn.execute("DELETE FROM reservations WHERE decision_id = ?", (decision_id,))

    def upsert_reduce_only_reservation(self, reservation: ReduceOnlyReservationRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO reduce_only_reservations (decision_id, instrument, quantity, updated_at)
            VALUES (:decision_id, :instrument, :quantity, :updated_at)
            ON CONFLICT (decision_id) DO UPDATE SET
                instrument = excluded.instrument,
                quantity = excluded.quantity,
                updated_at = excluded.updated_at
            """,
            {
                "decision_id": reservation.decision_id,
                "instrument": reservation.instrument,
                "quantity": str(reservation.quantity),
                "updated_at": _utc(reservation.updated_at).isoformat(),
            },
        )

    def release_reduce_only_reservation(self, decision_id: str) -> None:
        self._conn.execute(
            "DELETE FROM reduce_only_reservations WHERE decision_id = ?", (decision_id,)
        )

    def record_execution_id(self, kind: str, identifier: str, *, recorded_at: datetime) -> bool:
        """Durably track a used execution id / client order id (e.g. to
        reject a reused id after restart). Returns True if newly recorded,
        False if `(kind, identifier)` was already present."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO execution_ids (kind, identifier, recorded_at) VALUES (?, ?, ?)",
            (kind, identifier, _utc(recorded_at).isoformat()),
        )
        return cursor.rowcount > 0

    def has_execution_id(self, kind: str, identifier: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM execution_ids WHERE kind = ? AND identifier = ?",
            (kind, identifier),
        ).fetchone()
        return row is not None

    def set_portfolio_state(self, state: PortfolioStateRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO portfolio_state (id, starting_balance, realized_pnl, fees, updated_at)
            VALUES (1, :starting_balance, :realized_pnl, :fees, :updated_at)
            ON CONFLICT (id) DO UPDATE SET
                starting_balance = excluded.starting_balance,
                realized_pnl = excluded.realized_pnl,
                fees = excluded.fees,
                updated_at = excluded.updated_at
            """,
            {
                "starting_balance": str(state.starting_balance),
                "realized_pnl": str(state.realized_pnl),
                "fees": str(state.fees),
                "updated_at": _utc(state.updated_at).isoformat(),
            },
        )

    def set_reconciliation_state(self, state: ReconciliationStateRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO reconciliation_state
                (id, mode, reconciled, mismatch_reason, last_reconciled_at, updated_at,
                 reconciliation_state, reconciliation_source)
            VALUES (1, :mode, :reconciled, :mismatch_reason, :last_reconciled_at, :updated_at,
                    :reconciliation_state, :reconciliation_source)
            ON CONFLICT (id) DO UPDATE SET
                mode = excluded.mode,
                reconciled = excluded.reconciled,
                mismatch_reason = excluded.mismatch_reason,
                last_reconciled_at = excluded.last_reconciled_at,
                updated_at = excluded.updated_at,
                reconciliation_state = excluded.reconciliation_state,
                reconciliation_source = excluded.reconciliation_source
            """,
            {
                "reconciliation_state": state.state,
                "reconciliation_source": state.source,
                "mode": state.mode,
                "reconciled": int(state.reconciled),
                "mismatch_reason": state.mismatch_reason,
                "last_reconciled_at": (
                    None
                    if state.last_reconciled_at is None
                    else _utc(state.last_reconciled_at).isoformat()
                ),
                "updated_at": _utc(state.updated_at).isoformat(),
            },
        )

    def set_halt_state(self, state: HaltStateRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO halt_state (id, halted, reason, updated_at)
            VALUES (1, :halted, :reason, :updated_at)
            ON CONFLICT (id) DO UPDATE SET
                halted = excluded.halted,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            {
                "halted": int(state.halted),
                "reason": state.reason,
                "updated_at": _utc(state.updated_at).isoformat(),
            },
        )

    def set_component_state(self, component: str, payload: dict[str, Any]) -> None:
        """Store an arbitrary deterministic JSON-serializable blob under
        `component` (e.g. an execution/risk engine's internal dedup maps or
        an exit-position ladder), replacing any prior value for that key."""
        self._conn.execute(
            """
            INSERT INTO component_state (component, payload)
            VALUES (:component, :payload)
            ON CONFLICT (component) DO UPDATE SET payload = excluded.payload
            """,
            {"component": component, "payload": json.dumps(payload, sort_keys=True)},
        )

    def get_component_state(self, component: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM component_state WHERE component = ?", (component,)
        ).fetchone()
        return None if row is None else json.loads(row["payload"])

    # -- point reads -------------------------------------------------------

    def get_position(self, instrument: str) -> PositionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM positions WHERE instrument = ?", (instrument,)
        ).fetchone()
        return None if row is None else _row_to_position(row)

    def list_positions(self) -> list[PositionRecord]:
        rows = self._conn.execute("SELECT * FROM positions ORDER BY instrument").fetchall()
        return [_row_to_position(row) for row in rows]

    def get_order(self, client_order_id: str) -> OrderRecord | None:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return None if row is None else _row_to_order(row)

    def list_orders(self) -> list[OrderRecord]:
        rows = self._conn.execute("SELECT * FROM orders ORDER BY client_order_id").fetchall()
        return [_row_to_order(row) for row in rows]

    def list_fills(self) -> list[FillRecord]:
        # Ordered by `rowid` (SQLite's implicit, monotonically-increasing
        # insertion-order column for an ordinary rowid table), NOT by the
        # lexicographic `fill_id` sort a v1 reader used -- `fill_id` is not
        # guaranteed to sort chronologically, and PnL/portfolio replay
        # depends on true fill order.
        rows = self._conn.execute("SELECT * FROM fills ORDER BY rowid").fetchall()
        return [_row_to_fill(row) for row in rows]

    def list_reservations(self) -> list[ReservationRecord]:
        rows = self._conn.execute("SELECT * FROM reservations ORDER BY decision_id").fetchall()
        return [_row_to_reservation(row) for row in rows]

    def list_reduce_only_reservations(self) -> list[ReduceOnlyReservationRecord]:
        rows = self._conn.execute(
            "SELECT * FROM reduce_only_reservations ORDER BY decision_id"
        ).fetchall()
        return [_row_to_reduce_only_reservation(row) for row in rows]

    def list_execution_ids(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT kind, identifier FROM execution_ids ORDER BY kind, identifier"
        ).fetchall()
        return [(row["kind"], row["identifier"]) for row in rows]

    def get_portfolio_state(self) -> PortfolioStateRecord | None:
        row = self._conn.execute("SELECT * FROM portfolio_state WHERE id = 1").fetchone()
        return None if row is None else _row_to_portfolio_state(row)

    def get_reconciliation_state(self) -> ReconciliationStateRecord | None:
        row = self._conn.execute("SELECT * FROM reconciliation_state WHERE id = 1").fetchone()
        return None if row is None else _row_to_reconciliation_state(row)

    def get_halt_state(self) -> HaltStateRecord | None:
        row = self._conn.execute("SELECT * FROM halt_state WHERE id = 1").fetchone()
        return None if row is None else _row_to_halt_state(row)

    # -- full recovery -------------------------------------------------------

    def recover(self) -> RecoveryResult:
        """Reconstruct full state, or report why that is unsafe to do.

        Never raises for ordinary persisted-data problems: a state-format
        version mismatch, a row with an unparseable/invalid value, or a row
        that fails a sanity check (e.g. filled_quantity > quantity) all
        produce `RecoveryResult(ok=False, error=...)` instead of a partial or
        best-effort snapshot. Callers must fail closed (HALT) on `ok=False`.
        """
        version_row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'state_format_version'"
        ).fetchone()
        stored_version = None if version_row is None else version_row["value"]
        if stored_version != STATE_FORMAT_VERSION:
            return RecoveryResult(
                ok=False,
                error=(
                    "state_format_version mismatch: expected "
                    f"{STATE_FORMAT_VERSION!r}, found {stored_version!r} -- "
                    "cannot safely interpret persisted rows written by an "
                    "unknown/different format version"
                ),
                details={"expected_version": STATE_FORMAT_VERSION, "found_version": stored_version},
            )

        try:
            positions = tuple(
                _row_to_position(r) for r in self._conn.execute("SELECT * FROM positions")
            )
            orders = tuple(_row_to_order(r) for r in self._conn.execute("SELECT * FROM orders"))
            # rowid (insertion) order, not lexicographic fill_id order -- see
            # list_fills() above.
            fills = tuple(
                _row_to_fill(r) for r in self._conn.execute("SELECT * FROM fills ORDER BY rowid")
            )
            reservations = tuple(
                _row_to_reservation(r) for r in self._conn.execute("SELECT * FROM reservations")
            )
            reduce_only_reservations = tuple(
                _row_to_reduce_only_reservation(r)
                for r in self._conn.execute("SELECT * FROM reduce_only_reservations")
            )
            execution_ids = tuple(
                (r["kind"], r["identifier"])
                for r in self._conn.execute("SELECT kind, identifier FROM execution_ids")
            )
            portfolio_row = self._conn.execute(
                "SELECT * FROM portfolio_state WHERE id = 1"
            ).fetchone()
            portfolio_state = (
                None if portfolio_row is None else _row_to_portfolio_state(portfolio_row)
            )
            reconciliation_row = self._conn.execute(
                "SELECT * FROM reconciliation_state WHERE id = 1"
            ).fetchone()
            reconciliation_state = (
                None
                if reconciliation_row is None
                else _row_to_reconciliation_state(reconciliation_row)
            )
            halt_row = self._conn.execute("SELECT * FROM halt_state WHERE id = 1").fetchone()
            halt_state = None if halt_row is None else _row_to_halt_state(halt_row)
        except (ValueError, TypeError, sqlite3.Error) as exc:
            return RecoveryResult(
                ok=False,
                error=f"persisted state failed to parse/validate: {exc}",
            )

        snapshot = StateSnapshot(
            positions=positions,
            orders=orders,
            fills=fills,
            reservations=reservations,
            reduce_only_reservations=reduce_only_reservations,
            execution_ids=execution_ids,
            portfolio_state=portfolio_state,
            reconciliation_state=reconciliation_state,
            halt_state=halt_state,
        )
        return RecoveryResult(ok=True, snapshot=snapshot)


def _row_to_position(row: sqlite3.Row) -> PositionRecord:
    return PositionRecord(
        instrument=row["instrument"],
        quantity=_dec(row["quantity"], "quantity"),
        avg_entry_price=_dec(row["avg_entry_price"], "avg_entry_price"),
        mark_price=_dec_opt(row["mark_price"], "mark_price"),
        updated_at=_ts(row["updated_at"], "updated_at"),
    )


def _row_to_order(row: sqlite3.Row) -> OrderRecord:
    return OrderRecord(
        client_order_id=row["client_order_id"],
        request_id=row["request_id"],
        decision_id=row["decision_id"],
        instrument=row["instrument"],
        side=row["side"],
        order_type=row["order_type"],
        time_in_force=row["time_in_force"],
        quantity=_dec(row["quantity"], "quantity"),
        limit_price=_dec_opt(row["limit_price"], "limit_price"),
        reduce_only=bool(row["reduce_only"]),
        created_at=_ts(row["created_at"], "created_at"),
        status=row["status"],
        filled_quantity=_dec(row["filled_quantity"], "filled_quantity"),
        avg_fill_price=_dec(row["avg_fill_price"], "avg_fill_price"),
        role=row["role"],
        parent_client_order_id=row["parent_client_order_id"],
        oco_sibling_id=row["oco_sibling_id"],
        replaces_client_order_id=row["replaces_client_order_id"],
        replaced_by_client_order_id=row["replaced_by_client_order_id"],
        trigger_price=_dec_opt(row["trigger_price"], "trigger_price"),
        take_profit_price=_dec_opt(row["take_profit_price"], "take_profit_price"),
        updated_at=_ts(row["updated_at"], "updated_at"),
        metadata=_json_obj(row["metadata"], "metadata"),
    )


def _json_obj(value: str | None, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is not valid JSON: {value!r}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must decode to a JSON object: {value!r}")
    return parsed


def _row_to_fill(row: sqlite3.Row) -> FillRecord:
    return FillRecord(
        fill_id=row["fill_id"],
        instrument=row["instrument"],
        side=row["side"],
        quantity=_dec(row["quantity"], "quantity"),
        price=_dec(row["price"], "price"),
        fee=_dec(row["fee"], "fee"),
        timestamp=_ts(row["timestamp"], "timestamp"),
        applied_at=_ts(row["applied_at"], "applied_at"),
    )


def _row_to_reservation(row: sqlite3.Row) -> ReservationRecord:
    return ReservationRecord(
        decision_id=row["decision_id"],
        side=row["side"],
        abs_notional=_dec(row["abs_notional"], "abs_notional"),
        signed_notional=_dec(row["signed_notional"], "signed_notional"),
        updated_at=_ts(row["updated_at"], "updated_at"),
    )


def _row_to_reduce_only_reservation(row: sqlite3.Row) -> ReduceOnlyReservationRecord:
    return ReduceOnlyReservationRecord(
        decision_id=row["decision_id"],
        instrument=row["instrument"],
        quantity=_dec(row["quantity"], "quantity"),
        updated_at=_ts(row["updated_at"], "updated_at"),
    )


def _row_to_portfolio_state(row: sqlite3.Row) -> PortfolioStateRecord:
    return PortfolioStateRecord(
        starting_balance=_dec(row["starting_balance"], "starting_balance"),
        realized_pnl=_dec(row["realized_pnl"], "realized_pnl"),
        fees=_dec(row["fees"], "fees"),
        updated_at=_ts(row["updated_at"], "updated_at"),
    )


def _row_to_reconciliation_state(row: sqlite3.Row) -> ReconciliationStateRecord:
    return ReconciliationStateRecord(
        mode=row["mode"],
        reconciled=bool(row["reconciled"]),
        mismatch_reason=row["mismatch_reason"],
        last_reconciled_at=_ts_opt(row["last_reconciled_at"], "last_reconciled_at"),
        updated_at=_ts(row["updated_at"], "updated_at"),
        state=row["reconciliation_state"],
        source=row["reconciliation_source"],
    )


def _row_to_halt_state(row: sqlite3.Row) -> HaltStateRecord:
    return HaltStateRecord(
        halted=bool(row["halted"]),
        reason=row["reason"],
        updated_at=_ts(row["updated_at"], "updated_at"),
    )
