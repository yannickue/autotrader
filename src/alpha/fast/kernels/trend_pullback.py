"""Numba candidate kernel for the H1/M15 trend-pullback family."""

from __future__ import annotations

import numpy as np
from numba import njit

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.trend_pullback import VARIANTS, TrendPullbackParams


@njit(cache=True)
def _scan(o, h, low, c, atr, direction, strength, pullback, resumption, structure, buffer):
    indices = []
    sides = []
    stops = []
    needed = max(resumption + 1, structure)
    for i in range(needed, len(c)):
        side = 1 if direction[i] == 3 else -1 if direction[i] == 1 else 0
        if side == 0 or strength[i] != 3 or not pullback[i] or not np.isfinite(atr[i]):
            continue
        varied = False
        for j in range(i - structure + 1, i):
            if c[j] != c[j - 1]:
                varied = True
                break
        if not varied:
            continue
        pulled_back = False
        extreme = h[i - resumption] if side == 1 else low[i - resumption]
        for j in range(i - resumption, i):
            if side == 1:
                if h[j] > extreme:
                    extreme = h[j]
                if j > i - resumption and c[j] - c[j - 1] < 0.0:
                    pulled_back = True
            else:
                if low[j] < extreme:
                    extreme = low[j]
                if j > i - resumption and c[j] - c[j - 1] > 0.0:
                    pulled_back = True
        if not pulled_back:
            continue
        if side == 1:
            if not (c[i] > extreme and c[i] > o[i]):
                continue
            stop = low[i - structure]
            for j in range(i - structure + 1, i + 1):
                if low[j] < stop:
                    stop = low[j]
            stop -= buffer * atr[i]
        else:
            if not (c[i] < extreme and c[i] < o[i]):
                continue
            stop = h[i - structure]
            for j in range(i - structure + 1, i + 1):
                if h[j] > stop:
                    stop = h[j]
            stop += buffer * atr[i]
        indices.append(i)
        sides.append(side)
        stops.append(stop)
    return np.asarray(indices, dtype=np.int64), np.asarray(sides, dtype=np.int8), np.asarray(stops)


@register("GROUP_A_TREND_PULLBACK", VARIANTS)
def generate(features: FeatureSet, params: TrendPullbackParams) -> CandidateArrays:
    """Generate decisions with the same trailing windows as the semantic strategy."""
    indices, sides, stops = _scan(
        features["o"],
        features["h"],
        features["l"],
        features["c"],
        features["m5_atr14"],
        features["regime_direction"],
        features["regime_trend_strength"],
        features["context_pullback"],
        params.resumption_lookback,
        params.structure_lookback,
        params.stop_buffer_atr,
    )
    count = len(indices)
    return CandidateArrays(
        indices,
        sides,
        stops,
        np.full(count, np.nan),
        np.full(count, params.target_r),
        np.full(count, EXIT_FIXED_R, dtype=np.int8),
    )

