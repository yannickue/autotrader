from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from research.replay import replay_partition
from research.storage import ParquetStore, PartitionKey

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_parquet_partition_round_trips_and_replays_in_receive_order(tmp_path) -> None:
    store = ParquetStore(tmp_path)
    key = PartitionKey(
        venue="venue-a",
        instrument="BTCUSDT-PERP",
        event_type="quote",
        date=date(2026, 1, 1),
        dataset_version="v1",
    )
    rows = (
        {
            "source_key": "a",
            "event_time": NOW,
            "available_at": NOW + timedelta(seconds=2),
            "bid": 100.0,
        },
        {
            "source_key": "b",
            "event_time": NOW + timedelta(seconds=1),
            "available_at": NOW + timedelta(seconds=1),
            "bid": 101.0,
        },
    )

    manifest = store.write_partition(key, rows, schema_version=1, provenance={"feed": "capture"})
    replayed: list[str] = []
    summary = replay_partition(
        store,
        key,
        lambda row: replayed.append(row["source_key"]),
        configuration={"latency_model": "recorded"},
    )

    assert manifest.row_count == 2
    assert tuple(row["source_key"] for row in store.read_partition(key)) == ("b", "a")
    assert replayed == ["b", "a"]
    assert summary.event_count == 2
    assert len(summary.dataset_hash) == 64
    assert len(summary.configuration_hash) == 64
    assert summary.to_json() == summary.to_json()


def test_partition_is_immutable_and_duplicate_source_keys_are_idempotent(tmp_path) -> None:
    store = ParquetStore(tmp_path)
    key = PartitionKey("venue-a", "BTCUSDT-PERP", "quote", date(2026, 1, 1), "v1")
    row = {"source_key": "a", "event_time": NOW, "available_at": NOW, "bid": 100.0}

    store.write_partition(key, (row, row), schema_version=1)

    assert len(store.read_partition(key)) == 1
    with pytest.raises(FileExistsError, match="immutable partition"):
        store.write_partition(key, (row,), schema_version=1)


def test_partition_rejects_lookahead_data(tmp_path) -> None:
    store = ParquetStore(tmp_path)
    key = PartitionKey("venue-a", "BTCUSDT-PERP", "quote", date(2026, 1, 1), "v1")
    leaking_row = {
        "source_key": "future",
        "event_time": NOW,
        "available_at": NOW - timedelta(microseconds=1),
        "bid": 100.0,
    }

    with pytest.raises(ValueError, match="available_at cannot precede event_time"):
        store.write_partition(key, (leaking_row,), schema_version=1)


def test_partition_rejects_conflicting_payloads_for_one_source_key(tmp_path) -> None:
    store = ParquetStore(tmp_path)
    key = PartitionKey("venue-a", "BTCUSDT-PERP", "quote", date(2026, 1, 1), "v1")
    first = {"source_key": "a", "event_time": NOW, "available_at": NOW, "bid": 100.0}
    conflicting = {"source_key": "a", "event_time": NOW, "available_at": NOW, "bid": 101.0}

    with pytest.raises(ValueError, match="conflicting duplicate source_key"):
        store.write_partition(key, (first, conflicting), schema_version=1)


def test_partition_rejects_empty_rows_explicitly(tmp_path) -> None:
    store = ParquetStore(tmp_path)
    key = PartitionKey("venue-a", "BTCUSDT-PERP", "quote", date(2026, 1, 1), "v1")

    with pytest.raises(ValueError, match="partition rows cannot be empty"):
        store.write_partition(key, (), schema_version=1)


def test_partition_manifest_defensively_freezes_nested_provenance(tmp_path) -> None:
    store = ParquetStore(tmp_path)
    key = PartitionKey("venue-a", "BTCUSDT-PERP", "quote", date(2026, 1, 1), "v1")
    row = {"source_key": "a", "event_time": NOW, "available_at": NOW, "bid": 100.0}
    provenance = {"capture": {"feed": "primary"}}

    manifest = store.write_partition(key, (row,), schema_version=1, provenance=provenance)
    provenance["capture"]["feed"] = "mutated"

    assert manifest.to_dict()["provenance"] == {"capture": {"feed": "primary"}}
    with pytest.raises(TypeError):
        manifest.provenance["new"] = "value"
    with pytest.raises(TypeError):
        manifest.provenance["capture"]["feed"] = "value"


def test_partition_normalizes_aware_timestamps_to_utc(tmp_path) -> None:
    store = ParquetStore(tmp_path)
    key = PartitionKey("venue-a", "BTCUSDT-PERP", "quote", date(2026, 1, 1), "v1")
    offset = timezone(timedelta(hours=2))
    row = {
        "source_key": "a",
        "event_time": datetime(2026, 1, 1, 2, tzinfo=offset),
        "available_at": datetime(2026, 1, 1, 2, 0, 1, tzinfo=offset),
        "bid": 100.0,
    }

    store.write_partition(key, (row,), schema_version=1)
    restored = store.read_partition(key)[0]

    assert restored["event_time"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert restored["available_at"] == datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
