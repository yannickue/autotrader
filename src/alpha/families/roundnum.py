# ruff: noqa: E501
"""FAMILY 5 - ROUND: round-number gravity / rejection and break-and-hold.

HYPOTHESIS.  Prices cluster and stall at psychological round numbers.  The level grid is derived from the
MarketSpec tick scale (``data.ROUND_TICKS``: index 50/100 points, gold 5/10 USD, EURUSD 0.005/0.01 - "minor" /
"major").  Mode ``reject``: a bar reaches within ``prox_atr`` ATR of the nearest round level above (below) its
OPEN, does not close beyond it and closes at least ``rej_atr`` ATR away from its extreme (an upper/lower-wick
rejection) -> fade (SHORT at resistance / LONG at support).  Mode ``break``: a close beyond the level nearest the
PREVIOUS close by ``prox_atr`` ATR that then HOLDS for ``hold`` more bar closes -> trade the break.

FAILURE MODES.  Round numbers are dense on low-priced instruments (EURUSD 0.005 steps are within a few ATR of any
price: "every bar touches a level" -> the effect is diluted); the level set is a grid, not a price-derived
support/resistance, so most touches are noise; the wick rule fires on ordinary bars; spread/slippage vs the ATR stop.

CAUSALITY.  The level grid is a constant of the market; a bar's levels use its own OPEN (known at its open) or the
previous close; all trigger quantities are bars ``<= i``; break-and-hold decides at ``b + hold`` using closes
``b..b+hold`` only.  LONG/SHORT mirror when the price mirror ``p -> K - p`` maps the level grid to itself
(``K`` a multiple of the step); the tests use such a ``K``.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np
import pandas as pd

from alpha.families.common import Thr, cooldown, empty_candidates, entry_mask, finalize
from alpha.families.data import FamilyData
from alpha.families.spec import FamilySpec, check, thin_grid
from alpha.fast.sim import CandidateArrays

COOLDOWN = 6


@dataclass(frozen=True)
class ROUNDSpec(FamilySpec):
    FAMILY = "ROUND"
    mode: str = "reject"  # reject | break
    level: str = "major"  # minor | major
    prox_atr: float = 0.3
    rej_atr: float = 0.3  # reject
    hold: int = 0  # break
    stop_atr: float = 1.5
    target_r: float = 1.5
    tod: str = "all"
    side: str = "both"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        check(self.mode in ("reject", "break") and self.level in ("minor", "major"), "mode/level")
        check(self.prox_atr >= 0.0 and self.stop_atr > 0 and self.target_r > 0, "params")
        check(self.tod in ("all", "am", "pm") and self.side in ("both", "long", "short"), "tod/side")
        if self.mode == "reject":
            check(self.rej_atr > 0, "rej_atr > 0")
        else:
            check(1 <= self.hold <= 12, "hold 1..12")

    def canonical(self) -> ROUNDSpec:
        if self.mode == "reject":
            return ROUNDSpec(self.mode, self.level, self.prox_atr, self.rej_atr, 0, self.stop_atr, self.target_r, self.tod, self.side)
        return ROUNDSpec(self.mode, self.level, self.prox_atr, 0.0, self.hold, self.stop_atr, self.target_r, self.tod, self.side)

    @property
    def complexity(self) -> int:
        return 4 + int(self.tod != "all")


def grid(max_n: int | None = 400) -> list[FamilySpec]:
    out: list[FamilySpec] = []
    for lev, p, s, r, tod in product(("minor", "major"), (0.1, 0.3), (1.0, 1.5), (1.0, 2.0), ("all", "am", "pm")):
        for rej in (0.3, 0.6):
            out.append(ROUNDSpec("reject", lev, p, rej, 0, s, r, tod))
        for hold in (1, 3):
            out.append(ROUNDSpec("break", lev, p, 0.0, hold, s, r, tod))
    return thin_grid(out, max_n)


def fit(train: FamilyData, spec: ROUNDSpec) -> Thr:
    return Thr(())


def _shift(a: np.ndarray, k: int, fill) -> np.ndarray:
    out = np.full(len(a), fill, dtype=a.dtype)
    if k < len(a):
        out[k:] = a[: len(a) - k]
    return out


def generate(data: FamilyData, spec: ROUNDSpec, thr: Thr) -> CandidateArrays:
    n = len(data)
    if n == 0:
        return empty_candidates()
    step = data.round_steps[0 if spec.level == "minor" else 1]
    ok = entry_mask(data, spec.effective_window(data.cal), spec.tod)
    atr, o, h, low, c = data.atr, data.o, data.h, data.l, data.c
    with np.errstate(invalid="ignore", divide="ignore"):
        if spec.mode == "reject":
            fl = np.floor(o / step)
            l_up, l_dn = (fl + 1.0) * step, fl * step
            short = (h >= l_up - spec.prox_atr * atr) & (c < l_up) & ((h - c) >= spec.rej_atr * atr)
            long_ = (low <= l_dn + spec.prox_atr * atr) & (c > l_dn) & ((c - low) >= spec.rej_atr * atr)
            both = short & long_
            long_, short = long_ & ~both & ok, short & ~both & ok
        else:
            hd = spec.hold
            cp = np.r_[np.nan, c[:-1]]
            fl = np.floor(cp / step)
            l_up, l_dn = (fl + 1.0) * step, fl * step
            same_prev = data.run_start <= np.arange(n) - 1  # previous bar in the same run
            brk_up = same_prev & (c > l_up + spec.prox_atr * atr)
            brk_dn = same_prev & (c < l_dn - spec.prox_atr * atr)
            cmin = pd.Series(c).rolling(hd + 1).min().to_numpy()
            cmax = pd.Series(c).rolling(hd + 1).max().to_numpy()
            contiguous = data.run_start <= np.arange(n) - hd - 1  # bars b-1..b+hold all in one run
            long_ = _shift(brk_up, hd, False) & (cmin > _shift(l_up, hd, np.nan)) & contiguous & ok
            short = _shift(brk_dn, hd, False) & (cmax < _shift(l_dn, hd, np.nan)) & contiguous & ok
            both = long_ & short
            long_, short = long_ & ~both, short & ~both
    idx = cooldown(np.flatnonzero(long_ | short), COOLDOWN)
    if len(idx) == 0:
        return empty_candidates()
    dirn = np.where(long_[idx], 1, -1).astype(np.int8)
    stop = c[idx] - dirn * spec.stop_atr * atr[idx]
    return finalize(data, idx, dirn, stop, None, spec.target_r, spec.side)


__all__ = ("COOLDOWN", "ROUNDSpec", "fit", "generate", "grid")
