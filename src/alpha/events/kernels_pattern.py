"""Chart patterns stamped at the bar that confirms them (LONG frame; SHORT via mirror)."""

from __future__ import annotations

import numba as nb
import numpy as np


@nb.njit(cache=True)
def double_bottom(high, low, close, atr, pl, pl_o, tol_atr, max_span, expiry):
    """PATTERN_COMPLETE double_bottom.

    Pair = two ADJACENT confirmed pivot lows L1 (bar p1) and L2 (bar p2, confirmed at c2) with
    ``|L2 - L1| <= tol_atr * atr[c2]``, ``p2 - p1 <= max_span`` and no lower low between them.
    Neckline = max high over ``[p1, p2]``.  The pattern is armed at c2 (a newer pair replaces it)
    and completes at the FIRST bar i in ``[c2, c2 + expiry]`` with ``close[i] > neckline``; one
    pulse per pattern.  ``evl`` = invalidation price = min(L1, L2), ``evx`` = neckline,
    ``origin`` = p1.  Only bars <= i are read.  Returns ``(pulse, evl, evx, origin)``.
    """
    n = len(close)
    pulse = np.zeros(n, np.uint8)
    evl = np.full(n, np.nan)
    evx = np.full(n, np.nan)
    org = np.full(n, -1, np.int64)
    last_p = np.nan
    last_o = -1
    armed = False
    neck = np.nan
    inval = np.nan
    start = -1
    deadline = -1
    for i in range(n):
        if np.isfinite(pl[i]):
            p2 = pl_o[i]
            l2 = pl[i]
            armed = False
            if np.isfinite(last_p) and p2 - last_o <= max_span:
                a = atr[i]
                if np.isfinite(a) and abs(l2 - last_p) <= tol_atr * a:
                    lowest = min(l2, last_p)
                    top = high[last_o]
                    ok = True
                    for k in range(last_o, p2 + 1):
                        if low[k] < lowest:
                            ok = False
                            break
                        if high[k] > top:
                            top = high[k]
                    if ok:
                        armed = True
                        neck = top
                        inval = lowest
                        start = last_o
                        deadline = i + expiry
            last_p = l2
            last_o = p2
        if armed:
            if i > deadline:
                armed = False
            elif close[i] > neck:
                pulse[i] = 1
                evl[i] = inval
                evx[i] = neck
                org[i] = start
                armed = False
    return pulse, evl, evx, org


@nb.njit(cache=True)
def inside_bar_break_up(high, low, close, start):
    """PATTERN_COMPLETE inside_bar_break_up: bar i-1 is an inside bar of mother bar i-2
    (``high[i-1] <= high[i-2]`` and ``low[i-1] >= low[i-2]``) and bar i closes above the mother
    high.  ``evl`` = mother low (invalidation), ``evx`` = mother high (breakout level),
    ``origin`` = i-2.  Run-local (``i - 2 >= start[i]``)."""
    n = len(close)
    pulse = np.zeros(n, np.uint8)
    evl = np.full(n, np.nan)
    evx = np.full(n, np.nan)
    org = np.full(n, -1, np.int64)
    for i in range(2, n):
        if i - 2 < start[i]:
            continue
        m = i - 2
        if high[i - 1] <= high[m] and low[i - 1] >= low[m] and close[i] > high[m]:
            pulse[i] = 1
            evl[i] = low[m]
            evx[i] = high[m]
            org[i] = m
    return pulse, evl, evx, org
