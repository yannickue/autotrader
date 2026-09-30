# ruff: noqa: E501
"""ClockCheck per market: UTC -> market tz -> DST -> local minute -> session bucket -> SimWindow.

The market's own zone is used everywhere: a New York market never sees Berlin minutes, London markets
never see Berlin minutes, and DST transitions (US 2026-03-08 / 2026-11-01, EU 2026-03-29 / 2026-10-25)
move the UTC instant of the cash open while the LOCAL minute stays constant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from alpha.families.spec import MarketCalendar
from demo.opportunity.clock import forced_flat_utc, local_minute_of, make_clock_check
from demo.opportunity.engine import assemble_live


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def _berlin_minute(dt):
    b = dt.astimezone(ZoneInfo("Europe/Berlin"))
    return b.hour * 60 + b.minute


# (market, utc instant of the CASH OPEN, expected utc offset minutes, note)
CASH_OPEN_CASES = [
    # GER40 09:00 Berlin: EU DST 2026-03-29 and 2026-10-25
    ("GER40", _utc(2026, 3, 27, 8, 0), 60, "CET before EU DST"),
    ("GER40", _utc(2026, 3, 30, 7, 0), 120, "CEST after EU DST"),
    ("GER40", _utc(2026, 10, 23, 7, 0), 120, "CEST before EU end"),
    ("GER40", _utc(2026, 10, 26, 8, 0), 60, "CET after EU end"),
    # NAS100 / SPX500 09:30 New York: US DST 2026-03-08 and 2026-11-01
    ("NAS100", _utc(2026, 3, 6, 14, 30), -300, "EST before US DST"),
    ("NAS100", _utc(2026, 3, 9, 13, 30), -240, "EDT after US DST"),
    ("NAS100", _utc(2026, 3, 16, 13, 30), -240, "US/EU mismatch week (Berlin still CET)"),
    ("NAS100", _utc(2026, 10, 26, 13, 30), -240, "US/EU mismatch week autumn (Berlin CET, NY EDT)"),
    ("NAS100", _utc(2026, 11, 2, 14, 30), -300, "EST after US end"),
    ("SPX500", _utc(2026, 3, 6, 14, 30), -300, "EST before US DST"),
    ("SPX500", _utc(2026, 3, 9, 13, 30), -240, "EDT after US DST"),
    ("SPX500", _utc(2026, 11, 2, 14, 30), -300, "EST after US end"),
    # XAUUSD / EURUSD 08:00 London (provisional calendars)
    ("XAUUSD", _utc(2026, 3, 27, 8, 0), 0, "GMT"),
    ("XAUUSD", _utc(2026, 3, 30, 7, 0), 60, "BST"),
    ("XAUUSD", _utc(2026, 10, 26, 8, 0), 0, "GMT after end"),
    ("EURUSD", _utc(2026, 3, 30, 7, 0), 60, "BST"),
    ("EURUSD", _utc(2026, 10, 23, 7, 0), 60, "BST before end"),
    ("EURUSD", _utc(2026, 10, 26, 8, 0), 0, "GMT after end"),
]


@pytest.mark.parametrize(("market", "utc", "offset", "note"), CASH_OPEN_CASES)
def test_cash_open_is_the_same_local_minute_across_dst(mspecs, market, utc, offset, note):
    spec = mspecs[market]
    win = MarketCalendar.from_market_spec(spec).window()
    from alpha.families.spec import EffectiveWindow

    ew = EffectiveWindow(win.entry_start_min, win.entry_end_min, win.flat_min)
    cc = make_clock_check(spec, utc, ew)
    assert cc.local_minute == spec.calendar.cash_open_min, note
    assert cc.utc_offset_min == offset, note
    assert cc.market_tz == spec.calendar.tz
    assert cc.in_entry_window is True
    assert cc.calendar_status == spec.calendar.status
    assert cc.minutes_to_forced_flat == spec.calendar.forced_flat_min - cc.local_minute
    assert local_minute_of(spec, utc) == cc.local_minute
    assert cc.local_iso.endswith(f"{'+' if offset >= 0 else '-'}{abs(offset) // 60:02d}:{abs(offset) % 60:02d}")


def test_berlin_minutes_are_never_applied_to_other_markets(mspecs):
    ew_for = lambda s: MarketCalendar.from_market_spec(s).window()  # noqa: E731
    # US/EU mismatch week: NY cash open 13:30 UTC is 14:30 Berlin (870), NOT 570
    utc = _utc(2026, 3, 16, 13, 30)
    assert _berlin_minute(utc) == 870
    assert local_minute_of(mspecs["NAS100"], utc) == 570
    assert local_minute_of(mspecs["SPX500"], utc) == 570
    assert local_minute_of(mspecs["GER40"], utc) == 870
    assert ew_for(mspecs["NAS100"]).entry_start_min == 570
    # London markets in summer: 08:00 London == 09:00 Berlin
    summer = _utc(2026, 6, 10, 7, 0)
    assert local_minute_of(mspecs["XAUUSD"], summer) == 480
    assert _berlin_minute(summer) == 540


FORCED_FLAT_CASES = [
    ("GER40", _utc(2026, 6, 10, 8, 0), datetime(2026, 6, 10, 19, 30, tzinfo=UTC)),  # 21:30 CEST
    ("GER40", _utc(2026, 12, 10, 8, 0), datetime(2026, 12, 10, 20, 30, tzinfo=UTC)),  # 21:30 CET
    ("NAS100", _utc(2026, 6, 10, 14, 0), datetime(2026, 6, 10, 19, 55, tzinfo=UTC)),  # 15:55 EDT
    ("NAS100", _utc(2026, 12, 10, 15, 0), datetime(2026, 12, 10, 20, 55, tzinfo=UTC)),  # 15:55 EST
    ("SPX500", _utc(2026, 3, 16, 14, 0), datetime(2026, 3, 16, 19, 55, tzinfo=UTC)),  # mismatch week
    ("XAUUSD", _utc(2026, 6, 10, 9, 0), datetime(2026, 6, 10, 15, 55, tzinfo=UTC)),  # 16:55 BST
    ("EURUSD", _utc(2026, 12, 10, 9, 0), datetime(2026, 12, 10, 16, 55, tzinfo=UTC)),  # 16:55 GMT
]


@pytest.mark.parametrize(("market", "entry", "expected"), FORCED_FLAT_CASES)
def test_forced_flat_utc_uses_the_market_zone(mspecs, market, entry, expected):
    spec = mspecs[market]
    assert forced_flat_utc(spec, entry, spec.calendar.forced_flat_min) == expected


@pytest.mark.parametrize("market", ["GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD"])
def test_session_bucket_and_window_edges(mspecs, market):
    from alpha.families.spec import EffectiveWindow

    spec = mspecs[market]
    cal = spec.calendar
    ew = EffectiveWindow(cal.entry_start_min, cal.entry_end_min, cal.forced_flat_min)
    tz = ZoneInfo(cal.tz)

    def at(minute):
        loc = datetime(2026, 6, 10, minute // 60, minute % 60, tzinfo=tz)
        return make_clock_check(spec, loc.astimezone(UTC), ew)

    assert at(cal.entry_start_min).in_entry_window
    assert not at(cal.entry_start_min - 5).in_entry_window
    assert at(cal.entry_end_min - 5).in_entry_window
    assert not at(cal.entry_end_min).in_entry_window
    assert at(cal.forced_flat_min - 5).minutes_to_forced_flat == 5
    names = {b.name for b in cal.buckets}
    assert at(cal.entry_start_min).session_bucket in names
    weekend = datetime(2026, 6, 13, cal.entry_start_min // 60, cal.entry_start_min % 60, tzinfo=tz)
    assert not make_clock_check(spec, weekend.astimezone(UTC), ew).in_entry_window  # Saturday


@pytest.mark.parametrize(
    ("market", "start", "end"),
    [
        ("NAS100", "2026-03-05T12:00", "2026-03-11T22:00"),  # US DST 2026-03-08
        ("NAS100", "2025-10-30T12:00", "2025-11-04T22:00"),  # US DST end 2025-11-02
        ("GER40", "2026-03-26T12:00", "2026-03-31T20:00"),  # EU DST 2026-03-29
        ("SPX500", "2026-03-12T12:00", "2026-03-18T22:00"),  # mismatch week
    ],
)
def test_clock_minute_matches_the_family_data_minute_basis(frames, mspecs, market, start, end):
    """ClockCheck.local_minute of the ENTRY instant == FamilyData.minute of the entry bar (SimWindow basis)."""
    spec = mspecs[market]
    fr = frames[market]
    ts = pd.DatetimeIndex(fr["ts"])
    sel = fr[(ts >= pd.Timestamp(start, tz="UTC")) & (ts < pd.Timestamp(end, tz="UTC"))].reset_index(drop=True)
    assert len(sel) > 500
    data = assemble_live(market, spec, sel)  # last row is the placeholder
    checked = 0
    for k in range(len(sel) - 1):
        if not data.contig_next[k]:
            continue
        entry_utc = datetime.fromtimestamp(int(data.ts_ns[k + 1]) // 10**9, tz=UTC)
        assert local_minute_of(spec, entry_utc) == int(data.minute[k + 1])
        checked += 1
    assert checked > 400
    assert np.unique(data.minute).size > 100
