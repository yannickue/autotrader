"""Numba generator for the equal-time session TWAP family."""

from __future__ import annotations

import numpy as np
from numba import njit

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.vwap_reference.strategy import VARIANTS


@njit(cache=True)
def _kernel(o, h, low, c, day, m15_range, h1_range, direction, trend, pullback,
            stop_atr, target_r):
    n = len(c)
    idx, side, stops = np.empty(n, np.int64), np.empty(n, np.int8), np.empty(n, np.float64)
    count = 0
    current_day = -1
    total = 0.0
    bars = 0
    last_close = 0.0
    for i in range(n):
        if day[i] != current_day:
            current_day, total, bars = day[i], 0.0, 0
        prior_ref = total / bars if bars else np.nan
        prior_close = last_close if bars else np.nan
        typical = (h[i] + low[i] + c[i]) / 3.0
        total += typical
        bars += 1
        reference = total / bars
        last_close = c[i]
        d = 1 if direction[i] == 3 else -1 if direction[i] == 1 else 0
        if trend[i] != 3 or d == 0 or not pullback[i] or not np.isfinite(prior_ref):
            continue
        reclaimed = ((d > 0 and prior_close <= prior_ref and c[i] > reference)
                     or (d < 0 and prior_close >= prior_ref and c[i] < reference))
        if not reclaimed or (d > 0 and c[i] <= o[i]) or (d < 0 and c[i] >= o[i]):
            continue
        value = max(m15_range[i], h1_range[i], h[i] - low[i], 1e-9)
        distance = stop_atr * value
        stop = min(low[i], c[i] - distance) if d > 0 else max(h[i], c[i] + distance)
        idx[count], side[count], stops[count] = i, d, stop
        count += 1
    return idx[:count], side[:count], stops[:count]


@register("SESSION_TWAP_REFERENCE", VARIANTS)
def generate(features: FeatureSet, params) -> CandidateArrays:
    idx, side, stops = _kernel(
        features["o"], features["h"], features["l"], features["c"], features["berlin_day_id"],
        features["m15_range"], features["h1_range"], features["regime_direction"],
        features["regime_trend_strength"], features["context_pullback"], params.stop_atr,
        params.target_r,
    )
    n = len(idx)
    return CandidateArrays(idx, side, stops, np.full(n, np.nan),
                           np.full(n, params.target_r), np.full(n, EXIT_FIXED_R, np.int8))

