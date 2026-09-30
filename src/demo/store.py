# ruff: noqa: E501
"""Crash-safe SQLite lifecycle store for the DEMO trader (its OWN db, not `persistence.StateStore`).

Durability model (same philosophy as `persistence.store`): single writer per file, WAL journal,
`synchronous=FULL`, every write is one `BEGIN IMMEDIATE ... COMMIT` that has committed before the
method returns. "write -> process dies -> reopen" never loses an acknowledged write. No claim of
multi-process writer coordination; readers in other processes are fine (WAL).

Invariants enforced here (and by SQL triggers so raw SQL cannot bypass them):
  * snapshots / decisions / counterfactuals / outcomes / shadow_predictions are insert-once and
    IMMUTABLE (UPDATE and DELETE are aborted by triggers). Re-inserting byte-identical content is an
    idempotent no-op (returns False); differing content raises `ImmutableRecordError`.
  * A decision requires an existing snapshot (snapshot is persisted BEFORE the decision).
  * One intent per opportunity (`UNIQUE(opportunity_id)`), one row per `intent_id`; only an
    ACCEPTED decision may produce an intent. -> after a restart the same opportunity can never yield
    a second intent/order.
  * Intent lifecycle is a monotonic state machine (see `_ALLOWED`); illegal/backward transitions raise
    `IllegalTransition`; repeating the current state is an idempotent no-op returning False.
  * Shadow (challenger) predictions must be recorded BEFORE any outcome/counterfactual for the
    opportunity exists, and `created_utc` must not be later than the decision time (if a decision
    exists). Insert-order rule: prediction rows can only be added while the label is unknown.
  * `phase` (DISCOVERY | FROZEN) is stored and indexed on every table.

Cost sign convention: `ExecutionRecord.fees` / `.swap` are stored exactly as given (MT5 deal
convention: negative = cost). Reports treat `-(fees + swap)` as cost.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from demo.contracts import (
    PHASES,
    ClockCheck,
    CounterfactualLabel,
    Decision,
    ExecutionRecord,
    MarketState,
    OpportunitySnapshot,
    OutcomeRecord,
    RiskRecord,
    TradeGeometry,
    TradeIntent,
    TradeLearningRecord,
    stable_hash,
)

STORE_SCHEMA_VERSION = "demo-store-1"

# ---- lifecycle -------------------------------------------------------------------------------
PLANNED = "PLANNED"
RISK_APPROVED = "RISK_APPROVED"
RISK_REJECTED = "RISK_REJECTED"
SENT = "SENT"
FILLED = "FILLED"
PROTECTED = "PROTECTED"
CLOSED = "CLOSED"
SEND_FAILED = "SEND_FAILED"
CANCELLED = "CANCELLED"
# Non-terminal: the order was sent but its outcome is UNKNOWN (stack reported ``order_outcome_unknown``).
# The broker may hold a fill: a late Fill / PositionClosed / broker truth resolves it to
# SENT->FILLED->PROTECTED->CLOSED (or CANCELLED when the broker has no record). Added backward compatibly.
IN_DOUBT = "IN_DOUBT"

INTENT_STATES: tuple[str, ...] = (
    PLANNED,
    RISK_APPROVED,
    RISK_REJECTED,
    SENT,
    FILLED,
    PROTECTED,
    CLOSED,
    SEND_FAILED,
    CANCELLED,
    IN_DOUBT,
)
TERMINAL_STATES = frozenset({RISK_REJECTED, CLOSED, SEND_FAILED, CANCELLED})
# States for which the broker may hold an order/position we must reconcile after a restart.
OPEN_STATES: tuple[str, ...] = (SENT, IN_DOUBT, FILLED, PROTECTED)

_ALLOWED: dict[str, frozenset[str]] = {
    PLANNED: frozenset({RISK_APPROVED, RISK_REJECTED, CANCELLED}),
    RISK_APPROVED: frozenset({SENT, SEND_FAILED, CANCELLED, IN_DOUBT}),
    RISK_REJECTED: frozenset(),
    SENT: frozenset({FILLED, CANCELLED, IN_DOUBT}),
    IN_DOUBT: frozenset({SENT, FILLED, PROTECTED, CLOSED, CANCELLED}),
    # FILLED -> CLOSED: broker/stop/manual close before protection was ever confirmed.
    FILLED: frozenset({PROTECTED, CLOSED}),
    PROTECTED: frozenset({CLOSED}),
    CLOSED: frozenset(),
    SEND_FAILED: frozenset(),
    CANCELLED: frozenset(),
}


class DemoStoreError(Exception):
    pass


class ImmutableRecordError(DemoStoreError):
    pass


class IllegalTransition(DemoStoreError):
    pass


class DuplicateIntentError(DemoStoreError):
    pass


class MissingParentError(DemoStoreError):
    pass


class LeakageError(DemoStoreError):
    pass


class SchemaMismatch(DemoStoreError):
    pass


def client_order_id_for(intent_id: str) -> str:
    """Deterministic broker client order id derived from the intent id (<= 27 chars, stable)."""
    return "dt-" + stable_hash("client-order", intent_id, n=24)


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---- record reconstruction (contracts.from_dict does not rebuild nested dataclasses) ------------
def snapshot_from_dict(d: dict[str, Any]) -> OpportunitySnapshot:
    ms = dict(d["market_state"])
    ms["clock"] = ClockCheck.from_dict(ms["clock"])
    return OpportunitySnapshot.from_dict(
        {
            **d,
            "geometry": TradeGeometry.from_dict(d["geometry"]),
            "market_state": MarketState.from_dict(ms),
        }
    )


def decision_from_dict(d: dict[str, Any]) -> Decision:
    return Decision.from_dict({**d, "reasons": tuple(d.get("reasons", ()))})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS seen (
    opportunity_id TEXT PRIMARY KEY,
    first_seen_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    opportunity_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    market TEXT NOT NULL,
    signal_ts TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_phase ON snapshots(phase, market, signal_ts);

CREATE TABLE IF NOT EXISTS decisions (
    opportunity_id TEXT PRIMARY KEY REFERENCES snapshots(opportunity_id),
    phase TEXT NOT NULL,
    decided_utc TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    shadow TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_phase ON decisions(phase, accepted);

CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL UNIQUE REFERENCES decisions(opportunity_id),
    phase TEXT NOT NULL,
    market TEXT NOT NULL,
    state TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_intents_phase ON intents(phase, state);

CREATE TABLE IF NOT EXISTS intent_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    phase TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    ts TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_intent_events ON intent_events(intent_id, seq);

CREATE TABLE IF NOT EXISTS risk_records (
    intent_id TEXT PRIMARY KEY REFERENCES intents(intent_id),
    phase TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_risk_phase ON risk_records(phase);

-- Lane I additions (backward compatible: new tables only). Full machine-readable risk/sizing detail of
-- the stack decision (ACCEPTED and REJECTED-after-decision, incl. exact reject code + gate class) and the
-- per-fill transaction-cost analysis. Keyed by intent_id; contracts.py records are unchanged.
CREATE TABLE IF NOT EXISTS risk_detail (
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    kind TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    reject_code TEXT,
    gate_class TEXT,
    recorded_utc TEXT NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (intent_id, kind)
);
CREATE INDEX IF NOT EXISTS ix_risk_detail_phase ON risk_detail(phase, kind);

CREATE TABLE IF NOT EXISTS tca_records (
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    stage TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    recorded_utc TEXT NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (intent_id, stage)
);
CREATE INDEX IF NOT EXISTS ix_tca_phase ON tca_records(phase);

CREATE TABLE IF NOT EXISTS execution_records (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    phase TEXT NOT NULL,
    recorded_utc TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_exec ON execution_records(intent_id, seq);
CREATE INDEX IF NOT EXISTS ix_exec_phase ON execution_records(phase);

CREATE TABLE IF NOT EXISTS outcomes (
    intent_id TEXT PRIMARY KEY REFERENCES intents(intent_id),
    opportunity_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    closed_utc TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outcomes_phase ON outcomes(phase, closed_utc);

CREATE TABLE IF NOT EXISTS counterfactuals (
    opportunity_id TEXT PRIMARY KEY REFERENCES decisions(opportunity_id),
    phase TEXT NOT NULL,
    labelled_utc TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cf_phase ON counterfactuals(phase);

CREATE TABLE IF NOT EXISTS shadow_predictions (
    opportunity_id TEXT NOT NULL REFERENCES snapshots(opportunity_id),
    model_name TEXT NOT NULL,
    phase TEXT NOT NULL,
    prediction TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, model_name)
);
CREATE INDEX IF NOT EXISTS ix_shadow_phase ON shadow_predictions(phase);

-- Lane R2 additions (backward compatible: new tables only; an old DB simply has them created empty).
-- Closed-bar catch-up pointer: newest closed bar (by its CLOSE) whose evaluation is complete, per
-- (market, timeframe). Upserted atomically and monotonic; survives restarts.
CREATE TABLE IF NOT EXISTS bar_pointers (
    market TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    last_bar_close_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (market, timeframe)
);
-- A bar whose evaluation failed twice: auditable, never silent.
CREATE TABLE IF NOT EXISTS scan_errors (
    market TEXT NOT NULL,
    bar_close_utc TEXT NOT NULL,
    error TEXT NOT NULL,
    recorded_utc TEXT NOT NULL,
    PRIMARY KEY (market, bar_close_utc)
);
-- Trade type / censoring tag per intent. No row = legacy = STRATEGY, not censored.
CREATE TABLE IF NOT EXISTS trade_tags (
    intent_id TEXT PRIMARY KEY REFERENCES intents(intent_id),
    trade_type TEXT NOT NULL,
    censored INTEGER NOT NULL,
    exit_class TEXT,
    source TEXT,
    recorded_utc TEXT NOT NULL,
    json TEXT NOT NULL
);
-- Extra outcome analytics (time to 0.25R/0.5R/1R, giveback, ...) without touching OutcomeRecord.
CREATE TABLE IF NOT EXISTS outcome_extra (
    intent_id TEXT PRIMARY KEY REFERENCES intents(intent_id),
    phase TEXT NOT NULL,
    recorded_utc TEXT NOT NULL,
    json TEXT NOT NULL
);
"""

