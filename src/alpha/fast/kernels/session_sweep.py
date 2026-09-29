"""Numba generator for running-session sweep reversals."""

from __future__ import annotations

import numpy as np
from numba import njit

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.session_sweep.strategy import VARIANTS


@njit(cache=True)
def _kernel(h, low, c, day, session_high, session_low, m15_range, h1_range, trend, reversal,
            sweep_atr, stop_atr, target_r):
    n = len(c)
    idx, side, stops = np.empty(n, np.int64), np.empty(n, np.int8), np.empty(n, np.float64)
    count = 0
    current_day = -1
    prior_high = np.nan
    prior_low = np.nan
    for i in range(n):
        if day[i] != current_day:
            current_day, prior_high, prior_low = day[i], np.nan, np.nan
        old_high, old_low = prior_high, prior_low
        prior_high, prior_low = session_high[i], session_low[i]
        if (trend[i] != 1 and trend[i] != 2) or not reversal[i]:
            continue
        if not np.isfinite(old_high) or not np.isfinite(old_low):
            continue
        value = max(m15_range[i], h1_range[i], h[i] - low[i], 1e-9)
        buffer = sweep_atr * value
        d = 0
        if h[i] > old_high + buffer and c[i] < old_high:
            d = -1
        elif low[i] < old_low - buffer and c[i] > old_low:
            d = 1
        if d == 0:
            continue
        stop_buffer = stop_atr * value
        stop = h[i] + stop_buffer if d < 0 else low[i] - stop_buffer
        idx[count], side[count], stops[count] = i, d, stop
        count += 1
    return idx[:count], side[:count], stops[:count]


@register("SESSION_SWEEP_REVERSAL", VARIANTS)
def generate(features: FeatureSet, params) -> CandidateArrays:
    idx, side, stops = _kernel(
        features["h"], features["l"], features["c"], features["berlin_day_id"],
        features["session_high"], features["session_low"], features["m15_range"],
        features["h1_range"], features["regime_trend_strength"],
        features["context_reversal_context"], params.sweep_atr, params.stop_atr, params.target_r,
    )
    n = len(idx)
    return CandidateArrays(idx, side, stops, np.full(n, np.nan),
                           np.full(n, params.target_r), np.full(n, EXIT_FIXED_R, np.int8))
