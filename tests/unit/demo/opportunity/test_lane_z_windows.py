# ruff: noqa: E501
"""Lane Z: M3 (live entry-window extension must not change the frozen EOD family semantics) and the BTCUSD live entry
window computed per season from the flatten runway (no hard-coded clock string)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from alpha.families.eod import EODSpec, grid, session_end
from alpha.families.spec import MarketCalendar
from demo.opportunity.clock import live_spec
from demo.opportunity.engine import live_family_calendar
from demo.opportunity.operating_policy import load_operating_policy
from markets.phase2 import load_phase2_spec
from markets.spec import load_market_spec

POL = load_operating_policy()
BERLIN = ZoneInfo("Europe/Berlin")
NY = ZoneInfo("America/New_York")


def _utc(y, mo, d, hh, mm, tz=UTC) -> datetime:
    return datetime(y, mo, d, hh, mm, tzinfo=tz).astimezone(UTC)


# ------------------------------------------------------------------------------------------ M3: EOD golden
@pytest.mark.parametrize("market", ["NAS100", "SPX500", "GER40"])
@pytest.mark.parametrize("signal", [_utc(2026, 7, 15, 14, 5, NY), _utc(2026, 12, 16, 14, 5, NY), _utc(2026, 7, 15, 10, 0, BERLIN)])
def test_M3_eod_windows_and_session_end_are_bit_identical_to_research_after_the_live_extension(market, signal):
    ms = load_market_spec(market)
    research = MarketCalendar.from_market_spec(ms)
    overlay = live_spec(ms, POL, signal)
    assert overlay is not None
    live_cal = live_family_calendar(overlay, ms)
    assert session_end(live_cal) == session_end(research)  # T = min(cash_close, RESEARCH entry_end)
    for spec in grid(None):
        assert isinstance(spec, EODSpec)
        assert spec.effective_window(live_cal) == spec.effective_window(research)
        assert spec.exit_min(live_cal) == spec.exit_min(research)


def test_M3_the_live_extension_itself_is_real_and_only_the_eod_pin_neutralises_it():
    """NAS100/SPX500 live entries now run to the 15:55 NY runway cut; without the pin the EOD window would move from
    14:00-15:00 to 14:55-15:45 NY (thresholds fitted on the old window)."""
    for market in ("NAS100", "SPX500"):
        ms = load_market_spec(market)
        overlay = live_spec(ms, POL, _utc(2026, 7, 15, 14, 5, NY))
        assert overlay is not None and overlay.calendar.entry_end_min > ms.calendar.entry_end_min  # gating extended
        assert session_end(MarketCalendar.from_market_spec(overlay)) != session_end(MarketCalendar.from_market_spec(ms))  # the bug
        assert session_end(live_family_calendar(overlay, ms)) == session_end(MarketCalendar.from_market_spec(ms))  # the pin


def test_M3_without_a_live_overlay_the_calendar_is_the_research_one():
    ms = load_market_spec("GER40")
    assert live_family_calendar(ms, ms) == MarketCalendar.from_market_spec(ms)


# ------------------------------------------------------------------------------------------ BTC window
def _btc_end_utc_minute(signal: datetime) -> int | None:
    overlay = live_spec(load_phase2_spec("BTCUSD"), POL, signal)
    return None if overlay is None else overlay.calendar.entry_end_min


@pytest.mark.parametrize(
    "label,signal,expected_end_utc",
    [
        ("summer Wed: flat 21:55 Berlin = 19:55 UTC, entries to 21:45 Berlin = 19:45 UTC", _utc(2026, 7, 15, 18, 0), 19 * 60 + 45),
        ("winter Wed: the research flat 20:30 UTC binds (21:30 Berlin), entries to 20:20 UTC", _utc(2026, 12, 16, 18, 0), 20 * 60 + 20),
        ("Fri before the autumn DST change (CEST): break 20:55 UTC is after the 19:55 UTC flat", _utc(2026, 10, 23, 18, 0), 19 * 60 + 45),
        ("Mon after the autumn DST change (CET)", _utc(2026, 10, 26, 18, 0), 20 * 60 + 20),
        ("winter Fri: flat = min(20:30 market, 20:55 Berlin start, 20:55 break - 5 = 20:50) = 20:30", _utc(2026, 12, 18, 18, 0), 20 * 60 + 20),
        ("Fri before the spring DST change (CET)", _utc(2027, 3, 26, 18, 0), 20 * 60 + 20),
        ("Mon after the spring DST change (CEST)", _utc(2027, 3, 29, 17, 0), 19 * 60 + 45),
    ],
)
def test_btc_live_entry_end_follows_the_flatten_runway_per_season(label, signal, expected_end_utc):
    assert _btc_end_utc_minute(signal) == expected_end_utc, label


def test_btc_entry_window_is_extended_beyond_the_research_entry_end_and_never_past_the_runway():
    research_end = load_phase2_spec("BTCUSD").calendar.entry_end_min  # 19:30 UTC
    for signal in (_utc(2026, 7, 15, 18, 0), _utc(2026, 12, 16, 18, 0)):
        end = _btc_end_utc_minute(signal)
        assert end is not None and end > research_end
    # inside the last 10 minutes before the effective flat there is no entry window left to generate in
    summer_late = live_spec(load_phase2_spec("BTCUSD"), POL, _utc(2026, 7, 15, 19, 50))
    assert summer_late is None or summer_late.calendar.entry_end_min <= 19 * 60 + 45


def test_btc_research_spec_and_no_policy_are_untouched():
    ms = load_phase2_spec("BTCUSD")
    assert live_spec(ms, None, _utc(2026, 7, 15, 18, 0)) is ms  # research / default: identical object


def test_brent_window_is_deliberately_unchanged():
    assert POL.market("BRENT").entry_end_live_min is None and not POL.market("BRENT").entry_end_live_to_flat
    ms = load_phase2_spec("BRENT")
    overlay = live_spec(ms, POL, _utc(2026, 7, 15, 9, 0, BERLIN))
    assert overlay is None or overlay.calendar.entry_end_min <= ms.calendar.entry_end_min
