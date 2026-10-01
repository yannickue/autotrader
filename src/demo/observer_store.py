# ruff: noqa: E501
"""Separate SQLite store of the Market Structure Observer (OBSERVATION ONLY, DEFAULT OFF): ``<artifacts_dir>/observer.sqlite``.

Why a file of its own (Lane H): the live demo DB runs ``synchronous=FULL`` and holds the order / risk / intent state; the observer writes one ~5 KB row per
opportunity (rejected ones included). Observer I/O must not contend with, grow, or change that database, so:

* the live ``DemoStore`` schema is byte-identical to before the observer existed (no observer table, no trigger);
* this store is opened LAZILY, only when the observer flag is on and the first record is written (never in flatten-only / flag-off runs);
* its own connection, WAL, ``synchronous=NORMAL`` (a crash can lose the last observer rows, never the live state; the runner re-observes opportunities
  that were not yet committed and the insert-once key makes a replay idempotent);
* ONE transaction per runner cycle (``record_many``), not one per record;
* the per-definition constants (observer / schema version, group versions, the six 64-char definition hashes, the config hash, the feature column
  names) are stored ONCE in ``definitions`` and referenced per row by ``definition_id``; a row only holds the feature VALUES (JSON array, same order
  as the definition's column list) and the per-event meta. ``list_rows`` joins them back so the flat rows carry exactly the columns of
  ``market_observer.schema.ObserverRecord.to_row()`` (= the offline backfill's table; the parquet export is unchanged).

Rows are immutable (UPDATE / DELETE aborted by trigger) and insert-once by ``record_key`` (opportunity id, else event id). A re-insert of an identical
record is a no-op. The AUDIT-only meta fields (``market_observer.observer.AUDIT_META_KEYS``: loaded history, which depends on process start) are ignored by
the identity comparison, so the same opportunity re-observed after a restart is idempotent; any other difference raises ``ImmutableRecordError``.

This module is NOT imported by execution / risk / exits / adapters (static test) and nothing here is read by the engine or the runner's decisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from demo.store import ImmutableRecordError
from market_observer.observer import AUDIT_META_KEYS

OBSERVER_DB_NAME = "observer.sqlite"
OBSERVER_STORE_SCHEMA = "observer-store-1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS definitions (
    definition_id TEXT PRIMARY KEY,
    observer_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    versions_json TEXT NOT NULL,
    hashes_json TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observer_records (
    record_key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    opportunity_id TEXT,
    market TEXT NOT NULL,
    family TEXT,
    variant TEXT,
    direction INTEGER,
    is_control INTEGER NOT NULL,
    control_of TEXT,
    decision_ts_ns INTEGER NOT NULL,
    definition_id TEXT NOT NULL REFERENCES definitions(definition_id),
    warmup_ok INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    meta_json TEXT NOT NULL,
    created_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observer_records_event ON observer_records(event_id);
CREATE INDEX IF NOT EXISTS idx_observer_records_market_ts ON observer_records(market, decision_ts_ns);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_TRIGGERS = "\n".join(
    f"CREATE TRIGGER IF NOT EXISTS trg_{t}_no_{op.lower()} BEFORE {op} ON {t} BEGIN SELECT RAISE(ABORT, '{t} rows are immutable'); END;"
    for t in ("observer_records", "definitions") for op in ("UPDATE", "DELETE")
)
_COMPARED = (
    "record_key", "event_id", "opportunity_id", "market", "family", "variant", "direction", "is_control", "control_of", "decision_ts_ns",
    "definition_id", "warmup_ok", "values_json",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def is_definition_meta(key: str) -> bool:
    """Meta keys that are constants of the observer DEFINITION (stored once in ``definitions``): the per-group definition hashes and the config hash."""
    return key == "config_hash" or key.startswith("hash_")


def split_record(rec: Any) -> dict[str, Any]:
    """Duck-typed ``ObserverRecord`` -> the pieces that are stored (definition part, per-row part). Pure."""
    meta = dict(rec.meta)
    cols = rec.features.columns
    names = sorted(cols)
    hashes = {k: v for k, v in meta.items() if is_definition_meta(k)}
    definition = {
        "observer_version": rec.observer_version, "schema_version": rec.schema_version, "status": rec.status,
        "versions": dict(rec.features.versions), "hashes": hashes, "columns": names,
    }
    did = hashlib.sha256(_dumps(definition).encode()).hexdigest()[:20]
    row_meta = {k: v for k, v in meta.items() if not is_definition_meta(k)}
    return {
        "definition_id": did, "definition": definition, "row_meta": row_meta,
        "values": [cols[n] for n in names],
        "key": str(meta.get("opportunity_id") or rec.event_id),
    }


class ObserverStore:
    """See the module docstring. ``path`` is the file (``<artifacts_dir>/observer.sqlite``); it is created lazily on first use."""

    def __init__(self, path: str | os.PathLike[str], clock: Callable[[], str] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or _now_iso
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._known_definitions: set[str] = set()

    # ---- plumbing ----------------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(_SCHEMA)
            conn.executescript(_TRIGGERS)
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)", (OBSERVER_STORE_SCHEMA,))
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> ObserverStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- writes ------------------------------------------------------------------------------
    def record_many(self, records: Sequence[Any]) -> tuple[int, list[Exception]]:
        """Insert-once a batch in ONE transaction. Returns ``(rows written, per-record ImmutableRecordErrors)``. A differing record for an existing key
        is reported (not raised) and the rest of the batch still commits; any other database error rolls the whole batch back and is raised."""
        if not records:
            return 0, []
        parts = [split_record(r) for r in records]  # pure; a malformed record raises before the transaction starts
        written = 0
        conflicts: list[Exception] = []
        with self._lock:
            c = self._db()
            c.execute("BEGIN IMMEDIATE")
            try:
                now = self._clock()
                for rec, p in zip(records, parts, strict=True):
                    self._ensure_definition(c, p, now)
                    try:
                        written += 1 if self._insert(c, rec, p, now) else 0
                    except ImmutableRecordError as exc:
                        conflicts.append(exc)
            except BaseException:
                c.execute("ROLLBACK")
                self._known_definitions.clear()
                raise
            else:
                c.execute("COMMIT")
        return written, conflicts

    def record(self, rec: Any) -> bool:
        """Single-record convenience (one transaction). True = new row, False = identical re-insert; a differing record raises ``ImmutableRecordError``."""
        written, conflicts = self.record_many([rec])
        if conflicts:
            raise conflicts[0]
        return written == 1

    def _ensure_definition(self, c: sqlite3.Connection, p: dict[str, Any], now: str) -> None:
        did = p["definition_id"]
        if did in self._known_definitions:
            return
        d = p["definition"]
        c.execute(
            "INSERT OR IGNORE INTO definitions(definition_id,observer_version,schema_version,status,versions_json,hashes_json,columns_json,created_utc) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (did, d["observer_version"], d["schema_version"], d["status"], _dumps(d["versions"]), _dumps(d["hashes"]), _dumps(d["columns"]), now),
        )
        self._known_definitions.add(did)

    def _insert(self, c: sqlite3.Connection, rec: Any, p: dict[str, Any], now: str) -> bool:
        meta = p["row_meta"]
        payload = (
            p["key"], rec.event_id, meta.get("opportunity_id"), rec.market, rec.family, rec.variant, rec.direction, int(bool(rec.is_control)), rec.control_of,
            int(rec.features.decision_ts_ns), p["definition_id"], int(bool(meta.get("warmup_ok"))), _dumps(p["values"]),
        )
        cur = c.execute(
            "INSERT OR IGNORE INTO observer_records(record_key,event_id,opportunity_id,market,family,variant,direction,is_control,control_of,"
            "decision_ts_ns,definition_id,warmup_ok,values_json,meta_json,created_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*payload, _dumps(meta), now),
        )
        if cur.rowcount == 1:
            return True
        old = c.execute(f"SELECT {','.join(_COMPARED)}, meta_json FROM observer_records WHERE record_key=?", (p["key"],)).fetchone()
        if tuple(old)[: len(_COMPARED)] != payload:
            raise ImmutableRecordError(f"observer_records {p['key']}: different content for the same key")
        old_meta = {k: v for k, v in json.loads(old["meta_json"]).items() if k not in AUDIT_META_KEYS}
        new_meta = {k: v for k, v in json.loads(_dumps(meta)).items() if k not in AUDIT_META_KEYS}  # same JSON normalisation as the stored side
        if old_meta != new_meta:
            raise ImmutableRecordError(f"observer_records {p['key']}: different meta for the same key")
        return False  # identical (audit-only fields may differ: a replay after a restart)

    # ---- reads -------------------------------------------------------------------------------
    def count(self) -> int:
        if not self.path.exists() and self._conn is None:
            return 0
        with self._lock:
            return int(self._db().execute("SELECT COUNT(*) n FROM observer_records").fetchone()["n"])

    def list_rows(self, market: str | None = None) -> list[dict[str, Any]]:
        """Flat rows with the SAME column names as ``ObserverRecord.to_row()`` (= the offline backfill's table), ordered by (decision_ts_ns, key)."""
        if not self.path.exists() and self._conn is None:
            return []
        sql = (
            "SELECT r.*, d.observer_version, d.schema_version, d.status, d.versions_json, d.hashes_json, d.columns_json FROM observer_records r "
            "JOIN definitions d ON d.definition_id = r.definition_id" + (" WHERE r.market=?" if market else "") + " ORDER BY r.decision_ts_ns, r.record_key"
        )
        with self._lock:
            rows = self._db().execute(sql, (market,) if market else ()).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            row: dict[str, Any] = {
                "event_id": r["event_id"], "market": r["market"], "family": r["family"], "variant": r["variant"], "direction": r["direction"],
                "is_control": bool(r["is_control"]), "control_of": r["control_of"], "decision_ts_ns": r["decision_ts_ns"],
                "observer_version": r["observer_version"], "schema_version": r["schema_version"], "status": r["status"],
            }
            row.update({f"v_{g}": v for g, v in sorted(json.loads(r["versions_json"]).items())})
            row.update(dict(zip(json.loads(r["columns_json"]), json.loads(r["values_json"]), strict=True)))
            meta = json.loads(r["meta_json"])
            meta.update(json.loads(r["hashes_json"]))
            row.update({f"m_{k}": v for k, v in meta.items()})
            out.append(row)
        return out

    def definitions(self) -> list[dict[str, Any]]:
        if not self.path.exists() and self._conn is None:
            return []
        with self._lock:
            return [dict(r) for r in self._db().execute("SELECT * FROM definitions ORDER BY created_utc, definition_id").fetchall()]

