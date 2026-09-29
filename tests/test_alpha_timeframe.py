"""Causal multi-timeframe derivation from synthetic M5 data (no MT5 access)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from alpha.timeframe import MtfView, derive_mtf, truncation_invariant


def _bars(ts: pd.DatetimeIndex, base: float = 100.0) -> pd.DataFrame:
    values = base + np.arange(len(ts), dtype=float)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": values,
            "high": values + 2.0,
            "low": values - 1.0,
            "close": values + 1.0,
            "spread_pts": np.full(len(ts), 2.0),
        }
    )


def _local_bars(start: str, periods: int) -> pd.DataFrame:
    local = pd.date_range(start, periods=periods, freq="5min", tz="Europe/Berlin")
    return _bars(local.tz_convert("UTC"))


def test_at_1035_exposes_only_higher_bars_closed_by_1040() -> None:
    view = MtfView(_local_bars("2026-02-03 09:00", 20))

    state = view.at(pd.Timestamp("2026-02-03 10:35", tz="Europe/Berlin"))

    # Input timestamps are bar opens. At 10:35 the M5 bar is known at its 10:40 close.
    assert state.m5.name.tz_convert("Europe/Berlin").strftime("%H:%M") == "10:35"
    assert state.m15.name.tz_convert("Europe/Berlin").strftime("%H:%M") == "10:15"
    assert state.h1.name.tz_convert("Europe/Berlin").strftime("%H:%M") == "09:00"
    assert view.m15_alignment[19] == 5
    assert view.h1_alignment[19] == 0


def test_aggregation_keeps_incomplete_buckets_flagged_and_out_of_view() -> None:
    view = MtfView(_local_bars("2026-02-03 10:00", 5))

    assert view.m15["complete"].tolist() == [True, False]
    assert view.h1["complete"].tolist() == [False]
    assert view.at(4).m15.name.tz_convert("Europe/Berlin").strftime("%H:%M") == "10:00"
    assert view.at(4).h1 is None


def test_gap_marks_bucket_incomplete_instead_of_filling_it() -> None:
    frame = _local_bars("2026-02-03 10:00", 6).drop(index=1).reset_index(drop=True)

    mtf = derive_mtf(frame)

    first = mtf.m15.iloc[0]
    assert first["source_count"] == 2
    assert bool(first["complete"]) is False
    assert pd.isna(first["open"])
    assert MtfView(frame).at(1).m15 is None


def test_spring_dst_uses_berlin_wall_clock_buckets_without_phantom_hour() -> None:
    # 2025-03-30 01:00 UTC jumps from 01:59 CET to 03:00 CEST.
    frame = _bars(pd.date_range("2025-03-30 00:00", periods=48, freq="5min", tz="UTC"))

    h1 = derive_mtf(frame).h1
    local = h1.index.tz_convert("Europe/Berlin")

    assert h1["complete"].all()
    assert local.hour.tolist() == [1, 3, 4, 5]


def test_autumn_dst_distinguishes_both_repeated_berlin_hours() -> None:
    # 2025-10-26 01:00 UTC moves from 02:59 CEST back to 02:00 CET.
    frame = _bars(pd.date_range("2025-10-26 00:00", periods=36, freq="5min", tz="UTC"))

    h1 = derive_mtf(frame).h1
    local = h1.index.tz_convert("Europe/Berlin")

    assert h1["complete"].all()
    assert local.hour.tolist() == [2, 2, 3]
    assert [stamp.utcoffset().total_seconds() for stamp in local[:2]] == [7200.0, 3600.0]


def test_session_features_switch_phases_on_berlin_boundaries() -> None:
    frame = _local_bars("2026-02-03 08:55", 105)
    view = MtfView(frame)

    expected = {
        "2026-02-03 09:00": "EUROPEAN_OPEN",
        "2026-02-03 10:00": "MORNING",
        "2026-02-03 12:00": "MIDDAY",
        "2026-02-03 15:30": "US_CASH_OPEN_OVERLAP",
        "2026-02-03 17:30": "LATE",
    }
    for timestamp, phase in expected.items():
        assert view.at(pd.Timestamp(timestamp, tz="Europe/Berlin")).levels.phase == phase


def test_previous_day_levels_never_include_the_current_day() -> None:
    day1 = _local_bars("2026-02-02 09:00", 3)
    day2 = _local_bars("2026-02-03 09:00", 2)
    day2[["open", "high", "low", "close"]] += 1000.0
    view = MtfView(pd.concat([day1, day2], ignore_index=True))

    levels = view.at(3).levels

    assert levels.previous_day_high == 104.0
    assert levels.previous_day_low == 99.0
    assert levels.previous_day_close == 103.0
    assert levels.session_open == 1100.0


def test_rolling_session_extrema_are_causal_and_include_current_bar() -> None:
    frame = _local_bars("2026-02-03 09:00", 4)
    frame.loc[3, ["high", "low"]] = [9999.0, -9999.0]
    view = MtfView(frame)

    before = view.at(2).levels
    current = view.at(3).levels

    assert (before.session_high, before.session_low) == (104.0, 99.0)
    assert (current.session_high, current.session_low) == (9999.0, -9999.0)


def test_alignment_and_features_are_invariant_to_future_truncation_or_changes() -> None:
    frame = _local_bars("2026-02-02 09:00", 320)
    cutoff = 199
    full = MtfView(frame)
    prefix = MtfView(frame.iloc[: cutoff + 1].copy())
    altered = frame.copy()
    altered.loc[cutoff + 1 :, ["open", "high", "low", "close"]] += 1_000_000.0
    changed = MtfView(altered)

    assert truncation_invariant(frame, cutoff)
    np.testing.assert_array_equal(full.m15_alignment[: cutoff + 1], prefix.m15_alignment)
    np.testing.assert_array_equal(full.h1_alignment[: cutoff + 1], prefix.h1_alignment)
    for index in range(cutoff + 1):
        assert full.at(index).levels == prefix.at(index).levels == changed.at(index).levels
