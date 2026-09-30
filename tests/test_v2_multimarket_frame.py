"""V2 multi-market: GER40 parity (bit-identical), spec parameters flow into Frame/features/sim,
spread-cap units."""

from __future__ import annotations

import dataclasses
import hashlib

import numpy as np
import pandas as pd
import pytest

from alpha.common import frame as frame_mod
from alpha.common.dataset import BAR_SECONDS, BERLIN, POINT
from alpha.common.frame import GER40_PARAMS, Frame, params_from_spec, session_bucket
from alpha.common.sim import COST_SCENARIOS, ExitSpec, Signals, SimRules, SizingSpec, simulate
from alpha.fast.store import FeatureConfig, FeatureStore
from alpha.session import DEFAULT_CALENDAR
from markets.spec import CANONICALS, load_market_spec
from research.runners.v2_market_frame import (
    alpha_calendar,
    build_feature_store,
    feature_config_for,
)
from tests._v2mm_helpers import make_frame
from tests.unit.alpha.helpers import synthetic_dataframe

ALL = list(CANONICALS)
# sha256 of the Frame arrays for synthetic_dataframe(days=30), computed with the V1 code
# (before the parameterisation) in the main checkout.
PINNED_FRAME_DIGEST = "76f60470370c4a5e87fa8bc3414324d12b520f885b58b903eb93e9fef7a1f333"


def _arrays(fr: Frame) -> dict[str, np.ndarray]:
    return {k: getattr(fr, k) for k in ("o", "h", "l", "c", "spread", "minute", "day", "date",
                                        "contig_next")}


# ------------------------------------------------------------------ GER40 parity
def test_default_params_are_the_v1_constants():
    p = GER40_PARAMS
    assert (p.tz, p.point, p.bar_seconds) == (BERLIN, POINT, BAR_SECONDS)
    assert (p.entry_start_min, p.entry_end_min, p.flat_min) == (
        frame_mod.ENTRY_START_MIN, frame_mod.ENTRY_END_MIN, frame_mod.FLAT_MIN)
    assert p.buckets == frame_mod.SESSION_BUCKETS
    assert p.max_entry_spread_price == SimRules().max_entry_spread_pts == 8.0


def test_ger40_spec_reproduces_default_params_exactly():
    assert params_from_spec(load_market_spec("GER40")) == GER40_PARAMS


def test_ger40_frame_bit_identical_default_vs_spec():
    df = synthetic_dataframe(days=30)
    a = Frame.from_dataframe(df)
    b = Frame.from_dataframe(df, params_from_spec(load_market_spec("GER40")))
    for k, v in _arrays(a).items():
        assert np.array_equal(v, _arrays(b)[k]), k
        assert v.dtype == _arrays(b)[k].dtype


def test_frame_pinned_digest_ger40_synthetic():
    fr = Frame.from_dataframe(synthetic_dataframe(days=30))
    h = hashlib.sha256()
    for v in _arrays(fr).values():
        h.update(np.ascontiguousarray(v).tobytes())
    assert h.hexdigest() == PINNED_FRAME_DIGEST


def test_ger40_feature_config_identical_to_v1():
    cfg = feature_config_for(load_market_spec("GER40"))
    assert cfg == FeatureConfig(point_size=POINT)  # V1: {"point_size": POINT}, default calendar
    assert cfg.session is DEFAULT_CALENDAR


def test_ger40_features_bit_identical_v1_config_vs_spec_config():
    df = synthetic_dataframe(days=25)
    a = FeatureStore.build(df, {"point_size": POINT})
    b = build_feature_store(df, load_market_spec("GER40"))
    assert list(a) == list(b)
    for k in a:
        assert np.array_equal(a[k], b[k], equal_nan=a[k].dtype.kind == "f"), k


def test_simulate_spec_params_equal_default_for_ger40():
    df = synthetic_dataframe(days=8)
    a = Frame.from_dataframe(df)
    b = Frame.from_dataframe(df, params_from_spec(load_market_spec("GER40")))
    side = np.zeros(len(a), dtype=np.int8)
    side[np.arange(50, len(a), 97)] = 1
    sg = Signals(side=side, stop=np.where(side != 0, a.c - 30.0, np.nan))
    ta, sa = simulate(a, sg, ExitSpec("fixed_r", 1.5), COST_SCENARIOS["BASE"])
    tb, sb = simulate(b, sg, ExitSpec("fixed_r", 1.5), COST_SCENARIOS["BASE"])
    assert sa == sb and len(ta) > 0
    pd.testing.assert_frame_equal(ta, tb)


# ------------------------------------------------------------------ per-market parameters flow
@pytest.mark.parametrize("canonical", ALL)
def test_spec_parameters_flow_to_frame(canonical):
    spec = load_market_spec(canonical)
    cal = spec.calendar
    p = params_from_spec(spec)
    assert (p.tz, p.point, p.flat_min, p.entry_start_min, p.entry_end_min) == (
        cal.tz, spec.point_size, cal.forced_flat_min, cal.entry_start_min, cal.entry_end_min)
    assert p.buckets == cal.bucket_tuples()
    assert p.max_entry_spread_price == spec.max_entry_spread_price
    df = make_frame("2025-06-02", "2025-06-06", spread_pts=50.0)
    fr = Frame.from_dataframe(df, p)
    local = pd.DatetimeIndex(df["ts"]).tz_convert(cal.tz)
    assert np.array_equal(fr.minute, np.asarray(local.hour * 60 + local.minute))
    assert np.allclose(fr.spread, 50.0 * spec.point_size)
    assert fr.params is p and fr.head(10).params is p
    names = {b[0] for b in p.buckets}
    for m in (0, cal.entry_start_min, cal.forced_flat_min - 1, 1439):
        assert session_bucket(m, p.buckets) in names


