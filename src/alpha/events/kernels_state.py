"""Trend states on COMPLETE bars of one timeframe (M15 / H1 / D1)."""

from __future__ import annotations

import numpy as np
import talib


def trend_inputs(high: np.ndarray, low: np.ndarray, close: np.ndarray, lag: int = 3):
    """ATR14, ADX14 and ATR-scaled EMA20 slope on a compact series of complete bars.

    Same definitions as the V1 higher-timeframe features (``_ta_arrays``): ``ema_slope =
    (ema[i] - ema[i-lag]) / atr[i]``, NaN for the first ``lag`` bars.  All are trailing.
    """
    atr = talib.ATR(high, low, close, timeperiod=14)
    adx = talib.ADX(high, low, close, timeperiod=14)
    ema = talib.EMA(close, timeperiod=20)
    scale = np.where(atr > 0, atr, np.nan)
    slope = np.full(len(close), np.nan)
    if len(close) > lag:
        slope[lag:] = (ema[lag:] - ema[:-lag]) / scale[lag:]
    return atr, adx, slope


def trend_states(ema_slope: np.ndarray, adx: np.ndarray, adx_thr: float):
    """TREND_UP: ema slope > 0 and ADX > thr; TREND_DN: slope < 0 and ADX > thr (int8 0/1)."""
    ok = np.isfinite(ema_slope) & np.isfinite(adx) & (adx > adx_thr)
    up = (ok & (ema_slope > 0)).astype(np.int8)
    dn = (ok & (ema_slope < 0)).astype(np.int8)
    return up, dn
