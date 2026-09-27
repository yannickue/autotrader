"""Replay an immutable Parquet partition and emit a canonical summary."""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root))

from research.replay import replay_partition
from research.storage import ParquetStore, PartitionKey


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--instrument", required=True)
    parser.add_argument("--event-type", required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--configuration", default="{}", help="JSON replay configuration")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configuration = json.loads(args.configuration)
    if not isinstance(configuration, dict):
        raise ValueError("configuration must be a JSON object")
    key = PartitionKey(
        venue=args.venue,
        instrument=args.instrument,
        event_type=args.event_type,
        date=args.date,
        dataset_version=args.dataset_version,
    )
    summary = replay_partition(
        ParquetStore(args.root),
        key,
        lambda _row: None,
        configuration=configuration,
    )
    print(summary.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

