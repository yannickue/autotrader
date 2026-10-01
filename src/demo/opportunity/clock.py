# ruff: noqa: E501
"""Clock verification (addendum 3): UTC -> market tz -> DST -> local minute -> session -> SimWindow.

The chain is always started from a tz-aware UTC instant and converted with ``zoneinfo`` to the
MARKET's own zone (``MarketSpec.calendar.tz``), so a New-York market never sees Berlin minutes and
DST transitions are handled by the tz database, not by constants. The minute basis is exactly the
one of ``alpha.fast.sim.SimWindow`` (local minute of the bar OPEN; the entry bar opens at the
signal timestamp = close of the deciding bar).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from alpha.families.spec import EffectiveWindow
from demo.contracts import ClockCheck
from markets.spec import MarketSpec

if TYPE_CHECKING:  # the policy is live-only; research code never imports it
    from demo.opportunity.operating_policy import OperatingPolicy


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


def market_flat_utc(spec: MarketSpec, entry_utc: datetime, exit_min: int) -> datetime:
    """UTC instant of the MARKET-LOCAL clock exit on the local calendar day of the entry bar (research semantics)."""
    loc = local_of(spec, entry_utc)
    tz = ZoneInfo(spec.calendar.tz)
    # wall-clock arithmetic in the local zone, then to UTC (DST-correct for afternoon exits)
    naive = datetime.combine(loc.date(), time(0, 0)) + timedelta(minutes=exit_min)
    return naive.replace(tzinfo=tz).astimezone(UTC)


def forced_flat_utc(
    spec: MarketSpec, entry_utc: datetime, exit_min: int, operating: OperatingPolicy | None = None,
) -> datetime:
    """UTC instant of the clock exit: THE single choke point of the forced-flat time.

    ``operating=None`` (research, tests, default) = the market-local clock exit on the LOCAL calendar day of the
    entry bar, bit-identical to before.  With the live operating policy it is the EARLIEST of the market-local
    exit, the Berlin flatten start of the entry's Berlin day and (broker session close - safety buffer), i.e. the
    per-instrument deadline is MIN(22:00 Berlin global deadline, broker close - buffer) with the flatten phase
    starting ~21:55 Berlin."""
    flat = market_flat_utc(spec, entry_utc, exit_min)
    if operating is None:
        return flat
    return operating.effective_flat_utc(spec.canonical, flat, to_utc(entry_utc))


def live_spec(spec: MarketSpec, operating: OperatingPolicy | None, signal_utc: datetime) -> MarketSpec | None:
    """Live overlay of a MarketSpec for the entry bar ``signal_utc``: the per-market live ``entry_end`` replaces
    the research one (clamped to the local flat) and the entry window is cut at (effective flat - runway), so the
    families generate candidates exactly up to the flatten runway.  None = no entry is possible at all.
    ``operating=None`` returns ``spec`` itself (research / default unchanged; the market_spec hash is untouched)."""
    if operating is None:
        return spec
    cal = spec.calendar
    end = cal.entry_end_min
    mkt = operating.market(spec.canonical)
    live_end = cal.forced_flat_min if mkt.entry_end_live_to_flat else mkt.entry_end_live_min
    if live_end is not None:
        end = min(max(end, live_end), cal.forced_flat_min)
    sig = to_utc(signal_utc)
    if operating.flatten_active(sig):
        return None
    cutoff = forced_flat_utc(spec, sig, cal.forced_flat_min, operating) - timedelta(minutes=operating.min_entry_runway_min)
    cut_loc = local_of(spec, cutoff)
    sig_day = local_of(spec, sig).date()
    if cut_loc.date() < sig_day:
        return None
    if cut_loc.date() == sig_day:
        end = min(end, cut_loc.hour * 60 + cut_loc.minute)
    if end <= cal.entry_start_min:
        return None
    if end == cal.entry_end_min:
        return spec
    return dataclasses.replace(spec, calendar=dataclasses.replace(cal, entry_end_min=end))


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
