"""Deterministic Parquet datasets for validated historical bars/ticks (C3).

One file per (source, canonical instrument, data type, timeframe, range):

    <root>/<source>/<CANONICAL>/<bars_<tf>|ticks>/<start>__<end>.parquet

Guarantees:
- Refuses to write a dataset whose validation status is FAILED (no silent
  acceptance) -- the caller must pass the `HistoricalQualityReport`.
- Rows sorted by timestamp; fixed schema; fixed compression; provenance JSON
  (sorted keys) in the Parquet key-value metadata -> identical input +
  identical `retrieved_at` produces byte-identical files.
- Decimals stored as exact strings (see `data.parquet_store` rationale).
- `content_sha256` over the canonical row text is stored in provenance and
  re-verified on read (roundtrip integrity).
- Timestamps are UTC (`timestamp[ms, UTC]`); bar timestamp = bar OPEN time.
- CFD volume is BROKER_TICK_ACTIVITY (never exchange volume).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from data.historical_quality import HistoricalQualityReport, ValidationStatus
from data.parquet_store import BarRecord, TickRecord
from data.provenance import SemanticType, Source

SCHEMA_VERSION = "c3.historical.v1"
NORMALIZATION_POLICY = (
    "mt5_server_epoch->utc via ServerTimePolicy; decimals=str(float64); "
    "bar_ts=open_time; incomplete_last_bar_dropped; no_price_synthesis; "
    "tick_last_0.0->null"
)
_PROVENANCE_KEY = b"c3_provenance"


class DatasetIntegrityError(RuntimeError):
    """Stored dataset failed provenance/hash verification."""


class DatasetRejected(RuntimeError):
    """Refused to persist data whose validation FAILED."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DatasetProvenance:
    source: str
    broker_symbol: str
    canonical_instrument: str
    data_type: str  # "BARS" | "TICKS"
    timeframe: str  # e.g. "5m"; "tick" for ticks
    retrieved_at: str  # ISO UTC
    requested_start: str
    requested_end: str
    actual_start: str
    actual_end: str
    timezone_policy: dict[str, str]
    normalization_policy: str
    schema_version: str
    row_count: int
    volume_semantic_type: str
    broker_account_kind: str  # e.g. "DEMO" -- demo data may differ from live
    validation_status: str
    validation_summary: dict[str, int] = field(default_factory=dict)
    expected_gaps: int = 0
    suspicious_gap_count: int = 0
    content_sha256: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> DatasetProvenance:
        return cls(**json.loads(text))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _ts_ms(value: datetime) -> int:
    micros = int(value.astimezone(UTC).timestamp() * 1_000_000 + 0.5)
    return micros // 1000


def _hash_rows(rows: list[tuple]) -> str:
    h = hashlib.sha256()
    for row in rows:
        h.update(("|".join("" if c is None else str(c) for c in row) + "\n").encode())
    return h.hexdigest()


def _bar_rows(bars: list[BarRecord]) -> list[tuple]:
    return [
        (
            _ts_ms(b.timestamp),
            _dec(b.open),
            _dec(b.high),
            _dec(b.low),
            _dec(b.close),
            _dec(b.volume),
            b.spread_points,
            ",".join(b.quality_flags),
        )
        for b in sorted(bars, key=lambda x: x.timestamp)
    ]


def _tick_rows(ticks: list[TickRecord]) -> list[tuple]:
    return [
        (
            _ts_ms(t.timestamp),
            _dec(t.bid),
            _dec(t.ask),
            _dec(t.last),
            _dec(t.volume),
            ",".join(t.quality_flags),
        )
        for t in sorted(ticks, key=lambda x: x.timestamp)
    ]


_BAR_SCHEMA = pa.schema(
    [
        ("timestamp", pa.timestamp("ms", tz="UTC")),
        ("open", pa.string()),
        ("high", pa.string()),
        ("low", pa.string()),
        ("close", pa.string()),
        ("volume", pa.string()),
        ("spread_points", pa.int32()),
        ("quality_flags", pa.string()),
    ]
)
_TICK_SCHEMA = pa.schema(
    [
        ("timestamp", pa.timestamp("ms", tz="UTC")),
        ("bid", pa.string()),
        ("ask", pa.string()),
        ("last", pa.string()),
        ("volume", pa.string()),
        ("quality_flags", pa.string()),
    ]
)


