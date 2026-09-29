from __future__ import annotations

import numba as nb
import numpy as np

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.failed_breakout.strategy import VARIANTS


@nb.njit(cache=True)
def _kernel(
    high,
    low,
    close,
    m15_range,
    h1_range,
    previous_day_high,
    previous_day_low,
    trend_strength,
    breakout_atr,
    fail_window,
    stop_atr,
):
    n = len(close)
    out_i = np.empty(n, np.int64)
    out_d = np.empty(n, np.int8)
    out_s = np.empty(n, np.float64)
    count = 0
    broke = False
    broke_direction = 0
    level = 0.0
    waited = 0
    extreme = 0.0
    for i in range(n):
        if trend_strength[i] != 1:
            continue
        pdh = previous_day_high[i]
        pdl = previous_day_low[i]
        if not np.isfinite(pdh) or not np.isfinite(pdl):
            broke = False
            continue
        rng = max(high[i] - low[i], 1e-9)
        if np.isfinite(m15_range[i]):
            rng = max(rng, m15_range[i])
        if np.isfinite(h1_range[i]):
            rng = max(rng, h1_range[i])
        if not broke:
            if close[i] > pdh + breakout_atr * rng:
                broke = True
                broke_direction = 1
                level = pdh
                waited = 0
                extreme = high[i]
            elif close[i] < pdl - breakout_atr * rng:
                broke = True
                broke_direction = -1
                level = pdl
                waited = 0
                extreme = low[i]
            continue
        waited += 1
        extreme = max(extreme, high[i]) if broke_direction > 0 else min(extreme, low[i])
        direction = 0
        if broke_direction > 0 and close[i] < level:
            direction = -1
            broke = False
        elif broke_direction < 0 and close[i] > level:
            direction = 1
            broke = False
        elif waited > fail_window:
            broke = False
        if direction == 0:
            continue
        stop = extreme + stop_atr * rng if direction < 0 else extreme - stop_atr * rng
        if abs(close[i] - stop) <= 0.0:
            continue
        out_i[count] = i
        out_d[count] = direction
        out_s[count] = stop
        count += 1
    return out_i[:count], out_d[:count], out_s[:count]


@register("FAILED_BREAKOUT_REVERSAL", VARIANTS)
def generate(features: FeatureSet, params) -> CandidateArrays:
    decision_idx, direction, stop = _kernel(
        features["h"],
        features["l"],
        features["c"],
        features["m15_range"],
        features["h1_range"],
        features["previous_day_high"],
        features["previous_day_low"],
        features["regime_trend_strength"],
        float(params.breakout_atr),
        int(params.fail_window_bars),
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