@pytest.mark.parametrize("canonical", ALL)
def test_spec_flows_to_feature_config_and_features(canonical):
    spec = load_market_spec(canonical)
    cfg = feature_config_for(spec)
    assert cfg.point_size == spec.point_size
    cal = alpha_calendar(spec)
    assert (cal.tz, cal.entry_end_min, cal.flat_min) == (
        spec.calendar.tz, spec.calendar.entry_end_min, spec.calendar.forced_flat_min)
    df = make_frame("2025-06-02", "2025-06-13", spread_pts=40.0)
    fs = build_feature_store(df, spec)
    assert np.allclose(fs["spread"], 40.0 * spec.point_size)
    local = pd.DatetimeIndex(df["ts"]).tz_convert(spec.calendar.tz)
    minute = np.asarray(local.hour * 60 + local.minute)
    at_open = minute == spec.calendar.cash_open_min
    assert at_open.any()
    # the cash-open minute counter is measured in the calendar's timezone
    assert np.all(fs["minutes_since_cash_open"][at_open] == 0)


def test_non_ger40_calendar_differs_from_default():
    for c in ("NAS100", "SPX500", "XAUUSD", "EURUSD"):
        assert alpha_calendar(load_market_spec(c)) != DEFAULT_CALENDAR


def test_entry_window_and_forced_flat_use_frame_params():
    """14:50 New York = 20:50 Berlin: outside the GER40 window, inside the NAS100 window."""
    spec = load_market_spec("NAS100")
    ts = pd.date_range("2025-01-15 14:50", periods=14, freq="5min",
                       tz="America/New_York").tz_convert("UTC")
    df = pd.DataFrame({"ts": ts, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
                       "tick_volume": 1.0, "spread_pts": 0.0})
    side = np.zeros(len(df), dtype=np.int8)
    side[0] = 1
    stop = np.full(len(df), np.nan)
    stop[0] = 99.0
    sg = Signals(side=side, stop=stop)
    gross = COST_SCENARIOS["GROSS_REFERENCE"]
    _, skips = simulate(Frame.from_dataframe(df), sg, ExitSpec("fixed_r", 1.5), gross)
    assert skips["outside_window"] == 1  # V1 GER40 constants applied to NY timestamps
    nas = Frame.from_dataframe(df, params_from_spec(spec))
    sizing = SizingSpec(lot_step=0.2, min_lot=0.2, min_risk_pts=0.5)
    trades, skips = simulate(nas, sg, ExitSpec("fixed_r", 1.5), gross, sizing)
    assert skips["outside_window"] == 0 and len(trades) == 1
    assert trades.iloc[0].exit_reason == "SESSION_END"  # forced flat at 15:55 New York
    assert int(nas.minute[trades.iloc[0].exit_idx]) == spec.calendar.forced_flat_min


# ------------------------------------------------------------------ spread cap units
def test_spread_cap_is_in_price_units_ger40():
    spec = load_market_spec("GER40")
    assert spec.max_entry_spread_price == 8.0
    assert spec.max_entry_spread_recorded_points == pytest.approx(800.0)
    assert 800 * spec.point_size == pytest.approx(8.0)


@pytest.mark.parametrize("canonical", ["NAS100", "SPX500", "XAUUSD", "EURUSD"])
def test_spread_cap_units_pitfall_handled_via_spec(canonical):
    spec = load_market_spec(canonical)
    p = params_from_spec(spec)
    rules = p.sim_rules(SimRules())
    assert rules.max_entry_spread_pts == spec.max_entry_spread_price  # price units, NOT points
    assert SimRules().max_entry_spread_pts == 8.0  # the V1 default is untouched
    cap_pts = spec.max_entry_spread_recorded_points
    assert cap_pts == pytest.approx(spec.max_entry_spread_price / spec.point_size)
    # one candidate inside the entry window, flat prices so nothing else interferes
    cal = spec.calendar
    day = pd.Timestamp("2025-06-03", tz=cal.tz)
    ts = pd.date_range(day + pd.Timedelta(minutes=cal.entry_start_min + 10), periods=6,
                       freq="5min").tz_convert("UTC")

    def skips_for(recorded_pts, rules_):
        df = pd.DataFrame({"ts": ts, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
                           "tick_volume": 1.0, "spread_pts": recorded_pts})
        fr = Frame.from_dataframe(df, p)
        side = np.zeros(len(fr), dtype=np.int8)
        side[1] = 1
        stop = np.full(len(fr), np.nan)
        stop[1] = 99.0
        sizing = SizingSpec(lot_step=0.001, min_lot=0.001, min_risk_pts=0.0, max_risk_pts=1e9)
        return simulate(fr, Signals(side=side, stop=stop), ExitSpec("fixed_r", 1.5),
                        COST_SCENARIOS["GROSS_REFERENCE"], sizing, rules_)[1]

    assert skips_for(cap_pts * 0.9, rules)["spread_filter"] == 0
    assert skips_for(cap_pts * 1.1, rules)["spread_filter"] == 1
    if spec.max_entry_spread_price < 8.0:
        # the un-converted V1 cap (8.0, price units) would never filter this market
        assert skips_for(cap_pts * 1.1, SimRules())["spread_filter"] == 0
    assert dataclasses.is_dataclass(rules)
