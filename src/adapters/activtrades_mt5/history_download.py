"""Orchestration: resolve -> fetch -> normalize -> validate -> deterministic Parquet.

Works against any `MT5ClientProtocol` (FakeMT5Client in tests, the attached
real terminal in `scripts/mt5_download_sample.py`). Read-only: only
`symbol_info`, `copy_rates_range`, `copy_ticks_range`, `last_error` are used.
Failed validation raises (`DatasetRejected`) -- nothing is persisted.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adapters.activtrades_mt5.history import (
    TIMEFRAME_LABELS,
    TIMEFRAME_SECONDS,
    ServerTimePolicy,
    fetch_rates_range,
    fetch_ticks_range,
    rates_to_bar_records,
    resolve_broker_symbol,
    ticks_to_tick_records,
)
from data.historical import DatasetProvenance, write_bar_dataset, write_tick_dataset
from data.historical_quality import (
    HistoricalQualityConfig,
    HistoricalQualityReport,
    validate_bars,
    validate_ticks,
)

# Timeframes whose bar-open grid is aligned to the SERVER wall clock (00:00/04:00 server time),
# not to UTC: validate them on the server-clock grid (validation only; stored ts stay true UTC).
_SERVER_GRID_MIN_SECONDS = 14400


def _to_server_grid(bars: list, policy: ServerTimePolicy) -> list:
    zone = policy.zone
    return [
        replace(b, timestamp=b.timestamp.astimezone(zone).replace(tzinfo=UTC)) for b in bars
    ]


def download_bars(
    client: Any,
    *,
    root: Path | str,
    canonical: str,
    mt5_timeframe: int,
    start_utc: datetime,
    end_utc: datetime,
    retrieved_at: datetime,
    broker_account_kind: str,
    policy: ServerTimePolicy | None = None,
    quality: HistoricalQualityConfig | None = None,
    broker_symbol: str | None = None,
    path_prefix: str | None = None,
) -> tuple[Path, DatasetProvenance, HistoricalQualityReport]:
    policy = policy or ServerTimePolicy()
    broker_symbol = resolve_broker_symbol(
        client, canonical, broker_symbol=broker_symbol, path_prefix=path_prefix
    )
    rates = fetch_rates_range(
        client,
        broker_symbol=broker_symbol,
        mt5_timeframe=mt5_timeframe,
        start_utc=start_utc,
        end_utc=end_utc,
        policy=policy,
    )
    bars = rates_to_bar_records(
        rates,
        canonical_symbol=canonical.upper(),
        broker_symbol=broker_symbol,
        mt5_timeframe=mt5_timeframe,
        retrieved_at=retrieved_at,
        policy=policy,
        end_utc=end_utc,
    )
    # Range is [start, end): drop anything the server epoch shift let in below start.
    bars = [b for b in bars if b.timestamp >= start_utc]
    tf_seconds = TIMEFRAME_SECONDS[TIMEFRAME_LABELS[mt5_timeframe]]
    report = validate_bars(
        _to_server_grid(bars, policy) if tf_seconds >= _SERVER_GRID_MIN_SECONDS else bars,
        timeframe_seconds=tf_seconds,
        config=quality,
    )
    path, prov = write_bar_dataset(
        root,
        bars,
        report=report,
        requested_start=start_utc,
        requested_end=end_utc,
        retrieved_at=retrieved_at,
        timezone_policy=policy.to_dict(),
        broker_account_kind=broker_account_kind,
    )
    return path, prov, report


def download_ticks(
    client: Any,
    *,
    root: Path | str,
    canonical: str,
    start_utc: datetime,
    end_utc: datetime,
    retrieved_at: datetime,
    broker_account_kind: str,
    policy: ServerTimePolicy | None = None,
    quality: HistoricalQualityConfig | None = None,
) -> tuple[Path, DatasetProvenance, HistoricalQualityReport]:
    policy = policy or ServerTimePolicy()
    broker_symbol = resolve_broker_symbol(client, canonical)
    raw = fetch_ticks_range(
        client,
        broker_symbol=broker_symbol,
        start_utc=start_utc,
        end_utc=end_utc,
        policy=policy,
    )
    ticks = ticks_to_tick_records(
        raw,
        canonical_symbol=canonical.upper(),
        broker_symbol=broker_symbol,
        retrieved_at=retrieved_at,
        policy=policy,
    )
    ticks = [t for t in ticks if start_utc <= t.timestamp < end_utc]
    report = validate_ticks(ticks, config=quality)
    path, prov = write_tick_dataset(
        root,
        ticks,
        report=report,
        requested_start=start_utc,
        requested_end=end_utc,
        retrieved_at=retrieved_at,
        timezone_policy=policy.to_dict(),
        broker_account_kind=broker_account_kind,
    )
    return path, prov, report
