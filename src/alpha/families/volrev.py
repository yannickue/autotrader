# ruff: noqa: E501
"""FAMILY 4 - VOLREV: volatility-regime reversion, and compression -> expansion timing.

HYPOTHESIS (mode ``fade``).  When realised volatility is HIGH (relative ATR ``atr/close`` above the TRAIN-ONLY
quantile ``pct``) an intraday move that is extended ``k`` ATR away from its anchor (the cash session open, or the
tick-volume-weighted VWAP proxy of the cash session) over-shoots and mean-reverts: fade it.  The exit is a stop /
R target / the anchor itself, plus a CLOCK time stop (``exit_clock``; the simulator has no per-trade holding
limit, see ``spec.py``).
HYPOTHESIS (mode ``expand``).  After compression (relative ATR at the PREVIOUS bar below the Train quantile
``pct``) a close beyond the prior ``n_range``-bar range starts a volatility expansion in the break direction.

FAILURE MODES.  High-volatility fades are the classic "catch the falling knife" (fat left tails, low win-rate
payoffs); the relative-ATR threshold is a frozen Train number and regimes drift (a later fold can sit entirely
above/below it -> zero or constant firing); VWAP from tick volume is a proxy; compression breakouts fire often on
noise and the round-trip spread eats small ranges.

CAUSALITY.  ``atr[i]``, ``close[i]``, the anchor at ``i`` (running per-day cash values), rolling prior-N extremes
(shifted by one bar) and frozen Train quantiles.  ``entry`` = rising edge of the trigger inside the spec's
time-of-day window, cooldown ``COOLDOWN`` bars.  LONG/SHORT mirror exactly (``e -> -e``, low <-> high).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np
import pandas as pd

from alpha.families.common import (
    Thr,
    cooldown,
    empty_candidates,
    entry_mask,
    finalize,
    rising,
    train_quantile,
)
from alpha.families.data import FamilyData
from alpha.families.spec import FamilySpec, check, thin_grid
from alpha.fast.sim import CandidateArrays

COOLDOWN = 12


@dataclass(frozen=True)
class VOLREVSpec(FamilySpec):
    FAMILY = "VOLREV"
    mode: str = "fade"  # fade | expand
    anchor: str = "open"  # open | vwap (fade)
    k: float = 2.0  # extension in ATR (fade)
    pct: float = 0.75  # Train quantile of relative ATR
    n_range: int = 24  # prior-bar range (expand)
    stop_atr: float = 1.5
    target_kind: str = "r"  # r | anchor (fade)
    target: float = 1.5
    exit_clock: str = "flat"  # flat | close
    tod: str = "all"
    side: str = "both"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        check(self.mode in ("fade", "expand"), "mode")
        check(self.anchor in ("open", "vwap", "none"), "anchor")
        check(0.0 < self.pct < 1.0, "pct")
        check(self.stop_atr > 0 and self.target > 0, "stop/target")
        check(self.target_kind in ("r", "anchor") and not (self.mode == "expand" and self.target_kind == "anchor"), "target_kind")
        check(self.exit_clock in ("flat", "close") and self.tod in ("all", "am", "pm") and self.side in ("both", "long", "short"), "exit/tod/side")
        if self.mode == "fade":
            check(self.anchor in ("open", "vwap") and self.k > 0, "fade needs an anchor and k > 0")
        else:
            check(self.n_range >= 3, "n_range >= 3")

    def canonical(self) -> VOLREVSpec:
        if self.mode == "fade":
            return VOLREVSpec(self.mode, self.anchor, self.k, self.pct, 0, self.stop_atr, self.target_kind, 1.0 if self.target_kind == "anchor" else self.target, self.exit_clock, self.tod, self.side)
        return VOLREVSpec(self.mode, "none", 0.0, self.pct, self.n_range, self.stop_atr, "r", self.target, self.exit_clock, self.tod, self.side)

    @property
    def complexity(self) -> int:
        return 4 + int(self.tod != "all") + int(self.exit_clock != "flat") + int(self.target_kind == "anchor")


def grid(max_n: int | None = 400) -> list[FamilySpec]:
    out: list[FamilySpec] = []
    for a, k, p, s, (tk, tv), ex, tod in product(("open", "vwap"), (1.5, 2.5), (0.5, 0.75, 0.9), (1.0, 2.0),
                                                 (("r", 1.0), ("r", 2.0), ("anchor", 1.0)), ("flat", "close"), ("all", "am", "pm")):
        out.append(VOLREVSpec("fade", a, k, p, 0, s, tk, tv, ex, tod))
    for p, n, s, r, ex, tod in product((0.15, 0.3), (12, 24, 48), (1.0, 1.5), (1.5, 2.5), ("flat", "close"), ("all", "am", "pm")):
        out.append(VOLREVSpec("expand", "none", 0.0, p, n, s, "r", r, ex, tod))
    return thin_grid(out, max_n)


def rel_atr(data: FamilyData) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return data.memo("rel_atr", lambda: np.where(data.c > 0, data.atr / data.c, np.nan))


def fit(train: FamilyData, spec: VOLREVSpec) -> Thr:
    """Train quantile of relative ATR over the bars where an entry decision is possible (no tod restriction)."""
    ok = entry_mask(train, spec.effective_window(train.cal))
    return Thr((train_quantile(rel_atr(train)[ok], spec.pct),))


def _anchor(data: FamilyData, which: str) -> np.ndarray:
    return data.cash["sess_open_cash"] if which == "open" else data.vwap


def generate(data: FamilyData, spec: VOLREVSpec, thr: Thr) -> CandidateArrays:
    if not thr.ok or len(data) == 0:
        return empty_candidates()
    ok = entry_mask(data, spec.effective_window(data.cal), spec.tod)
    n = len(data)
    rv = rel_atr(data)
    thr_v = thr.values[0]
    tgt = None
    if spec.mode == "fade":
        anc = _anchor(data, spec.anchor)
        with np.errstate(invalid="ignore", divide="ignore"):
            e = (data.c - anc) / data.atr
            hot = np.isfinite(rv) & (rv >= thr_v) & np.isfinite(e)
            longs = rising(hot & (e <= -spec.k) & ok, data)
            shorts = rising(hot & (e >= spec.k) & ok, data)
        sig = longs | shorts
        idx = cooldown(np.flatnonzero(sig), COOLDOWN)
        if len(idx) == 0:
            return empty_candidates()
        dirn = np.where(longs[idx], 1, -1).astype(np.int8)
        if spec.target_kind == "anchor":
            tgt = anc[idx]
    else:
        n_r = spec.n_range
        hi = pd.Series(data.h).rolling(n_r).max().shift(1).to_numpy()
        lo = pd.Series(data.l).rolling(n_r).min().shift(1).to_numpy()
        prev_rv = np.r_[np.nan, rv[:-1]]
        with np.errstate(invalid="ignore"):
            comp = np.isfinite(prev_rv) & (prev_rv <= thr_v) & (data.run_start <= np.arange(n) - n_r)
            longs = comp & (data.c > hi)
            shorts = comp & (data.c < lo)
        sig = (longs | shorts) & ok
        idx = cooldown(np.flatnonzero(sig), COOLDOWN)
        if len(idx) == 0:
            return empty_candidates()
        dirn = np.where(longs[idx], 1, -1).astype(np.int8)
    stop = data.c[idx] - dirn * spec.stop_atr * data.atr[idx]
    return finalize(data, idx, dirn, stop, tgt, spec.target if tgt is None else np.ones(len(idx)), spec.side)


__all__ = ("COOLDOWN", "VOLREVSpec", "fit", "generate", "grid", "rel_atr")
