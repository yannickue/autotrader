"""Numba candidate kernel for the momentum-continuation family."""

from __future__ import annotations

import numpy as np
from numba import njit

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.momentum import VARIANTS, MomentumParams


@njit(cache=True)
def _scan(o, h, low, c, atr, direction, strength, context, count, max_range, impulse_atr, buffer):
    indices = []
    sides = []
    stops = []
    for i in range(count + 2, len(c)):
        side = 1 if direction[i] == 3 else -1 if direction[i] == 1 else 0
        if side == 0 or strength[i] != 3 or not context[i] or not np.isfinite(atr[i]):
            continue
        pause_high = h[i - count]
        pause_low = low[i - count]
        for j in range(i - count + 1, i):
            if h[j] > pause_high:
                pause_high = h[j]
            if low[j] < pause_low:
                pause_low = low[j]
        if pause_high - pause_low > max_range * atr[i]:
            continue
        impulse = c[i - count] - c[i - count - 2]
        if side == 1:
            if impulse < impulse_atr * atr[i] or not (c[i] > pause_high and c[i] > o[i]):
                continue
            stop = pause_low - buffer * atr[i]
        else:
            if impulse > -impulse_atr * atr[i] or not (c[i] < pause_low and c[i] < o[i]):
                continue
            stop = pause_high + buffer * atr[i]
        indices.append(i)
        sides.append(side)
        stops.append(stop)
    return np.asarray(indices, dtype=np.int64), np.asarray(sides, dtype=np.int8), np.asarray(stops)


@register("GROUP_A_MOMENTUM_CONTINUATION", VARIANTS)
def generate(features: FeatureSet, params: MomentumParams) -> CandidateArrays:
    """Generate decisions from aligned regime/context and trailing M5 arrays."""
    indices, sides, stops = _scan(
        features["o"],
        features["h"],
        features["l"],
        features["c"],
        features["m5_atr14"],
        features["regime_direction"],
        features["regime_trend_strength"],
        features["context_momentum_continuation"],
        params.consolidation_bars,
        params.max_range_atr,
        params.impulse_atr,
        params.stop_buffer_atr,
    )
    size = len(indices)
    return CandidateArrays(
        indices,
        sides,
        stops,
        np.full(size, np.nan),
        np.full(size, params.target_r),
        np.full(size, EXIT_FIXED_R, dtype=np.int8),
    )

