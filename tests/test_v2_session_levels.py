"""V2: cash-session levels (alpha.session) - hand-computed values, holidays, causality."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alpha.fast.store import SESSION_FEATURE_NAMES, FeatureConfig, FeatureStore
from alpha.session import SessionCalendar, cash_session_arrays
from tests.test_alpha_fast_price_action import _synthetic_multi_day

TZ = "Europe/Berlin"


def _frame(days: dict[str, tuple[str, str]], **overrides: dict[str, float]) -> pd.DataFrame:
    """Flat 100/101/99 bars per local day between the given (from, to) Berlin clock times.

    ``overrides`` maps "YYYY-MM-DD HH:MM" (Berlin) to bar values.
    """
    parts = []
    for day, (start, end) in days.items():
        local = pd.date_range(f"{day} {start}", f"{day} {end}", freq="5min", tz=TZ)
        parts.append(local.tz_convert("UTC"))
    ts = parts[0].append(parts[1:])
    frame = pd.DataFrame(
        {"ts": ts, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "spread_pts": 1.0}
    )
    local_key = pd.DatetimeIndex(ts).tz_convert(TZ).strftime("%Y-%m-%d %H:%M")
    for key, values in overrides.items():
        (pos,) = np.flatnonzero(local_key == key)
        for column, value in values.items():
            frame.loc[pos, column] = value
    return frame


def _at(frame: pd.DataFrame, features, key: str) -> int:
    local_key = pd.DatetimeIndex(frame["ts"]).tz_convert(TZ).strftime("%Y-%m-%d %H:%M")
    return int(np.flatnonzero(local_key == key)[0])


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    return a.dtype == b.dtype and np.array_equal(a, b, equal_nan=True)


def _hand_frame() -> pd.DataFrame:
    days = {d: ("07:00", "21:55") for d in ("2024-01-08", "2024-01-09")}
    return _frame(
        days,
        **{
            "2024-01-08 09:00": {"open": 99.5},
            "2024-01-08 10:00": {"high": 110.0},
            "2024-01-08 12:00": {"low": 95.0},
            "2024-01-08 17:25": {"close": 102.0},  # last cash bar (opens 17:25, closes 17:30)
            "2024-01-08 17:30": {"high": 150.0, "low": 50.0},  # first post-close bar: NOT cash
            "2024-01-08 19:00": {"high": 106.0, "low": 98.0},
            "2024-01-09 07:30": {"high": 107.0, "low": 93.0},
            "2024-01-09 09:00": {"open": 105.0, "high": 106.0},
            "2024-01-09 11:00": {"high": 130.0},
        },
    )


def test_cash_levels_hand_computed() -> None:
    frame = _hand_frame()
    f = FeatureStore.build(frame, FeatureConfig())
    i = lambda key: _at(frame, f, key)  # noqa: E731

    # ---- day 1 (first day): no previous cash session, so-far levels start at the open
    assert np.isnan(f["pdh_cash"][: i("2024-01-09 07:00")]).all()
    assert np.isnan(f["gap_cash_atr"][: i("2024-01-09 07:00")]).all()
    assert np.isnan(f["sess_open_cash"][i("2024-01-08 08:55")])
    assert np.isnan(f["sess_high_cash"][i("2024-01-08 08:55")])
    assert f["sess_open_cash"][i("2024-01-08 09:00")] == 99.5
    assert f["sess_high_cash"][i("2024-01-08 09:55")] == 101.0
    assert f["sess_high_cash"][i("2024-01-08 10:00")] == 110.0
    assert f["sess_low_cash"][i("2024-01-08 11:55")] == 99.0
    assert f["sess_low_cash"][i("2024-01-08 12:00")] == 95.0
    # cash extremes are frozen after the close: the 17:30 spike (150/50) is NOT cash
    assert f["sess_high_cash"][i("2024-01-08 17:30")] == 110.0
    assert f["sess_low_cash"][i("2024-01-08 21:55")] == 95.0
    # overnight (day 1): pre-open bars 07:00-08:55 only, running; frozen from the cash open on
    assert f["overnight_high"][i("2024-01-08 07:00")] == 101.0
    assert f["overnight_high"][i("2024-01-08 10:00")] == 101.0
    assert f["overnight_high"][i("2024-01-08 19:00")] == 101.0  # post-close: frozen day-1 range

    # ---- day 2
    at = i("2024-01-09 08:55")
    assert (f["pdh_cash"][at], f["pdl_cash"][at], f["pdc_cash"][at]) == (110.0, 95.0, 102.0)
    assert np.isnan(f["sess_open_cash"][at]) and np.isnan(f["sess_high_cash"][at])
    assert np.isnan(f["dist_sess_open_cash_atr"][at])
    assert f["minutes_since_cash_open"][at] == -5.0
    assert f["minutes_since_cash_open"][i("2024-01-09 09:00")] == 0.0
    assert f["minutes_since_cash_open"][i("2024-01-09 10:30")] == 90.0
    # overnight running before the open: post-close day-1 bars (150/50 spike, 106/98) + 07:xx
    assert f["overnight_high"][i("2024-01-09 07:00")] == 150.0
    assert f["overnight_low"][i("2024-01-09 07:00")] == 50.0
    assert f["overnight_high"][at] == 150.0 and f["overnight_low"][at] == 50.0
    o = i("2024-01-09 09:00")
    assert f["sess_open_cash"][o] == 105.0
    assert f["sess_high_cash"][o] == 106.0 and f["sess_low_cash"][o] == 99.0
    assert f["sess_high_cash"][i("2024-01-09 11:00")] == 130.0
    assert f["sess_high_cash"][i("2024-01-09 20:00")] == 130.0  # frozen
    assert f["overnight_high"][o] == 150.0 and f["overnight_high"][i("2024-01-09 20:00")] == 150.0
    atr = f["m5_atr14"][o]
    assert atr > 0
    assert f["gap_cash_atr"][o] == pytest.approx((105.0 - 102.0) / atr)
    assert f["dist_pdh_cash_atr"][o] == pytest.approx((100.0 - 110.0) / atr)
    assert f["dist_sess_high_cash_atr"][o] == pytest.approx((100.0 - 106.0) / atr)
    assert f["dist_ovn_low_atr"][o] == pytest.approx((100.0 - 50.0) / atr)
    # V1 whole-day levels are untouched (they still include the overnight bars)
    assert f["previous_day_high"][o] == 150.0


def test_post_close_bars_show_the_frozen_overnight_of_their_own_session() -> None:
    days = {d: ("07:00", "21:55") for d in ("2024-01-08", "2024-01-09")}
    frame = _frame(
        days,
        **{
            "2024-01-09 07:30": {"high": 107.0, "low": 93.0},
            "2024-01-09 19:00": {"high": 140.0},  # post-close: must NOT enter today's overnight
        },
    )
    f = FeatureStore.build(frame, FeatureConfig())
    i = lambda key: _at(frame, f, key)  # noqa: E731
    assert f["overnight_high"][i("2024-01-09 09:00")] == 107.0
    assert f["overnight_high"][i("2024-01-09 18:00")] == 107.0
    assert f["overnight_high"][i("2024-01-09 19:00")] == 107.0
    assert f["overnight_low"][i("2024-01-09 21:55")] == 93.0


def test_holiday_and_half_day_use_last_day_with_cash_bars() -> None:
    frame = _frame(
        {
            "2024-01-08": ("07:00", "21:55"),  # full cash day
            "2024-01-09": ("18:00", "21:55"),  # holiday: overnight bars only, no cash bars
            "2024-01-10": ("07:00", "21:55"),
            "2024-01-11": ("07:00", "13:00"),  # half day: cash bars 09:00-13:00 only
            "2024-01-12": ("07:00", "21:55"),
        },
        **{
            "2024-01-08 12:00": {"high": 120.0, "low": 90.0, "close": 111.0},
            "2024-01-08 17:25": {"close": 103.0},
            "2024-01-10 12:00": {"high": 118.0, "low": 92.0},
            "2024-01-11 10:00": {"high": 125.0, "low": 96.0},
            "2024-01-11 13:00": {"close": 107.0},  # last cash bar of the half day
        },
    )
    f = FeatureStore.build(frame, FeatureConfig())
    i = lambda key: _at(frame, f, key)  # noqa: E731
    # holiday bars: previous cash session is Monday's; no cash open, no so-far levels
    hol = i("2024-01-09 19:00")
    assert (f["pdh_cash"][hol], f["pdl_cash"][hol], f["pdc_cash"][hol]) == (120.0, 90.0, 103.0)
    assert np.isnan(f["sess_open_cash"][hol]) and np.isnan(f["sess_high_cash"][hol])
    # day after the holiday still refers to Monday (last day WITH cash bars)
    wed = i("2024-01-10 09:00")
    assert (f["pdh_cash"][wed], f["pdc_cash"][wed]) == (120.0, 103.0)
    # overnight of Wednesday spans the holiday (Mon post-close ... Wed pre-open)
    assert np.isfinite(f["overnight_high"][wed])
    fri = i("2024-01-12 09:00")
    assert (f["pdh_cash"][fri], f["pdl_cash"][fri], f["pdc_cash"][fri]) == (125.0, 96.0, 107.0)
    assert f["gap_cash_atr"][fri] == pytest.approx((100.0 - 107.0) / f["m5_atr14"][fri])


def test_session_calendar_buckets_and_validation() -> None:
    cal = SessionCalendar()
    assert cal.bucket_names == ("EUROPE_OPEN", "MIDDAY", "US_OVERLAP", "OVERNIGHT")
    minutes = np.array([0, 539, 540, 659, 660, 929, 930, 1199, 1200, 1439])
    assert cal.bucket_codes(minutes).tolist() == [3, 3, 0, 0, 1, 1, 2, 2, 3, 3]
    with pytest.raises(ValueError):
        SessionCalendar(cash_open_min=600, cash_close_min=600)
    with pytest.raises(ValueError):
        SessionCalendar(buckets=(("A", 0, 100), ("B", 50, 200)))


def test_custom_calendar_changes_cash_window_and_is_part_of_cache_identity(tmp_path) -> None:
    frame = _hand_frame()
    default = FeatureStore.build(frame, FeatureConfig())
    # a New-York-like calendar on the same frame gives a different cash session
    custom = FeatureConfig(session=SessionCalendar(name="X", cash_open_min=8 * 60,
                                                   cash_close_min=16 * 60 + 30))
    other = FeatureStore.build(frame, custom)
    assert not _same(default["sess_open_cash"], other["sess_open_cash"])
    a = FeatureStore.load_or_build(frame, FeatureConfig(), tmp_path)
    b = FeatureStore.load_or_build(frame, custom, tmp_path)
    assert a.metadata["cache_key"] != b.metadata["cache_key"]
    # mapping config (JSON round trip) reproduces the same identity
    c = FeatureStore.load_or_build(frame, {"session": {"name": "X", "cash_open_min": 480,
                                                        "cash_close_min": 990}}, tmp_path)
    assert c.metadata["cache_key"] == b.metadata["cache_key"] and c.metadata["cache_hit"]


def test_direct_kernel_matches_store_arrays() -> None:
    frame = _hand_frame()
    f = FeatureStore.build(frame, FeatureConfig())
    out = cash_session_arrays(f["berlin_minute"], f["berlin_day_id"], f["o"], f["h"], f["l"],
                              f["c"], f["m5_atr14"])
    assert set(out) == set(SESSION_FEATURE_NAMES)
    for name, value in out.items():
        assert _same(value, f[name]), name


# ------------------------------------------------------------------ causality (truncation)
def _assert_truncation_invariant(frame: pd.DataFrame, cuts: list[int]) -> None:
    full = FeatureStore.build(frame, FeatureConfig())
    for cut in cuts:
        short = FeatureStore.build(frame.iloc[:cut].copy().reset_index(drop=True), FeatureConfig())
        for name in SESSION_FEATURE_NAMES:
            assert _same(full[name][:cut], short[name]), (name, cut)


def test_truncation_invariance_day_boundary_cash_open_close_and_dst_week() -> None:
    frame = _synthetic_multi_day("2024-03-25", 14)  # weekday 07:00-21:00 UTC, DST 2024-03-31
    full = FeatureStore.build(frame, FeatureConfig())
    minute, day = full["berlin_minute"], full["berlin_day_id"]
    boundary = int(np.flatnonzero(np.diff(day) != 0)[1]) + 1
    ts = pd.DatetimeIndex(frame["ts"])
    dst_bar = int(np.searchsorted(ts, pd.Timestamp("2024-04-01", tz="UTC")))
    cash_open = int(np.flatnonzero((minute == 540) & (day == day[boundary]))[0])
    cash_close = int(np.flatnonzero((minute == 1050) & (day == day[boundary]))[0])
    cuts = [300, cash_open - 1, cash_open, cash_open + 7, cash_close - 1, cash_close,
            boundary - 1, boundary, boundary + 1, dst_bar - 1, dst_bar, dst_bar + 40,
            len(frame) - 5]
    _assert_truncation_invariant(frame, cuts)


def test_future_values_do_not_change_past_session_levels() -> None:
    frame = _synthetic_multi_day("2024-03-25", 10)
    cutoff = 600
    altered = frame.copy()
    altered.loc[cutoff + 1 :, ["open", "high", "low", "close"]] += 1_000.0
    base = FeatureStore.build(frame, FeatureConfig())
    changed = FeatureStore.build(altered, FeatureConfig())
    for name in SESSION_FEATURE_NAMES:
        assert _same(base[name][: cutoff + 1], changed[name][: cutoff + 1]), name
