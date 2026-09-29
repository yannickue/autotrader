"""GER40_BREAKOUT_V1 - two non-naive breakout rules (research rules).

ORB  (opening range breakout, Berlin time):
  OR = high/low of the first `or_bars` M5 bars from 09:00. After the OR is complete, the FIRST
  bar per day and direction that CLOSES beyond the OR by `buffer_atr` * ATR14 is a signal (bar
  close confirmation, at most one long and one short attempt per day, decisions until 12:00).
  stop: OR midpoint ("or_mid") or 2 * ATR14 ("atr2") from the signal close.

SQUEEZE (compression -> expansion):
  prior N=24 closed bars (excluding the signal bar) have a total range <= `squeeze` * ATR14
  (compressed), and the signal bar is the first CLOSE beyond that range (previous close was
  inside). Decisions 09:30-17:00 Berlin. stop = 2 * ATR14.

Never enters "every new high/low": both rules need compression/OR structure and a confirmed close.
"""

from __future__ import annotations

import itertools

import numpy as np

from alpha.common.frame import Frame
from alpha.common.sim import Signals

NAME = "GER40_BREAKOUT_V1"
SQUEEZE_N = 24
ORB_LAST_DECISION_MIN = 12 * 60
SQ_FIRST_MIN, SQ_LAST_MIN = 9 * 60 + 30, 17 * 60

GRID: list[dict] = [
    {"kind": "orb", "or_bars": ob, "buffer_atr": bf, "stop_mode": sm}
    for ob, bf, sm in itertools.product((3, 6), (0.0, 0.25), ("or_mid", "atr2"))
] + [{"kind": "squeeze", "squeeze": sq} for sq in (3.5, 5.0)]


def _orb(fr: Frame, p: dict) -> Signals:
    n = len(fr)
    atr = fr.atr(14)
    side = np.zeros(n, dtype=np.int8)
    stop = np.full(n, np.nan)
    or_bars = int(p["or_bars"])
    buf = float(p["buffer_atr"])
    start = 9 * 60
    days = fr.day
    bounds = np.flatnonzero(np.diff(days, prepend=-1) != 0)
    ends = np.append(bounds[1:], n)
    for s, e in zip(bounds, ends, strict=True):
        idx = np.arange(s, e)
        m = fr.minute[idx]
        or_idx = idx[(m >= start) & (m < start + 5 * or_bars)]
        if len(or_idx) != or_bars or (fr.minute[or_idx[-1]] != start + 5 * (or_bars - 1)):
            continue  # incomplete opening range (missing bars) -> no trade that day
        if not fr.contig_next[or_idx[:-1]].all():
            continue
        hi, lo = fr.h[or_idx].max(), fr.l[or_idx].min()
        mid = 0.5 * (hi + lo)
        long_done = short_done = False
        for i in idx[idx > or_idx[-1]]:
            if fr.minute[i] >= ORB_LAST_DECISION_MIN:
                break
            a = atr[i]
            if not np.isfinite(a):
                continue
            if not long_done and fr.c[i] > hi + buf * a:
                long_done = True
                side[i] = 1
                stop[i] = mid if p["stop_mode"] == "or_mid" else fr.c[i] - 2.0 * a
            elif not short_done and fr.c[i] < lo - buf * a:
                short_done = True
                side[i] = -1
                stop[i] = mid if p["stop_mode"] == "or_mid" else fr.c[i] + 2.0 * a
            if long_done and short_done:
                break
    return Signals(side=side, stop=stop)


def _squeeze(fr: Frame, p: dict) -> Signals:
    atr = fr.atr(14)
    hh = fr.rolling_max_prev(fr.h, SQUEEZE_N)
    ll = fr.rolling_min_prev(fr.l, SQUEEZE_N)
    prev_c = np.concatenate([[np.nan], fr.c[:-1]])
    in_window = (fr.minute >= SQ_FIRST_MIN) & (fr.minute < SQ_LAST_MIN)
    with np.errstate(invalid="ignore"):
        compressed = (hh - ll) <= p["squeeze"] * atr
        up = compressed & (fr.c > hh) & (prev_c <= hh) & in_window
        dn = compressed & (fr.c < ll) & (prev_c >= ll) & in_window
    side = np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)
    stop = np.where(up, fr.c - 2.0 * atr, np.where(dn, fr.c + 2.0 * atr, np.nan))
    return Signals(side=side, stop=stop)


def signals(fr: Frame, p: dict) -> Signals:
    return _orb(fr, p) if p["kind"] == "orb" else _squeeze(fr, p)
