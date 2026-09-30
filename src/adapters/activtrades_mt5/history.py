"""MT5 historical read path: fetch + schema-check + normalize to `BarRecord`/`TickRecord`.

Real field names/dtypes, observed directly against the live ActivTrades DEMO
terminal (2026-09-29, `scripts/mt5_diag_inspect_rates_ticks.py`), never guessed:

    copy_rates_*  -> numpy.ndarray, fields:
        time (<i8, epoch SECONDS = bar OPEN time), open/high/low/close (<f8),
        tick_volume (<u8), spread (<i4, POINTS), real_volume (<u8, observed 0)

    copy_ticks_*  -> numpy.ndarray, fields:
        time (<i8), bid/ask/last (<f8), volume (<u8), time_msc (<i8, epoch
        MILLISECONDS), flags (<u4), volume_real (<f8)

Rows are `numpy.void` structured scalars: subscript access only.

TIME (critical, observed): MT5 `time` values are the terminal SERVER wall clock
encoded as if it were UTC epoch -- they are NOT true UTC. Measured on
2026-09-29: `symbol_info_tick.time_msc/1000 - system_utc == +7199s` (server
= UTC+2 during CEST). `ServerTimePolicy` converts via `Europe/Berlin`
(server-clock rule INFERRED, not broker-confirmed -- see DEVELOPMENT_LEDGER).
Conversions are fail-closed on ambiguous/non-existent local times (DST
transition hours). Requests to `copy_*_range` must be expressed in server
epoch too (`ServerTimePolicy.utc_to_request_datetime`), otherwise the tail of
the window is silently clipped (observed: a true-UTC `now` end returned data
2h short).

VOLUME: ActivTrades CFD volume is broker tick activity, never exchange volume;
always tagged `BROKER_TICK_ACTIVITY`. `real_volume`/tick `volume` observed 0.
Tick `last` is 0.0 for this CFD index -> `None`, never a fabricated price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from data.parquet_store import BarRecord, TickRecord
from data.provenance import SemanticType, Source

# MT5 TIMEFRAME_* constants (duplicated to keep this module importable without
# the MetaTrader5 package). Only timeframes actually verified are mapped.
TIMEFRAME_LABELS: dict[int, str] = {
    1: "1m",
    5: "5m",
    15: "15m",
    30: "30m",
    16385: "1h",
    16388: "4h",
    16408: "1d",
}
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}
COPY_TICKS_ALL = -1

RATES_FIELDS = (
    "time",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
)
TICKS_FIELDS = ("time", "bid", "ask", "last", "volume", "time_msc", "flags", "volume_real")
# Full observed dtypes (MetaTrader5 5.0.6231, live ActivTrades demo, 2026-09-29).
RATES_DTYPES = ("<i8", "<f8", "<f8", "<f8", "<f8", "<u8", "<i4", "<u8")
TICKS_DTYPES = ("<i8", "<f8", "<f8", "<f8", "<u8", "<i8", "<u4", "<f8")

# Canonical instrument -> candidate broker symbols (exact match, in priority
# order). 'Ger40Dec26' (dated future-style contract) is deliberately absent.
CANONICAL_BROKER_SYMBOLS: dict[str, tuple[str, ...]] = {"GER40": ("Ger40",)}
CASH_INDEX_PATH_PREFIX = "Cash Indices"


class MT5HistoryError(RuntimeError):
    """Base error for the historical read path (fail closed)."""


class MT5SchemaError(MT5HistoryError):
    """Returned array does not have the observed field layout."""


class MT5SymbolResolutionError(MT5HistoryError):
    """Canonical instrument could not be mapped to exactly one broker symbol."""


class AmbiguousServerTime(MT5HistoryError):
    """Server wall-clock value falls in a DST-ambiguous or non-existent hour."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ServerTimePolicy:
    """Maps MT5 server-clock epochs <-> true UTC."""

    zone_name: str = "Europe/Berlin"
    basis: str = "INFERRED_FROM_LIVE_OFFSET_+7199s_2026-09-29_NOT_BROKER_CONFIRMED"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.zone_name)

    def server_epoch_to_utc(self, epoch_seconds: float) -> datetime:
        """Server-clock epoch (seconds, may be fractional) -> aware UTC datetime."""
        naive = datetime.fromtimestamp(float(epoch_seconds), tz=UTC).replace(tzinfo=None)
        zone = self.zone
        first = naive.replace(tzinfo=zone, fold=0)
        second = naive.replace(tzinfo=zone, fold=1)
        utc_first = first.astimezone(UTC)
        utc_second = second.astimezone(UTC)
        if utc_first != utc_second:
            raise AmbiguousServerTime(
                f"ambiguous server time {naive.isoformat()} ({self.zone_name})"
            )
        if utc_first.astimezone(zone).replace(tzinfo=None) != naive:
            raise AmbiguousServerTime(
                f"non-existent server time {naive.isoformat()} ({self.zone_name})"
            )
        return utc_first

    def utc_to_request_datetime(self, moment: datetime) -> datetime:
        """True UTC -> aware datetime whose UTC epoch equals the server epoch MT5 expects."""
        if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
            raise ValueError("moment must be UTC")
        local = moment.astimezone(self.zone)
        return local.replace(tzinfo=UTC)

    def to_dict(self) -> dict[str, str]:
        return {"zone": self.zone_name, "basis": self.basis}


