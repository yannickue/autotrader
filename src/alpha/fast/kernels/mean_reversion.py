"""Numba generator for the range mean-reversion family.

The M15 range comes straight from the context frame (``context_range_low/high`` store arrays),
exactly like the semantic strategy; no bucket reconstruction happens in the kernel.
"""

from __future__ import annotations

import numpy as np
from numba import njit

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.mean_reversion.strategy import VARIANTS


@njit(cache=True)
def _kernel(o, h, low, c, m15_h, trend, extreme, range_low_a, range_high_a,
            extreme_fraction, stop_buffer, target_r):
    n = len(c)
    idx = np.empty(n, np.int64)
    side = np.empty(n, np.int8)
    stops = np.empty(n, np.float64)
    count = 0
    for i in range(n):
        if trend[i] != 1 or not extreme[i] or not np.isfinite(m15_h[i]):
            continue
        range_low = range_low_a[i]
        range_high = range_high_a[i]
        if not range_low < range_high:
            continue
        width = range_high - range_low
        direction = 0
        if c[i] <= range_low + extreme_fraction * width:
            direction = 1
        elif c[i] >= range_high - extreme_fraction * width:
            direction = -1
        rejected = ((direction > 0 and low[i] <= range_low and c[i] > range_low and c[i] > o[i])
                    or (direction < 0 and h[i] >= range_high and c[i] < range_high and c[i] < o[i]))
        if not rejected:
            continue
        stop = (
            range_low - stop_buffer * width
            if direction > 0
            else range_high + stop_buffer * width
        )
        risk = c[i] - stop if direction > 0 else stop - c[i]
        target = c[i] + direction * target_r * risk
        mid = (range_high + range_low) / 2.0
        if (direction > 0 and target > mid) or (direction < 0 and target < mid):
            continue
        idx[count], side[count], stops[count] = i, direction, stop
        count += 1
    return idx[:count], side[:count], stops[:count]


@register("RANGE_MEAN_REVERSION", VARIANTS)
def generate(features: FeatureSet, params) -> CandidateArrays:
    idx, side, stops = _kernel(
        features["o"], features["h"], features["l"], features["c"], features["m15_h"],
        features["regime_trend_strength"], features["context_range_extreme"],
        features["context_range_low"], features["context_range_high"],
        params.extreme_fraction, params.stop_buffer, params.target_r,
    )
    n = len(idx)
    return CandidateArrays(idx, side, stops, np.full(n, np.nan),
                           np.full(n, params.target_r), np.full(n, EXIT_FIXED_R, np.int8))
