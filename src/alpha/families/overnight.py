# ruff: noqa: E501
"""FAMILY 3 - OVERNIGHT: overnight drift / range continuation versus reversal, entered at the cash open + N minutes.

HYPOTHESIS.  The drift from the previous CASH close to today's cash open (the overnight/pre-market session) and
where the open sits inside the overnight RANGE carry information about the first hour: a large drift with the
open AT the overnight extreme (``pos_filter='extreme'``) marks one-sided overnight flow that either persists
(mode ``continue``) or is faded once the cash session absorbs it (mode ``reverse``).  The drift threshold is the
TRAIN-ONLY quantile ``drift_q`` of ``|drift| / ATR``.  Entry is at the cash open + ``entry_min`` minutes (the
decision is the close of the bar that opens at ``cash_open + entry_min - 5``).

Difference to GAP: no size buckets / gap-fill target / confirmation bars; this family uses the overnight range
position and a fixed clock entry.  (The drift itself equals the cash gap by construction: the two families share the
input but ask different questions; that overlap is a stated caveat.)

FAILURE MODES.  One decision per day; the drift/gap is the single most trend-following-vs-mean-reverting driver and
flips sign across regimes; a wide overnight range with the open at its extreme is a scarce state.

CAUSALITY.  drift, overnight range (frozen at the cash open), ATR (bar before the open) and the frozen Train
quantile are all known at the cash open; the decision at ``cash_open + entry_min - 5`` reads ``atr`` of that bar.
LONG/SHORT mirror exactly (sign of the drift; ``pos`` mirrored as ``1 - pos``).
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

POS_EXTREME = 0.8


@dataclass(frozen=True)
class OVERNIGHTSpec(FamilySpec):
    FAMILY = "OVERNIGHT"
    mode: str = "continue"  # continue | reverse
    drift_q: float = 0.75
    pos_filter: str = "none"  # none | extreme
    entry_min: int = 15
    stop_atr: float = 1.5
    target_r: float = 1.5
    side: str = "both"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        check(self.mode in ("continue", "reverse"), "mode")
        check(0.0 <= self.drift_q < 1.0, "drift_q")
        check(self.pos_filter in ("none", "extreme"), "pos_filter")
        check(self.entry_min >= 5 and self.entry_min % 5 == 0 and self.entry_min <= 120, "entry_min")
        check(self.stop_atr > 0 and self.target_r > 0, "stop/target")
        check(self.side in ("both", "long", "short"), "side")

    @property
    def complexity(self) -> int:
        return 3 + int(self.pos_filter == "extreme")


def grid(max_n: int | None = 400) -> list[FamilySpec]:
    out = [OVERNIGHTSpec(m, q, p, e, s, r) for m, q, p, e, s, r in product(
        ("continue", "reverse"), (0.5, 0.75, 0.9), ("none", "extreme"), (5, 15, 30, 60), (1.0, 1.5, 2.0), (1.0, 2.0))]
    return thin_grid(out, max_n)


def fit(train: FamilyData, spec: OVERNIGHTSpec) -> Thr:
    return Thr((train_quantile(np.abs(open_table(train)["gap_atr"]), spec.drift_q),))


def generate(data: FamilyData, spec: OVERNIGHTSpec, thr: Thr) -> CandidateArrays:
    if not thr.ok or len(data) == 0:
        return empty_candidates()
    cal = data.cal
    t = open_table(data)
    a = np.abs(t["gap_atr"])
    with np.errstate(invalid="ignore", divide="ignore"):
        rng = t["ovn_h"] - t["ovn_l"]
        pos = (data.o[t["kf"]] - t["ovn_l"]) / rng
        sel = np.isfinite(a) & (a >= thr.values[0]) & (np.abs(t["gap"]) > 0)
        if spec.pos_filter == "extreme":
            up = t["gap"] > 0
            sel &= np.isfinite(pos) & (rng > 0) & np.where(up, pos >= POS_EXTREME, pos <= 1.0 - POS_EXTREME)
    sel_idx = np.flatnonzero(sel)
    kf = t["kf"][sel_idx]
    i = kf + (spec.entry_min // 5 - 1)
    inb = i < len(data)
    kf, i, sel_idx = kf[inb], i[inb], sel_idx[inb]
    ok = entry_mask(data, spec.effective_window(cal))
    valid = (data.day[i] == data.day[kf]) & (data.minute[i] == cal.cash_open_min + spec.entry_min - 5) & (data.run_start[i] <= kf) & ok[i]
    i, sel_idx = i[valid], sel_idx[valid]
    if len(i) == 0:
        return empty_candidates()
    g = np.sign(t["gap"][sel_idx]).astype(np.int8)
    dirn = g if spec.mode == "continue" else (-g).astype(np.int8)
    stop = data.c[i] - dirn * spec.stop_atr * data.atr[i]
    return finalize(data, i, dirn, stop, None, spec.target_r, spec.side)


__all__ = ("POS_EXTREME", "OVERNIGHTSpec", "fit", "generate", "grid")