def _check_fields(arr: Any, names: tuple[str, ...], dtypes: tuple[str, ...], what: str) -> None:
    dtype = getattr(arr, "dtype", None)
    got_names = tuple(getattr(dtype, "names", None) or ())
    if got_names != names:
        raise MT5SchemaError(f"{what}: expected fields {names}, got {got_names}")
    got = tuple(dtype[n].str for n in got_names)
    if got != dtypes:
        raise MT5SchemaError(f"{what}: expected dtypes {dtypes}, got {got}")


def validate_rates_schema(arr: Any) -> None:
    _check_fields(arr, RATES_FIELDS, RATES_DTYPES, "copy_rates")


def validate_ticks_schema(arr: Any) -> None:
    _check_fields(arr, TICKS_FIELDS, TICKS_DTYPES, "copy_ticks")


def resolve_broker_symbol(
    client: Any,
    canonical: str,
    *,
    broker_symbol: str | None = None,
    path_prefix: str | None = None,
) -> str:
    """Resolve canonical instrument to exactly one broker symbol.

    Default (GER40 legacy): exact name from `CANONICAL_BROKER_SYMBOLS`, path under
    `CASH_INDEX_PATH_PREFIX`. V2 markets pass the OBSERVED `broker_symbol` (from the market
    spec / discovery snapshot) and the expected `path_prefix`; the name must match exactly and
    the path must start with the prefix. Never guesses.
    """
    if broker_symbol is not None:
        candidates: tuple[str, ...] = (broker_symbol,)
        prefix = path_prefix if path_prefix is not None else ""
    else:
        candidates = CANONICAL_BROKER_SYMBOLS.get(canonical.upper(), ())
        prefix = path_prefix if path_prefix is not None else CASH_INDEX_PATH_PREFIX
    if not candidates:
        raise MT5SymbolResolutionError(f"no broker-symbol mapping for {canonical!r}")
    matches = []
    for name in candidates:
        info = client.symbol_info(name)
        if info is None:
            continue
        if getattr(info, "name", None) != name:
            continue
        path = str(getattr(info, "path", ""))
        if not path.startswith(prefix):
            raise MT5SymbolResolutionError(f"{name!r} path {path!r} is not under {prefix!r}")
        matches.append(name)
    if len(matches) != 1:
        raise MT5SymbolResolutionError(
            f"expected exactly one broker symbol for {canonical!r}, found {matches}"
        )
    return matches[0]


def fetch_rates_range(
    client: Any,
    *,
    broker_symbol: str,
    mt5_timeframe: int,
    start_utc: datetime,
    end_utc: datetime,
    policy: ServerTimePolicy,
) -> Any:
    """Read-only `copy_rates_range` with server-epoch request window + schema check."""
    if mt5_timeframe not in TIMEFRAME_LABELS:
        raise MT5HistoryError(f"unmapped MT5 timeframe {mt5_timeframe}")
    rates = client.copy_rates_range(
        broker_symbol,
        mt5_timeframe,
        policy.utc_to_request_datetime(start_utc),
        policy.utc_to_request_datetime(end_utc),
    )
    if rates is None:
        raise MT5HistoryError(f"copy_rates_range returned None: {client.last_error()}")
    if len(rates) == 0:
        raise MT5HistoryError("copy_rates_range returned no rows")
    validate_rates_schema(rates)
    return rates


