# ruff: noqa: E501
"""FAMILY 1 - ORB: opening-range breakout and failed-breakout fade (both sides, one attempt per day).

HYPOTHESIS.  The first ``range_min`` minutes after the CASH open discover the day's initial balance.  A close
beyond that range (+- ``buffer_atr`` * ATR) signals initiative flow that continues (mode ``breakout``); a
breach that is rejected within ``fail_bars`` bars (a bar closes back inside the range) signals a trapped-trader
squeeze in the OPPOSITE direction (mode ``fade``).  Both modes are symmetric in price (LONG/SHORT mirror).

FAILURE MODES.  (i) On trend-less days the breakout is mean-reverting and stops out; (ii) the opposite range
side is far away on wide-range days (risk-band skips, poor payoff); (iii) fade entries into a real trend day
lose the full stop; (iv) round-trip spread vs. a small range is a large fraction of 1R on 5-minute ranges.

CAUSALITY.  The range uses only bars whose OPEN lies in ``[cash_open, cash_open + range_min)`` and is used from
the first bar AFTER that window; the range must be complete (all ``range_min/5`` bars present).  The decision at
bar ``i`` reads ``c[i]``, ``atr[i]``, the frozen range, and (fade) extremes of bars ``<= i``.

ENTRY.  Signal at bar close, fill at the next open (simulator).  STOP: ``breakout`` - the broken side minus
``stop_frac * width`` towards the opposite side (1.0 = the opposite range side); ``fade`` - the breach extreme
plus ``PAD_ATR * ATR``.  TARGET: R multiple (V1 path, NaN target) or ``target`` * range width (finite price).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np
from numba import njit

from alpha.families.common import Thr, empty_candidates, entry_mask, finalize
from alpha.families.data import FamilyData
from alpha.families.spec import FamilySpec, MarketCalendar, check, thin_grid
from alpha.fast.sim import CandidateArrays

PAD_ATR = 0.25
FAIL_BARS = 6


@dataclass(frozen=True)
class ORBSpec(FamilySpec):
    FAMILY = "ORB"
    mode: str = "breakout"  # breakout | fade
    range_min: int = 15
    buffer_atr: float = 0.0
    stop_frac: float = 1.0  # breakout only
    target_kind: str = "r"  # r | range
    target: float = 1.5
    window_min: int = 240  # decisions only while bar open < cash_open + window_min
    side: str = "both"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        check(self.mode in ("breakout", "fade"), "mode")
        check(self.range_min in (5, 10, 15, 30, 45, 60, 90), "range_min must be a multiple-of-5 window <= 90")
        check(self.buffer_atr >= 0.0, "buffer_atr >= 0")
        check(0.0 < self.stop_frac <= 2.0 or (self.mode == "fade" and self.stop_frac == 0.0), "stop_frac in (0, 2]")
        check(self.target_kind in ("r", "range") and self.target > 0.0, "target")
        check(self.window_min > self.range_min, "window_min must exceed range_min")
        check(self.side in ("both", "long", "short"), "side")

    def canonical(self) -> ORBSpec:
        if self.mode == "fade":
            return ORBSpec(self.mode, self.range_min, self.buffer_atr, 0.0, self.target_kind, self.target, self.window_min, self.side)
        return self

    @property
    def complexity(self) -> int:
        return 3 + int(self.buffer_atr > 0) + int(self.target_kind == "range") + int(self.mode == "fade")


def grid(max_n: int | None = 400) -> list[FamilySpec]:
    out: list[FamilySpec] = []
    targets = [("r", 1.0), ("r", 2.0), ("range", 0.5), ("range", 1.0)]
    for mode, rng, buf, sf, (tk, tv), win in product(
        ("breakout", "fade"), (5, 15, 30, 60), (0.0, 0.1, 0.25), (0.5, 1.0), targets, (90, 240)
    ):
        out.append(ORBSpec(mode, rng, buf, sf, tk, tv, win))
    return thin_grid(out, max_n)


def fit(train: FamilyData, spec: ORBSpec) -> Thr:
    return Thr(())


@njit(cache=True)
def _kernel(h, low, c, atr, minute, day, ok, co, rng, buf, fade, fail_bars, dec_end, stop_frac, tkind, tval, pad):
    n = len(c)
    out_i = np.empty(n, np.int64)
    out_d = np.empty(n, np.int8)
    out_s = np.empty(n)
    out_t = np.empty(n)
    cnt = 0
    need = rng // 5
    i = 0
    while i < n:
        a = i
        d0 = day[i]
        while i < n and day[i] == d0:
            i += 1
        b = i
        rh = -np.inf
        rl = np.inf
        nb = 0
        first = -1
        for k in range(a, b):
            m = minute[k]
            if m < co:
                continue
            if m < co + rng:
                if h[k] > rh:
                    rh = h[k]
                if low[k] < rl:
                    rl = low[k]
                nb += 1
            else:
                first = k
                break
        if first < 0 or nb != need:
            continue
        width = rh - rl
        if not width > 0.0:
            continue
        b_up = -1
        b_dn = -1
        ext_hi = -np.inf
        ext_lo = np.inf
        up_dead = False
        dn_dead = False
        for k in range(first, b):
            if minute[k] >= co + dec_end:
                break
            a_k = atr[k]
            if not fade:
                if not ok[k]:
                    continue
                dr = 0
                if c[k] > rh + buf * a_k:
                    dr = 1
                elif c[k] < rl - buf * a_k:
                    dr = -1
                if dr == 0:
                    continue
                out_i[cnt] = k
                out_d[cnt] = dr
                if dr > 0:
                    out_s[cnt] = rh - stop_frac * width
                    out_t[cnt] = c[k] + tval * width if tkind == 1 else np.nan
                else:
                    out_s[cnt] = rl + stop_frac * width
                    out_t[cnt] = c[k] - tval * width if tkind == 1 else np.nan
                cnt += 1
                break
            # ---- failed-breakout fade -------------------------------------------------------
            if b_up < 0 and not up_dead and a_k > 0.0 and h[k] > rh + buf * a_k:
                b_up = k
                ext_hi = h[k]
            elif b_up >= 0:
                if h[k] > ext_hi:
                    ext_hi = h[k]
            if b_dn < 0 and not dn_dead and a_k > 0.0 and low[k] < rl - buf * a_k:
                b_dn = k
                ext_lo = low[k]
            elif b_dn >= 0:
                if low[k] < ext_lo:
                    ext_lo = low[k]
            up_fail = b_up >= 0 and k - b_up <= fail_bars and c[k] < rh
            dn_fail = b_dn >= 0 and k - b_dn <= fail_bars and c[k] > rl
            if b_up >= 0 and k - b_up >= fail_bars and not up_fail:
                up_dead = True
                b_up = -1
            if b_dn >= 0 and k - b_dn >= fail_bars and not dn_fail:
                dn_dead = True
                b_dn = -1
            if up_fail == dn_fail:  # neither, or both (ambiguous): no decision on this bar
                continue
            if not ok[k]:
                continue
            out_i[cnt] = k
            if up_fail:
                out_d[cnt] = -1
                out_s[cnt] = ext_hi + pad * a_k
                out_t[cnt] = c[k] - tval * width if tkind == 1 else np.nan
            else:
                out_d[cnt] = 1
                out_s[cnt] = ext_lo - pad * a_k
                out_t[cnt] = c[k] + tval * width if tkind == 1 else np.nan
            cnt += 1
            break
    return out_i[:cnt], out_d[:cnt], out_s[:cnt], out_t[:cnt]


def generate(data: FamilyData, spec: ORBSpec, thr: Thr) -> CandidateArrays:
    cal: MarketCalendar = data.cal
    win = spec.effective_window(cal)
    ok = entry_mask(data, win)
    idx, dr, stop, tgt = _kernel(
        data.h, data.l, data.c, data.atr, data.minute, data.day, ok, cal.cash_open_min, spec.range_min,
        spec.buffer_atr, spec.mode == "fade", FAIL_BARS, spec.window_min, spec.stop_frac, 1 if spec.target_kind == "range" else 0,
        spec.target, PAD_ATR,
    )
    if len(idx) == 0:
        return empty_candidates()
    tr = np.full(len(idx), spec.target if spec.target_kind == "r" else np.nan)
    if spec.target_kind == "range":
        tr = np.ones(len(idx))  # unused: finite targets ignore target_r
    return finalize(data, idx, dr, stop, tgt if spec.target_kind == "range" else None, tr, spec.side)


__all__ = ("FAIL_BARS", "PAD_ATR", "ORBSpec", "fit", "generate", "grid")
