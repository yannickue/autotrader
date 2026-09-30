"""Synthetic M5 frames for the V2 multi-market tests (no MT5, no real data)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_frame(start_utc: str, end_utc: str, *, seed: int = 1, spread_pts: float = 100.0,
               daily_windows: tuple[tuple[int, int], ...] | None = None,
               price: float = 100.0) -> pd.DataFrame:
    """M5 random-walk bars (V1 schema), weekdays only. `daily_windows`: UTC (start_min, end_min)
    pairs kept per day; default = the whole day."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start_utc, end_utc, freq="5min", tz="UTC", inclusive="left")
    idx = idx[idx.dayofweek < 5]
    if daily_windows is not None:
        m = np.asarray(idx.hour * 60 + idx.minute)
        keep = np.zeros(len(idx), dtype=bool)
        for a, b in daily_windows:
            keep |= (m >= a) & (m < b)
        idx = idx[keep]
    n = len(idx)
    step = rng.normal(0, 0.05, n)
    close = price + np.cumsum(step)
    open_ = np.r_[price, close[:-1]]
    wig = np.abs(rng.normal(0, 0.03, n))
    return pd.DataFrame({
        "ts": idx, "open": open_, "high": np.maximum(open_, close) + wig,
        "low": np.minimum(open_, close) - wig, "close": close,
        "tick_volume": rng.integers(10, 500, n).astype(float),
        "spread_pts": np.full(n, float(spread_pts)),
    })
