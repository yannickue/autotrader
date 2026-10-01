# ruff: noqa: E501
"""Lane R (d)/(e): the late-entry cutoff is DERIVED (flat - runway), never a clock constant; Brent's effective deadline.

Pure: policy + checked-in market calendars, zoneinfo for the Berlin times (summer and winter).  No broker."""

from __future__ import annotations

import dataclasses
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from demo.execution.live import StackConfig
from demo.opportunity.clock import forced_flat_utc, live_spec, local_of
from demo.opportunity.operating_policy import (
    DEFAULT_POLICY_PATH,
    derive_entry_runway,
    load_operating_policy,
    policy_from_dict,
)
from demo.opportunity.policy_report import REPORT_MARKETS, market_row, operating_policy_report
from markets.spec import load_market_spec

BERLIN = ZoneInfo("Europe/Berlin")
POL = load_operating_policy()
SUMMER_WED = (2026, 7, 15)
WINTER_WED = (2026, 12, 16)
SUMMER_FRI = (2026, 7, 17)
WINTER_FRI = (2026, 12, 18)


def sig(day, hh=10, mm=0) -> datetime:
    return datetime(*day, hh, mm, tzinfo=BERLIN).astimezone(UTC)


def _flat(canonical, day) -> datetime:
    spec = load_market_spec(canonical)
    return forced_flat_utc(spec, sig(day), spec.calendar.forced_flat_min, POL)


def _live_end_utc(canonical, day) -> datetime | None:
    spec = load_market_spec(canonical)
    s = sig(day)
    ov = live_spec(spec, POL, s)
    if ov is None:
        return None
    loc = local_of(spec, s)
    local_midnight = datetime(loc.year, loc.month, loc.day, tzinfo=ZoneInfo(spec.calendar.tz))
    return (local_midnight + timedelta(minutes=ov.calendar.entry_end_min)).astimezone(UTC)


