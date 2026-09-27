"""Immutable versioned Parquet partitions for historical data and replay."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as Date
from pathlib import Path
from typing import Any

from research.immutability import freeze, thaw

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class PartitionKey:
    venue: str
    instrument: str
    event_type: str
    date: Date
    dataset_version: str

    def __post_init__(self) -> None:
        for component in (self.venue, self.instrument, self.event_type, self.dataset_version):
            if not _SAFE_COMPONENT.fullmatch(component):
                raise ValueError(f"unsafe partition component: {component!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartitionManifest:
    schema_version: int
    row_count: int
    dataset_hash: str
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", freeze(self.provenance))

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_hash": self.dataset_hash,
            "provenance": thaw(self.provenance),
            "row_count": self.row_count,
            "schema_version": self.schema_version,
        }


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _ordered_rows(rows: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    unique: dict[str, dict[str, Any]] = {}
    for original in rows:
        row = dict(original)
        source_key = row.get("source_key")
        if not isinstance(source_key, str) or not source_key:
            raise ValueError("source_key must be a non-empty string")
        event_time = _utc(row.get("event_time"), "event_time")
        available_at = _utc(row.get("available_at"), "available_at")
        if available_at < event_time:
            raise ValueError("available_at cannot precede event_time")
        row["event_time"] = event_time
        row["available_at"] = available_at
        previous = unique.get(source_key)
        if previous is not None and previous != row:
            raise ValueError(f"conflicting duplicate source_key: {source_key}")
        unique.setdefault(source_key, row)
    return tuple(
        sorted(
            unique.values(),
            key=lambda row: (row["available_at"], row["event_time"], row["source_key"]),
        )
    )


class ParquetStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def partition_path(self, key: PartitionKey) -> Path:
        return (
            self.root
            / f"dataset_version={key.dataset_version}"
            / f"venue={key.venue}"
            / f"instrument={key.instrument}"
            / f"event_type={key.event_type}"
            / f"date={key.date.isoformat()}"
        )

    def write_partition(
        self,
        key: PartitionKey,
        rows: tuple[dict[str, Any], ...],
        *,
        schema_version: int,
        provenance: dict[str, Any] | None = None,
    ) -> PartitionManifest:
        import pyarrow as pa
        import pyarrow.parquet as pq

        if schema_version <= 0:
            raise ValueError("schema_version must be positive")
        if not rows:
            raise ValueError("partition rows cannot be empty")
        destination = self.partition_path(key)
        if destination.exists():
            raise FileExistsError(f"immutable partition already exists: {destination}")
        ordered = _ordered_rows(rows)
        destination.mkdir(parents=True)
        data_path = destination / "data.parquet"
        pq.write_table(pa.Table.from_pylist(list(ordered)), data_path)
        dataset_hash = hashlib.sha256(data_path.read_bytes()).hexdigest()
        manifest = PartitionManifest(
            schema_version=schema_version,
            row_count=len(ordered),
            dataset_hash=dataset_hash,
            provenance=dict(provenance or {}),
        )
        (destination / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        return manifest

    def manifest(self, key: PartitionKey) -> PartitionManifest:
        payload = json.loads((self.partition_path(key) / "manifest.json").read_text("utf-8"))
        return PartitionManifest(**payload)

    def read_partition(self, key: PartitionKey) -> tuple[dict[str, Any], ...]:
        import pyarrow.parquet as pq

        data_path = self.partition_path(key) / "data.parquet"
        manifest = self.manifest(key)
        actual_hash = hashlib.sha256(data_path.read_bytes()).hexdigest()
        if actual_hash != manifest.dataset_hash:
            raise ValueError("Parquet partition hash does not match manifest")
        return tuple(pq.read_table(data_path).to_pylist())
