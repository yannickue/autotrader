"""Durable intent registry of the DEMO stack (restart adoption + exactly-once bookkeeping).

Tiny SQLite file next to the MT5 adapter state. It is NOT trading state: the broker owns positions
and Nautilus owns orders. It only remembers, per ``intent_id``, what the stack decided and which
market/stop/target/forced-flat the position was opened with, so that after a restart an open
broker position can be re-adopted by intent id (and its exit attributed correctly) and a repeated
intent id can never open a second position.

Status flow: ``ACCEPTED`` (sized, not yet sent) -> ``SENT`` (handed to the adapter) -> ``OPEN``
(position confirmed at the broker) -> ``CLOSED``; terminal alternatives ``REJECTED``, ``SHADOW``
(dry run, never sent) and ``IN_DOUBT`` (send outcome unknown; resolved from broker truth).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

ACCEPTED, SENT, OPEN, CLOSED = "ACCEPTED", "SENT", "OPEN", "CLOSED"
REJECTED, SHADOW, IN_DOUBT = "REJECTED", "SHADOW", "IN_DOUBT"
LIVE_STATUSES = (ACCEPTED, SENT, OPEN, IN_DOUBT)


@dataclass(frozen=True, slots=True)
class IntentRow:
    intent_id: str
    client_order_id: str
    market: str
    direction: int
    stop: str
    target: str | None
    forced_flat_utc: str | None
    status: str
    position_ticket: int | None
    exit_hint: str | None
    risk_money: str | None
    created_utc: str
    detail: str | None
    context: str | None = None  # JSON: family, quality inputs, sizing summary (tranche ledger data)


_COLUMNS = (
    "intent_id, client_order_id, market, direction, stop, target, forced_flat_utc, status, "
    "position_ticket, exit_hint, risk_money, created_utc, detail, context"
)


class StackRegistry:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.RLock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    market TEXT NOT NULL,
                    direction INTEGER NOT NULL,
                    stop TEXT NOT NULL,
                    target TEXT,
                    forced_flat_utc TEXT,
                    status TEXT NOT NULL,
                    position_ticket INTEGER,
                    exit_hint TEXT,
                    risk_money TEXT,
                    created_utc TEXT NOT NULL,
                    detail TEXT,
                    context TEXT
                )"""
            )
            columns = {r[1] for r in self._db.execute("PRAGMA table_info(intents)").fetchall()}
            if "context" not in columns:  # registry created before the tranche-ledger context
                self._db.execute("ALTER TABLE intents ADD COLUMN context TEXT")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- intents ---------------------------------------------------------------------------------

    def insert(
        self,
        *,
        intent_id: str,
        client_order_id: str,
        market: str,
        direction: int,
        stop: str,
        target: str | None,
        forced_flat_utc: str | None,
        status: str,
        created_utc: str,
        risk_money: str | None = None,
        detail: str | None = None,
        context: str | None = None,
    ) -> bool:
        """False if the intent id (or its client order id) is already registered."""
        with self._lock:
            try:
                self._db.execute(
                    f"INSERT INTO intents ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        intent_id, client_order_id, market, direction, stop, target,
                        forced_flat_utc, status, None, None, risk_money, created_utc, detail,
                        context,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    @staticmethod
    def _row(raw: tuple | None) -> IntentRow | None:
        return None if raw is None else IntentRow(*raw)

    def get(self, intent_id: str) -> IntentRow | None:
        with self._lock:
            cur = self._db.execute(
                f"SELECT {_COLUMNS} FROM intents WHERE intent_id=?", (intent_id,)
            )
            return self._row(cur.fetchone())

    def by_client_order_id(self, client_order_id: str) -> IntentRow | None:
        with self._lock:
            cur = self._db.execute(
                f"SELECT {_COLUMNS} FROM intents WHERE client_order_id=?", (client_order_id,)
            )
            return self._row(cur.fetchone())

    def with_status(self, *statuses: str) -> list[IntentRow]:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            cur = self._db.execute(
                f"SELECT {_COLUMNS} FROM intents WHERE status IN ({marks}) ORDER BY created_utc",
                statuses,
            )
            return [IntentRow(*r) for r in cur.fetchall()]

    def open_for_market(self, market: str) -> list[IntentRow]:
        with self._lock:
            cur = self._db.execute(
                f"SELECT {_COLUMNS} FROM intents WHERE market=? AND status IN (?,?,?,?) "
                "ORDER BY created_utc",
                (market, *LIVE_STATUSES),
            )
            return [IntentRow(*r) for r in cur.fetchall()]

    def update(self, intent_id: str, **fields: object) -> None:
        allowed = {"status", "position_ticket", "exit_hint", "risk_money", "detail", "context"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown registry fields {unknown}")
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE intents SET {sets} WHERE intent_id=?", (*fields.values(), intent_id)
            )

    # -- meta ------------------------------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        with self._lock:
            cur = self._db.execute("SELECT v FROM meta WHERE k=?", (key,))
            row = cur.fetchone()
            return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )
