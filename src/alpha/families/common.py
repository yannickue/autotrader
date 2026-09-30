# ruff: noqa: E501
"""Shared generator helpers: entry-eligibility mask, Train-fitted thresholds, candidate finalisation.

Every family produces sparse decisions at a bar CLOSE (``decision_idx``); the simulator fills at the next
open.  ``entry_mask`` encodes the calendar facts a decision needs (next bar exists, is contiguous, lies in the
same local day and inside the spec's effective entry window / time-of-day sub-window, ATR is finite): none of
them depends on price information beyond bar ``i``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from alpha.families.data import FamilyData
from alpha.families.spec import EffectiveWindow, FamilySpec, tod_bounds
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays

MIN_FIT_N = 30  # minimum finite values to fit a Train quantile (else the threshold is NaN -> no candidates)


@dataclass(frozen=True)
class Thr:
    """Frozen Train-fitted numbers of one spec (a quantile of a Train-only distribution).  NaN = unfitted."""

    values: tuple[float, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(np.isnan(v) for v in self.values)


def train_quantile(values: np.ndarray, q: float) -> float:
    a = np.asarray(values, dtype=float)
    a = a[np.isfinite(a)]
    return float(np.quantile(a, q)) if len(a) >= MIN_FIT_N else float("nan")


def empty_candidates() -> CandidateArrays:
    z = np.zeros(0)
    return CandidateArrays(z.astype(np.int64), z.astype(np.int8), z, z, z, z.astype(np.int8))


def entry_mask(data: FamilyData, win: EffectiveWindow, tod: str = "all") -> np.ndarray:
    """bool[n]: a decision at the close of bar i may enter at bar i+1 (calendar + finite ATR only)."""

    def build() -> np.ndarray:
        lo, hi = tod_bounds(win.entry_start_min, win.entry_end_min, tod)
        n = len(data)
        ok = np.zeros(n, dtype=bool)
        if n > 1:
            m1 = data.minute[1:]
            ok[:-1] = data.contig_next[:-1] & (data.day[1:] == data.day[:-1]) & (m1 >= lo) & (m1 < hi)
        return ok & np.isfinite(data.atr) & (data.atr > 0)

    return data.memo(("entry_mask", win, tod), build)


def cooldown(idx: np.ndarray, k: int) -> np.ndarray:
    """Keep decision bars at least ``k`` bars apart (greedy, chronological)."""
    if k <= 1 or len(idx) == 0:
        return idx
    keep = []
    last = -(10**12)
    for i in idx.tolist():
        if i - last >= k:
            keep.append(i)
            last = i
    return np.asarray(keep, dtype=np.int64)


def rising(cond: np.ndarray, data: FamilyData) -> np.ndarray:
    """cond is true at i and was false (or unavailable) at i-1 inside the same contiguous run."""
    prev = np.r_[False, cond[:-1]]
    prev &= data.run_start <= np.arange(len(cond)) - 1
    return cond & ~prev


def finalize(
    data: FamilyData, idx: np.ndarray, direction: np.ndarray, stop: np.ndarray, target: np.ndarray | None,
    target_r: float | np.ndarray, side: str = "both",
) -> CandidateArrays:
    """Validated ``CandidateArrays`` (fixed-R exit).  ``target`` = finite structural target prices or NaN (R path).

    Drops candidates whose stop is not strictly on the loss side of the decision close or whose finite target is
    not strictly beyond it (the simulator re-checks both at the actual fill), and applies the side filter."""
    idx = np.asarray(idx, dtype=np.int64)
    if len(idx) == 0:
        return empty_candidates()
    direction = np.asarray(direction, dtype=np.int8)
    stop = np.asarray(stop, dtype=float)
    tgt = np.full(len(idx), np.nan) if target is None else np.asarray(target, dtype=float)
    tr = np.broadcast_to(np.asarray(target_r, dtype=float), (len(idx),)).astype(float)
    cl = data.c[idx]
    d = direction.astype(float)
    keep = np.isfinite(stop) & (d * (cl - stop) > 0)
    keep &= ~np.isfinite(tgt) | (d * (tgt - cl) > 0)
    keep &= np.isfinite(tgt) | (np.isfinite(tr) & (tr > 0))
    if side == "long":
        keep &= direction > 0
    elif side == "short":
        keep &= direction < 0
    order = np.argsort(idx[keep], kind="stable")
    sel = np.flatnonzero(keep)[order]
    idx, direction, stop, tgt, tr = idx[sel], direction[sel], stop[sel], tgt[sel], tr[sel]
    if len(idx) > 1:  # strictly increasing decision bars (defensive; kernels emit at most one per bar)
        first = np.r_[True, idx[1:] != idx[:-1]]
        idx, direction, stop, tgt, tr = idx[first], direction[first], stop[first], tgt[first], tr[first]
    return CandidateArrays(idx, direction, stop, tgt, tr, np.full(len(idx), EXIT_FIXED_R, dtype=np.int8))


def target_arrays(kind: str, value: float, n: int) -> tuple[np.ndarray | None, np.ndarray]:
    """('r', R) -> (None, R array); price targets are built by the family itself."""
    if kind == "r":
        return None, np.full(n, float(value))
    raise ValueError(kind)


def open_table(data: FamilyData) -> dict[str, np.ndarray]:
    """One row per local day whose first CASH bar is exactly at the cash open (causal per row).

    ``kf`` first cash bar; ``gap`` = open[kf] - previous cash close; ``atr_pre`` = ATR at the bar BEFORE the cash open
    (NaN unless that bar is the immediate predecessor in the same local day); ``gap_atr`` = gap / atr_pre;
    ``ovn_h/ovn_l`` complete overnight range (frozen from the cash open on).  Every value is known at the close of
    bar ``kf`` (gap needs only o[kf]); decisions using it are made at bars ``>= kf``."""

    def build() -> dict[str, np.ndarray]:
        cal = data.cal
        n = len(data)
        m = data.minute
        cash = (m >= cal.cash_open_min) & (m < cal.cash_close_min)
        prev_cash = np.r_[False, cash[:-1]] & np.r_[False, data.day[1:] == data.day[:-1]]
        first = cash & ~prev_cash & (m == cal.cash_open_min)
        kf = np.flatnonzero(first)
        pre = kf - 1
        okp = (pre >= 0) & (data.day[np.maximum(pre, 0)] == data.day[kf]) & data.contig_next[np.maximum(pre, 0)]
        atr_pre = np.where(okp, data.atr[np.maximum(pre, 0)], np.nan)
        atr_pre = np.where(atr_pre > 0, atr_pre, np.nan)
        pdc = data.cash["pdc_cash"][kf] if n else np.zeros(0)
        gap = data.o[kf] - pdc
        with np.errstate(invalid="ignore", divide="ignore"):
            gap_atr = gap / atr_pre
        return {
            "kf": kf, "gap": gap, "atr_pre": atr_pre, "gap_atr": gap_atr, "pdc": pdc,
            "ovn_h": data.cash["overnight_high"][kf], "ovn_l": data.cash["overnight_low"][kf],
        }

    return data.memo("open_table", build)


def spec_side(spec: FamilySpec) -> str:
    return getattr(spec, "side", "both")


__all__ = (
    "MIN_FIT_N",
    "Thr",
    "cooldown",
    "empty_candidates",
    "entry_mask",
    "finalize",
    "open_table",
    "rising",
    "spec_side",
    "target_arrays",
    "train_quantile",
)
