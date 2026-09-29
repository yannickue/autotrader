"""Restart-safe adapter tables: ClientOrderId <-> MT5 ticket map + ingested-deal set.

These are IDENTITY/DEDUPLICATION tables, not trading state: Nautilus owns
orders, fills, positions and PnL. Durability: every write commits before
returning (SQLite, single writer), so a crash never loses an acknowledged row.

Write-ahead intent: a row is inserted (status INTENT, with a short `token`
that travels in the MT5 order `comment`) BEFORE `order_send`. If the process
dies after the broker executed but before we saw the result, the token lets
restart reconciliation re-associate the broker order/deal with the original
ClientOrderId instead of treating it as an unknown external order.

Deal dedupe ordering rule: `mark_ingested` is called ONLY after the Nautilus
fill event was produced successfully. A crash/failure in between leaves the
deal un-marked, so the next pass ingests it again (never lost).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
TOKEN_PREFIX = "NT"


class StateStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OrderRow:
    client_order_id: str
    token: str
    strategy_id: str
    instrument_id: str
    kind: str  # MARKET | PROTECT_SL | PROTECT_TP | EXIT
    side: str
    quantity: str
    status: str  # INTENT | SENT | ACCEPTED | DONE | REJECTED | IN_DOUBT | CANCELED
    order_ticket: int | None
    position_ticket: int | None
    venue_order_id: str | None
    created_ns: int


_COLUMNS = (
    "client_order_id, token, strategy_id, instrument_id, kind, side, quantity, status, "
    "order_ticket, position_ticket, venue_order_id, created_ns"
)


class Mt5StateStore:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    # -- schema ------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._tx() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
            cur.execute(
                """CREATE TABLE IF NOT EXISTS order_map (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL UNIQUE,
                    token TEXT NOT NULL UNIQUE,
                    strategy_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_ticket INTEGER,
                    position_ticket INTEGER,
                    venue_order_id TEXT,
                    created_ns INTEGER NOT NULL)"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS ix_order_ticket ON order_map(order_ticket)")
            cur.execute(
                """CREATE TABLE IF NOT EXISTS deals_ingested (
                    deal_ticket INTEGER PRIMARY KEY,
                    order_ticket INTEGER,
                    ingested_ns INTEGER NOT NULL)"""
            )
            row = cur.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
            if row is None:
                cur.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row[0]) != SCHEMA_VERSION:
                raise StateStoreError(
                    f"unsupported state schema {row[0]} (expected {SCHEMA_VERSION})"
                )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- order map ---------------------------------------------------------

    def record_intent(
        self,
        *,
        client_order_id: str,
        strategy_id: str,
        instrument_id: str,
        kind: str,
        side: str,
        quantity: str,
        position_ticket: int | None = None,
        created_ns: int | None = None,
    ) -> str:
        """Write-ahead record; returns the MT5 `comment` token. Idempotent per ClientOrderId."""
        existing = self.by_client_order_id(client_order_id)
        if existing is not None:
            return existing.token
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO order_map (client_order_id, token, strategy_id, instrument_id, kind, "
                "side, quantity, status, position_ticket, created_ns) "
                "VALUES (?, 'PENDING', ?, ?, ?, ?, ?, 'INTENT', ?, ?)",
                (
                    client_order_id,
                    strategy_id,
                    instrument_id,
                    kind,
                    side,
                    quantity,
                    position_ticket,
                    created_ns or time.time_ns(),
                ),
            )
            token = f"{TOKEN_PREFIX}{cur.lastrowid}"
            cur.execute("UPDATE order_map SET token=? WHERE seq=?", (token, cur.lastrowid))
        return token

    def update_order(
        self,
        client_order_id: str,
        *,
        status: str | None = None,
        order_ticket: int | None = None,
        position_ticket: int | None = None,
        venue_order_id: str | None = None,
    ) -> None:
        sets: list[str] = []
        args: list[object] = []
        for column, value in (
            ("status", status),
            ("order_ticket", order_ticket),
            ("position_ticket", position_ticket),
            ("venue_order_id", venue_order_id),
        ):
            if value is not None:
                sets.append(f"{column}=?")
                args.append(value)
        if not sets:
            return
        args.append(client_order_id)
        with self._tx() as cur:
            cur.execute(f"UPDATE order_map SET {', '.join(sets)} WHERE client_order_id=?", args)
            if cur.rowcount != 1:
                raise StateStoreError(f"unknown client_order_id {client_order_id}")

    @staticmethod
    def _row(raw: tuple | None) -> OrderRow | None:
        return None if raw is None else OrderRow(*raw)

    def by_client_order_id(self, client_order_id: str) -> OrderRow | None:
        cur = self._conn.execute(
            f"SELECT {_COLUMNS} FROM order_map WHERE client_order_id=?", (client_order_id,)
        )
        return self._row(cur.fetchone())

    def by_order_ticket(self, order_ticket: int) -> OrderRow | None:
        cur = self._conn.execute(
            f"SELECT {_COLUMNS} FROM order_map WHERE order_ticket=?", (order_ticket,)
        )
        return self._row(cur.fetchone())

    def by_token(self, token: str) -> OrderRow | None:
        cur = self._conn.execute(f"SELECT {_COLUMNS} FROM order_map WHERE token=?", (token,))
        return self._row(cur.fetchone())

    def unresolved(self) -> list[OrderRow]:
        """Orders whose broker outcome we have not confirmed (need reconciliation)."""
        cur = self._conn.execute(
            f"SELECT {_COLUMNS} FROM order_map WHERE status IN ('INTENT','SENT','IN_DOUBT') "
            "ORDER BY seq"
        )
        return [OrderRow(*r) for r in cur.fetchall()]

    def all_orders(self) -> list[OrderRow]:
        cur = self._conn.execute(f"SELECT {_COLUMNS} FROM order_map ORDER BY seq")
        return [OrderRow(*r) for r in cur.fetchall()]

    # -- deal dedupe -------------------------------------------------------

    def is_ingested(self, deal_ticket: int) -> bool:
        cur = self._conn.execute("SELECT 1 FROM deals_ingested WHERE deal_ticket=?", (deal_ticket,))
        return cur.fetchone() is not None

    def mark_ingested(self, deal_ticket: int, order_ticket: int | None) -> bool:
        """Returns True if newly marked, False if it was already present."""
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO deals_ingested VALUES (?, ?, ?)",
                (deal_ticket, order_ticket, time.time_ns()),
            )
            return cur.rowcount == 1

    def ingested_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM deals_ingested").fetchone()[0])
