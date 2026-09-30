"""Swing pivots, swing levels, close-based BOS and CHOCH (V2 temporal engine, W1).

All kernels work on one timeframe's own bar series (M5 bars or the compact series of complete
HTF bars) and return arrays indexed by that series' bar index.  ``htf.py`` maps them to M5.

Confirmation-lag rule: a pivot at ``p`` is exposed at ``p + order`` (never earlier, never
repainted).  Origin indices (``p``) are returned for reporting only.
"""

from __future__ import annotations

import numba as nb
import numpy as np


@nb.njit(cache=True)
def pivot_confirmations(high, low, order):
    """Confirmed pivots stamped at their confirmation bar ``p + order``.

    Same rule as V1 ``_swings``: pivot high = finite, strictly greater than every other bar in
    ``[p-order, p+order]`` (NaN bars ignored); pivot low mirrored.  Returns
    ``(ph_price, ph_origin, pl_price, pl_origin)``; price NaN / origin -1 where no pivot is
    confirmed at that bar.
    """
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    ph_o = np.full(n, -1, np.int64)
    pl_o = np.full(n, -1, np.int64)
    for conf in range(2 * order, n):
        p = conf - order
        hp = high[p]
        if np.isfinite(hp):
            ok = True
            for k in range(p - order, p + order + 1):
                if k != p and high[k] >= hp:  # NaN compares False (ignored)
                    ok = False
                    break
            if ok:
                ph[conf] = hp
                ph_o[conf] = p
        lp = low[p]
        if np.isfinite(lp):
            ok = True
            for k in range(p - order, p + order + 1):
                if k != p and low[k] <= lp:
                    ok = False
                    break
            if ok:
                pl[conf] = lp
                pl_o[conf] = p
    return ph, ph_o, pl, pl_o


@nb.njit(cache=True)
def swing_levels(ph, ph_o, pl, pl_o):
    """Last confirmed swing high/low (and origin), forward filled from the confirmation bar."""
    n = len(ph)
    hi = np.full(n, np.nan)
    lo = np.full(n, np.nan)
    hi_o = np.full(n, -1, np.int64)
    lo_o = np.full(n, -1, np.int64)
    cur_h = np.nan
    cur_l = np.nan
    cur_ho = -1
    cur_lo = -1
    for i in range(n):
        if np.isfinite(ph[i]):
            cur_h = ph[i]
            cur_ho = ph_o[i]
        if np.isfinite(pl[i]):
            cur_l = pl[i]
            cur_lo = pl_o[i]
        hi[i] = cur_h
        lo[i] = cur_l
        hi_o[i] = cur_ho
        lo_o[i] = cur_lo
    return hi, hi_o, lo, lo_o


@nb.njit(cache=True)
def bos_choch(close, ph, ph_o, pl, pl_o):
    """Close-based break of structure, exactly one pulse per confirmed swing.

    BOS_UP at bar i: the FIRST close above the last confirmed swing high, considering only
    swings confirmed at bars < i (a swing confirmed at bar i can never be broken at i because
    the pivot is the window maximum).  Once broken, the swing is disarmed until a NEW swing high
    is confirmed.  BOS_DN mirrored.  Prevailing structure = direction of the most recent BOS
    (0 before the first one); CHOCH_UP is a BOS_UP while the prevailing structure is down
    (set by an earlier BOS_DN), CHOCH_DN mirrored.  Returns
    ``(bos_up, bos_dn, choch_up, choch_dn, lvl_up, lvl_dn, org_up, org_dn, structure)``;
    ``lvl_*`` is the broken swing level at pulse bars, ``org_*`` its pivot bar (informational),
    ``structure`` the prevailing state AFTER bar i (int8).
    """
    n = len(close)
    bos_up = np.zeros(n, np.uint8)
    bos_dn = np.zeros(n, np.uint8)
    ch_up = np.zeros(n, np.uint8)
    ch_dn = np.zeros(n, np.uint8)
    lvl_up = np.full(n, np.nan)
    lvl_dn = np.full(n, np.nan)
    org_up = np.full(n, -1, np.int64)
    org_dn = np.full(n, -1, np.int64)
    structure = np.zeros(n, np.int8)
    sh = np.nan
    sh_o = -1
    sl = np.nan
    sl_o = -1
    armed_h = False
    armed_l = False
    state = 0
    for i in range(n):
        c = close[i]
        if armed_h and c > sh:
            bos_up[i] = 1
            lvl_up[i] = sh
            org_up[i] = sh_o
            if state == -1:
                ch_up[i] = 1
            state = 1
            armed_h = False
        if armed_l and c < sl:
            bos_dn[i] = 1
            lvl_dn[i] = sl
            org_dn[i] = sl_o
            if state == 1:
                ch_dn[i] = 1
            state = -1
            armed_l = False
        # swings confirmed at this bar become known at its close
        if np.isfinite(ph[i]):
            sh = ph[i]
            sh_o = ph_o[i]
            armed_h = True
        if np.isfinite(pl[i]):
            sl = pl[i]
            sl_o = pl_o[i]
            armed_l = True
        structure[i] = state
    return bos_up, bos_dn, ch_up, ch_dn, lvl_up, lvl_dn, org_up, org_dn, structure
