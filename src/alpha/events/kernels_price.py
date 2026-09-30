"""Price events: liquidity sweeps and momentum resumption (LONG frame; SHORT via mirror).

The SHORT frame is computed by running the LONG kernel on the price mirror
(``o,h,l,c -> -o,-l,-h,-c``) and negating price outputs, so the pair is exactly symmetric.
"""

from __future__ import annotations

import numba as nb
import numpy as np


def mirror_bars(o, h, low, c):
    """Price mirror used to derive SHORT-frame events from the LONG kernels."""
    return -o, -low, -h, -c


@nb.njit(cache=True)
def prior_extreme(values, window, start, is_min):
    """Extreme of ``values[i-window .. i-1]`` (known before bar i); NaN unless the window lies
    inside the run (``i - window >= start[i]``).  Also returns the argmin/argmax bar index."""
    n = len(values)
    level = np.full(n, np.nan)
    origin = np.full(n, -1, np.int64)
    for i in range(n):
        if i - window < start[i]:
            continue
        best = values[i - window]
        arg = i - window
        for k in range(i - window + 1, i):
            v = values[k]
            if (is_min and v < best) or ((not is_min) and v > best):
                best = v
                arg = k
        level[i] = best
        origin[i] = arg
    return level, origin


@nb.njit(cache=True)
def session_prior_extreme(values, day_id, is_min):
    """Running extreme of the current day's bars strictly before bar i (NaN on a day's first
    bar).  ``day_id`` is any per-bar day label; a change resets the run."""
    n = len(values)
    level = np.full(n, np.nan)
    cur = np.nan
    for i in range(n):
        if i == 0 or day_id[i] != day_id[i - 1]:
            cur = np.nan
        level[i] = cur
        v = values[i]
        if np.isfinite(v) and (
            not np.isfinite(cur) or (is_min and v < cur) or ((not is_min) and v > cur)
        ):
            cur = v
    return level


@nb.njit(cache=True)
def shift_known_before(level, origin):
    """Level known before bar i = the level state at the close of bar i-1."""
    n = len(level)
    out = np.full(n, np.nan)
    out_o = np.full(n, -1, np.int64)
    for i in range(1, n):
        out[i] = level[i - 1]
        out_o[i] = origin[i - 1]
    return out, out_o


@nb.njit(cache=True)
def sweep_low(low, close, level, level_origin):
    """SWEEP_LOW: wick below a level known before the bar, close back above it (stamped at that
    close).  ``level`` is the level known before bar i.  Returns
    ``(pulse, evl=level, evx=wick low, origin)``."""
    n = len(low)
    pulse = np.zeros(n, np.uint8)
    evl = np.full(n, np.nan)
    evx = np.full(n, np.nan)
    org = np.full(n, -1, np.int64)
    for i in range(n):
        lv = level[i]
        if np.isfinite(lv) and low[i] < lv and close[i] > lv:
            pulse[i] = 1
            evl[i] = lv
            evx[i] = low[i]
            org[i] = level_origin[i]
    return pulse, evl, evx, org


@nb.njit(cache=True)
def momentum_resume_up(o, h, low, c, atr, start, window, impulse_atr, depth_atr):
    """MOMENTUM_RESUME_UP: a close resumes up after a shallow pullback.

    In the ``window`` bars before i, the latest bar j holding the highest high must lie at least
    two bars back (>=1 pullback bar), the pullback low ``pl = min(low[j+1..i-1])`` must be at
    most ``depth_atr`` ATR below ``h[j]`` (and strictly below it), and the leg into j must span at
    least ``impulse_atr`` ATR (``h[j] - min(low[i-window..j])``).  Bar i must close above the
    previous high and above its own open while the previous bar did not itself close above the
    high before it (first bar of the resumption).  evl = pullback low.  Returns ``(pulse, evl)``.
    """
    n = len(c)
    pulse = np.zeros(n, np.uint8)
    evl = np.full(n, np.nan)
    for i in range(2, n):
        if i - window < start[i]:
            continue
        a = atr[i]
        if not (np.isfinite(a) and a > 0.0):
            continue
        j = i - window
        best = h[j]
        for k in range(i - window + 1, i):
            if h[k] >= best:
                best = h[k]
                j = k
        if j > i - 2:
            continue
        pl = low[j + 1]
        for k in range(j + 2, i):
            if low[k] < pl:
                pl = low[k]
        depth = best - pl
        if not (depth > 0.0 and depth <= depth_atr * a):
            continue
        base = low[i - window]
        for k in range(i - window + 1, j + 1):
            if low[k] < base:
                base = low[k]
        if best - base < impulse_atr * a:
            continue
        if not (c[i] > h[i - 1] and c[i] > o[i]):
            continue
        if c[i - 1] > h[i - 2]:
            continue
        pulse[i] = 1
        evl[i] = pl
    return pulse, evl
