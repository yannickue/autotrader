"""GER40_PULLBACK_V1 - buy/sell the resumption after a volatility-normalized pullback in a trend.

Trend state (deterministic, no chart patterns):
  UP   : EMA50 > EMA200 and EMA50_i > EMA50_{i-12}     DOWN: mirrored.
Setup (LONG; SHORT mirrored) on closed bar i:
  * swing = highest high of the last 12 bars (incl. i); pullback low = lowest low of the last 6
  * depth = (swing - pullback low) / ATR14 >= `depth`             (retracement, vol-normalized)
  * pullback low <= EMA(ema_fast)                                  (reached the dynamic level)
  * resumption: close_i > EMA(ema_fast)_i, close_i > open_i, and at least one of the previous 3
    closes was at/below its EMA(ema_fast)                          (failed counter-move)
  stop: "swing" = pullback low - 0.25 * ATR14, or "atr2" = close - 2 * ATR14.
"""

from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from alpha.common.frame import Frame
from alpha.common.sim import Signals

NAME = "GER40_PULLBACK_V1"
SWING_N, PB_N, SLOPE_N, RESUME_N = 12, 6, 12, 3
GRID: list[dict] = [
    {"ema_fast": ef, "depth": dp, "stop_mode": sm}
    for ef, dp, sm in itertools.product((21, 34), (1.0, 2.0), ("swing", "atr2"))
]


def signals(fr: Frame, p: dict) -> Signals:
    atr = fr.atr(14)
    ef = fr.ema(int(p["ema_fast"]))
    e50, e200 = fr.ema(50), fr.ema(200)
    e50_lag = pd.Series(e50).shift(SLOPE_N).to_numpy()
    h, low, c, o = fr.h, fr.l, fr.c, fr.o
    swing_hi = pd.Series(h).rolling(SWING_N, min_periods=SWING_N).max().to_numpy()
    swing_lo = pd.Series(low).rolling(SWING_N, min_periods=SWING_N).min().to_numpy()
    pb_lo = pd.Series(low).rolling(PB_N, min_periods=PB_N).min().to_numpy()
    pb_hi = pd.Series(h).rolling(PB_N, min_periods=PB_N).max().to_numpy()
    below = pd.Series((c <= ef).astype(float)).where(~np.isnan(ef))
    above = pd.Series((c >= ef).astype(float)).where(~np.isnan(ef))
    was_below = below.shift(1).rolling(RESUME_N, min_periods=RESUME_N).max().to_numpy() > 0
    was_above = above.shift(1).rolling(RESUME_N, min_periods=RESUME_N).max().to_numpy() > 0
    with np.errstate(invalid="ignore", divide="ignore"):
        up_trend = (e50 > e200) & (e50 > e50_lag)
        dn_trend = (e50 < e200) & (e50 < e50_lag)
        depth_long = (swing_hi - pb_lo) / atr
        depth_short = (pb_hi - swing_lo) / atr
        long_ok = (
            up_trend & (depth_long >= p["depth"]) & (pb_lo <= ef) & (c > ef) & (c > o) & was_below
        )
        short_ok = (
            dn_trend & (depth_short >= p["depth"]) & (pb_hi >= ef) & (c < ef) & (c < o) & was_above
        )
    side = np.where(long_ok, 1, np.where(short_ok, -1, 0)).astype(np.int8)
    if p["stop_mode"] == "swing":
        s_long, s_short = pb_lo - 0.25 * atr, pb_hi + 0.25 * atr
    else:
        s_long, s_short = c - 2.0 * atr, c + 2.0 * atr
    stop = np.where(long_ok, s_long, np.where(short_ok, s_short, np.nan))
    return Signals(side=side, stop=stop)
