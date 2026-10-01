# ruff: noqa: E501
"""DemoStore ``observer_records``: additive + migration-safe, insert-once / immutable, flat export with the offline column names, snapshot payload untouched.

Catalogue items covered: feature serialisation + schema version in the stored row; decision_feature vs outcome_label separation in the persisted row;
persistence never inside the opportunity snapshot payload; migration-safe on an existing DB (table created empty, older rows untouched).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import numpy as np
import pyarrow.parquet as pq
import pytest

from demo.export import export_observer_records
from demo.store import DemoStore, ImmutableRecordError
from demo.testing import make_pair
from market_observer import bars_adapter as BA
from market_observer import observer as O
from market_observer.schema import SessionSpec

T = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)


def make_record(direction=1, opp="opp-1", family="STRUCT"):
    rng = np.random.default_rng(3)
    n = 300
    c = 100 + np.cumsum(rng.normal(0, 0.3, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + 0.1
    low = np.minimum(o, c) - 0.1
    ts = np.int64(1_781_000_000) * 10**9 + np.arange(n, dtype=np.int64) * 300 * 10**9
    bars = BA.build_observer_bars("GER40", ts, o, h, low, c, np.full(n, 80.0), np.full(n, 0.02), tick_size=0.01, session=SessionSpec("Europe/Berlin", 540, 1050))
    return O.observe_event(bars, n - 1, O.ObservedEvent(direction, float(c[-1]), family, "breakout", opportunity_id=opp, structure_event_id="se"), O.OBSERVER_CONFIG)


@pytest.fixture
def store(tmp_path):
    s = DemoStore(tmp_path / "demo.sqlite", clock=lambda: T.isoformat())
    yield s
    s.close()


def test_record_roundtrip_has_versions_schema_hashes_and_the_flat_offline_column_names(store):
    rec = make_record()
    assert store.record_observer(rec) is True
    assert store.record_observer(rec) is False  # identical re-insert: idempotent no-op
    (row,) = store.list_observer_rows()
    assert set(row) == set(rec.to_row())
    assert row["schema_version"] == "mso-schema-1" and row["observer_version"] == "market-structure-observer-v1"
    assert row["m_warmup_ok"] is False and row["m_opportunity_id"] == "opp-1" and row["m_hash_levels"] and row["v_levels"] == "mso-levels-1"
    assert not any(k.startswith("y_") for k in row)  # no labels in the live path
    for k, v in rec.to_row().items():
        assert row[k] == v, k
    assert store.count_observer_records() == 1
    raw = sqlite3.connect(store.path).execute("SELECT warmup_ok, features_json FROM observer_records").fetchone()
    assert raw[0] == 0 and all(k.startswith("f_") for k in json.loads(raw[1]))


def test_rows_are_immutable_and_a_differing_row_for_the_same_key_is_refused(store):
    rec = make_record()
    store.record_observer(rec)
    with pytest.raises(ImmutableRecordError):
        store.record_observer(make_record(direction=-1))  # same opportunity id key, different content
    db = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE observer_records SET market='X'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM observer_records")


def test_event_id_is_the_key_when_there_is_no_opportunity(store):
    r = make_record(opp=None)
    store.record_observer(r)
    assert sqlite3.connect(store.path).execute("SELECT record_key FROM observer_records").fetchone()[0] == r.event_id


def test_migration_on_an_existing_db_is_additive_and_leaves_old_rows_untouched(tmp_path):
    path = tmp_path / "old.sqlite"
    s = DemoStore(path, clock=lambda: T.isoformat())
    snap, dec, _ = make_pair(market="GER40", signal_ts=T)
    s.record_snapshot(snap)
    s.record_decision(dec)
    s.close()
    db = sqlite3.connect(path)
    db.execute("DROP TABLE observer_records")  # an 'old' DB without the observer table
    tables_before = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.commit()
    db.close()
    assert "observer_records" not in tables_before
    s2 = DemoStore(path, clock=lambda: T.isoformat())  # reopen: the table is created empty, nothing else changes
    try:
        assert s2.count_observer_records() == 0
        assert s2.get_snapshot(snap.opportunity_id).to_json() == snap.to_json()
        assert s2.get_decision(snap.opportunity_id).to_json() == dec.to_json()
        db2 = sqlite3.connect(path)
        assert {r[0] for r in db2.execute("SELECT name FROM sqlite_master WHERE type='table'")} == tables_before | {"observer_records"}
        db2.close()
    finally:
        s2.close()


def test_observer_rows_never_enter_the_opportunity_snapshot(store):
    snap, _dec, _ = make_pair(market="GER40", signal_ts=T)
    store.record_snapshot(snap)
    before = sqlite3.connect(store.path).execute("SELECT * FROM snapshots").fetchall()
    store.record_observer(make_record(opp=snap.opportunity_id))
    after = sqlite3.connect(store.path).execute("SELECT * FROM snapshots").fetchall()
    assert before == after and "observer" not in json.dumps(after).lower() and "f_levels" not in json.dumps(after)
    assert store.record_snapshot(snap) is False  # replay of the same snapshot stays a byte-identical no-op


def test_parquet_export_has_the_offline_column_names(store, tmp_path):
    assert export_observer_records(store, tmp_path / "x") is None  # nothing to export
    rec = make_record()
    store.record_observer(rec)
    store.record_observer(make_record(direction=-1, opp="opp-2"))
    path = export_observer_records(store, tmp_path / "out")
    t = pq.read_table(path)
    assert t.num_rows == 2 and set(t.column_names) == set(rec.to_row())
    assert not [c for c in t.column_names if c.startswith("y_")]
    assert set(t.column("schema_version").to_pylist()) == {"mso-schema-1"}
