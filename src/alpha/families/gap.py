# ruff: noqa: E501
"""FAMILY 2 - GAP: cash-open gap versus the previous cash close, gap-fade and gap-go.

HYPOTHESIS.  The cash open gap ``open[first cash bar] - previous cash close`` (``alpha.session`` cash arrays; the
overnight/pre-market futures move) is either partly filled during the session (mode ``fade``: trade towards the
previous close) or extended (mode ``go``: trade in the gap direction), depending on its size.  Size is bucketed in
ATR units (gap / ATR of the bar before the open) with thresholds that are TRAIN-ONLY quantiles of ``|gap_atr|``
(``fit``); a spec names the quantile bucket ``[bucket_lo_q, bucket_hi_q)``.  The entry is taken at the close of the
``confirm_bars``-th cash bar: with ``confirm`` the first bars must already move in the trade direction
(fade: back towards the previous close; go: away from it), without it the entry is unconditional.

FAILURE MODES.  Few trading days (one decision per day) -> wide Train confidence intervals and a large best-of-grid
penalty; news gaps trend (fades lose fully); gap size is ATR-normalised with a pre-open ATR that is very low on
quiet overnights; small gaps carry no information relative to the spread.

CAUSALITY.  Day gap, ATR (the bar before the cash open) and the bucket thresholds (frozen Train numbers) are known
at the cash open; the decision at cash bar ``N`` uses ``c`` of bars ``<= i`` only.  LONG/SHORT are exact mirrors
(``|gap|`` bucket, direction from the gap sign).

STOP: ``stop_atr * ATR`` from the decision close.  TARGET: R multiple (V1 path) or (fade only) the previous cash
close ``pdc`` (finite structural target; the simulator skips it when already crossed at the fill).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np

from alpha.families.common import (
    Thr,
    empty_candidates,
    entry_mask,
    finalize,
    open_table,
    train_quantile,
)
from alpha.families.data import FamilyData
from alpha.families.spec import FamilySpec, check, thin_grid
from alpha.fast.sim import CandidateArrays

BUCKETS = ((0.25, 0.5), (0.5, 0.75), (0.75, 0.9), (0.9, 1.0), (0.5, 1.0), (0.75, 1.0))


@dataclass(frozen=True)
class GAPSpec(FamilySpec):
    FAMILY = "GAP"
    mode: str = "fade"  # fade | go
    bucket_lo_q: float = 0.5
    bucket_hi_q: float = 1.0
    confirm_bars: int = 3
    confirm: bool = True
    stop_atr: float = 1.5
    target_kind: str = "r"  # r | fill (fade only)
    target: float = 1.5
    side: str = "both"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        check(self.mode in ("fade", "go"), "mode")
        check(0.0 <= self.bucket_lo_q < self.bucket_hi_q <= 1.0, "bucket quantiles")
        check(1 <= self.confirm_bars <= 12, "confirm_bars")
        check(self.stop_atr > 0.0 and self.target > 0.0, "stop/target")
        check(self.target_kind in ("r", "fill") and not (self.target_kind == "fill" and self.mode == "go"), "target_kind")
        check(self.side in ("both", "long", "short"), "side")

    def canonical(self) -> GAPSpec:
        if self.target_kind == "fill":
            return GAPSpec(self.mode, self.bucket_lo_q, self.bucket_hi_q, self.confirm_bars, self.confirm, self.stop_atr, "fill", 1.0, self.side)
        return self

    @property
    def complexity(self) -> int:
        return 3 + int(self.confirm) + int(self.target_kind == "fill")


def grid(max_n: int | None = 400) -> list[FamilySpec]:
    out: list[FamilySpec] = []
    for mode, (lo, hi), n, conf, stop in product(("fade", "go"), BUCKETS, (1, 3, 6), (True, False), (1.0, 1.5)):
        for tk, tv in (("r", 1.0), ("r", 2.0)) + ((("fill", 1.0),) if mode == "fade" else ()):
            out.append(GAPSpec(mode, lo, hi, n, conf, stop, tk, tv))
    return thin_grid(out, max_n)


def fit(train: FamilyData, spec: GAPSpec) -> Thr:
    """Bucket bounds in |gap_atr| units from the Train days only."""
    t = open_table(train)
    a = np.abs(t["gap_atr"])
    lo = train_quantile(a, spec.bucket_lo_q)
    hi = float("inf") if spec.bucket_hi_q >= 1.0 else train_quantile(a, spec.bucket_hi_q)
    return Thr((lo, hi))


def generate(data: FamilyData, spec: GAPSpec, thr: Thr) -> CandidateArrays:
    if not thr.ok or len(data) == 0:
        return empty_candidates()
    lo, hi = thr.values
    cal = data.cal
    t = open_table(data)
    a = np.abs(t["gap_atr"])
    with np.errstate(invalid="ignore"):
        sel = np.isfinite(a) & (a >= lo) & (a < hi) & (np.abs(t["gap"]) > 0)
    kf = t["kf"][sel]
    if len(kf) == 0:
        return empty_candidates()
    n = spec.confirm_bars
    i = kf + n - 1
    inb = i < len(data)
    kf, i, sel_idx = kf[inb], i[inb], np.flatnonzero(sel)[inb]
    win = spec.effective_window(cal)
    ok = entry_mask(data, win)
    valid = (data.day[i] == data.day[kf]) & (data.minute[i] == cal.cash_open_min + 5 * (n - 1)) & (data.run_start[i] <= kf) & ok[i]
    g = np.sign(t["gap"][sel_idx]).astype(np.int8)
    mv = np.sign(data.c[i] - data.o[kf]).astype(np.int8)
    dirn = g if spec.mode == "go" else (-g).astype(np.int8)
    if spec.confirm:
        valid &= mv == dirn
    i, dirn, sel_idx = i[valid], dirn[valid], sel_idx[valid]
    if len(i) == 0:
        return empty_candidates()
    stop = data.c[i] - dirn * spec.stop_atr * data.atr[i]
    if spec.target_kind == "fill":
        return finalize(data, i, dirn, stop, t["pdc"][sel_idx], np.ones(len(i)), spec.side)
    return finalize(data, i, dirn, stop, None, spec.target, spec.side)


__all__ = ("BUCKETS", "GAPSpec", "fit", "generate", "grid")
