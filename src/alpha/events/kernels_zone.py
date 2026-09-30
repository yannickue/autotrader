"""Zones (ENTER/EXIT) and trendlines (TOUCH/BREAK), all boundaries from data known at the stamp.

Zone objects are immutable: bounds are fixed when the object forms and never rewritten; a new
object gets a new ``zid`` (>= 1, 0 = no zone).  ``kb_*`` arguments are the zone known BEFORE
bar i (closed bars < i, or day-level constants known at the day's start), so an ENTER/EXIT at
bar i compares the close of bar i with a zone that already existed.
"""

from __future__ import annotations

import numba as nb
import numpy as np


@nb.njit(cache=True)
def cluster_zones(ph, pl, atr, tol_atr, pad_atr, max_pivots, ttl):
    """Zone = cluster of confirmed same-side pivots.

    At each pivot confirmation (bar i, price P, ATR a = atr[i]) the previous ``max_pivots``
    confirmed pivots of the same side within ``tol_atr * a`` of P are collected (bounded, O(N *
    max_pivots)).  If at least one exists the cluster (they plus P) forms a NEW zone
    ``[min - pad_atr*a, max + pad_atr*a]`` stamped at i.  Lows are processed before highs at the
    same bar (highs win if both form).  A zone expires ``ttl`` bars after formation (lo/hi NaN,
    zid 0);
    ids are never reused.  Returns ``(lo, hi, zid, born)``: the zone at the CLOSE of bar i.
    """
    n = len(ph)
    lo = np.full(n, np.nan)
    hi = np.full(n, np.nan)
    zid = np.zeros(n, np.int32)
    born = np.full(n, -1, np.int64)
    buf_l = np.full(max_pivots, np.nan)
    buf_h = np.full(max_pivots, np.nan)
    cnt_l = 0
    cnt_h = 0
    z = 0
    cur_lo = np.nan
    cur_hi = np.nan
    cur_born = -1
    for i in range(n):
        a = atr[i]
        for side in range(2):
            p = pl[i] if side == 0 else ph[i]
            if not np.isfinite(p):
                continue
            if np.isfinite(a) and a > 0.0:
                mn = p
                mx = p
                found = False
                m = cnt_l if side == 0 else cnt_h
                for k in range(m):
                    q = buf_l[k] if side == 0 else buf_h[k]
                    if abs(q - p) <= tol_atr * a:
                        found = True
                        if q < mn:
                            mn = q
                        if q > mx:
                            mx = q
                if found:
                    z += 1
                    cur_lo = mn - pad_atr * a
                    cur_hi = mx + pad_atr * a
                    cur_born = i
            # push P into the ring buffer (newest first, bounded)
            if side == 0:
                for k in range(min(cnt_l, max_pivots - 1), 0, -1):
                    buf_l[k] = buf_l[k - 1]
                buf_l[0] = p
                cnt_l = min(cnt_l + 1, max_pivots)
            else:
                for k in range(min(cnt_h, max_pivots - 1), 0, -1):
                    buf_h[k] = buf_h[k - 1]
                buf_h[0] = p
                cnt_h = min(cnt_h + 1, max_pivots)
        if cur_born >= 0 and i - cur_born < ttl:
            lo[i] = cur_lo
            hi[i] = cur_hi
            zid[i] = z
            born[i] = cur_born
    return lo, hi, zid, born


@nb.njit(cache=True)
def level_zone_ids(lo, hi):
    """Identity of a zone given by per-bar bounds: a new id whenever the (lo, hi) pair changes;
    0 while unknown.  Returns ``(zid, born)`` (born = first bar of the current object)."""
    n = len(lo)
    zid = np.zeros(n, np.int32)
    born = np.full(n, -1, np.int64)
    z = 0
    b = -1
    for i in range(n):
        if np.isfinite(lo[i]) and np.isfinite(hi[i]):
            if i == 0 or not (
                np.isfinite(lo[i - 1])
                and np.isfinite(hi[i - 1])
                and lo[i] == lo[i - 1]
                and hi[i] == hi[i - 1]
            ):
                z += 1
                b = i
            zid[i] = z
            born[i] = b
    return zid, born


@nb.njit(cache=True)
def zone_events(close, kb_lo, kb_hi, kb_zid, kb_born):
    """ZONE_ENTER: first close inside the zone known before the bar (previous close outside it).
    ZONE_EXIT: first close outside the zone after a close inside it.  Both compare closes of
    bars i-1 and i with the same (kb) bounds.  Returns ``(enter, exit, origin=born)``."""
    n = len(close)
    enter = np.zeros(n, np.uint8)
    leave = np.zeros(n, np.uint8)
    org = np.full(n, -1, np.int64)
    for i in range(1, n):
        if kb_zid[i] <= 0:
            continue
        lo = kb_lo[i]
        hi = kb_hi[i]
        c = close[i]
        p = close[i - 1]
        if not (np.isfinite(lo) and np.isfinite(hi) and np.isfinite(c) and np.isfinite(p)):
            continue
        inside = lo <= c <= hi
        was_inside = lo <= p <= hi
        if inside and not was_inside:
            enter[i] = 1
            org[i] = kb_born[i]
        elif (not inside) and was_inside:
            leave[i] = 1
            org[i] = kb_born[i]
    return enter, leave, org


