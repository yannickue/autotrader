# ruff: noqa: E501
"""``demo.observer_store.ObserverStore`` (the observer's OWN ``observer.sqlite``): lazy + separate from the live DB, definitions stored once, one
transaction per batch, insert-once / immutable / idempotent replay, flat export with the offline column names; the live ``DemoStore`` is untouched.

Catalogue items covered: feature serialisation + schema version in the stored row; decision_feature vs outcome_label separation in the persisted row;
the live DB schema is byte-identical to the pre-observer baseline (git 0d2ec08); restart replay (crash between the observer write and the bar pointer).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from demo import store as demo_store
from demo.export import export_observer_records
from demo.observer_store import OBSERVER_DB_NAME, ObserverStore
from demo.store import DemoStore, ImmutableRecordError
from market_observer import bars_adapter as BA
from market_observer import observer as O
from market_observer.schema import SessionSpec

T = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


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
    s = ObserverStore(tmp_path / OBSERVER_DB_NAME, clock=lambda: T.isoformat())
    yield s
    s.close()


def raw(store: ObserverStore) -> sqlite3.Connection:
    return sqlite3.connect(store.path)


def test_record_roundtrip_has_versions_schema_hashes_and_the_flat_offline_column_names(store):
    rec = make_record()
    assert store.record(rec) is True
    assert store.record(rec) is False  # identical re-insert: idempotent no-op
    (row,) = store.list_rows()
    assert set(row) == set(rec.to_row())
    assert row["schema_version"] == "mso-schema-1" and row["observer_version"] == "market-structure-observer-v1"
    assert row["m_warmup_ok"] is False and row["m_opportunity_id"] == "opp-1" and row["m_hash_levels"] and row["v_levels"] == "mso-levels-1"
    assert not any(k.startswith("y_") for k in row)  # no labels in the live path
    for k, v in rec.to_row().items():
        assert row[k] == v, k
    assert store.count() == 1
    db = raw(store)
    warm, values = db.execute("SELECT warmup_ok, values_json FROM observer_records").fetchone()
    assert warm == 0 and isinstance(json.loads(values), list)  # feature VALUES only; the column names live once in the definition
    cols = json.loads(db.execute("SELECT columns_json FROM definitions").fetchone()[0])
    assert all(c.startswith("f_") for c in cols) and len(cols) == len(json.loads(values))
    db.close()


def test_definitions_are_stored_once_and_referenced_per_row(store):
    store.record_many([make_record(opp=f"o{i}", direction=1 if i % 2 else -1) for i in range(6)])
    db = raw(store)
    assert db.execute("SELECT COUNT(*) FROM definitions").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(DISTINCT definition_id) FROM observer_records").fetchone()[0] == 1
    metas = [json.loads(r[0]) for r in db.execute("SELECT meta_json FROM observer_records")]
    assert all(not any(k.startswith("hash_") or k == "config_hash" for k in m) for m in metas)  # the six 64-char hashes are NOT repeated per row
    hashes = json.loads(db.execute("SELECT hashes_json FROM definitions").fetchone()[0])
    assert len([k for k in hashes if k.startswith("hash_")]) >= 5 and "config_hash" in hashes
    per_row = db.execute("SELECT AVG(LENGTH(values_json) + LENGTH(meta_json)) FROM observer_records").fetchone()[0]
    full = make_record()
    legacy = len(json.dumps({k: v for k, v in full.to_row().items() if k.startswith("f_")}, sort_keys=True)) + len(json.dumps(dict(full.meta), sort_keys=True))
    db.close()
    assert per_row < 0.6 * legacy, (per_row, legacy)  # the old row repeated the feature names and the hashes
    assert len(store.list_rows()) == 6 and all(set(r) == set(full.to_row()) for r in store.list_rows())


def test_one_batch_is_one_transaction(store):
    store.count()  # opens the connection
    seen: list[str] = []
    store._db().set_trace_callback(seen.append)
    written, conflicts = store.record_many([make_record(opp=f"b{i}") for i in range(5)])
    stmts = [s.strip().upper() for s in seen]
    store._db().set_trace_callback(None)
    assert written == 5 and conflicts == []
    assert sum(s.startswith("BEGIN") for s in stmts) == 1 and sum(s == "COMMIT" for s in stmts) == 1, stmts


def test_a_conflicting_record_in_a_batch_is_reported_and_the_rest_commits(store):
    store.record(make_record(opp="same"))
    written, conflicts = store.record_many([make_record(direction=-1, opp="same"), make_record(opp="fresh")])
    assert written == 1 and len(conflicts) == 1 and isinstance(conflicts[0], ImmutableRecordError) and store.count() == 2


def test_rows_are_immutable_and_a_differing_row_for_the_same_key_is_refused(store):
    store.record(make_record())
    with pytest.raises(ImmutableRecordError):
        store.record(make_record(direction=-1))  # same opportunity id key, different content
    db = raw(store)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE observer_records SET market='X'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM observer_records")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE definitions SET status='X'")
    db.close()


def test_replay_after_a_crash_between_persist_and_bar_pointer_is_idempotent(store):
    """LOW 9: the same opportunity re-observed after a restart reports different audit meta (history that depends on process start): no error, no row."""
    rec = make_record()
    assert store.record(rec) is True  # ... crash before set_bar_pointer; the process restarts and re-observes the same bar
    replay = dataclasses.replace(rec, meta={**rec.meta, "bars_available": 6000, "prev_days_available": 24})
    assert replay.meta["bars_available"] != rec.meta["bars_available"]
    assert store.record(replay) is False
    assert store.record_many([replay]) == (0, [])
    assert store.count() == 1
    (row,) = store.list_rows()
    assert row["m_bars_available"] == rec.meta["bars_available"]  # the first write stays untouched
    other = dataclasses.replace(rec, meta={**rec.meta, "event_price": rec.meta["event_price"] + 1.0})  # a non-audit meta difference is still refused
    with pytest.raises(ImmutableRecordError):
        store.record(other)


def test_event_id_is_the_key_when_there_is_no_opportunity(store):
    r = make_record(opp=None)
    store.record(r)
    assert raw(store).execute("SELECT record_key FROM observer_records").fetchone()[0] == r.event_id


def test_the_file_is_separate_lazy_and_never_created_by_reads(tmp_path):
    path = tmp_path / "art" / OBSERVER_DB_NAME
    s = ObserverStore(path)
    assert not path.exists() and not s.is_open
    assert s.count() == 0 and s.list_rows() == [] and s.definitions() == [] and not path.exists() and not s.is_open  # reads never create it
    s.record(make_record())
    assert path.exists() and s.is_open
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"definitions", "observer_records", "meta"}
    db.close()
    assert s._db().execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL on the observer's own connection
    s.close()


def test_live_demo_db_schema_has_no_observer_symbol_and_equals_the_pre_observer_baseline(tmp_path):
    """LOW 10: flag off => the live DemoStore is exactly what it was before Lane O (baseline: ``git show 0d2ec08:src/demo/store.py``)."""
    s = DemoStore(tmp_path / "demo.sqlite", clock=lambda: T.isoformat())
    db = sqlite3.connect(s.path)
    now = sorted((r[0], r[1], r[2]) for r in db.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    db.close()
    s.close()
    assert not [r for r in now if "observer" in (r[1] + (r[2] or "")).lower()]
    assert "observer" not in Path(demo_store.__file__).read_text(encoding="utf-8").lower()
    try:
        base_src = subprocess.run(["git", "show", "0d2ec08:src/demo/store.py"], cwd=ROOT, capture_output=True, text=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git baseline 0d2ec08 not available")
    spec = importlib.util.spec_from_loader("demo_store_baseline_0d2ec08", loader=None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        exec(compile(base_src, "store_0d2ec08.py", "exec"), mod.__dict__)
        b = mod.DemoStore(tmp_path / "base.sqlite", clock=lambda: T.isoformat())
        db = sqlite3.connect(b.path)
        base = sorted((r[0], r[1], r[2]) for r in db.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        db.close()
        b.close()
    finally:
        sys.modules.pop(spec.name, None)
    assert now == base


def test_parquet_export_has_the_offline_column_names(store, tmp_path):
    assert export_observer_records(store, tmp_path / "x") is None  # nothing to export
    rec = make_record()
    store.record(rec)
    store.record(make_record(direction=-1, opp="opp-2"))
    path = export_observer_records(store, tmp_path / "out")
    t = pq.read_table(path)
    assert t.num_rows == 2 and set(t.column_names) == set(rec.to_row())
    assert not [c for c in t.column_names if c.startswith("y_")]
    assert set(t.column("schema_version").to_pylist()) == {"mso-schema-1"}
    first = {k: v[0] for k, v in t.to_pydict().items()}
    for k, v in rec.to_row().items():
        assert first[k] == v, k  # values equal the in-memory ObserverRecord row (definitions joined back)
