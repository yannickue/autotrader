"""The persisted reconciliation record keeps WHAT it was reconciled against.

A `paper_self_check` must never be readable as a venue reconciliation, and no
stored record ever grants reconciliation authority after a restart.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from persistence.models import (
    RECONCILIATION_SOURCES,
    RECONCILIATION_STATES,
    ReconciliationStateRecord,
)
from persistence.store import STATE_FORMAT_VERSION, SQLiteStore
from pipeline.paper import recover_pipeline  # noqa: F401  (import guard: module still wires)
from risk.models import ReconciliationSource, ReconciliationState
from tests.integration import e2e_scenarios as scenarios
from tests.unit.pipeline.test_persistence_recovery import _db_path, _open_long, _recover

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _record(**changes: object) -> ReconciliationStateRecord:
    values: dict[str, object] = {
        "mode": "ready",
        "reconciled": True,
        "state": "reconciled",
        "source": "paper_self_check",
        "mismatch_reason": None,
        "last_reconciled_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return ReconciliationStateRecord(**values)


def test_persisted_vocabulary_matches_the_risk_enums() -> None:
    assert {s.value for s in ReconciliationState} == RECONCILIATION_STATES
    assert {s.value for s in ReconciliationSource} == RECONCILIATION_SOURCES


def test_state_source_and_reconciled_at_round_trip(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    store.set_reconciliation_state(_record())
    fetched = store.get_reconciliation_state()
    store.close()
    assert fetched is not None
    assert (fetched.state, fetched.source, fetched.last_reconciled_at) == (
        "reconciled",
        "paper_self_check",
        NOW,
    )


def test_a_paper_self_check_record_never_grants_venue_authority(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    store.set_reconciliation_state(_record(source="paper_self_check"))
    paper = store.get_reconciliation_state()
    store.set_reconciliation_state(_record(source="venue_snapshot", updated_at=NOW))
    venue = store.get_reconciliation_state()
    store.close()
    assert paper is not None and paper.grants_venue_authority is False
    assert venue is not None and venue.grants_venue_authority is True


def test_record_validation_rejects_inconsistent_or_unknown_values() -> None:
    with pytest.raises(ValueError):
        _record(source="broker")  # unknown source
    with pytest.raises(ValueError):
        _record(state="ok")  # unknown state
    with pytest.raises(ValueError):
        _record(reconciled=False)  # flag disagrees with state
    with pytest.raises(ValueError):
        _record(source=None)  # a reconciled record must say what it compared against
    with pytest.raises(ValueError):
        _record(last_reconciled_at=None)
    with pytest.raises(ValueError):  # only a reconciled record may carry a source
        _record(state="mismatch", reconciled=False, source="venue_snapshot")


def test_corrupt_source_in_the_database_fails_recovery_closed(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    store = SQLiteStore(db)
    store.set_reconciliation_state(_record())
    store.close()
    raw = sqlite3.connect(db)
    raw.execute("UPDATE reconciliation_state SET reconciliation_source = 'broker'")
    raw.commit()
    raw.close()

    store2 = SQLiteStore(db)
    result = store2.recover()
    store2.close()
    assert result.ok is False and result.snapshot is None


def test_pre_source_database_is_migrated_for_writes_but_refused_for_recovery(
    tmp_path: Path,
) -> None:
    """A format-version-2 DB (no state/source columns) opens without crashing but
    is never interpreted: recover() refuses it, so a legacy `reconciled=1` row
    cannot become an authority."""
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('state_format_version', '2');
        CREATE TABLE reconciliation_state (
            id INTEGER PRIMARY KEY CHECK (id = 1), mode TEXT NOT NULL,
            reconciled INTEGER NOT NULL, mismatch_reason TEXT,
            last_reconciled_at TEXT, updated_at TEXT NOT NULL);
        INSERT INTO reconciliation_state VALUES (1, 'ready', 1, NULL, '2026-01-01T12:00:00+00:00',
            '2026-01-01T12:00:00+00:00');
        """
    )
    raw.commit()
    raw.close()

    store = SQLiteStore(db)
    result = store.recover()
    assert result.ok is False
    assert result.details["found_version"] == "2"
    assert result.details["expected_version"] == STATE_FORMAT_VERSION
    store.set_reconciliation_state(_record())  # migrated columns: write works
    store.close()


# -- restart: the source survives, and no stored record grants authority ----


def test_pipeline_persists_paper_source_and_restart_never_upgrades_it(tmp_path: Path) -> None:
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness()  # harness engine reconciled with a VENUE_SNAPSHOT-shaped call
    store = SQLiteStore(db_path)
    h.pipeline.store = store
    _open_long(h)
    persisted = store.get_reconciliation_state()
    assert persisted is not None
    assert (persisted.state, persisted.source) == ("reconciled", "venue_snapshot")
    store.close()

    later = NOW + timedelta(hours=1)
    recovered, result, store2 = _recover(scenarios.build_harness(), db_path, now=later)
    assert result.ok is True
    engine = recovered.execution_engine
    # The restart's own comparison is a paper self-check, labelled as such: the
    # earlier stored 'venue_snapshot' did NOT carry over as authority.
    assert engine.reconciliation_source is ReconciliationSource.PAPER_SELF_CHECK
    assert engine.venue_reconciled is False

    recovered._persist(now=later)
    after = store2.get_reconciliation_state()
    assert after is not None
    assert (after.state, after.source, after.last_reconciled_at) == (
        "reconciled",
        "paper_self_check",
        later,
    )
    assert after.grants_venue_authority is False
    store2.close()


def test_mismatch_state_persists_without_a_source(tmp_path: Path) -> None:
    h = scenarios.build_harness()
    store = SQLiteStore(_db_path(tmp_path))
    h.pipeline.store = store
    h.execution_engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, NOW)
    h.pipeline._persist(now=NOW)
    record = store.get_reconciliation_state()
    store.close()
    assert record is not None
    assert (record.state, record.source, record.reconciled) == ("mismatch", None, False)