@nb.njit(cache=True)
def trendline_lines(ph, ph_o, pl, pl_o, ttl):
    """Trendline through the last two confirmed pivots of one side, live from the confirmation
    of the 2nd anchor.  Support line = last two pivot LOWS if ascending; resistance line = last
    two pivot HIGHS if descending.  The current line is the valid one with the most recent 2nd
    confirmation (ties: support); it expires ``ttl`` bars after that confirmation.  Geometry uses
    the pivot bar positions (slope), which are known at the 2nd confirmation.
    Returns ``(lv, slope, side, zid, born)``: line value at each bar (NaN none), slope per bar,
    side (+1 support, -1 resistance, 0 none), line id, confirmation bar of the 2nd anchor.
    """
    n = len(ph)
    lv = np.full(n, np.nan)
    slope = np.full(n, np.nan)
    side = np.zeros(n, np.int8)
    zid = np.zeros(n, np.int32)
    born = np.full(n, -1, np.int64)
    l_p = np.nan
    l_o = -1
    hl_p = np.nan
    hl_o = -1
    has_l = False
    h_p = np.nan
    h_o = -1
    hh_p = np.nan
    hh_o = -1
    has_h = False
    sup_valid = False
    sup_conf = -1
    sup_slope = 0.0
    res_valid = False
    res_conf = -1
    res_slope = 0.0
    z = 0
    cur_side = 0
    cur_conf = -1
    for i in range(n):
        if np.isfinite(pl[i]):
            hl_p = l_p
            hl_o = l_o
            has_l = np.isfinite(hl_p)
            l_p = pl[i]
            l_o = pl_o[i]
            if has_l and l_p > hl_p:
                sup_valid = True
                sup_conf = i
                sup_slope = (l_p - hl_p) / (l_o - hl_o)
            else:
                sup_valid = False
        if np.isfinite(ph[i]):
            hh_p = h_p
            hh_o = h_o
            has_h = np.isfinite(hh_p)
            h_p = ph[i]
            h_o = ph_o[i]
            if has_h and h_p < hh_p:
                res_valid = True
                res_conf = i
                res_slope = (h_p - hh_p) / (h_o - hh_o)
            else:
                res_valid = False
        use_sup = sup_valid and i - sup_conf < ttl
        use_res = res_valid and i - res_conf < ttl
        s = 0
        if use_sup and (not use_res or sup_conf >= res_conf):
            s = 1
        elif use_res:
            s = -1
        if s == 0:
            continue
        conf = sup_conf if s == 1 else res_conf
        if s != cur_side or conf != cur_conf:
            z += 1
            cur_side = s
            cur_conf = conf
        if s == 1:
            lv[i] = l_p + sup_slope * (i - l_o)
            slope[i] = sup_slope
        else:
            lv[i] = h_p + res_slope * (i - h_o)
            slope[i] = res_slope
        side[i] = s
        zid[i] = z
        born[i] = conf
    return lv, slope, side, zid, born


@nb.njit(cache=True)
def trendline_events(high, low, close, atr, lv, slope, side, born, tol_atr):
    """TRENDLINE_TOUCH / TRENDLINE_BREAK against the line live at bar i (line born at bar < i).

    Support: TOUCH = low <= v + tol*atr and close > v (first such bar after a non-touching bar);
    BREAK = close < v - tol*atr while the previous close was not below its (extrapolated) line
    minus tol.  Resistance mirrored.  evl = line value at the bar.  Returns
    ``(touch, brk, evl_touch, evl_break)``."""
    n = len(close)
    touch = np.zeros(n, np.uint8)
    brk = np.zeros(n, np.uint8)
    evl_t = np.full(n, np.nan)
    evl_b = np.full(n, np.nan)
    for i in range(1, n):
        s = side[i]
        if s == 0 or born[i] >= i:
            continue
        a = atr[i]
        if not (np.isfinite(a) and np.isfinite(lv[i])):
            continue
        v = lv[i]
        vp = v - slope[i]
        ap = atr[i - 1] if np.isfinite(atr[i - 1]) else a
        tol = tol_atr * a
        tolp = tol_atr * ap
        if s == 1:
            touching = low[i] <= v + tol and close[i] > v
            touching_prev = low[i - 1] <= vp + tolp and close[i - 1] > vp and born[i] < i - 1
            broke = close[i] < v - tol and close[i - 1] >= vp - tolp
        else:
            touching = high[i] >= v - tol and close[i] < v
            touching_prev = high[i - 1] >= vp - tolp and close[i - 1] < vp and born[i] < i - 1
            broke = close[i] > v + tol and close[i - 1] <= vp + tolp
        if touching and not touching_prev:
            touch[i] = 1
            evl_t[i] = v
        if broke:
            brk[i] = 1
            evl_b[i] = v
    return touch, brk, evl_t, evl_b
