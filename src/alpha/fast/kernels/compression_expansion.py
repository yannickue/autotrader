from __future__ import annotations

import numba as nb
import numpy as np

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.compression_expansion.strategy import VARIANTS


@nb.njit(cache=True)
def _kernel(
    high,
    low,
    close,
    m15_range,
    h1_range,
    compression,
    vol_state,
    volatility,
    box_bars,
    expansion_mult,
    memory_bars,
    stop_atr,
):
    n = len(close)
    out_i = np.empty(n, np.int64)
    out_d = np.empty(n, np.int8)
    out_s = np.empty(n, np.float64)
    count = 0
    since_compression = -1
    for i in range(n):
        if compression[i]:
            since_compression = 0
        elif since_compression >= 0:
            since_compression += 1
        eligible = vol_state[i] == 1 or volatility[i] == 1
        if not eligible or i < box_bars or since_compression < 0 or since_compression > memory_bars:
            continue
        box_high = high[i - box_bars]
        box_low = low[i - box_bars]
        range_sum = 0.0
        for j in range(i - box_bars, i):
            box_high = max(box_high, high[j])
            box_low = min(box_low, low[j])
            range_sum += high[j] - low[j]
        mean_range = range_sum / box_bars
        if mean_range <= 0.0 or high[i] - low[i] < expansion_mult * mean_range:
            continue
        direction = 0
        if close[i] > box_high:
            direction = 1
        elif close[i] < box_low:
            direction = -1
        if direction == 0:
            continue
        rng = max(high[i] - low[i], 1e-9)
        if np.isfinite(m15_range[i]):
            rng = max(rng, m15_range[i])
        if np.isfinite(h1_range[i]):
            rng = max(rng, h1_range[i])
        stop = box_low - stop_atr * rng if direction > 0 else box_high + stop_atr * rng
        if abs(close[i] - stop) <= 0.0:
            continue
        out_i[count] = i
        out_d[count] = direction
        out_s[count] = stop
        count += 1
    return out_i[:count], out_d[:count], out_s[:count]


@register("COMPRESSION_EXPANSION", VARIANTS)
def generate(features: FeatureSet, params) -> CandidateArrays:
    decision_idx, direction, stop = _kernel(
        features["h"],
        features["l"],
        features["c"],
        features["m15_range"],
        features["h1_range"],
        features["context_compression"],
        features["regime_vol_state"],
        features["regime_volatility"],
        int(params.box_bars),
        float(params.expansion_mult),
        int(params.compression_memory_bars),
        float(params.stop_atr),
    )
    count = len(decision_idx)
    return CandidateArrays(
        decision_idx,
        direction,
        stop,
        np.full(count, np.nan),
        np.full(count, float(params.target_r)),
        np.full(count, EXIT_FIXED_R, np.int8),
    )


__all__ = ["VARIANTS", "generate"]
