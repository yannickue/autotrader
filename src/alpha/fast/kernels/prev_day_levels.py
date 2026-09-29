"""Numba generator for previous-day high/low rejections."""

from __future__ import annotations

import numpy as np
from numba import njit

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.prev_day_levels.strategy import VARIANTS


@njit(cache=True)
def _kernel(o, h, low, c, pdh, pdl, m15_range, h1_range, trend, extreme, reversal,
            tolerance_atr, stop_atr, target_r):
    n = len(c)
    idx, side, stops = np.empty(n, np.int64), np.empty(n, np.int8), np.empty(n, np.float64)
    count = 0
    for i in range(n):
        if (trend[i] != 1 and trend[i] != 2) or (not extreme[i] and not reversal[i]):
            continue
        if not np.isfinite(pdh[i]) or not np.isfinite(pdl[i]):
            continue
        value = max(m15_range[i], h1_range[i], h[i] - low[i], 1e-9)
        tolerance = tolerance_atr * value
        d, level = 0, 0.0
        if h[i] >= pdh[i] - tolerance:
            d, level = -1, pdh[i]
        elif low[i] <= pdl[i] + tolerance:
            d, level = 1, pdl[i]
        rejected = ((d < 0 and h[i] >= level and c[i] < level and c[i] < o[i])
                    or (d > 0 and low[i] <= level and c[i] > level and c[i] > o[i]))
        if not rejected:
            continue
        buffer = stop_atr * value
        stop = max(h[i], level + buffer) if d < 0 else min(low[i], level - buffer)
        idx[count], side[count], stops[count] = i, d, stop
        count += 1
    return idx[:count], side[:count], stops[:count]


@register("PREVIOUS_DAY_LEVELS", VARIANTS)
def generate(features: FeatureSet, params) -> CandidateArrays:
    idx, side, stops = _kernel(
        features["o"], features["h"], features["l"], features["c"],
        features["previous_day_high"], features["previous_day_low"], features["m15_range"],
        features["h1_range"], features["regime_trend_strength"],
        features["context_range_extreme"], features["context_reversal_context"],
        params.tolerance_atr, params.stop_atr, params.target_r,
    )
    n = len(idx)
    return CandidateArrays(idx, side, stops, np.full(n, np.nan),
                           np.full(n, params.target_r), np.full(n, EXIT_FIXED_R, np.int8))

