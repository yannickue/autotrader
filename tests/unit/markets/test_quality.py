"""Bar-completeness analysis on synthetic frames (no I/O, no MT5)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from markets.quality import (
    analyze_coarse,
    analyze_intraday,
    build_profile,
    dst_transition_days,
    volume_peaks_utc,
)

NOW = datetime(2026, 7, 1, tzinfo=UTC)
WEEKDAYS = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]


def _frame(days: list[str], drop: dict[str, int] | None = None) -> pd.DataFrame:
    """Each listed day: 09:00-10:55 UTC M5 bars; `drop` removes the last N bars of a day."""
    rows = []
    for d in days:
        idx = pd.date_range(f"{d} 09:00", f"{d} 10:55", freq="300s", tz="UTC")
        n_drop = (drop or {}).get(d, 0)
        for ts in idx[: len(idx) - n_drop]:
            rows.append((ts, 1.0, 1.1, 0.9, 1.05, 10.0, 5))
    cols = ["ts", "open", "high", "low", "close", "tick_volume", "spread_pts"]
    return pd.DataFrame(rows, columns=cols)


def test_clean_frame_is_complete():
    rep = analyze_intraday(_frame(WEEKDAYS), 300, now_utc=NOW)
    assert rep["monotonic_strict"] and rep["duplicate_ts"] == 0 and rep["future_ts"] == 0
    assert rep["off_grid_ts"] == 0 and rep["bad_ohlc_rows"] == 0
    assert rep["n_short_days"] == 0 and rep["n_missing_weekdays"] == 0
    assert rep["n_intraday_gaps_gt3bars"] == 0
    assert rep["spread_pts"]["zero_or_neg"] == 0


def _weekdays(n_weeks: int) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-06-01", periods=5 * n_weeks)]


def test_missing_weekday_short_day_and_gap_are_reported():
    days = [d for d in _weekdays(5) if d != "2026-06-10"]  # one Wednesday of five missing
    rep = analyze_intraday(_frame(days, drop={"2026-06-12": 20}), 300, now_utc=NOW)
    assert rep["missing_weekdays"] == ["2026-06-10"]
    assert any(s["day"] == "2026-06-12" for s in rep["short_days"])
    df2 = _frame(WEEKDAYS)
    # remove 10:00-10:30 on day 2 (7 bars) -> one intraday gap
    drop_idx = df2[(df2.ts.dt.day == 2) & (df2.ts.dt.hour == 10) & (df2.ts.dt.minute <= 30)].index
    rep2 = analyze_intraday(df2.drop(drop_idx), 300, now_utc=NOW)
    assert rep2["n_intraday_gaps_gt3bars"] == 1


def test_future_duplicate_and_bad_ohlc_are_flagged():
    df = _frame(WEEKDAYS)
    df.loc[3, "high"] = 0.5  # high below open/close
    df = pd.concat([df, df.iloc[[5]]], ignore_index=True)
    rep = analyze_intraday(df, 300, now_utc=datetime(2026, 6, 3, tzinfo=UTC))
    assert rep["bad_ohlc_rows"] >= 1
    assert rep["duplicate_ts"] == 1
    assert rep["future_ts"] > 0


def test_profile_ignores_rarely_open_weekday():
    # 3 weeks of weekdays plus Sunday 06-07 only (Sunday 06-14 absent: 1 of 2 = 50% < 80%).
    df = _frame([*_weekdays(3), "2026-06-07"])
    prof = build_profile(df, 300)
    assert not any(wd == 6 for wd, _ in prof)
    assert any(wd == 0 for wd, _ in prof)


def test_dst_transition_days():
    days = dst_transition_days(date(2026, 1, 1), date(2026, 12, 31))
    got = {(d["kind"], d["date"]) for d in days}
    assert ("EU_spring", "2026-03-29") in got
    assert ("EU_autumn", "2026-10-25") in got
    assert ("US_spring", "2026-03-08") in got
    assert ("US_autumn", "2026-11-01") in got


def test_volume_peak_shifts_with_dst():
    def day(d: str, peak: str) -> pd.DataFrame:
        idx = pd.date_range(f"{d} 05:00", f"{d} 10:00", freq="5min", tz="UTC")
        vol = [100.0 if ts.strftime("%H:%M") == peak else 10.0 for ts in idx]
        return pd.DataFrame({"ts": idx, "tick_volume": vol})

    df = pd.concat([day("2026-07-01", "07:00"), day("2026-01-14", "08:00")], ignore_index=True)
    assert volume_peaks_utc(df, top=1) == {"dst_summer": ["07:00"], "std_winter": ["08:00"]}


def test_coarse_report_uses_server_grid():
    # Server midnight in winter = 23:00 UTC the previous day: on the server grid, off the UTC grid.
    ts = pd.to_datetime(["2026-01-12 23:00", "2026-01-13 23:00"], utc=True)
    df = pd.DataFrame(
        {
            "ts": ts,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0,
            "tick_volume": 5,
            "spread_pts": 3,
        }
    )
    rep = analyze_coarse(df, 86400, now_utc=datetime(2026, 2, 1, tzinfo=UTC))
    assert rep["off_server_grid_ts"] == 0 and rep["monotonic_strict"]
