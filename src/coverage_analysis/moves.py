# ruff: noqa: E501
"""Hindsight move finder: clean directional moves from a swing pivot (diagnostics only)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from alpha.families.data import FamilyData


@dataclass(frozen=True, slots=True)
class MoveParams:
    n_atr: float = 3.0  # favourable excursion, in ATR at the start bar
    m_bars: int = 24  # ... reached within M bars
    max_adverse_atr: float = 1.0  # clean: never more than this against the pivot before the excursion is reached
    swing_k: int = 6  # pivot: extreme of the k bars before and after
    cover_pre: int = 3  # a trigger at bars [start - pre, min(start + post, reach - 1)] covers the move
    cover_post: int = 12


@dataclass(frozen=True, slots=True)
class Move:
    direction: int
    start: int  # pivot bar index (first bar of the move; a decision is possible from its close)
    reach: int  # first bar whose extreme reached the excursion
    atr: float
    excursion_atr: float  # favourable excursion at `reach`, in ATR


def find_moves(data: FamilyData, p: MoveParams | None = None, lo: int = 0, hi: int | None = None) -> list[Move]:
    """Non-overlapping (per direction) pivot -> excursion moves inside bars ``[lo, hi)``.

    Up-move: bar ``t`` is a swing LOW (strictly below the ``k`` bars before, not above the ``k`` bars after)
    and within ``m`` bars the high reaches ``low[t] + n*ATR[t]`` without the low first dropping more than
    ``max_adverse*ATR`` below the pivot. Down-moves mirror. Bars ``t..reach`` must be one contiguous run."""
    p = p or MoveParams()
    n = len(data)
    hi = n if hi is None else min(hi, n)
    h, low, atr, rs = data.h, data.l, data.atr, data.run_start
    k = p.swing_k
    out: list[Move] = []
    last_reach = {1: -1, -1: -1}
    for t in range(max(lo, k), hi - 1):
        a = atr[t]
        if not (np.isfinite(a) and a > 0):
            continue
        for d in (1, -1):
            if t <= last_reach[d]:
                continue
            if d == 1:
                ref = low[t]
                if not (ref < low[t - k:t].min() and ref <= low[t + 1:t + k + 1].min()):
                    continue
            else:
                ref = h[t]
                if not (ref > h[t - k:t].max() and ref >= h[t + 1:t + k + 1].max()):
                    continue
            for r in range(t + 1, min(t + p.m_bars, hi - 1) + 1):
                if rs[r] > t:  # run broken (gap / new day)
                    break
                adverse = (ref - low[r]) if d == 1 else (h[r] - ref)
                if adverse > p.max_adverse_atr * a:
                    break
                fav = (h[r] - ref) if d == 1 else (ref - low[r])
                if fav >= p.n_atr * a:
                    out.append(Move(d, t, r, float(a), float(fav / a)))
                    last_reach[d] = r
                    break
    out.sort(key=lambda mv: (mv.start, mv.direction))
    return out
