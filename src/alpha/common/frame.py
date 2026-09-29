"""Numpy view of the bar dataset plus causal feature helpers.

Every feature at index i uses bars <= i only (bar i is CLOSED at decision time; the simulator
trades at the OPEN of bar i+1). `tests/unit/alpha/test_no_lookahead.py` enforces this by
truncation-invariance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from alpha.common.dataset import BAR_SECONDS, BERLIN, POINT

# Berlin wall-clock minute-of-day boundaries (Xetra cash session 09:00-17:30).
ENTRY_START_MIN = 9 * 60
ENTRY_END_MIN = 20 * 60  # last entry bar OPEN must be before 20:00
FLAT_MIN = 21 * 60 + 30  # forced flat at 21:30 (no overnight -> no swap)

SESSION_BUCKETS = (
    ("PRE", 0, 9 * 60),
    ("OPEN_09_10", 9 * 60, 10 * 60),
    ("MORNING_10_12", 10 * 60, 12 * 60),
    ("MIDDAY_12_1530", 12 * 60, 15 * 60 + 30),
    ("US_OVERLAP_1530_1730", 15 * 60 + 30, 17 * 60 + 30),
    ("LATE_1730_2130", 17 * 60 + 30, 24 * 60),
)


def session_bucket(minute_of_day: int) -> str:
    for name, lo, hi in SESSION_BUCKETS:
        if lo <= minute_of_day < hi:
            return name
    raise ValueError(minute_of_day)


@dataclass
class Frame:
    """Arrays are treated as immutable; derived features are cached per key."""

    ts: pd.DatetimeIndex
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    spread: np.ndarray  # price units (points * 0.01)
    minute: np.ndarray  # Berlin minute of day at bar OPEN
    day: np.ndarray  # integer Berlin-date id (monotone)
    date: np.ndarray  # Berlin date (datetime64[D])
    contig_next: np.ndarray  # bool: bar i+1 exists and starts exactly one bar after bar i
    _cache: dict = field(default_factory=dict)

    @classmethod
    def from_dataframe(cls, df: pd.DataFrame) -> Frame:
        ts = pd.DatetimeIndex(df["ts"])
        local = ts.tz_convert(BERLIN)
        minute = np.asarray(local.hour * 60 + local.minute)
        dates = local.normalize().tz_localize(None).to_numpy().astype("datetime64[D]")
        _, day = np.unique(dates, return_inverse=True)
        secs = ts.as_unit("s").asi8
        contig = np.zeros(len(df), dtype=bool)
        contig[:-1] = (secs[1:] - secs[:-1]) == BAR_SECONDS
        return cls(
            ts=ts,
            o=df["open"].to_numpy(float),
            h=df["high"].to_numpy(float),
            l=df["low"].to_numpy(float),
            c=df["close"].to_numpy(float),
            spread=df["spread_pts"].to_numpy(float) * POINT,
            minute=minute,
            day=day.astype(np.int64),
            date=dates,
            contig_next=contig,
        )

    def __len__(self) -> int:
        return len(self.c)

    def head(self, n: int) -> Frame:
        """Prefix view (used by the look-ahead tests)."""
        contig = self.contig_next[:n].copy()
        if n:
            contig[-1] = False
        return Frame(
            ts=self.ts[:n],
            o=self.o[:n],
            h=self.h[:n],
            l=self.l[:n],
            c=self.c[:n],
            spread=self.spread[:n],
            minute=self.minute[:n],
            day=self.day[:n],
            date=self.date[:n],
            contig_next=contig,
        )

    # ---- causal features -------------------------------------------------------------
    def ema(self, n: int) -> np.ndarray:
        key = ("ema", n)
        if key not in self._cache:
            self._cache[key] = (
                pd.Series(self.c).ewm(span=n, adjust=False, min_periods=n).mean().to_numpy()
            )
        return self._cache[key]

    def atr(self, n: int = 14) -> np.ndarray:
        """Wilder ATR on closed bars (true range uses the previous close, so it includes gaps)."""
        key = ("atr", n)
        if key not in self._cache:
            pc = np.concatenate([[np.nan], self.c[:-1]])
            tr = np.fmax(self.h - self.l, np.fmax(np.abs(self.h - pc), np.abs(self.l - pc)))
            tr[0] = self.h[0] - self.l[0]
            self._cache[key] = (
                pd.Series(tr).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
            )
        return self._cache[key]

    def lagged(self, arr: np.ndarray, k: int, same_day: bool = True) -> np.ndarray:
        """arr[i-k]; NaN if unavailable or (same_day) in a different Berlin date."""
        out = np.full(len(arr), np.nan)
        if 0 < k < len(arr):
            out[k:] = arr[:-k]
            if same_day:
                other = self.day[k:] != self.day[:-k]
                seg = out[k:]
                seg[other] = np.nan
        elif k == 0:
            out[:] = arr
        return out

    def rolling_max_prev(self, arr: np.ndarray, n: int) -> np.ndarray:
        """max(arr[i-n .. i-1]) (excludes the current bar)."""
        return pd.Series(arr).shift(1).rolling(n, min_periods=n).max().to_numpy()

    def rolling_min_prev(self, arr: np.ndarray, n: int) -> np.ndarray:
        return pd.Series(arr).shift(1).rolling(n, min_periods=n).min().to_numpy()

    def efficiency_ratio(self, n: int = 48) -> np.ndarray:
        """Kaufman efficiency ratio over n closed bars: |net move| / path length (0..1)."""
        key = ("er", n)
        if key not in self._cache:
            s = pd.Series(self.c)
            net = s.diff(n).abs()
            path = s.diff().abs().rolling(n, min_periods=n).sum()
            self._cache[key] = (net / path.replace(0, np.nan)).to_numpy()
        return self._cache[key]

    def prior_day_levels(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Previous Berlin day's high / low / close, broadcast to every bar of the current day."""
        key = ("pdl",)
        if key not in self._cache:
            n_days = int(self.day.max()) + 1 if len(self.day) else 0
            hi = np.full(n_days, -np.inf)
            lo = np.full(n_days, np.inf)
            np.maximum.at(hi, self.day, self.h)
            np.minimum.at(lo, self.day, self.l)
            last_idx = np.zeros(n_days, dtype=np.int64)
            last_idx[self.day] = np.arange(len(self.day))  # last write wins = last bar of the day
            cl = self.c[last_idx]
            ph = np.concatenate([[np.nan], hi[:-1]])[self.day]
            pl = np.concatenate([[np.nan], lo[:-1]])[self.day]
            pc = np.concatenate([[np.nan], cl[:-1]])[self.day]
            self._cache[key] = (ph, pl, pc)
        return self._cache[key]