# ------------------------------------------------------------------------------------------------ P: shipped policy
def test_P_policy_is_live_op_3_and_has_no_runway_constant_or_clock_cutoff():
    raw = tomllib.loads(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    assert raw["policy"]["version"] == "live-op-3"
    assert "min_entry_runway_min" not in raw["policy"]  # the plain constant is gone
    assert "runway" in raw["policy"] and POL.runway is not None and POL.legacy_runway_min is None
    for name, m in raw["markets"].items():  # no market carries a wall-clock entry end any more except "flat"
        assert m.get("entry_end_live", "flat") == "flat", name
    assert POL.market("NAS100").entry_end_live_to_flat and POL.market("SPX500").entry_end_live_to_flat


def test_P_runway_stack_constants_are_the_real_ones():
    cfg = StackConfig()
    assert POL.runway.flatten_wait_s == cfg.flatten_wait_s and POL.runway.close_grace_s == cfg.close_grace_s
    assert POL.retry_backoff_s[0] == 5.0


def test_P_toml_asset_classes_equal_the_market_specs():
    for m in REPORT_MARKETS:
        assert POL.market(m).asset_class == load_market_spec(m).asset_class, m


def test_P_components_sum_and_round_up_to_a_bar_multiple():
    rw = derive_entry_runway(POL, "NAS100", _flat("NAS100", SUMMER_WED))
    assert rw.decision_latency_s == 5 * 60 + 30
    assert rw.execution_reconcile_s == 35 + 5 + 35 + 90  # first attempt + backoff[0] + retry attempt + close_grace
    assert rw.broker_margin_s == 0 and rw.liquidity_allowance_s == 60 and rw.liquidity_source == "placeholder"
    assert rw.total_s == pytest.approx(330 + 165 + 0 + 60)
    assert rw.minutes % 5 == 0 and rw.minutes * 60 >= rw.total_s and (rw.minutes - 5) * 60 < rw.total_s


@pytest.mark.parametrize("canonical", ["NAS100", "SPX500", "BTCUSD", "GER40", "BRENT"])
@pytest.mark.parametrize("day", [SUMMER_WED, WINTER_WED, SUMMER_FRI, WINTER_FRI])
def test_P_live_entry_end_equals_effective_flat_minus_derived_runway(canonical, day):
    flat = _flat(canonical, day)
    runway = derive_entry_runway(POL, canonical, flat).minutes
    end = _live_end_utc(canonical, day)
    latest_safe = flat - timedelta(minutes=runway)
    if canonical in ("NAS100", "SPX500", "BTCUSD"):  # entry_end_live = "flat": the derived cutoff IS the entry end
        assert end == latest_safe, (canonical, day, end, latest_safe)
    else:  # research entry end binds earlier; the derived cutoff is only a further bound
        assert end is None or end <= latest_safe


def test_P_nas100_spx500_btc_values_summer_and_winter():
    for m in ("NAS100", "SPX500"):
        for day in (SUMMER_WED, WINTER_WED):  # 15:55 New York = 21:55 Berlin in both seasons (both DST zones move together)
            assert _live_end_utc(m, day).astimezone(BERLIN).strftime("%H:%M") == "21:45"
    assert _live_end_utc("BTCUSD", SUMMER_WED).astimezone(BERLIN).strftime("%H:%M") == "21:45"
    assert _live_end_utc("BTCUSD", WINTER_WED).astimezone(BERLIN).strftime("%H:%M") == "21:20"  # the research flat 21:30 binds in winter


@pytest.mark.parametrize(
    "tweak",
    [
        {"close_grace_s": 90 + 300},  # slower exit-deal visibility
        {"evaluation_budget_s": 30 + 300},
        {"flatten_wait_s": 35 + 150},
        {"liquidity_allowance_s": {"index_cfd": 60 + 600, "crypto_cfd": 90 + 600}},
    ],
)
def test_P_changing_a_component_changes_the_cutoff(tweak):
    longer = dataclasses.replace(POL, runway=dataclasses.replace(POL.runway, **tweak))
    for canonical in ("NAS100", "SPX500", "BTCUSD"):
        spec = load_market_spec(canonical)
        s = sig(SUMMER_WED)
        base_ov, new_ov = live_spec(spec, POL, s), live_spec(spec, longer, s)
        assert new_ov is not None and base_ov is not None
        if canonical == "BTCUSD" and "liquidity_allowance_s" in tweak:
            assert new_ov.calendar.entry_end_min < base_ov.calendar.entry_end_min
        elif "liquidity_allowance_s" not in tweak:
            assert new_ov.calendar.entry_end_min < base_ov.calendar.entry_end_min, canonical
        else:
            assert new_ov.calendar.entry_end_min < base_ov.calendar.entry_end_min
        flat = _flat(canonical, SUMMER_WED)
        assert derive_entry_runway(longer, canonical, flat).minutes > derive_entry_runway(POL, canonical, flat).minutes


def test_P_a_component_change_moves_the_entry_gate_of_the_stack_too():
    s = sig(SUMMER_WED)
    flat = _flat("NAS100", SUMMER_WED)
    late = flat - timedelta(minutes=derive_entry_runway(POL, "NAS100", flat).minutes - 1)
    assert POL.entry_refusal("NAS100", late, flat) == "entry_runway_too_short"
    early = flat - timedelta(minutes=derive_entry_runway(POL, "NAS100", flat).minutes + 1)
    assert POL.entry_refusal("NAS100", early, flat) is None
    longer = dataclasses.replace(POL, runway=dataclasses.replace(POL.runway, close_grace_s=900))
    assert longer.entry_refusal("NAS100", early, flat) == "entry_runway_too_short"
    assert s < early


def test_P_broker_margin_only_when_the_broker_closes_closer_than_the_execution_budget():
    tight = dataclasses.replace(POL, broker_close_buffer_min=1)  # broker close - 1 min = flat: gap 60 s < the 165 s budget
    close = tight.broker_close_utc("GER40", sig(SUMMER_WED))
    flat = close - timedelta(minutes=1)
    rw = derive_entry_runway(tight, "GER40", flat)
    assert rw.broker_margin_s == pytest.approx(165 - 60) and rw.minutes >= derive_entry_runway(POL, "GER40", _flat("GER40", SUMMER_WED)).minutes
    assert derive_entry_runway(POL, "GER40", _flat("GER40", SUMMER_WED)).broker_margin_s == 0  # shipped 5 min buffer > the budget


def test_P_measured_p95_replaces_the_placeholder_only_when_larger():
    flat = _flat("NAS100", SUMMER_WED)
    base = derive_entry_runway(POL, "NAS100", flat)
    assert derive_entry_runway(POL, "NAS100", flat, measured_close_p95_s=10).liquidity_source == "placeholder"
    big = derive_entry_runway(POL, "NAS100", flat, measured_close_p95_s=400)
    assert big.liquidity_source == "measured_p95" and big.liquidity_allowance_s == 400 and big.minutes > base.minutes


def test_P_legacy_dict_with_the_constant_still_loads_for_old_callers():
    raw = tomllib.loads(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    raw["policy"].pop("runway")
    raw["policy"]["min_entry_runway_min"] = 10
    legacy = policy_from_dict(raw)
    assert legacy.runway is None and legacy.min_entry_runway_min == 10
    assert legacy.entry_runway_min("NAS100", _flat("NAS100", SUMMER_WED)) == 10


def test_P_a_policy_without_any_runway_definition_is_refused():
    raw = tomllib.loads(Path(DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    raw["policy"].pop("runway")
    with pytest.raises(ValueError):
        policy_from_dict(raw)


# ------------------------------------------------------------------------------------------------ Q: DST
@pytest.mark.parametrize(
    "day,expected_offset_h",
    [(SUMMER_WED, 2), (WINTER_WED, 1), ((2026, 10, 23), 2), ((2026, 10, 26), 1), ((2027, 3, 26), 1), ((2027, 3, 29), 2)],
)
def test_Q_berlin_times_follow_zoneinfo_not_a_fixed_utc_offset(day, expected_offset_h):
    assert POL.flatten_start_utc(datetime(*day).date()).hour == 21 - expected_offset_h + 0 and POL.flatten_start_utc(datetime(*day).date()).minute == 55
    assert POL.deadline_utc(datetime(*day).date()).hour == 22 - expected_offset_h


# ------------------------------------------------------------------------------------------------ E: Brent + diagnostic
def test_E_brent_effective_deadline_is_min_of_global_and_broker_and_the_provisional_calendar():
    spec = load_market_spec("BRENT")
    assert spec.calendar.status != "verified"  # provisional calendar
    for day in (SUMMER_WED, WINTER_WED):
        row = market_row(POL, "BRENT", datetime(*day).date())
        assert row["effective_flat_deadline_berlin"] == "18:55" and row["binding_constraint"] == "market_calendar_flat"
        cands = row["candidates_berlin"]
        assert cands["global_flatten_start"] == "21:55"
        assert cands["broker_close_minus_buffer"] == ("22:50" if day == SUMMER_WED else "21:50")  # break 20:55 UTC
        # never later than any candidate: MIN(global, broker close - buffer, calendar)
        assert row["effective_flat_deadline_berlin"] <= min(cands.values())
    assert market_row(POL, "BRENT", datetime(*WINTER_WED).date())["broker_close_berlin"] == "21:55"
    assert market_row(POL, "BRENT", datetime(*SUMMER_WED).date())["broker_close_berlin"] == "22:55"


def test_E_the_diagnostic_lists_every_market_with_runway_components():
    rep = operating_policy_report(POL)
    assert rep["policy"]["version"] == "live-op-3"
    for m in ("NAS100", "SPX500", "BTCUSD", "GER40", "BRENT"):
        for label in ("summer_wed", "winter_wed"):
            row = rep["markets"][m][label]
            assert row["runway"]["minutes"] >= 5 and row["effective_flat_deadline_berlin"]
            assert set(row["runway"]) >= {"decision_latency_s", "execution_reconcile_s", "broker_margin_s", "liquidity_allowance_s"}
