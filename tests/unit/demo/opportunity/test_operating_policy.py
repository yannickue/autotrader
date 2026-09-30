# ruff: noqa: E501
"""Lane P: live operating policy (Berlin flatten deadline, per-market deadline, entry window overlay, broker sessions).

Required scenarios (A B C K L M) + research/default invariants. Pure: no broker, no runner."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from demo.opportunity.clock import forced_flat_utc, live_spec, market_flat_utc
from demo.opportunity.operating_policy import (
    ENTRY_FLATTEN_WINDOW,
    ENTRY_RUNWAY,
    SESSION_OPEN,
    SESSION_PAUSE,
    SESSION_WEEKEND,
    OperatingPolicyError,
    load_operating_policy,
    policy_from_dict,
)
from markets.spec import load_market_spec

BERLIN = ZoneInfo("Europe/Berlin")
SUMMER = (2026, 7, 15)  # Wed, CEST / EDT (both DST)
WINTER = (2026, 12, 15)  # Tue, CET / EST
MISMATCH = (2026, 10, 28)  # Wed, EU winter time since 25 Oct, US still EDT until 1 Nov


def berlin(day: tuple[int, int, int], hh: int, mm: int = 0) -> datetime:
    return datetime(*day, hh, mm, tzinfo=BERLIN).astimezone(UTC)


@pytest.fixture(scope="module")
def pol():
    return load_operating_policy()


def _flat_berlin(pol, market, day, hh=10, mm=0):
    spec = load_market_spec(market)
    sig = berlin(day, hh, mm)
    return forced_flat_utc(spec, sig, spec.calendar.forced_flat_min, pol).astimezone(BERLIN)


# ------------------------------------------------------------------------------------------ config
def test_policy_is_versioned_hashed_and_carries_the_user_rules(pol):
    assert pol.version == "live-op-1" and len(pol.policy_hash) == 16
    assert (pol.flatten_start_min, pol.deadline_min) == (21 * 60 + 55, 22 * 60)
    assert pol.tz == "Europe/Berlin" and pol.broker_close_buffer_min == 5
    assert pol.min_entry_runway_min == 10
    assert pol.operating_days == frozenset({0, 1, 2, 3, 4})
    assert load_operating_policy().policy_hash == pol.policy_hash  # deterministic


def test_bad_policy_is_refused():
    with pytest.raises(OperatingPolicyError):
        policy_from_dict({})
    raw = {"policy": {"version": "x", "global_flat_deadline": "21:00", "flatten_start": "21:30",
                      "broker_close_buffer_min": 5, "min_entry_runway_min": 10,
                      "operating_days": ["Mon"], "flatten_retry_backoff_s": [5]}}
    with pytest.raises(OperatingPolicyError):
        policy_from_dict(raw)  # flatten start must precede the deadline


# ----------------------------------------------------------------- A / B / C: late NAS100 entries
def test_A_entry_at_2110_berlin_is_allowed(pol):
    """No 21:00 entry cutoff: NAS100/SPX500 research window ends 15:00 NY (= 21:00 Berlin); live it does not."""
    for market in ("NAS100", "SPX500"):
        spec = load_market_spec(market)
        assert spec.calendar.entry_end_min == 15 * 60  # research window untouched
        for day in (SUMMER, WINTER):
            sig = berlin(day, 21, 10)
            ov = live_spec(spec, pol, sig)
            assert ov is not None and ov.calendar.entry_end_min > 15 * 60 + 10  # 21:10 Berlin = 15:10 NY
            flat = forced_flat_utc(spec, sig, spec.calendar.forced_flat_min, pol)
            assert pol.entry_refusal(market, sig, flat) is None


def test_B_entry_at_2140_berlin_is_allowed_before_the_flatten_window(pol):
    spec = load_market_spec("NAS100")
    sig = berlin(SUMMER, 21, 40)
    ov = live_spec(spec, pol, sig)
    local_min = 15 * 60 + 40
    assert ov is not None and local_min < ov.calendar.entry_end_min  # 15:40 NY inside the live window
    flat = forced_flat_utc(spec, sig, spec.calendar.forced_flat_min, pol)
    assert pol.entry_refusal("NAS100", sig, flat) is None
    # the runway: 21:45 (10 min before 21:55) is the first refused minute
    assert live_spec(spec, pol, berlin(SUMMER, 21, 45)).calendar.entry_end_min == 15 * 60 + 45
    assert pol.entry_refusal("NAS100", berlin(SUMMER, 21, 46), flat) == ENTRY_RUNWAY


def test_C_flatten_phase_blocks_new_exposure(pol):
    spec = load_market_spec("NAS100")
    for day in (SUMMER, WINTER):
        sig = berlin(day, 21, 55)
        assert pol.flatten_active(sig) and not pol.flatten_active(berlin(day, 21, 54))
        assert live_spec(spec, pol, sig) is None
        flat = forced_flat_utc(spec, berlin(day, 10), spec.calendar.forced_flat_min, pol)
        assert pol.entry_refusal("NAS100", sig, flat) == ENTRY_FLATTEN_WINDOW
    assert pol.flatten_active(berlin(SUMMER, 23, 59)) and not pol.flatten_active(berlin(SUMMER, 0, 1))


def test_default_research_behaviour_is_bit_identical_without_a_policy():
    for market in ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD"):
        spec = load_market_spec(market)
        sig = berlin(SUMMER, 12)
        assert live_spec(spec, None, sig) is spec
        assert forced_flat_utc(spec, sig, spec.calendar.forced_flat_min) == market_flat_utc(spec, sig, spec.calendar.forced_flat_min)


def test_markets_whose_strategy_windows_end_earlier_keep_them(pol):
    for market in ("GER40", "EURUSD", "XAUUSD"):
        spec = load_market_spec(market)
        ov = live_spec(spec, pol, berlin(SUMMER, 12))
        assert ov is not None and ov.calendar.entry_end_min == spec.calendar.entry_end_min


# ----------------------------------------------------------------------------- K / L: DST tables
@pytest.mark.parametrize(("market", "summer", "winter"), [
    ("NAS100", "21:55", "21:55"),
    ("SPX500", "21:55", "21:55"),
    ("GER40", "21:30", "21:30"),
    ("EURUSD", "17:55", "17:55"),
    ("XAUUSD", "17:55", "17:55"),
    ("BRENT", "18:55", "18:55"),
    ("BTCUSD", "21:55", "21:30"),  # 20:30 UTC = 22:30 Berlin in summer (violates 22:00) -> flatten start wins
])
def test_K_L_effective_flat_in_berlin_summer_and_winter(pol, market, summer, winter):
    assert _flat_berlin(pol, market, SUMMER).strftime("%H:%M") == summer
    assert _flat_berlin(pol, market, WINTER).strftime("%H:%M") == winter


def test_no_market_is_ever_flat_after_the_global_deadline(pol):
    for market in ("NAS100", "SPX500", "GER40", "EURUSD", "XAUUSD", "BRENT", "BTCUSD"):
        for day in (SUMMER, WINTER, MISMATCH):
            flat = _flat_berlin(pol, market, day)
            assert flat.hour * 60 + flat.minute <= pol.flatten_start_min, (market, day, flat)
            assert flat <= pol.deadline_utc(flat.date()).astimezone(BERLIN)


def test_L_us_eu_dst_mismatch_week_flattens_an_hour_earlier_in_berlin(pol):
    # NY is still on EDT, Berlin already on CET: 15:55 NY = 20:55 Berlin (the market-local flat wins)
    assert _flat_berlin(pol, "NAS100", MISMATCH).strftime("%H:%M") == "20:55"
    spec = load_market_spec("NAS100")
    ov = live_spec(spec, pol, berlin(MISMATCH, 20, 30))
    assert ov is not None and ov.calendar.entry_end_min == 15 * 60 + 45  # 20:45 Berlin = 15:45 NY
    late = live_spec(spec, pol, berlin(MISMATCH, 20, 46))  # 15:46 NY: past the runway (15:45) of the earlier flat
    assert late is not None and late.calendar.entry_end_min <= 15 * 60 + 46


# ------------------------------------------------------------------------ M: earlier broker close
def test_M_instrument_closing_before_2200_gets_an_earlier_effective_deadline(pol):
    raw = {"policy": {"version": "t", "global_flat_deadline": "22:00", "flatten_start": "21:55",
                      "broker_close_buffer_min": 5, "min_entry_runway_min": 10, "operating_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                      "flatten_retry_backoff_s": [5]},
           "markets": {"GER40": {"session": {"status": "provisional", "source": "synthetic", "pauses": [
               {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "20:30", "end": "00:00"}]}}}}
    early = policy_from_dict(raw)
    spec = load_market_spec("GER40")
    sig = berlin(SUMMER, 10)
    flat = forced_flat_utc(spec, sig, spec.calendar.forced_flat_min, early).astimezone(BERLIN)
    assert flat.strftime("%H:%M") == "20:25"  # broker close 20:30 - 5 min buffer, before flatten start and market flat
    assert forced_flat_utc(spec, sig, spec.calendar.forced_flat_min, pol).astimezone(BERLIN).strftime("%H:%M") == "21:30"
    # the sweep of such an instrument starts at that earlier instant, the global one does not
    assert early.sweep_start_utc("GER40", berlin(SUMMER, 20, 40)).astimezone(BERLIN).strftime("%H:%M") == "20:25"
    assert pol.sweep_start_utc("GER40", berlin(SUMMER, 20, 40)).astimezone(BERLIN).strftime("%H:%M") == "21:55"


def test_brent_winter_broker_break_is_the_binding_close_when_nothing_else_is(pol):
    # Brent break 20:55 UTC (provisional, UTC-fixed) = 21:55 Berlin in winter, 22:55 in summer
    close_w = pol.broker_close_utc("BRENT", berlin(WINTER, 12)).astimezone(BERLIN)
    close_s = pol.broker_close_utc("BRENT", berlin(SUMMER, 12)).astimezone(BERLIN)
    assert (close_w.strftime("%H:%M"), close_s.strftime("%H:%M")) == ("21:55", "22:55")
    assert pol.sweep_start_utc("BRENT", berlin(WINTER, 12)).astimezone(BERLIN).strftime("%H:%M") == "21:50"
    assert pol.market("BRENT").session_status == "provisional" and pol.market("NAS100").session_status == "observed"


# ---------------------------------------------------------------------------- broker sessions
def test_session_states_known_pause_weekend_and_dst(pol):
    assert pol.session_state("NAS100", berlin(SUMMER, 23, 30)) == SESSION_PAUSE
    assert pol.session_state("NAS100", berlin(WINTER, 23, 30)) == SESSION_PAUSE
    assert pol.session_state("NAS100", berlin(SUMMER, 22, 30)) == SESSION_OPEN  # after the cash close, still open
    assert pol.session_state("NAS100", berlin(SUMMER, 0, 30)) == SESSION_OPEN
    assert pol.session_state("NAS100", berlin((2026, 7, 18), 12)) == SESSION_WEEKEND  # Saturday
    assert pol.session_state("NAS100", berlin((2026, 7, 17), 23, 30)) == SESSION_PAUSE  # Friday close
    assert pol.session_state("GER40", berlin(SUMMER, 22, 30)) == SESSION_PAUSE
    # GER40 reopens 00:15 UTC = 02:15 Berlin in summer, 01:15 Berlin in winter
    assert pol.session_state("GER40", berlin(SUMMER, 2, 10)) == SESSION_PAUSE
    assert pol.session_state("GER40", berlin(SUMMER, 2, 20)) == SESSION_OPEN
    assert pol.session_state("GER40", berlin(WINTER, 1, 10)) == SESSION_PAUSE
    assert pol.session_state("GER40", berlin(WINTER, 1, 20)) == SESSION_OPEN
    assert pol.session_state("EURUSD", berlin(SUMMER, 23, 30)) == SESSION_OPEN  # no daily break
    assert pol.session_state("NOPE", berlin(SUMMER, 12)) is None  # unknown market -> caller falls back


def test_operating_day_is_the_berlin_date(pol):
    assert pol.is_operating_day(berlin((2026, 7, 17), 23, 59))  # Friday
    assert not pol.is_operating_day(berlin((2026, 7, 18), 0, 1))  # Saturday 00:01 Berlin
    # Friday 22:30 UTC is already Saturday in Berlin (CEST): not an operating day
    assert not pol.is_operating_day(datetime(2026, 7, 17, 22, 30, tzinfo=UTC))


def test_overlay_never_breaks_the_calendar_invariants(pol):
    for market in ("NAS100", "SPX500", "GER40", "EURUSD", "XAUUSD", "BRENT", "BTCUSD"):
        spec = load_market_spec(market)
        for day in (SUMMER, WINTER, MISMATCH):
            for hh in range(0, 24):
                ov = live_spec(spec, pol, berlin(day, hh, 30))
                if ov is None:
                    continue
                c = ov.calendar
                assert c.entry_start_min < c.entry_end_min <= c.forced_flat_min
                if ov is not spec:  # only the entry end may differ from the research spec
                    assert dataclasses.replace(c, entry_end_min=spec.calendar.entry_end_min) == spec.calendar
