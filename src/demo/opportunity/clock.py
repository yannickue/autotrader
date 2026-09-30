"""Clock verification (addendum 3): UTC -> market tz -> DST -> local minute -> session -> SimWindow.

The chain is always started from a tz-aware UTC instant and converted with ``zoneinfo`` to the
MARKET's own zone (``MarketSpec.calendar.tz``), so a New-York market never sees Berlin minutes and
DST transitions are handled by the tz database, not by constants. The minute basis is exactly the
one of ``alpha.fast.sim.SimWindow`` (local minute of the bar OPEN; the entry bar opens at the
signal timestamp = close of the deciding bar).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from alpha.families.spec import EffectiveWindow
from demo.contracts import ClockCheck
from markets.spec import MarketSpec


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC)


def local_of(spec: MarketSpec, utc: datetime) -> datetime:
    return to_utc(utc).astimezone(ZoneInfo(spec.calendar.tz))


def local_minute_of(spec: MarketSpec, utc: datetime) -> int:
    loc = local_of(spec, utc)
    return loc.hour * 60 + loc.minute


def session_bucket_of(spec: MarketSpec, minute: int) -> str:
    for b in spec.calendar.buckets:
        if b.start_min <= minute < b.end_min:
            return b.name
    raise ValueError(f"minute {minute} not covered by buckets")  # buckets tile 00:00-24:00


def forced_flat_utc(spec: MarketSpec, entry_utc: datetime, exit_min: int) -> datetime:
    """UTC instant of the clock exit on the LOCAL calendar day of the entry bar."""
    loc = local_of(spec, entry_utc)
    tz = ZoneInfo(spec.calendar.tz)
    # wall-clock arithmetic in the local zone, then to UTC (DST-correct for afternoon exits)
    naive = datetime.combine(loc.date(), time(0, 0)) + timedelta(minutes=exit_min)
    return naive.replace(tzinfo=tz).astimezone(UTC)


def make_clock_check(spec: MarketSpec, entry_utc: datetime, window: EffectiveWindow) -> ClockCheck:
    """ClockCheck of the ENTRY instant (= signal timestamp) against the spec's effective window."""
    utc = to_utc(entry_utc)
    loc = local_of(spec, utc)
    minute = loc.hour * 60 + loc.minute
    offset = loc.utcoffset()
    assert offset is not None
    return ClockCheck(
        utc=utc.isoformat(),
        market_tz=spec.calendar.tz,
        local_iso=loc.isoformat(),
        utc_offset_min=int(offset.total_seconds() // 60),
        local_minute=minute,
        session_bucket=session_bucket_of(spec, minute),
        in_entry_window=(
            loc.weekday() < 5 and window.entry_start_min <= minute < window.entry_end_min
        ),
        minutes_to_forced_flat=window.exit_min - minute,
        calendar_status=spec.calendar.status,
    )
