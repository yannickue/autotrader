from __future__ import annotations

import numba as nb
import numpy as np

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.breakout_retest.strategy import VARIANTS


@nb.njit(cache=True)
def _kernel(
    high,
    low,
    close,
    m15_range,
    h1_range,
    minute,
    day_id,
    trend_strength,
    breakout_atr,
    retest_atr,
    stop_atr,
    max_wait,
):
    n = len(close)
    out_i = np.empty(n, np.int64)
    out_d = np.empty(n, np.int8)
    out_s = np.empty(n, np.float64)
    count = 0
    active_day = -1
    or_high = 0.0
    or_low = 0.0
    or_known = False
    or_done = False
    broke = False
    broke_direction = 0
    broke_level = 0.0
    waited = 0
    traded = False
    for i in range(n):
        if day_id[i] != active_day:
            active_day = day_id[i]
            or_known = False
            or_done = False
            broke = False
            traded = False
        if 540 <= minute[i] < 570:
            if not or_known:
                or_high = high[i]
                or_low = low[i]
                or_known = True
            else:
                or_high = max(or_high, high[i])
                or_low = min(or_low, low[i])
            or_done = minute[i] == 565
            continue
        if not or_done or minute[i] >= 990 or traded or trend_strength[i] == 1:
            continue
        rng = max(high[i] - low[i], 1e-9)
        if np.isfinite(m15_range[i]):
            rng = max(rng, m15_range[i])
        if np.isfinite(h1_range[i]):
            rng = max(rng, h1_range[i])
        if not broke:
            if close[i] > or_high + breakout_atr * rng:
                broke = True
                broke_direction = 1
                broke_level = or_high
                waited = 0
            elif close[i] < or_low - breakout_atr * rng:
                broke = True
                broke_direction = -1
                broke_level = or_low
                waited = 0
            continue
        waited += 1
        mid = (or_high + or_low) / 2.0
        lost = close[i] < mid if broke_direction > 0 else close[i] > mid
        if waited > max_wait or lost:
            broke = False
            continue
        retested = (
            broke_direction > 0
            and low[i] <= broke_level + retest_atr * rng
            and close[i] > broke_level
        ) or (
            broke_direction < 0
            and high[i] >= broke_level - retest_atr * rng
            and close[i] < broke_level
        )
        if not retested:
            continue
        stop = (
            min(low[i], broke_level - stop_atr * rng)
            if broke_direction > 0
            else max(high[i], broke_level + stop_atr * rng)
        )
        if abs(close[i] - stop) <= 0.0:
            continue
        out_i[count] = i
        out_d[count] = broke_direction
        out_s[count] = stop
        count += 1
        traded = True
    return out_i[:count], out_d[:count], out_s[:count]


@register("OPENING_RANGE_BREAKOUT_RETEST", VARIANTS)
def generate(features: FeatureSet, params) -> CandidateArrays:
    decision_idx, direction, stop = _kernel(
        features["h"],
        features["l"],
        features["c"],
        features["m15_range"],
        features["h1_range"],
        features["berlin_minute"],
        features["berlin_day_id"],
        features["regime_trend_strength"],
        float(params.breakout_atr),
        float(params.retest_tolerance_atr),
        float(params.stop_atr),
        int(params.max_wait_bars),
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
