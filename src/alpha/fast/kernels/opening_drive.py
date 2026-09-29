"""Numba candidate kernel for Berlin cash-open drive continuation."""

from __future__ import annotations

import numpy as np
from numba import njit

from alpha.fast.registry import register
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import FeatureSet
from alpha.strategies.opening_drive import VARIANTS, OpeningDriveParams


@njit(cache=True)
def _scan(
    o, h, low, c, atr, minute, day, phase, direction, strength, pullback_context,
    continuation_context, pullback_bars, minimum_impulse, max_fraction, buffer,
):
    indices = []
    sides = []
    stops = []
    day_start = 0
    for i in range(len(c)):
        if i == 0 or day[i] != day[i - 1]:
            day_start = i
        if i < pullback_bars or (phase[i] != 0 and phase[i] != 1) or not np.isfinite(atr[i]):
            continue
        opening_count = 0
        opening_first = -1
        opening_last = -1
        opening_high = -np.inf
        opening_low = np.inf
        for j in range(day_start, i + 1):
            if 540 <= minute[j] < 600:
                if opening_first < 0:
                    opening_first = j
                opening_last = j
                opening_count += 1
                if h[j] > opening_high:
                    opening_high = h[j]
                if low[j] < opening_low:
                    opening_low = low[j]
        if opening_count != 12:
            continue
        drive = c[opening_last] - o[opening_first]
        side = 1 if drive > 0.0 else -1
        expected = 3 if side == 1 else 1
        if direction[i] != expected or (strength[i] != 3 and strength[i] != 2):
            continue
        if not (pullback_context[i] or continuation_context[i]):
            continue
        if abs(drive) < minimum_impulse * atr[i]:
            continue
        pause_high = h[i - pullback_bars]
        pause_low = low[i - pullback_bars]
        for j in range(i - pullback_bars + 1, i):
            if h[j] > pause_high:
                pause_high = h[j]
            if low[j] < pause_low:
                pause_low = low[j]
        depth = opening_high - pause_low if side == 1 else pause_high - opening_low
        if not (depth > 0.0 and depth <= max_fraction * abs(drive)):
            continue
        if side == 1:
            if not (c[i] > pause_high and c[i] > o[i]):
                continue
            stop = pause_low - buffer * atr[i]
        else:
            if not (c[i] < pause_low and c[i] < o[i]):
                continue
            stop = pause_high + buffer * atr[i]
        indices.append(i)
        sides.append(side)
        stops.append(stop)
    return np.asarray(indices, dtype=np.int64), np.asarray(sides, dtype=np.int8), np.asarray(stops)


@register("GROUP_A_OPENING_DRIVE", VARIANTS)
def generate(features: FeatureSet, params: OpeningDriveParams) -> CandidateArrays:
    """Generate cash-open continuation decisions with causal per-day scans."""
    indices, sides, stops = _scan(
        features["o"],
        features["h"],
        features["l"],
        features["c"],
        features["m5_atr14"],
        features["berlin_minute"],
        features["berlin_day_id"],
        features["phase_code"],
        features["regime_direction"],
        features["regime_trend_strength"],
        features["context_pullback"],
        features["context_trend_continuation"],
        params.pullback_bars,
        params.minimum_impulse_atr,
        params.max_pullback_fraction,
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

