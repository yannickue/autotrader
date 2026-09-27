"""Deterministic replay over immutable historical partitions."""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from research.storage import ParquetStore, PartitionKey


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplaySummary:
    event_count: int
    dataset_hash: str
    configuration_hash: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "configuration_hash": self.configuration_hash,
                "dataset_hash": self.dataset_hash,
                "event_count": self.event_count,
            },
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, default=str, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def replay_partition(
    store: ParquetStore,
    key: PartitionKey,
    handler: Callable[[dict[str, Any]], None],
    *,
    configuration: Mapping[str, Any],
) -> ReplaySummary:
    rows = store.read_partition(key)
    for row in rows:
        handler(row)
    return ReplaySummary(
        event_count=len(rows),
        dataset_hash=store.manifest(key).dataset_hash,
        configuration_hash=_canonical_hash(configuration),
    )
