"""Bar-completeness / data-quality analysis for downloaded broker bars (pure pandas, no I/O).

Input frames follow the parquet bar schema: `ts` (UTC bar-open, tz-aware),
`open/high/low/close` (bid),
`spread_pts`, `tick_volume`. Expected bars are derived from the OBSERVED per-weekday trading-slot
profile in the broker SERVER-local clock (Europe/Berlin per `ServerTimePolicy`), because CFD
trading hours are broker-defined, not exchange cash hours. Deviations from the profile (short
days, missing weekdays, intraday gaps) are reported; nothing is repaired or filled.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

SERVER_TZ = "Europe/Berlin"
PROFILE_PRESENT_FRACTION = 0.8  # a slot is "expected" if present on >=80% of that weekday's days
SHORT_DAY_RATIO = 0.6  # a day with < 60% of the expected bars is a short day (holiday/half day)


def _epoch_seconds(ts: pd.Series) -> pd.Series:
    """Seconds since epoch as int64, independent of the datetime64 resolution (ns/us)."""
    naive = ts.dt.tz_convert("UTC").dt.tz_localize(None) if ts.dt.tz is not None else ts
    return ((naive - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).astype("int64")


def _local(df: pd.DataFrame) -> pd.DataFrame:
    ts = pd.to_datetime(df["ts"], utc=True)
    loc = ts.dt.tz_convert(SERVER_TZ)
    out = pd.DataFrame({"ts": ts})
    out["day"] = loc.dt.date
    out["wd"] = loc.dt.weekday
    out["minute"] = loc.dt.hour * 60 + loc.dt.minute
    return out


def _day_is_dst(day: date) -> bool:
    """True if the server zone (Europe/Berlin) observes DST at noon of `day`."""
    return bool(pd.Timestamp(day).replace(hour=12).tz_localize(SERVER_TZ).dst())


def build_profile(df: pd.DataFrame, tf_seconds: int) -> dict[tuple[int, bool], frozenset[int]]:
    """(weekday, server-DST) -> set of expected server-local minute-of-day slots.

    A slot is expected if present on >= 80% of ALL calendar days of that (weekday, DST) class
    inside the data range (denominator = calendar days, so rarely-open weekdays such as Sunday
    drop out instead of being selected by their own presence).
    """
    loc = _local(df)
    step = tf_seconds // 60
    loc = loc.assign(slot=(loc["minute"] // step) * step)
    first_day, last_day = loc["day"].min(), loc["day"].max()
    n_days: dict[tuple[int, bool], int] = {}
    d = first_day
    while d <= last_day:
        key = (d.weekday(), _day_is_dst(d))
        n_days[key] = n_days.get(key, 0) + 1
        d += timedelta(days=1)
    dst_by_day = {d: _day_is_dst(d) for d in loc["day"].unique()}
    loc = loc.assign(dst=loc["day"].map(dst_by_day))
    counts = loc.groupby(["wd", "dst", "slot"])["day"].nunique()
    prof: dict[tuple[int, bool], set[int]] = {}
    for (wd, dst, sl), n in counts.items():
        key = (int(wd), bool(dst))
        if n / max(n_days.get(key, 1), 1) >= PROFILE_PRESENT_FRACTION:
            prof.setdefault(key, set()).add(int(sl))
    return {k: frozenset(v) for k, v in prof.items()}


def spread_stats(df: pd.DataFrame) -> dict[str, Any]:
    s = pd.to_numeric(df["spread_pts"], errors="coerce")
    pos = s[s > 0]
    return {
        "n": int(s.notna().sum()),
        "zero_or_neg": int((s <= 0).sum()),
        "median": float(pos.median()) if len(pos) else None,
        "p95": float(pos.quantile(0.95)) if len(pos) else None,
        "p99": float(pos.quantile(0.99)) if len(pos) else None,
        "max": float(s.max()) if len(s) else None,
    }


def dst_transition_days(first: date, last: date) -> list[dict[str, str]]:
    """EU (last Sunday Mar/Oct) and US (2nd Sun Mar, 1st Sun Nov) DST changes within range."""
    out = []
    for y in range(first.year, last.year + 1):

        def nth_sunday(month: int, n: int, y: int = y) -> date:
            d = date(y, month, 1)
            d += timedelta(days=(6 - d.weekday()) % 7)
            return d + timedelta(weeks=n - 1)

        def last_sunday(month: int, y: int = y) -> date:
            d = date(y, month, 28)
            d += timedelta(days=4)  # always spills into next month
            d = d - timedelta(days=d.day)  # last day of month
            return d - timedelta(days=(d.weekday() + 1) % 7)

        for kind, d in (
            ("EU_spring", last_sunday(3)),
            ("EU_autumn", last_sunday(10)),
            ("US_spring", nth_sunday(3, 2)),
            ("US_autumn", nth_sunday(11, 1)),
        ):
            if first <= d <= last:
                out.append({"kind": kind, "date": d.isoformat()})
    return sorted(out, key=lambda x: x["date"])


def volume_peaks_utc(df: pd.DataFrame, top: int = 3) -> dict[str, list[str]]:
    """UTC HH:MM of the highest mean tick-volume bars, split by server-DST season.

    Cross-check of the server-clock -> UTC policy: an exchange-linked open (e.g. Xetra 09:00
    Berlin, NYSE 09:30 NY) must peak one hour earlier in UTC in the DST season than in winter.
    """
    ts = pd.to_datetime(df["ts"], utc=True)
    dst = ts.dt.tz_convert(SERVER_TZ).map(lambda t: bool(t.dst()))
    minute = ts.dt.hour * 60 + ts.dt.minute
    out: dict[str, list[str]] = {}
    for name, mask in (("dst_summer", dst), ("std_winter", ~dst)):
        if mask.sum() == 0:
            continue
        g = pd.to_numeric(df["tick_volume"])[mask].groupby(minute[mask]).mean()
        out[name] = [
            f"{m // 60:02d}:{m % 60:02d}" for m in g.sort_values(ascending=False).index[:top]
        ]
    return out


def analyze_intraday(
    df: pd.DataFrame, tf_seconds: int, *, now_utc: datetime | None = None
) -> dict[str, Any]:
    """Full completeness/quality report for an intraday (<= H1) bar frame."""
    now_utc = now_utc or datetime.now(UTC)
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
    ts = pd.to_datetime(df["ts"], utc=True)
    loc = _local(df)
    step = tf_seconds // 60
    prof = build_profile(df, tf_seconds)
    exp_per_wd = {k: len(sl) for k, sl in prof.items()}

    def _exp(day: date) -> int:
        return exp_per_wd.get((day.weekday(), _day_is_dst(day)), 0)

    day_counts = loc.groupby("day").size()
    short_days, missing_weekdays = [], []
    first_day, last_day = loc["day"].iloc[0], loc["day"].iloc[-1]
    d = first_day
    while d <= last_day:
        wd = d.weekday()
        exp = _exp(d)
        n = int(day_counts.get(d, 0))
        if exp and n == 0 and wd < 5:
            missing_weekdays.append(d.isoformat())
        elif exp and n < SHORT_DAY_RATIO * exp:
            short_days.append({"day": d.isoformat(), "bars": n, "expected": exp})
        d += timedelta(days=1)

    # Intraday gaps: consecutive bars > 3 steps apart inside one server-local day (excl. weekend).
    delta_min = ts.diff().dt.total_seconds().div(60)
    same_day = loc["day"] == loc["day"].shift()
    gap_idx = np.where((delta_min > 3 * step) & same_day)[0]
    gaps = [
        {
            "start": ts.iloc[i - 1].isoformat(),
            "end": ts.iloc[i].isoformat(),
            "minutes": float(delta_min.iloc[i]),
        }
        for i in gap_idx
    ]

    # Month rollup: expected vs actual over the days present in the profile weekdays.
    loc = loc.assign(month=pd.to_datetime(loc["day"]).dt.strftime("%Y-%m"))
    months = {}
    for m, g in loc.groupby("month"):
        days = sorted(g["day"].unique())
        exp_total = sum(_exp(dd) for dd in days)
        months[m] = {
            "bars": len(g),
            "expected_on_present_days": int(exp_total),
            "ratio": round(len(g) / exp_total, 4) if exp_total else None,
            "days": len(days),
        }

    # Timestamp integrity.
    grid_bad = int((_epoch_seconds(ts) % tf_seconds != 0).sum())
    ohlc = df[["open", "high", "low", "close"]].astype(float)
    flat = (
        (ohlc["open"] == ohlc["high"])
        & (ohlc["high"] == ohlc["low"])
        & (ohlc["low"] == ohlc["close"])
    )
    bad_ohlc = int(
        (
            (ohlc["high"] < ohlc[["open", "close"]].max(axis=1))
            | (ohlc["low"] > ohlc[["open", "close"]].min(axis=1))
        ).sum()
    )

    # DST-week check: per-day bar ratio vs profile around each transition (Fri..Mon).
    dst = []
    for t in dst_transition_days(first_day, last_day):
        td = date.fromisoformat(t["date"])
        row = {"kind": t["kind"], "date": t["date"], "days": {}}
        worst = 1.0
        for off in (-2, -1, 0, 1):
            dd = td + timedelta(days=off)
            exp = _exp(dd)
            n = int(day_counts.get(dd, 0))
            if exp:
                worst = min(worst, n / exp)
            row["days"][dd.isoformat()] = {"bars": n, "expected": exp}
        row["worst_ratio"] = round(worst, 3)
        row["ok"] = worst >= SHORT_DAY_RATIO
        dst.append(row)

    # Weekly open (first bar of each server-local week, in UTC) -- reveals tz-policy problems.
    # +3h shift: a Sunday-evening FX open belongs to the following trading week.
    wk = (ts.dt.tz_convert(SERVER_TZ) + pd.Timedelta(hours=3)).dt.strftime("%G-%V")
    first_of_week = ts.groupby(wk).min()
    weekly_open_utc = first_of_week.dt.strftime("%H:%M").value_counts().head(6).to_dict()

    return {
        "rows": len(df),
        "first_ts": ts.iloc[0].isoformat(),
        "last_ts": ts.iloc[-1].isoformat(),
        "monotonic_strict": bool(ts.is_monotonic_increasing and ts.is_unique),
        "duplicate_ts": int(ts.duplicated().sum()),
        "future_ts": int((ts > pd.Timestamp(now_utc)).sum()),
        "tick_volume_peak_utc_by_season": volume_peaks_utc(df),
        "off_grid_ts": grid_bad,
        "bad_ohlc_rows": bad_ohlc,
        "flat_bars": int(flat.sum()),
        "flat_bar_ratio": round(float(flat.mean()), 5),
        "zero_tick_volume": int((pd.to_numeric(df["tick_volume"]) <= 0).sum()),
        "spread_pts": spread_stats(df),
        "expected_bars_by_weekday_and_server_dst": {
            f"wd{k[0]}_{'dst' if k[1] else 'std'}": v for k, v in sorted(exp_per_wd.items())
        },
        "months": months,
        "short_days": short_days[:60],
        "n_short_days": len(short_days),
        "missing_weekdays": missing_weekdays[:60],
        "n_missing_weekdays": len(missing_weekdays),
        "intraday_gaps_gt3bars": gaps[:60],
        "n_intraday_gaps_gt3bars": len(gaps),
        "dst_weeks": dst,
        "weekly_open_utc_hhmm_top": weekly_open_utc,
    }


def analyze_coarse(
    df: pd.DataFrame, tf_seconds: int, *, now_utc: datetime | None = None
) -> dict[str, Any]:
    """Light report for H4/D1 (grid is server-clock aligned; no slot profile)."""
    now_utc = now_utc or datetime.now(UTC)
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
    ts = pd.to_datetime(df["ts"], utc=True)
    srv = ts.dt.tz_convert(SERVER_TZ).dt.tz_localize(None)
    grid_bad = int((_epoch_seconds(srv) % tf_seconds != 0).sum())
    ohlc = df[["open", "high", "low", "close"]].astype(float)
    return {
        "rows": len(df),
        "first_ts": ts.iloc[0].isoformat(),
        "last_ts": ts.iloc[-1].isoformat(),
        "monotonic_strict": bool(ts.is_monotonic_increasing and ts.is_unique),
        "duplicate_ts": int(ts.duplicated().sum()),
        "future_ts": int((ts > pd.Timestamp(now_utc)).sum()),
        "off_server_grid_ts": grid_bad,
        "bad_ohlc_rows": int(
            (
                (ohlc["high"] < ohlc[["open", "close"]].max(axis=1))
                | (ohlc["low"] > ohlc[["open", "close"]].min(axis=1))
            ).sum()
        ),
        "spread_pts": spread_stats(df),
        "zero_tick_volume": int((pd.to_numeric(df["tick_volume"]) <= 0).sum()),
    }


def load_frame(paths: list) -> pd.DataFrame:
    """Read parquet bar files (content hash verified by `read_bar_dataset`) into one frame."""
    from data.historical import read_bar_dataset

    frames = []
    for p in sorted(paths, key=str):
        bars, _prov = read_bar_dataset(p)
        frames.append(
            pd.DataFrame(
                {
                    "ts": [b.timestamp for b in bars],
                    "open": [float(b.open) for b in bars],
                    "high": [float(b.high) for b in bars],
                    "low": [float(b.low) for b in bars],
                    "close": [float(b.close) for b in bars],
                    "tick_volume": [float(b.volume) for b in bars],
                    "spread_pts": [b.spread_points for b in bars],
                }
            )
        )
    if not frames:
        return pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "tick_volume", "spread_pts"]
        )
    out = pd.concat(frames, ignore_index=True)
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    return out