def fetch_ticks_range(
    client: Any,
    *,
    broker_symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    policy: ServerTimePolicy,
) -> Any:
    ticks = client.copy_ticks_range(
        broker_symbol,
        policy.utc_to_request_datetime(start_utc),
        policy.utc_to_request_datetime(end_utc),
        COPY_TICKS_ALL,
    )
    if ticks is None:
        raise MT5HistoryError(f"copy_ticks_range returned None: {client.last_error()}")
    if len(ticks) == 0:
        raise MT5HistoryError("copy_ticks_range returned no rows")
    validate_ticks_schema(ticks)
    return ticks


def _finite_decimal(value: Any, what: str) -> Decimal:
    dec = Decimal(str(float(value)))
    if not dec.is_finite():
        # Keep the non-finite marker visible to the quality layer rather than raising.
        return dec
    return dec


def mt5_rate_to_bar_record(
    raw: Any,
    *,
    canonical_symbol: str,
    broker_symbol: str,
    mt5_timeframe: int,
    retrieved_at: datetime,
    policy: ServerTimePolicy,
) -> BarRecord:
    """One raw `copy_rates_*` row -> `BarRecord` (timestamp = bar OPEN time, UTC).

    Raises `KeyError` on an unmapped timeframe and `AmbiguousServerTime` inside
    DST transition hours. Non-finite/invalid prices are preserved (not
    repaired) so the quality layer can reject them.
    """
    return BarRecord(
        canonical_symbol=canonical_symbol,
        broker_symbol=broker_symbol,
        timestamp=policy.server_epoch_to_utc(int(raw["time"])),
        timeframe=TIMEFRAME_LABELS[mt5_timeframe],
        open=_finite_decimal(raw["open"], "open"),
        high=_finite_decimal(raw["high"], "high"),
        low=_finite_decimal(raw["low"], "low"),
        close=_finite_decimal(raw["close"], "close"),
        volume=Decimal(int(raw["tick_volume"])),
        volume_semantic_type=SemanticType.BROKER_TICK_ACTIVITY,
        source=Source.ACTIVTRADES_MT5_CFD,
        quality_flags=(),
        ingested_at=retrieved_at,
        spread_points=int(raw["spread"]),
    )


def mt5_tick_to_tick_record(
    raw: Any,
    *,
    canonical_symbol: str,
    broker_symbol: str,
    retrieved_at: datetime,
    policy: ServerTimePolicy,
) -> TickRecord:
    """One raw `copy_ticks_*` row -> `TickRecord` using millisecond `time_msc`."""
    last_raw = float(raw["last"])
    return TickRecord(
        canonical_symbol=canonical_symbol,
        broker_symbol=broker_symbol,
        timestamp=policy.server_epoch_to_utc(int(raw["time_msc"]) / 1000.0),
        bid=_finite_decimal(raw["bid"], "bid"),
        ask=_finite_decimal(raw["ask"], "ask"),
        last=Decimal(str(last_raw)) if last_raw > 0 else None,
        volume=Decimal(int(raw["volume"])),
        volume_semantic_type=SemanticType.BROKER_TICK_ACTIVITY,
        source=Source.ACTIVTRADES_MT5_CFD,
        quality_flags=(),
        ingested_at=retrieved_at,
    )


def rates_to_bar_records(
    rates: Any,
    *,
    canonical_symbol: str,
    broker_symbol: str,
    mt5_timeframe: int,
    retrieved_at: datetime,
    policy: ServerTimePolicy,
    end_utc: datetime | None = None,
) -> list[BarRecord]:
    """Convert a rates array; drop any bar not fully closed by `end_utc` (open+tf > end)."""
    validate_rates_schema(rates)
    tf_seconds = TIMEFRAME_SECONDS[TIMEFRAME_LABELS[mt5_timeframe]]
    bars: list[BarRecord] = []
    for raw in rates:
        bar = mt5_rate_to_bar_record(
            raw,
            canonical_symbol=canonical_symbol,
            broker_symbol=broker_symbol,
            mt5_timeframe=mt5_timeframe,
            retrieved_at=retrieved_at,
            policy=policy,
        )
        if end_utc is not None and bar.timestamp + timedelta(seconds=tf_seconds) > end_utc:
            continue
        bars.append(bar)
    return bars


def ticks_to_tick_records(
    ticks: Any,
    *,
    canonical_symbol: str,
    broker_symbol: str,
    retrieved_at: datetime,
    policy: ServerTimePolicy,
) -> list[TickRecord]:
    validate_ticks_schema(ticks)
    return [
        mt5_tick_to_tick_record(
            raw,
            canonical_symbol=canonical_symbol,
            broker_symbol=broker_symbol,
            retrieved_at=retrieved_at,
            policy=policy,
        )
        for raw in ticks
    ]
