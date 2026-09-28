"""Parquet-backed bar/tick ingestion foundation.

Standalone: no dependency on `src/adapters`, `src/pipeline`, `src/risk`, or
`src/execution`, and no venue I/O -- callers hand it already-fetched records.

Layout on disk:

    data/<source>/<canonical_symbol>/bars/<YYYY-MM-DD>.parquet
    data/<source>/<canonical_symbol>/ticks/<YYYY-MM-DD>.parquet

Each parquet file is a UTC-date partition. `write_bars`/`write_ticks` merge
new records into the partition file(s) they belong to, deduplicating by a
natural key so re-running an ingestion job on unchanged data never
duplicates rows (see `_merge_and_write` below).

Decimal precision: Arrow's native `decimal128` type has a fixed
precision/scale declared up front, which silently loses precision for a
price with more fractional digits than that scale allows. Because instrument
tick sizes/digits vary per broker/symbol (see `src/instruments/models.py`)
and this module has no per-symbol schema registry, every `Decimal` field is
stored as its exact `str(...)` form in a `utf8` column and parsed back with
`Decimal(...)` on read. This is a deliberate precision-over-native-type
trade-off: it guarantees lossless round-tripping of arbitrary-precision
prices regardless of symbol, at the cost of native Arrow decimal
compute/columnar-filter support (out of scope here; this module reads whole
partitions back into Python dataclasses, it does not push down decimal
predicates into Arrow).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import timedelta as _timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from data.provenance import SemanticType, Source


def _require_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must be UTC")


@dataclass(frozen=True, slots=True, kw_only=True)
class TickRecord:
    """One normalized bid/ask/last tick, with provenance."""

    canonical_symbol: str
    broker_symbol: str
    timestamp: datetime
    bid: Decimal
    ask: Decimal
    last: Decimal | None
    volume: Decimal
    volume_semantic_type: SemanticType
    source: Source
    quality_flags: tuple[str, ...] = ()
    ingested_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.canonical_symbol.strip():
            raise ValueError("canonical_symbol must be non-empty")
        if not self.broker_symbol.strip():
            raise ValueError("broker_symbol must be non-empty")
        _require_utc("timestamp", self.timestamp)
        if self.ingested_at is None:
            object.__setattr__(self, "ingested_at", datetime.now(UTC))
        else:
            _require_utc("ingested_at", self.ingested_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class BarRecord:
    """One OHLC bar, with provenance. `timeframe` e.g. `"1m"`, `"1h"`, `"1d"`."""

    canonical_symbol: str
    broker_symbol: str
    timestamp: datetime
    timeframe: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    volume_semantic_type: SemanticType
    source: Source
    quality_flags: tuple[str, ...] = ()
    ingested_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.canonical_symbol.strip():
            raise ValueError("canonical_symbol must be non-empty")
        if not self.broker_symbol.strip():
            raise ValueError("broker_symbol must be non-empty")
        if not self.timeframe.strip():
            raise ValueError("timeframe must be non-empty")
        _require_utc("timestamp", self.timestamp)
        if self.ingested_at is None:
            object.__setattr__(self, "ingested_at", datetime.now(UTC))
        else:
            _require_utc("ingested_at", self.ingested_at)


_TICK_COLUMNS = (
    "canonical_symbol",
    "broker_symbol",
    "timestamp",
    "bid",
    "ask",
    "last",
    "volume",
    "volume_semantic_type",
    "source",
    "quality_flags",
    "ingested_at",
)

_BAR_COLUMNS = (
    "canonical_symbol",
    "broker_symbol",
    "timestamp",
    "timeframe",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "volume_semantic_type",
    "source",
    "quality_flags",
    "ingested_at",
)


def _dt_to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _iso_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _dec_to_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _str_to_dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _tick_key(row: dict) -> tuple:
    return (row["canonical_symbol"], row["broker_symbol"], row["timestamp"])


def _bar_key(row: dict) -> tuple:
    return (row["canonical_symbol"], row["broker_symbol"], row["timeframe"], row["timestamp"])


def _tick_to_row(record: TickRecord) -> dict:
    return {
        "canonical_symbol": record.canonical_symbol,
        "broker_symbol": record.broker_symbol,
        "timestamp": _dt_to_iso(record.timestamp),
        "bid": _dec_to_str(record.bid),
        "ask": _dec_to_str(record.ask),
        "last": _dec_to_str(record.last),
        "volume": _dec_to_str(record.volume),
        "volume_semantic_type": str(record.volume_semantic_type),
        "source": str(record.source),
        "quality_flags": list(record.quality_flags),
        "ingested_at": _dt_to_iso(record.ingested_at),
    }


def _row_to_tick(row: dict) -> TickRecord:
    return TickRecord(
        canonical_symbol=row["canonical_symbol"],
        broker_symbol=row["broker_symbol"],
        timestamp=_iso_to_dt(row["timestamp"]),
        bid=_str_to_dec(row["bid"]),
        ask=_str_to_dec(row["ask"]),
        last=_str_to_dec(row["last"]),
        volume=_str_to_dec(row["volume"]),
        volume_semantic_type=SemanticType(row["volume_semantic_type"]),
        source=Source(row["source"]),
        quality_flags=tuple(row["quality_flags"] or ()),
        ingested_at=_iso_to_dt(row["ingested_at"]),
    )


def _bar_to_row(record: BarRecord) -> dict:
    return {
        "canonical_symbol": record.canonical_symbol,
        "broker_symbol": record.broker_symbol,
        "timestamp": _dt_to_iso(record.timestamp),
        "timeframe": record.timeframe,
        "open": _dec_to_str(record.open),
        "high": _dec_to_str(record.high),
        "low": _dec_to_str(record.low),
        "close": _dec_to_str(record.close),
        "volume": _dec_to_str(record.volume),
        "volume_semantic_type": str(record.volume_semantic_type),
        "source": str(record.source),
        "quality_flags": list(record.quality_flags),
        "ingested_at": _dt_to_iso(record.ingested_at),
    }


def _row_to_bar(row: dict) -> BarRecord:
    return BarRecord(
        canonical_symbol=row["canonical_symbol"],
        broker_symbol=row["broker_symbol"],
        timestamp=_iso_to_dt(row["timestamp"]),
        timeframe=row["timeframe"],
        open=_str_to_dec(row["open"]),
        high=_str_to_dec(row["high"]),
        low=_str_to_dec(row["low"]),
        close=_str_to_dec(row["close"]),
        volume=_str_to_dec(row["volume"]),
        volume_semantic_type=SemanticType(row["volume_semantic_type"]),
        source=Source(row["source"]),
        quality_flags=tuple(row["quality_flags"] or ()),
        ingested_at=_iso_to_dt(row["ingested_at"]),
    )


def _partition_path(
    root: Path, source: Source, canonical_symbol: str, kind: str, day: date
) -> Path:
    return root / str(source) / canonical_symbol / kind / f"{day.isoformat()}.parquet"


def _read_partition_rows(path: Path, columns: tuple[str, ...]) -> list[dict]:
    if not path.exists():
        return []
    table = pq.read_table(path)
    return table.to_pylist()


def _write_partition_rows(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {col: [row[col] for row in rows] for col in columns}
    table = pa.table(arrays)
    tmp_path = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_path)
    tmp_path.replace(path)


def _merge_and_write(
    path: Path,
    columns: tuple[str, ...],
    new_rows: list[dict],
    key_fn,
) -> int:
    """Merge `new_rows` into the partition at `path`, deduped by `key_fn`.

    Returns the number of genuinely new rows written (rows whose key was not
    already present). Idempotency strategy: last-write-wins per key -- if a
    row with the same key already exists, it is REPLACED by the incoming
    value (supports correcting a bad ingestion) rather than duplicated.
    """
    existing_rows = _read_partition_rows(path, columns)
    by_key: dict[tuple, dict] = {key_fn(row): row for row in existing_rows}

    new_key_count = 0
    for row in new_rows:
        key = key_fn(row)
        if key not in by_key:
            new_key_count += 1
        by_key[key] = row

    merged_rows = sorted(by_key.values(), key=lambda r: r["timestamp"])
    _write_partition_rows(path, columns, merged_rows)
    return new_key_count


class ParquetStore:
    """Writer/reader for bar/tick parquet partitions rooted at `root`."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def write_ticks(self, records: list[TickRecord]) -> int:
        """Write a batch of ticks, deduping by (symbol, broker_symbol, timestamp).

        Returns the count of genuinely new rows added across all partitions
        touched (a second call with the same records returns 0).
        """
        return self._write_batch(records, kind="ticks", to_row=_tick_to_row, key_fn=_tick_key)

    def write_bars(self, records: list[BarRecord]) -> int:
        """Write a batch of bars, deduping by (symbol, broker_symbol, timeframe, timestamp)."""
        return self._write_batch(records, kind="bars", to_row=_bar_to_row, key_fn=_bar_key)

    def _write_batch(self, records, *, kind: str, to_row, key_fn) -> int:
        by_partition: dict[tuple[Source, str, date], list[dict]] = {}
        for record in records:
            ts = record.timestamp.astimezone(UTC)
            partition_key = (record.source, record.canonical_symbol, ts.date())
            by_partition.setdefault(partition_key, []).append(to_row(record))

        columns = _TICK_COLUMNS if kind == "ticks" else _BAR_COLUMNS
        total_new = 0
        for (source, canonical_symbol, day), rows in by_partition.items():
            path = _partition_path(self.root, source, canonical_symbol, kind, day)
            total_new += _merge_and_write(path, columns, rows, key_fn)
        return total_new

    def read_ticks(
        self,
        *,
        source: Source,
        canonical_symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[TickRecord]:
        rows = self._read_range(source, canonical_symbol, "ticks", _TICK_COLUMNS, start, end)
        return [_row_to_tick(row) for row in rows]

    def read_bars(
        self,
        *,
        source: Source,
        canonical_symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[BarRecord]:
        rows = self._read_range(source, canonical_symbol, "bars", _BAR_COLUMNS, start, end)
        return [_row_to_bar(row) for row in rows]

    def _read_range(
        self,
        source: Source,
        canonical_symbol: str,
        kind: str,
        columns: tuple[str, ...],
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        _require_utc("start", start)
        _require_utc("end", end)
        if start > end:
            raise ValueError("start cannot be after end")

        rows: list[dict] = []
        day = start.astimezone(UTC).date()
        last_day = end.astimezone(UTC).date()
        while day <= last_day:
            path = _partition_path(self.root, source, canonical_symbol, kind, day)
            rows.extend(_read_partition_rows(path, columns))
            day += _ONE_DAY

        filtered = [row for row in rows if start <= _iso_to_dt(row["timestamp"]) <= end]
        filtered.sort(key=lambda r: r["timestamp"])
        return filtered


_ONE_DAY = _timedelta(days=1)