def dataset_path(root: Path | str, provenance: DatasetProvenance) -> Path:
    kind = f"bars_{provenance.timeframe}" if provenance.data_type == "BARS" else "ticks"
    start = provenance.actual_start[:19].replace(":", "")
    end = provenance.actual_end[:19].replace(":", "")
    return (
        Path(root)
        / provenance.source
        / provenance.canonical_instrument
        / kind
        / f"{start}__{end}.parquet"
    )


def _write(path: Path, schema: pa.Schema, rows: list[tuple], provenance: DatasetProvenance) -> None:
    columns = list(zip(*rows, strict=True)) if rows else [[] for _ in schema]
    arrays = []
    for field_, col in zip(schema, columns, strict=True):
        if pa.types.is_timestamp(field_.type):
            arrays.append(pa.array(col, type=pa.int64()).cast(field_.type))
        else:
            arrays.append(pa.array(col, type=field_.type))
    table = pa.Table.from_arrays(
        arrays, schema=schema.with_metadata({_PROVENANCE_KEY: provenance.to_json().encode()})
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd", use_dictionary=False, write_statistics=False)
    tmp.replace(path)


def _check_report(report: HistoricalQualityReport, expected_rows: int) -> None:
    if report.status is ValidationStatus.FAILED:
        raise DatasetRejected(f"validation FAILED: {report.summary}")
    if report.row_count != expected_rows:
        raise DatasetRejected("quality report does not match the records being written")


def write_bar_dataset(
    root: Path | str,
    bars: list[BarRecord],
    *,
    report: HistoricalQualityReport,
    requested_start: datetime,
    requested_end: datetime,
    retrieved_at: datetime,
    timezone_policy: dict[str, str],
    broker_account_kind: str,
) -> tuple[Path, DatasetProvenance]:
    _check_report(report, len(bars))
    ordered = sorted(bars, key=lambda b: b.timestamp)
    first = ordered[0]
    if any(b.volume_semantic_type is not SemanticType.BROKER_TICK_ACTIVITY for b in bars):
        raise DatasetRejected("CFD bar volume must be BROKER_TICK_ACTIVITY")
    rows = _bar_rows(bars)
    prov = DatasetProvenance(
        source=str(Source.ACTIVTRADES_MT5_CFD),
        broker_symbol=first.broker_symbol,
        canonical_instrument=first.canonical_symbol,
        data_type="BARS",
        timeframe=first.timeframe,
        retrieved_at=_iso(retrieved_at),
        requested_start=_iso(requested_start),
        requested_end=_iso(requested_end),
        actual_start=_iso(ordered[0].timestamp),
        actual_end=_iso(ordered[-1].timestamp),
        timezone_policy=timezone_policy,
        normalization_policy=NORMALIZATION_POLICY,
        schema_version=SCHEMA_VERSION,
        row_count=len(rows),
        volume_semantic_type=str(SemanticType.BROKER_TICK_ACTIVITY),
        broker_account_kind=broker_account_kind,
        validation_status=str(report.status),
        validation_summary=dict(report.summary),
        expected_gaps=report.expected_gaps,
        suspicious_gap_count=len(report.suspicious_gaps),
        content_sha256=_hash_rows(rows),
    )
    path = dataset_path(root, prov)
    _write(path, _BAR_SCHEMA, rows, prov)
    return path, prov


def write_tick_dataset(
    root: Path | str,
    ticks: list[TickRecord],
    *,
    report: HistoricalQualityReport,
    requested_start: datetime,
    requested_end: datetime,
    retrieved_at: datetime,
    timezone_policy: dict[str, str],
    broker_account_kind: str,
) -> tuple[Path, DatasetProvenance]:
    _check_report(report, len(ticks))
    ordered = sorted(ticks, key=lambda t: t.timestamp)
    first = ordered[0]
    if any(t.volume_semantic_type is not SemanticType.BROKER_TICK_ACTIVITY for t in ticks):
        raise DatasetRejected("CFD tick volume must be BROKER_TICK_ACTIVITY")
    rows = _tick_rows(ticks)
    prov = DatasetProvenance(
        source=str(Source.ACTIVTRADES_MT5_CFD),
        broker_symbol=first.broker_symbol,
        canonical_instrument=first.canonical_symbol,
        data_type="TICKS",
        timeframe="tick",
        retrieved_at=_iso(retrieved_at),
        requested_start=_iso(requested_start),
        requested_end=_iso(requested_end),
        actual_start=_iso(ordered[0].timestamp),
        actual_end=_iso(ordered[-1].timestamp),
        timezone_policy=timezone_policy,
        normalization_policy=NORMALIZATION_POLICY,
        schema_version=SCHEMA_VERSION,
        row_count=len(rows),
        volume_semantic_type=str(SemanticType.BROKER_TICK_ACTIVITY),
        broker_account_kind=broker_account_kind,
        validation_status=str(report.status),
        validation_summary=dict(report.summary),
        content_sha256=_hash_rows(rows),
    )
    path = dataset_path(root, prov)
    _write(path, _TICK_SCHEMA, rows, prov)
    return path, prov


def _read_table(path: Path) -> tuple[pa.Table, DatasetProvenance]:
    table = pq.read_table(path)
    meta: dict[bytes, bytes] = table.schema.metadata or {}
    raw = meta.get(_PROVENANCE_KEY)
    if raw is None:
        raise DatasetIntegrityError(f"{path}: provenance metadata missing")
    prov = DatasetProvenance.from_json(raw.decode())
    if prov.schema_version != SCHEMA_VERSION:
        raise DatasetIntegrityError(f"unsupported schema_version {prov.schema_version}")
    if table.num_rows != prov.row_count:
        raise DatasetIntegrityError("row_count mismatch")
    return table, prov


def _ms_to_dt(value: Any) -> datetime:
    return (
        value.astimezone(UTC)
        if isinstance(value, datetime)
        else datetime.fromtimestamp(value / 1000, tz=UTC)
    )


def read_bar_dataset(path: Path | str) -> tuple[list[BarRecord], DatasetProvenance]:
    table, prov = _read_table(Path(path))
    if prov.data_type != "BARS":
        raise DatasetIntegrityError("not a BARS dataset")
    retrieved = datetime.fromisoformat(prov.retrieved_at)
    bars: list[BarRecord] = []
    rows: list[tuple] = []
    for r in table.to_pylist():
        ts = _ms_to_dt(r["timestamp"])
        rows.append(
            (
                _ts_ms(ts),
                r["open"],
                r["high"],
                r["low"],
                r["close"],
                r["volume"],
                r["spread_points"],
                r["quality_flags"],
            )
        )
        bars.append(
            BarRecord(
                canonical_symbol=prov.canonical_instrument,
                broker_symbol=prov.broker_symbol,
                timestamp=ts,
                timeframe=prov.timeframe,
                open=Decimal(r["open"]),
                high=Decimal(r["high"]),
                low=Decimal(r["low"]),
                close=Decimal(r["close"]),
                volume=Decimal(r["volume"]),
                volume_semantic_type=SemanticType(prov.volume_semantic_type),
                source=Source(prov.source),
                quality_flags=tuple(f for f in r["quality_flags"].split(",") if f),
                ingested_at=retrieved,
                spread_points=r["spread_points"],
            )
        )
    if _hash_rows(rows) != prov.content_sha256:
        raise DatasetIntegrityError("content_sha256 mismatch")
    return bars, prov


def read_tick_dataset(path: Path | str) -> tuple[list[TickRecord], DatasetProvenance]:
    table, prov = _read_table(Path(path))
    if prov.data_type != "TICKS":
        raise DatasetIntegrityError("not a TICKS dataset")
    retrieved = datetime.fromisoformat(prov.retrieved_at)
    ticks: list[TickRecord] = []
    rows: list[tuple] = []
    for r in table.to_pylist():
        ts = _ms_to_dt(r["timestamp"])
        rows.append((_ts_ms(ts), r["bid"], r["ask"], r["last"], r["volume"], r["quality_flags"]))
        ticks.append(
            TickRecord(
                canonical_symbol=prov.canonical_instrument,
                broker_symbol=prov.broker_symbol,
                timestamp=ts,
                bid=Decimal(r["bid"]),
                ask=Decimal(r["ask"]),
                last=None if r["last"] is None else Decimal(r["last"]),
                volume=Decimal(r["volume"]),
                volume_semantic_type=SemanticType(prov.volume_semantic_type),
                source=Source(prov.source),
                quality_flags=tuple(f for f in r["quality_flags"].split(",") if f),
                ingested_at=retrieved,
            )
        )
    if _hash_rows(rows) != prov.content_sha256:
        raise DatasetIntegrityError("content_sha256 mismatch")
    return ticks, prov
