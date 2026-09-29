"""Numba generator for the range mean-reversion family."""

from __future__ import annotations

import numpy as np
from numba import njit

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.mean_reversion.strategy import VARIANTS


@njit(cache=True)
def _kernel(ts, o, h, low, c, m15_o, m15_h, m15_l, m15_c, m15_range, h1_range, trend, extreme,
            extreme_fraction, stop_buffer, target_r):
    n = len(c)
    idx = np.empty(n, np.int64)
    side = np.empty(n, np.int8)
    stops = np.empty(n, np.float64)
    count = 0
    completed_h = np.empty(n, np.float64)
    completed_l = np.empty(n, np.float64)
    completed_count = 0
    last_key = np.int64(-9223372036854775807)
    last_o = last_h = last_l = last_c = np.nan
    for i in range(n):
        # M5 decisions are made at the close: the 09:10 bar already knows the
        # M15 bucket that becomes available at 09:15.
        key = (ts[i] + 300000000000) // 900000000000 - 1
        changed = (m15_o[i] != last_o or m15_h[i] != last_h or m15_l[i] != last_l
                   or m15_c[i] != last_c)
        if key != last_key and changed and np.isfinite(m15_h[i]) and np.isfinite(m15_l[i]):
            completed_h[completed_count] = m15_h[i]
            completed_l[completed_count] = m15_l[i]
            completed_count += 1
            last_key = key
            last_o, last_h, last_l, last_c = m15_o[i], m15_h[i], m15_l[i], m15_c[i]
        if trend[i] != 1 or not extreme[i] or completed_count < 13:
            continue
        start = completed_count - 13
        range_high = completed_h[start]
        range_low = completed_l[start]
        for j in range(start + 1, completed_count - 1):
            if completed_h[j] > range_high:
                range_high = completed_h[j]
            if completed_l[j] < range_low:
                range_low = completed_l[j]
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
        features["ts_ns"], features["o"], features["h"], features["l"], features["c"],
        features["m15_o"], features["m15_h"], features["m15_l"], features["m15_c"],
        features["m15_range"], features["h1_range"],
        features["regime_trend_strength"], features["context_range_extreme"],
        params.extreme_fraction, params.stop_buffer, params.target_r,
    )
    n = len(idx)
    return CandidateArrays(idx, side, stops, np.full(n, np.nan),
                           np.full(n, params.target_r), np.full(n, EXIT_FIXED_R, np.int8))
