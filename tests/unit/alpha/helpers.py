"""Synthetic frames for alpha tests (no real market data, no MT5)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from alpha.common.frame import Frame


def micro_frame(
    start_berlin: str,
    bars: list[tuple[float, float, float, float]],
    *,
    spread_pts: float = 200.0,
    skip: tuple[int, ...] = (),
) -> Frame:
    """Bars are (open, high, low, close) BID; 5-minute cadence from a Berlin wall-clock start.

    `skip` removes bar indexes from the sequence (to create data gaps).
    """
    start = pd.Timestamp(start_berlin, tz="Europe/Berlin")
    rows = []
    for i, (o, h, lo, c) in enumerate(bars):
        if i in skip:
            continue
        ts = (start + pd.Timedelta(minutes=5 * i)).tz_convert("UTC")
        rows.append(
            {"ts": ts, "open": o, "high": h, "low": lo, "close": c, "spread_pts": spread_pts}
        )
    return Frame.from_dataframe(pd.DataFrame(rows))


def flat_bars(n: int, price: float = 24000.0) -> list[tuple[float, float, float, float]]:
    return [(price, price, price, price)] * n


def synthetic_dataframe(days: int = 60, seed: int = 7) -> pd.DataFrame:
    """Random-walk M5 bars, weekdays only, 00:15-19:55 UTC (like the real Ger40 series)."""
    rng = np.random.default_rng(seed)
    rows = []
    price = 24000.0
    day_index = pd.bdate_range("2025-02-03", periods=days, tz="UTC")
    for d in day_index:
        times = pd.date_range(
            d + pd.Timedelta(minutes=15), d + pd.Timedelta(hours=19, minutes=55), freq="5min"
        )
        vol = rng.uniform(4.0, 10.0)
        drift = rng.normal(0, 0.15)
        for t in times:
            o = price
            step = rng.normal(drift, vol)
            c = o + step
            wig = abs(rng.normal(0, vol * 0.6))
            h = max(o, c) + wig
            lo = min(o, c) - wig
            rows.append(
                {
                    "ts": t, "open": o, "high": h, "low": lo, "close": c,
                    "spread_pts": float(rng.integers(120, 400)),
                }
            )  # fmt: skip
            price = c
    return pd.DataFrame(rows)