_IMMUTABLE_TABLES = (
    "snapshots", "decisions", "counterfactuals", "outcomes", "shadow_predictions",
    "risk_detail", "tca_records",
)
RISK_DETAIL_KINDS = ("ACCEPTED", "REJECTED")
TCA_STAGES = ("ENTRY", "EXIT")


def _triggers() -> str:
    parts = []
    for t in _IMMUTABLE_TABLES:
        for op in ("UPDATE", "DELETE"):
            parts.append(
                f"CREATE TRIGGER IF NOT EXISTS trg_{t}_no_{op.lower()} BEFORE {op} ON {t} "
                f"BEGIN SELECT RAISE(ABORT, '{t} rows are immutable'); END;"
            )
    for t in ("intent_events", "execution_records"):
        for op in ("UPDATE", "DELETE"):
            parts.append(
                f"CREATE TRIGGER IF NOT EXISTS trg_{t}_no_{op.lower()} BEFORE {op} ON {t} "
                f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
            )
    return "\n".join(parts)


def _check_phase(phase: str | None) -> None:
    if phase is not None and phase not in PHASES:
        raise ValueError(f"bad phase {phase!r}")


class DemoStore:
    """See module docstring. `clock` (callable returning an ISO-UTC string) is injectable for tests."""

    def __init__(
        self, path: str | os.PathLike[str], clock: Callable[[], str] | None = None
    ) -> None:
        self.path = Path(path)
        self._clock = clock or _now_iso
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, timeout=30.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.executescript(_triggers())
        with self._tx():
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                    (STORE_SCHEMA_VERSION,),
                )
            elif row["value"] != STORE_SCHEMA_VERSION:
                raise SchemaMismatch(f"store schema {row['value']!r} != {STORE_SCHEMA_VERSION!r}")

    # ---- plumbing ----------------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> DemoStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args).fetchall())

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    # ---- meta --------------------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a mutable meta value (atomic)."""
        with self._tx() as c:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---- closed-bar catch-up pointer ----------------------------------------------------------
    def get_bar_pointer(self, market: str, timeframe: str = "M5") -> str | None:
        """ISO UTC CLOSE of the newest fully processed closed bar of (market, timeframe), or None."""
        r = self._one(
            "SELECT last_bar_close_utc FROM bar_pointers WHERE market=? AND timeframe=?", (market, timeframe)
        )
        return None if r is None else r["last_bar_close_utc"]

    def set_bar_pointer(self, market: str, close_utc: str, timeframe: str = "M5") -> bool:
        """Atomic insert-or-advance; never moves backwards. True if the pointer changed."""
        new = parse_utc(close_utc)
        with self._tx() as c:
            r = c.execute(
                "SELECT last_bar_close_utc FROM bar_pointers WHERE market=? AND timeframe=?", (market, timeframe)
            ).fetchone()
            if r is not None and parse_utc(r["last_bar_close_utc"]) >= new:
                return False
            c.execute(
                "INSERT INTO bar_pointers(market,timeframe,last_bar_close_utc,updated_utc) VALUES(?,?,?,?) "
                "ON CONFLICT(market,timeframe) DO UPDATE SET last_bar_close_utc=excluded.last_bar_close_utc, "
                "updated_utc=excluded.updated_utc",
                (market, timeframe, new.isoformat(), self._clock()),
            )
            return True

    def record_scan_error(self, market: str, bar_close_utc: str, error: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO scan_errors(market,bar_close_utc,error,recorded_utc) VALUES(?,?,?,?)",
                (market, parse_utc(bar_close_utc).isoformat(), error[:500], self._clock()),
            )

    def scan_errors(self) -> list[dict[str, str]]:
        return [dict(r) for r in self._q("SELECT * FROM scan_errors ORDER BY bar_close_utc, market")]

    def bar_pointers(self) -> dict[tuple[str, str], str]:
        return {(r["market"], r["timeframe"]): r["last_bar_close_utc"] for r in self._q("SELECT * FROM bar_pointers")}

    def set_meta_once(self, key: str, value: str) -> bool:
        """Insert-once marker (e.g. 'milestone emitted'). Returns True only for the first writer."""
        with self._tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (key, value))
            return cur.rowcount == 1

    # ---- exact-once (SeenStore compatible) -----------------------------------------------------
    def seen(self, opportunity_id: str) -> bool:
        return (
            self._one(
                "SELECT 1 FROM seen WHERE opportunity_id=? UNION SELECT 1 FROM snapshots WHERE opportunity_id=?",
                (opportunity_id, opportunity_id),
            )
            is not None
        )

    def mark_seen(self, opportunity_id: str) -> bool:
        """Persist that the id was seen. Returns True if newly marked."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO seen(opportunity_id, first_seen_utc) VALUES(?,?)",
                (opportunity_id, self._clock()),
            )
            return cur.rowcount == 1

    # ---- snapshots -----------------------------------------------------------------------------
    def record_snapshot(self, snap: OpportunitySnapshot) -> bool:
        """Insert-once. True if newly stored; False if byte-identical already stored; else raises."""
        payload = snap.to_json()
        with self._tx() as c:
            row = c.execute(
                "SELECT json FROM snapshots WHERE opportunity_id=?", (snap.opportunity_id,)
            ).fetchone()
            if row is not None:
                if row["json"] == payload:
                    return False
                raise ImmutableRecordError(f"snapshot {snap.opportunity_id} is immutable")
            c.execute(
                "INSERT INTO snapshots(opportunity_id,phase,market,signal_ts,created_utc,json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    snap.opportunity_id,
                    snap.phase,
                    snap.market,
                    snap.signal_ts_utc,
                    snap.created_utc,
                    payload,
                ),
            )
            c.execute(
                "INSERT OR IGNORE INTO seen(opportunity_id, first_seen_utc) VALUES(?,?)",
                (snap.opportunity_id, self._clock()),
            )
            return True

    def get_snapshot(self, opportunity_id: str) -> OpportunitySnapshot | None:
        row = self._one("SELECT json FROM snapshots WHERE opportunity_id=?", (opportunity_id,))
        return None if row is None else snapshot_from_dict(json.loads(row["json"]))

    def list_snapshots(
        self, phase: str | None = None, market: str | None = None
    ) -> list[OpportunitySnapshot]:
        _check_phase(phase)
        sql, args = "SELECT json FROM snapshots WHERE 1=1", []
        if phase:
            sql += " AND phase=?"
            args.append(phase)
        if market:
            sql += " AND market=?"
            args.append(market)
        rows = self._q(sql + " ORDER BY signal_ts, opportunity_id", tuple(args))
        return [snapshot_from_dict(json.loads(r["json"])) for r in rows]

    # ---- decisions -----------------------------------------------------------------------------
    def record_decision(self, dec: Decision) -> bool:
        payload = dec.to_json()
        with self._tx() as c:
            snap = c.execute(
                "SELECT phase, created_utc FROM snapshots WHERE opportunity_id=?",
                (dec.opportunity_id,),
            ).fetchone()
            if snap is None:
                raise MissingParentError(
                    f"no snapshot for {dec.opportunity_id}: persist snapshot before decision"
                )
            if snap["phase"] != dec.phase:
                raise DemoStoreError("decision phase differs from snapshot phase")
            row = c.execute(
                "SELECT * FROM decisions WHERE opportunity_id=?", (dec.opportunity_id,)
            ).fetchone()
            if row is not None:
                same = (
                    bool(row["accepted"]) == dec.accepted
                    and row["policy_id"] == dec.policy_id
                    and row["decided_utc"] == dec.decided_utc
                    and json.loads(row["reasons"]) == list(dec.reasons)
                    and json.loads(row["shadow"]) == json.loads(payload)["shadow"]
                )
                if same:
                    return False
                raise ImmutableRecordError(f"decision {dec.opportunity_id} is immutable")
            for s in c.execute(
                "SELECT model_name, created_utc FROM shadow_predictions WHERE opportunity_id=?",
                (dec.opportunity_id,),
            ).fetchall():
                if parse_utc(s["created_utc"]) > parse_utc(dec.decided_utc):
                    raise LeakageError(
                        f"shadow prediction {s['model_name']} is later than decision time"
                    )
            d = json.loads(payload)
            c.execute(
                "INSERT INTO decisions(opportunity_id,phase,decided_utc,accepted,reasons,policy_id,shadow) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    dec.opportunity_id,
                    dec.phase,
                    dec.decided_utc,
                    int(dec.accepted),
                    json.dumps(d["reasons"]),
                    dec.policy_id,
                    json.dumps(d["shadow"], sort_keys=True),
                ),
            )
            return True

    def _decision_from_row(self, r: sqlite3.Row) -> Decision:
        return Decision(
            opportunity_id=r["opportunity_id"],
            phase=r["phase"],
            decided_utc=r["decided_utc"],
            accepted=bool(r["accepted"]),
            reasons=tuple(json.loads(r["reasons"])),
            policy_id=r["policy_id"],
            shadow=json.loads(r["shadow"]),
        )

    def get_decision(self, opportunity_id: str) -> Decision | None:
        row = self._one("SELECT * FROM decisions WHERE opportunity_id=?", (opportunity_id,))
        return None if row is None else self._decision_from_row(row)

    def list_decisions(
        self, phase: str | None = None, accepted: bool | None = None
    ) -> list[Decision]:
        _check_phase(phase)
        sql, args = "SELECT * FROM decisions WHERE 1=1", []
        if phase:
            sql += " AND phase=?"
            args.append(phase)
        if accepted is not None:
            sql += " AND accepted=?"
            args.append(int(accepted))
        return [
            self._decision_from_row(r)
            for r in self._q(sql + " ORDER BY decided_utc, opportunity_id", tuple(args))
        ]

    # ---- shadow predictions --------------------------------------------------------------------
    def record_shadow_prediction(
        self,
        opportunity_id: str,
        model_name: str,
        prediction: dict[str, Any],
        created_utc: str | None = None,
    ) -> bool:
        """Insert-once per (opportunity, model). Must precede any outcome/counterfactual and must not
        be later than the decision time (when a decision exists). Violations raise `LeakageError`."""
        created = created_utc or self._clock()
        payload = json.dumps(prediction, sort_keys=True, default=str)
        with self._tx() as c:
            snap = c.execute(
                "SELECT phase FROM snapshots WHERE opportunity_id=?", (opportunity_id,)
            ).fetchone()
            if snap is None:
                raise MissingParentError(f"no snapshot for {opportunity_id}")
            row = c.execute(
                "SELECT prediction FROM shadow_predictions WHERE opportunity_id=? AND model_name=?",
                (opportunity_id, model_name),
            ).fetchone()
            if row is not None:
                if row["prediction"] == payload:
                    return False
                raise ImmutableRecordError("shadow prediction is immutable")
            if (
                c.execute(
                    "SELECT 1 FROM counterfactuals WHERE opportunity_id=?", (opportunity_id,)
                ).fetchone()
                or c.execute(
                    "SELECT 1 FROM outcomes WHERE opportunity_id=?", (opportunity_id,)
                ).fetchone()
            ):
                raise LeakageError("shadow prediction after outcome/label exists")
            dec = c.execute(
                "SELECT decided_utc FROM decisions WHERE opportunity_id=?", (opportunity_id,)
            ).fetchone()
            if dec is not None and parse_utc(created) > parse_utc(dec["decided_utc"]):
                raise LeakageError("shadow prediction created after decision time")
            c.execute(
                "INSERT INTO shadow_predictions(opportunity_id,model_name,phase,prediction,created_utc) "
                "VALUES(?,?,?,?,?)",
                (opportunity_id, model_name, snap["phase"], payload, created),
            )
            return True

    def list_shadow_predictions(
        self, opportunity_id: str | None = None, phase: str | None = None
    ) -> list[dict[str, Any]]:
        _check_phase(phase)
        sql, args = "SELECT * FROM shadow_predictions WHERE 1=1", []
        if opportunity_id:
            sql += " AND opportunity_id=?"
            args.append(opportunity_id)
        if phase:
            sql += " AND phase=?"
            args.append(phase)
        return [
            {
                "opportunity_id": r["opportunity_id"],
                "model_name": r["model_name"],
                "phase": r["phase"],
                "prediction": json.loads(r["prediction"]),
                "created_utc": r["created_utc"],
            }
            for r in self._q(sql + " ORDER BY opportunity_id, model_name", tuple(args))
        ]

    # ---- intents / lifecycle -------------------------------------------------------------------
    def record_intent(self, intent: TradeIntent) -> bool:
        """Create the PLANNED intent for an ACCEPTED decision. True if new, False if identical repeat.
        A different intent_id for an opportunity that already has one raises `DuplicateIntentError`."""
        payload = intent.to_json()
        with self._tx() as c:
            dec = c.execute(
                "SELECT accepted, phase FROM decisions WHERE opportunity_id=?",
                (intent.opportunity_id,),
            ).fetchone()
            if dec is None:
                raise MissingParentError("no decision for intent")
            if not dec["accepted"]:
                raise DemoStoreError("cannot create an intent for a rejected decision")
            if dec["phase"] != intent.phase:
                raise DemoStoreError("intent phase differs from decision phase")
            by_id = c.execute(
                "SELECT opportunity_id, json FROM intents WHERE intent_id=?", (intent.intent_id,)
            ).fetchone()
            if by_id is not None:
                if by_id["opportunity_id"] == intent.opportunity_id and by_id["json"] == payload:
                    return False
                raise ImmutableRecordError(
                    f"intent {intent.intent_id} exists with different content"
                )
            other = c.execute(
                "SELECT intent_id FROM intents WHERE opportunity_id=?", (intent.opportunity_id,)
            ).fetchone()
            if other is not None:
                raise DuplicateIntentError(
                    f"opportunity {intent.opportunity_id} already has intent {other['intent_id']}"
                )
            now = self._clock()
            c.execute(
                "INSERT INTO intents(intent_id,opportunity_id,phase,market,state,client_order_id,created_utc,"
                "updated_utc,json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    intent.intent_id,
                    intent.opportunity_id,
                    intent.phase,
                    intent.market,
                    PLANNED,
                    client_order_id_for(intent.intent_id),
                    now,
                    now,
                    payload,
                ),
            )
            c.execute(
                "INSERT INTO intent_events(intent_id,phase,from_state,to_state,ts,detail) VALUES(?,?,?,?,?,?)",
                (intent.intent_id, intent.phase, None, PLANNED, now, "{}"),
            )
            return True

    def transition(
        self,
        intent_id: str,
        new_state: str,
        *,
        detail: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> bool:
        """Advance the lifecycle. True if the state changed; False if already in `new_state`
        (idempotent no-op). Illegal/backward moves raise `IllegalTransition`."""
        if new_state not in INTENT_STATES:
            raise IllegalTransition(f"unknown state {new_state!r}")
        with self._tx() as c:
            row = c.execute(
                "SELECT state, phase FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None:
                raise MissingParentError(f"unknown intent {intent_id}")
            cur = row["state"]
            if cur == new_state:
                return False
            if new_state not in _ALLOWED[cur]:
                raise IllegalTransition(f"{cur} -> {new_state} not allowed for {intent_id}")
            now = ts or self._clock()
            c.execute(
                "UPDATE intents SET state=?, updated_utc=? WHERE intent_id=?",
                (new_state, now, intent_id),
            )
            c.execute(
                "INSERT INTO intent_events(intent_id,phase,from_state,to_state,ts,detail) VALUES(?,?,?,?,?,?)",
                (
                    intent_id,
                    row["phase"],
                    cur,
                    new_state,
                    now,
                    json.dumps(detail or {}, sort_keys=True, default=str),
                ),
            )
            return True

    def _intent_row_to_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        d = json.loads(r["json"])
        d.update(
            state=r["state"],
            client_order_id=r["client_order_id"],
            created_utc=r["created_utc"],
            updated_utc=r["updated_utc"],
        )
        return d

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM intents WHERE intent_id=?", (intent_id,))
        return None if r is None else self._intent_row_to_dict(r)

    def intent_for_opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM intents WHERE opportunity_id=?", (opportunity_id,))
        return None if r is None else self._intent_row_to_dict(r)

    def get_state(self, intent_id: str) -> str | None:
        r = self._one("SELECT state FROM intents WHERE intent_id=?", (intent_id,))
        return None if r is None else r["state"]

    def list_intents(
        self, phase: str | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        _check_phase(phase)
        sql, args = "SELECT * FROM intents WHERE 1=1", []
        if phase:
            sql += " AND phase=?"
            args.append(phase)
        if state:
            sql += " AND state=?"
            args.append(state)
        return [
            self._intent_row_to_dict(r)
            for r in self._q(sql + " ORDER BY created_utc, intent_id", tuple(args))
        ]

    def intent_events(self, intent_id: str) -> list[dict[str, Any]]:
        return [
            {
                "seq": r["seq"],
                "from_state": r["from_state"],
                "to_state": r["to_state"],
                "ts": r["ts"],
                "detail": json.loads(r["detail"]),
            }
            for r in self._q(
                "SELECT * FROM intent_events WHERE intent_id=? ORDER BY seq", (intent_id,)
            )
        ]

    def recover_open_intents(self) -> list[dict[str, Any]]:
        """Intents that may hold a live broker order/position (SENT/FILLED/PROTECTED, no CLOSED):
        the runner MUST reconcile these with the broker before any new exposure."""
        ph = ",".join("?" * len(OPEN_STATES))
        rows = self._q(
            f"SELECT * FROM intents WHERE state IN ({ph}) ORDER BY created_utc, intent_id",
            OPEN_STATES,
        )
        return [self._intent_row_to_dict(r) for r in rows]

    def unfinished_intents(self) -> list[dict[str, Any]]:
        """Non-terminal intents needing a restart decision: PLANNED/RISK_APPROVED (safe to re-drive or
        cancel: nothing was sent) plus the open ones."""
        ph = ",".join("?" * len(TERMINAL_STATES))
        rows = self._q(
            f"SELECT * FROM intents WHERE state NOT IN ({ph}) ORDER BY created_utc, intent_id",
            tuple(sorted(TERMINAL_STATES)),
        )
        return [self._intent_row_to_dict(r) for r in rows]

    def closed_without_outcome(self) -> list[str]:
        rows = self._q(
            "SELECT i.intent_id FROM intents i LEFT JOIN outcomes o ON o.intent_id=i.intent_id "
            "WHERE i.state=? AND o.intent_id IS NULL ORDER BY i.intent_id",
            (CLOSED,),
        )
        return [r["intent_id"] for r in rows]

    # ---- risk / execution / outcome ------------------------------------------------------------
    def _intent_phase(self, c: sqlite3.Connection, intent_id: str) -> str:
        r = c.execute("SELECT phase FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        if r is None:
            raise MissingParentError(f"unknown intent {intent_id}")
        return r["phase"]

    def record_risk(self, intent_id: str, risk: RiskRecord) -> bool:
        payload = risk.to_json()
        with self._tx() as c:
            phase = self._intent_phase(c, intent_id)
            row = c.execute(
                "SELECT json FROM risk_records WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is not None:
                if row["json"] == payload:
                    return False
                raise ImmutableRecordError("risk record is immutable")
            c.execute(
                "INSERT INTO risk_records(intent_id,phase,json) VALUES(?,?,?)",
                (intent_id, phase, payload),
            )
            return True

    def get_risk(self, intent_id: str) -> RiskRecord | None:
        r = self._one("SELECT json FROM risk_records WHERE intent_id=?", (intent_id,))
        return None if r is None else RiskRecord.from_dict(json.loads(r["json"]))

    # ---- Lane I: risk detail + TCA (new tables, contracts.py untouched) --------------------------------
    def record_risk_detail(
        self, intent_id: str, kind: str, detail: dict[str, Any] | None
    ) -> bool:
        """Insert-once full risk/sizing detail of the stack decision for ``intent_id``.

        ``kind`` is ``ACCEPTED`` (sized and approved) or ``REJECTED`` (SKIP / refused after the engine
        accepted). Both can exist for one intent (``[Accepted, Rejected]`` after-sizing refusals).
        ``reject_code`` / ``gate_class`` are also stored as indexed columns. Identical re-insert is a
        no-op (False); a differing payload raises ``ImmutableRecordError``."""
        if kind not in RISK_DETAIL_KINDS:
            raise ValueError(f"bad risk_detail kind {kind!r}")
        if detail is None:
            return False
        payload = json.dumps(detail, sort_keys=True, default=str)
        d = json.loads(payload)
        with self._tx() as c:
            phase = self._intent_phase(c, intent_id)
            opp = c.execute(
                "SELECT opportunity_id FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()["opportunity_id"]
            row = c.execute(
                "SELECT json FROM risk_detail WHERE intent_id=? AND kind=?", (intent_id, kind)
            ).fetchone()
            if row is not None:
                if row["json"] == payload:
                    return False
                raise ImmutableRecordError("risk_detail is immutable")
            c.execute(
                "INSERT INTO risk_detail(intent_id,kind,opportunity_id,phase,reject_code,gate_class,"
                "recorded_utc,json) VALUES(?,?,?,?,?,?,?,?)",
                (intent_id, kind, opp, phase, d.get("reject_code"), d.get("gate_reject_class"),
                 self._clock(), payload),
            )
            return True

    def get_risk_detail(self, intent_id: str, kind: str) -> dict[str, Any] | None:
        r = self._one("SELECT json FROM risk_detail WHERE intent_id=? AND kind=?", (intent_id, kind))
        return None if r is None else json.loads(r["json"])

    def list_risk_details(
        self, phase: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """Rows ``{intent_id, opportunity_id, kind, phase, reject_code, gate_class, detail}``."""
        _check_phase(phase)
        sql, args = "SELECT * FROM risk_detail WHERE 1=1", []
        if phase is not None:
            sql += " AND phase=?"
            args.append(phase)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        return [
            {
                "intent_id": r["intent_id"], "opportunity_id": r["opportunity_id"], "kind": r["kind"],
                "phase": r["phase"], "reject_code": r["reject_code"], "gate_class": r["gate_class"],
                "detail": json.loads(r["json"]),
            }
            for r in self._q(sql + " ORDER BY recorded_utc, intent_id, kind", tuple(args))
        ]

    def record_tca(self, intent_id: str, tca: dict[str, Any] | None, stage: str = "ENTRY") -> bool:
        """Insert-once per-fill transaction-cost analysis (price units unless noted).

        ``ENTRY`` is written at the fill (slippage, fill vs mid, fees in price units, cost, movement to
        cost, latencies), ``EXIT`` at the close (closing costs, exit slippage vs level, broker vs computed
        P&L). Identical re-insert is a no-op; a differing payload raises ``ImmutableRecordError``."""
        if stage not in TCA_STAGES:
            raise ValueError(f"bad tca stage {stage!r}")
        if not tca:
            return False
        payload = json.dumps(tca, sort_keys=True, default=str)
        with self._tx() as c:
            phase = self._intent_phase(c, intent_id)
            opp = c.execute(
                "SELECT opportunity_id FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()["opportunity_id"]
            row = c.execute(
                "SELECT json FROM tca_records WHERE intent_id=? AND stage=?", (intent_id, stage)
            ).fetchone()
            if row is not None:
                if row["json"] == payload:
                    return False
                raise ImmutableRecordError("tca record is immutable")
            c.execute(
                "INSERT INTO tca_records(intent_id,stage,opportunity_id,phase,recorded_utc,json) "
                "VALUES(?,?,?,?,?,?)",
                (intent_id, stage, opp, phase, self._clock(), payload),
            )
            return True

    def get_tca(self, intent_id: str, stage: str = "ENTRY") -> dict[str, Any] | None:
        r = self._one(
            "SELECT json FROM tca_records WHERE intent_id=? AND stage=?", (intent_id, stage)
        )
        return None if r is None else json.loads(r["json"])

    def list_tca(self, phase: str | None = None) -> list[dict[str, Any]]:
        _check_phase(phase)
        rows = (
            self._q("SELECT * FROM tca_records WHERE phase=? ORDER BY recorded_utc, stage", (phase,))
            if phase is not None
            else self._q("SELECT * FROM tca_records ORDER BY recorded_utc, stage")
        )
        return [
            {"intent_id": r["intent_id"], "opportunity_id": r["opportunity_id"],
             "phase": r["phase"], "stage": r["stage"], **json.loads(r["json"])}
            for r in rows
        ]

    def funnel_rows(self, phase: str | None = None) -> list[dict[str, Any]]:
        """One row per recorded opportunity with everything the rejection funnel needs, in ONE query:
        market, family, engine decision (accepted, reasons), intent state, stack reject code / gate class
        (from ``risk_detail`` REJECTED, falling back to ``risk_records.reject_reason``), the
        ``otherwise_valid`` flag of the stack decision and whether a risk approval exists."""
        _check_phase(phase)
        sql = (
            "SELECT s.opportunity_id AS opportunity_id, s.market AS market, "
            "json_extract(s.json,'$.signal.family') AS family, d.accepted AS accepted, "
            "d.reasons AS reasons, i.intent_id AS intent_id, i.state AS state, "
            "rd.reject_code AS rd_code, rd.gate_class AS rd_class, "
            "json_extract(rd.json,'$.otherwise_valid') AS otherwise_valid, "
            "json_extract(rk.json,'$.reject_reason') AS rk_reason, "
            "json_extract(rk.json,'$.approved') AS approved, "
            "EXISTS(SELECT 1 FROM intent_events e WHERE e.intent_id=i.intent_id AND e.to_state='CANCELLED' "
            "AND json_extract(e.detail,'$.reason')='shadow_dry_run') AS shadow_dry_run, "
            "(SELECT COUNT(*) FROM outcomes o WHERE o.intent_id=i.intent_id) AS has_outcome "
            "FROM snapshots s "
            "LEFT JOIN decisions d ON d.opportunity_id=s.opportunity_id "
            "LEFT JOIN intents i ON i.opportunity_id=s.opportunity_id "
            "LEFT JOIN risk_detail rd ON rd.intent_id=i.intent_id AND rd.kind='REJECTED' "
            "LEFT JOIN risk_records rk ON rk.intent_id=i.intent_id"
        )
        args: tuple = ()
        if phase is not None:
            sql += " WHERE s.phase=?"
            args = (phase,)
        out = []
        for r in self._q(sql, args):
            out.append({
                "opportunity_id": r["opportunity_id"], "market": r["market"], "family": r["family"],
                "accepted": None if r["accepted"] is None else bool(r["accepted"]),
                "reasons": [] if r["reasons"] is None else list(json.loads(r["reasons"])),
                "intent_id": r["intent_id"], "state": r["state"],
                "stack_reject_code": r["rd_code"] or r["rk_reason"],
                "stack_gate_class": r["rd_class"],
                "otherwise_valid": None if r["otherwise_valid"] is None else bool(r["otherwise_valid"]),
                "approved": None if r["approved"] is None else bool(r["approved"]),
                "shadow_dry_run": bool(r["shadow_dry_run"]),
                "has_outcome": bool(r["has_outcome"]),
            })
        return out

    def record_execution(self, intent_id: str, ex: ExecutionRecord) -> bool:
        """Append-only log; the latest row is the current record (fill, then verified fees/swap, ...).
        Appending a record identical to the latest one is a no-op (False)."""
        payload = ex.to_json()
        with self._tx() as c:
            phase = self._intent_phase(c, intent_id)
            last = c.execute(
                "SELECT json FROM execution_records WHERE intent_id=? ORDER BY seq DESC LIMIT 1",
                (intent_id,),
            ).fetchone()
            if last is not None and last["json"] == payload:
                return False
            c.execute(
                "INSERT INTO execution_records(intent_id,phase,recorded_utc,json) VALUES(?,?,?,?)",
                (intent_id, phase, self._clock(), payload),
            )
            return True

    def get_execution(self, intent_id: str) -> ExecutionRecord | None:
        r = self._one(
            "SELECT json FROM execution_records WHERE intent_id=? ORDER BY seq DESC LIMIT 1",
            (intent_id,),
        )
        return None if r is None else ExecutionRecord.from_dict(json.loads(r["json"]))

    def execution_history(self, intent_id: str) -> list[ExecutionRecord]:
        return [
            ExecutionRecord.from_dict(json.loads(r["json"]))
            for r in self._q(
                "SELECT json FROM execution_records WHERE intent_id=? ORDER BY seq", (intent_id,)
            )
        ]

    def record_outcome(self, intent_id: str, outcome: OutcomeRecord) -> bool:
        """Insert-once. Requires the intent to be CLOSED (transition first, then write the outcome;
        `closed_without_outcome()` finds the crash window between the two)."""
        payload = outcome.to_json()
        with self._tx() as c:
            r = c.execute(
                "SELECT phase, opportunity_id, state FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if r is None:
                raise MissingParentError(f"unknown intent {intent_id}")
            row = c.execute("SELECT json FROM outcomes WHERE intent_id=?", (intent_id,)).fetchone()
            if row is not None:
                if row["json"] == payload:
                    return False
                raise ImmutableRecordError("outcome is immutable")
            if r["state"] != CLOSED:
                raise IllegalTransition(f"outcome requires CLOSED intent, is {r['state']}")
            c.execute(
                "INSERT INTO outcomes(intent_id,opportunity_id,phase,closed_utc,json) VALUES(?,?,?,?,?)",
                (intent_id, r["opportunity_id"], r["phase"], outcome.closed_utc, payload),
            )
            return True

    def get_outcome(self, intent_id: str) -> OutcomeRecord | None:
        r = self._one("SELECT json FROM outcomes WHERE intent_id=?", (intent_id,))
        return None if r is None else OutcomeRecord.from_dict(json.loads(r["json"]))

    def list_outcomes(self, phase: str | None = None) -> list[tuple[str, str, OutcomeRecord]]:
        """(intent_id, opportunity_id, outcome) ordered by close time."""
        _check_phase(phase)
        sql, args = "SELECT * FROM outcomes WHERE 1=1", []
        if phase:
            sql += " AND phase=?"
            args.append(phase)
        return [
            (r["intent_id"], r["opportunity_id"], OutcomeRecord.from_dict(json.loads(r["json"])))
            for r in self._q(sql + " ORDER BY closed_utc, intent_id", tuple(args))
        ]

    def count_trades(self, phase: str | None = None) -> int:
        _check_phase(phase)
        if phase:
            return self._q("SELECT COUNT(*) n FROM outcomes WHERE phase=?", (phase,))[0]["n"]
        return self._q("SELECT COUNT(*) n FROM outcomes")[0]["n"]

    # ---- counterfactuals -----------------------------------------------------------------------
    def record_counterfactual(self, label: CounterfactualLabel) -> bool:
        """Insert-once for REJECTED decisions. Re-labelling with the same values (labelled_utc aside)
        is a no-op (False); differing values raise."""
        new = label.to_dict()
        with self._tx() as c:
            dec = c.execute(
                "SELECT accepted, phase FROM decisions WHERE opportunity_id=?",
                (label.opportunity_id,),
            ).fetchone()
            if dec is None:
                raise MissingParentError("no decision for counterfactual")
            if dec["accepted"]:
                raise DemoStoreError("counterfactuals are for REJECTED decisions only")
            if dec["phase"] != label.phase:
                raise DemoStoreError("counterfactual phase differs from decision phase")
            row = c.execute(
                "SELECT json FROM counterfactuals WHERE opportunity_id=?", (label.opportunity_id,)
            ).fetchone()
            if row is not None:
                old = json.loads(row["json"])
                old.pop("labelled_utc", None)
                new.pop("labelled_utc", None)
                if old == new:
                    return False
                raise ImmutableRecordError("counterfactual label is immutable")
            c.execute(
                "INSERT INTO counterfactuals(opportunity_id,phase,labelled_utc,json) VALUES(?,?,?,?)",
                (label.opportunity_id, label.phase, label.labelled_utc, label.to_json()),
            )
            return True

    def get_counterfactual(self, opportunity_id: str) -> CounterfactualLabel | None:
        r = self._one("SELECT json FROM counterfactuals WHERE opportunity_id=?", (opportunity_id,))
        return None if r is None else CounterfactualLabel.from_dict(json.loads(r["json"]))

    def list_counterfactuals(self, phase: str | None = None) -> list[CounterfactualLabel]:
        _check_phase(phase)
        sql, args = "SELECT json FROM counterfactuals WHERE 1=1", []
        if phase:
            sql += " AND phase=?"
            args.append(phase)
        return [
            CounterfactualLabel.from_dict(json.loads(r["json"]))
            for r in self._q(sql + " ORDER BY opportunity_id", tuple(args))
        ]

    def rejected_unlabelled(
        self, phase: str | None = None
    ) -> list[tuple[Decision, OpportunitySnapshot]]:
        _check_phase(phase)
        sql = (
            "SELECT d.*, s.json AS sjson FROM decisions d JOIN snapshots s USING(opportunity_id) "
            "LEFT JOIN counterfactuals c USING(opportunity_id) WHERE d.accepted=0 AND c.opportunity_id IS NULL"
        )
        args: tuple = ()
        if phase:
            sql += " AND d.phase=?"
            args = (phase,)
        return [
            (self._decision_from_row(r), snapshot_from_dict(json.loads(r["sjson"])))
            for r in self._q(sql + " ORDER BY d.decided_utc, d.opportunity_id", args)
        ]

    # ---- learning records ----------------------------------------------------------------------
    def learning_records(self, phase: str | None = None) -> list[TradeLearningRecord]:
        """Denormalised join for every CLOSED trade with outcome, risk and execution present."""
        out: list[TradeLearningRecord] = []
        for intent_id, opp_id, outcome in self.list_outcomes(phase):
            snap, dec = self.get_snapshot(opp_id), self.get_decision(opp_id)
            risk, ex = self.get_risk(intent_id), self.get_execution(intent_id)
            if snap is None or dec is None or risk is None or ex is None:
                continue
            out.append(
                TradeLearningRecord(
                    opportunity_id=opp_id,
                    phase=snap.phase,
                    snapshot=snap,
                    decision=dec,
                    risk=risk,
                    execution=ex,
                    outcome=outcome,
                )
            )
        return out
