"""Observational candidate-conflict and open-trade-overlap records."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import pandas as pd

from .candidate import SignalCandidate


@dataclass(frozen=True)
class ConflictRecord:
    kind: str
    timestamp: pd.Timestamp
    strategy_ids: tuple[str, ...]
    details: dict[str, object]


def record_conflicts(
    candidates: list[SignalCandidate], trades: pd.DataFrame | None = None
) -> list[ConflictRecord]:
    """Record relationships only; never rank candidates or choose a winner."""

    records: list[ConflictRecord] = []
    by_stamp: dict[pd.Timestamp, list[SignalCandidate]] = {}
    for item in candidates:
        by_stamp.setdefault(item.signal_ts, []).append(item)
    for stamp, items in sorted(by_stamp.items(), key=lambda pair: pair[0]):
        for left, right in combinations(sorted(items, key=lambda item: item.strategy_id), 2):
            kind = (
                "SAME_DIRECTION_AGREEMENT"
                if left.direction == right.direction
                else "OPPOSITE_DIRECTION_CONFLICT"
            )
            records.append(
                ConflictRecord(
                    kind,
                    stamp,
                    (left.strategy_id, right.strategy_id),
                    {"directions": (left.direction, right.direction)},
                )
            )
    if trades is not None and not trades.empty:
        ordered = trades.sort_values(["entry_ts", "strategy_id"], kind="stable")
        for (_, left), (_, right) in combinations(ordered.iterrows(), 2):
            start = max(pd.Timestamp(left["entry_ts"]), pd.Timestamp(right["entry_ts"]))
            end = min(pd.Timestamp(left["exit_ts"]), pd.Timestamp(right["exit_ts"]))
            if start <= end:
                records.append(
                    ConflictRecord(
                        "OVERLAPPING_OPEN_TRADES",
                        start,
                        tuple(sorted((str(left["strategy_id"]), str(right["strategy_id"])))),
                        {"overlap_end": end},
                    )
                )
    return records

