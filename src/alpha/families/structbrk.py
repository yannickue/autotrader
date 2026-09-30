# ruff: noqa: E501
"""FAMILY 8 - STRUCT: session-agnostic range-structure breakout family (Lane F, PHASE2_DISCOVERY / NOT_ALPHA_VALIDATED).

PURPOSE.  BTCUSD trades ~24/7 and has no cash open; Brent's broker calendar is not an equity session.  The session
families (ORB/GAP/OVERNIGHT/EOD) anchor on ``cash_open``/``cash_close`` and would invent an open for those markets.
STRUCT uses NO session anchor at all: only the last ``n_range`` CONFIRMED bars of the instrument's own price
structure.  It is a Discovery family: it exists to generate causal, cost-aware opportunity flow and data; it makes
NO claim of expectancy.

STRUCTURE (shared by every variant).  At bar ``k`` the prior range is the high/low of the ``n_range`` bars BEFORE k
(shifted by one; bar k is never part of its own range).  A BREAK is a bar CLOSE beyond the range, provided

  * the range was COMPRESSED relative to the prior ATR:   MIN_WIDTH_SQRT <= width / (atr[k-1] * sqrt(n_range)) <= COMP_MAX_SQRT
    (random-walk scaling: the expected 1-sigma range of n bars is ~sqrt(n) ATR units, so the ratio is scale-free in n);
  * the break bar is a volatility EXPANSION:              true_range[k] >= EXPANSION_TR_ATR * atr[k-1];
  * GAP-AWARE:  the range bars AND the ATR window all lie inside ONE contiguous 5-minute segment, so a range/ATR is
    never built across a broker break (daily Brent break, weekend gap, missing bars) - ``k - seg_start(k) >=
    max(n_range, ATR_WINDOW + 1)``.  The segment start is derived only from bars <= k (causal).

VARIANTS (parameter ``mode``; the SAME break definition, different entry timing; real trading uses ONE primary)
  breakout   decision at the break bar close.
  confirmed  decision one bar later, only if that next bar also closes beyond the broken edge.
  retest     after the break, within RETEST_BARS a bar whose extreme comes back within RETEST_TOL_ATR of the broken
             edge and still closes beyond it (first such bar); a close back inside the range before that cancels.
  fade       failed break: within FAIL_BARS the first bar that closes back INSIDE the range; the trade is the
             OPPOSITE direction of the break.
STOP (structural invalidation).  Continuation variants: the OPPOSITE range edge -/+ STOP_BUFFER_ATR * ATR.  Fade:
the extreme of the failed excursion +/- STOP_BUFFER_ATR * ATR.  Exit: ``target`` R multiple (fixed-R), clock exit as
every family (``exit_clock``).  COOLDOWN bars between decisions.  LONG/SHORT mirror exactly.

DISCOVERY PLACEHOLDERS (NOT fitted, NOT tuned, chosen BEFORE any BTC/Brent history was looked at; random-walk scaling
and reuse of ORB/VOLREV constants): COMP_MAX_SQRT, MIN_WIDTH_SQRT, EXPANSION_TR_ATR, STOP_BUFFER_ATR, FAIL_BARS,
RETEST_BARS, RETEST_TOL_ATR, COOLDOWN, the default ``n_range`` and ``target``.  They are not parameters of any
optimisation; a later validated phase must replace them with researched values (``CONSTANTS_VERSION`` then changes
and so does every spec hash).

CAUSALITY.  A decision at bar ``i`` reads only bars ``<= i`` (break detection, confirmation/retest/fail scan are
walked forward from the break bar up to ``i``).  ``prefix(n)`` (truncation) provably reproduces the same decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any

import numpy as np
import pandas as pd

from alpha.families.common import Thr, cooldown, empty_candidates, entry_mask, finalize
from alpha.families.data import ATR_WINDOW, FamilyData, true_range
from alpha.families.spec import FamilySpec, check, thin_grid
from alpha.fast.sim import CandidateArrays

CONSTANTS_VERSION = "struct-discovery-placeholders-v1"
COMP_MAX_SQRT = 0.8  # range width <= 0.8 * sqrt(n) ATR  (~3.9 ATR at n=24): "compressed" vs the ~1.0*sqrt(n) random-walk expectation
MIN_WIDTH_SQRT = 0.25  # ... and >= 0.25 * sqrt(n) ATR (~1.2 ATR at n=24): a degenerate range gives a meaningless stop
EXPANSION_TR_ATR = 1.0  # break bar true range >= the prior ATR (not a below-average bar)
STOP_BUFFER_ATR = 0.25  # same value as ORB PAD_ATR
FAIL_BARS = 6  # same as ORB FAIL_BARS
RETEST_BARS = 12  # 1 hour
RETEST_TOL_ATR = 0.25
COOLDOWN = 12  # same as VOLREV COOLDOWN
MODES = ("breakout", "confirmed", "retest", "fade")
PHASE_TAG = "PHASE2_DISCOVERY"
ALPHA_STATUS = "NOT_ALPHA_VALIDATED"
SWING_LOOKBACK = 96  # bars scanned for the last confirmed swing pivots in ``structure_levels``


def constants() -> dict[str, Any]:
    return {
        "version": CONSTANTS_VERSION, "comp_max_sqrt": COMP_MAX_SQRT, "min_width_sqrt": MIN_WIDTH_SQRT,
        "expansion_tr_atr": EXPANSION_TR_ATR, "stop_buffer_atr": STOP_BUFFER_ATR, "fail_bars": FAIL_BARS,
        "retest_bars": RETEST_BARS, "retest_tol_atr": RETEST_TOL_ATR, "cooldown": COOLDOWN,
        "status": "DISCOVERY_PLACEHOLDER_NOT_FITTED",
    }


@dataclass(frozen=True)
class STRUCTSpec(FamilySpec):
    FAMILY = "STRUCT"
    mode: str = "breakout"  # breakout | confirmed | retest | fade
    n_range: int = 24
    target: float = 1.5  # fixed-R multiple
    exit_clock: str = "flat"  # flat | close
    tod: str = "all"
    side: str = "both"
    constants_version: str = CONSTANTS_VERSION  # binds the placeholder constants into the spec hash

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        check(self.mode in MODES, "mode")
        check(self.n_range >= 6, "n_range >= 6")
        check(self.target > 0, "target > 0")
        check(self.exit_clock in ("flat", "close") and self.tod in ("all", "am", "pm") and self.side in ("both", "long", "short"), "exit/tod/side")

    @property
    def complexity(self) -> int:
        return 4 + int(self.tod != "all") + int(self.exit_clock != "flat") + int(self.mode in ("retest", "fade"))


def grid(max_n: int | None = 400) -> list[FamilySpec]:
    out: list[FamilySpec] = []
    for mode, n, tgt, ex, tod in product(MODES, (6, 12, 24, 48), (1.0, 1.5, 2.0), ("flat", "close"), ("all", "am", "pm")):
        out.append(STRUCTSpec(mode, n, tgt, ex, tod))
    return thin_grid(out, max_n)


def fit(train: FamilyData, spec: STRUCTSpec) -> Thr:
    """Fit-free by construction: every number is a class-level DISCOVERY PLACEHOLDER."""
    return Thr(())


# ------------------------------------------------------------------------------------------ structure
def segment_start(data: FamilyData) -> np.ndarray:
    """Index of the first bar of the contiguous 5-minute segment each bar belongs to (day changes do NOT split a
    segment; only a missing bar / broker break does).  ``seg[i]`` depends on ``contig_next[:i]`` only."""

    def build() -> np.ndarray:
        n = len(data)
        new = np.ones(n, dtype=bool)
        if n > 1:
            new[1:] = ~data.contig_next[:-1]
        return np.maximum.accumulate(np.where(new, np.arange(n), 0)).astype(np.int64)

    return data.memo("struct_seg", build)


def _break_arrays(data: FamilyData, r: int) -> dict[str, np.ndarray]:
    def build() -> dict[str, np.ndarray]:
        n = len(data)
        idx = np.arange(n)
        seg = segment_start(data)
        hi = pd.Series(data.h).rolling(r).max().shift(1).to_numpy()
        lo = pd.Series(data.l).rolling(r).min().shift(1).to_numpy()
        atr_p = np.r_[np.nan, data.atr[:-1]]
        tr = true_range(data.h, data.l, data.c)
        enough = (idx - seg) >= max(r, ATR_WINDOW + 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            wa = (hi - lo) / (atr_p * np.sqrt(r))
            comp = enough & np.isfinite(wa) & (atr_p > 0) & (wa >= MIN_WIDTH_SQRT) & (wa <= COMP_MAX_SQRT)
            expn = tr >= EXPANSION_TR_ATR * atr_p
            up = comp & expn & (data.c > hi)
            dn = comp & expn & (data.c < lo)
        return {"hi": hi, "lo": lo, "up": up, "dn": dn, "seg": seg, "width_atr": (hi - lo) / atr_p}

    return data.memo(("struct_break", r), build)


def _events(data: FamilyData, spec: STRUCTSpec) -> dict[str, np.ndarray]:
    """All (unfiltered by window/cooldown) decision events of the spec: parallel arrays sorted by decision index."""

    def build() -> dict[str, np.ndarray]:
        r = spec.n_range
        n = len(data)
        b = _break_arrays(data, r)
        hi, lo, seg = b["hi"], b["lo"], b["seg"]
        c, h, low, atr = data.c, data.h, data.l, data.atr
        rows: list[tuple[int, int, float, float, float, int]] = []  # (dec, dir, stop, hi, lo, break_idx)
        for k in np.flatnonzero(b["up"] | b["dn"]).tolist():
            up = bool(b["up"][k])
            e_hi, e_lo = float(hi[k]), float(lo[k])
            edge = e_hi if up else e_lo
            if spec.mode == "breakout":
                j = k
            elif spec.mode == "confirmed":
                j = k + 1
                if j >= n or seg[j] > k or not ((c[j] > edge) if up else (c[j] < edge)):
                    continue
            elif spec.mode == "retest":
                j = -1
                for t in range(k + 1, min(k + RETEST_BARS, n - 1) + 1):
                    if seg[t] > k:
                        break
                    if (up and c[t] <= edge) or (not up and c[t] >= edge):
                        break  # closed back inside: cancelled
                    tol = RETEST_TOL_ATR * atr[t]
                    touched = (low[t] <= edge + tol) if up else (h[t] >= edge - tol)
                    if touched and np.isfinite(tol):
                        j = t
                        break
                if j < 0:
                    continue
            else:  # fade
                j = -1
                for t in range(k + 1, min(k + FAIL_BARS, n - 1) + 1):
                    if seg[t] > k:
                        break
                    if (up and c[t] < edge) or (not up and c[t] > edge):
                        j = t
                        break
                if j < 0:
                    continue
            a_j = atr[j]
            if not (np.isfinite(a_j) and a_j > 0):
                continue
            if spec.mode == "fade":
                if up:
                    dirn, stop = -1, float(np.max(h[k: j + 1])) + STOP_BUFFER_ATR * a_j
                else:
                    dirn, stop = 1, float(np.min(low[k: j + 1])) - STOP_BUFFER_ATR * a_j
            elif up:
                dirn, stop = 1, e_lo - STOP_BUFFER_ATR * a_j
            else:
                dirn, stop = -1, e_hi + STOP_BUFFER_ATR * a_j
            rows.append((j, dirn, stop, e_hi, e_lo, k))
        rows.sort(key=lambda t: (t[0], t[5]))
        arr = np.array(rows, dtype=float).reshape(-1, 6)
        return {
            "dec": arr[:, 0].astype(np.int64), "dir": arr[:, 1].astype(np.int8), "stop": arr[:, 2],
            "hi": arr[:, 3], "lo": arr[:, 4], "brk": arr[:, 5].astype(np.int64),
        }

    return data.memo(("struct_events", spec.mode, spec.n_range), build)


def generate(data: FamilyData, spec: STRUCTSpec, thr: Thr) -> CandidateArrays:
    if len(data) == 0:
        return empty_candidates()
    ev = _events(data, spec)
    if len(ev["dec"]) == 0:
        return empty_candidates()
    ok = entry_mask(data, spec.effective_window(data.cal), spec.tod)
    keep = ok[ev["dec"]]
    dec, dirn, stop = ev["dec"][keep], ev["dir"][keep], ev["stop"][keep]
    first = np.r_[True, dec[1:] != dec[:-1]] if len(dec) else dec.astype(bool)  # one candidate per decision bar
    dec, dirn, stop = dec[first], dirn[first], stop[first]
    kept = cooldown(dec, COOLDOWN)
    if len(kept) == 0:
        return empty_candidates()
    sel = np.searchsorted(dec, kept)
    return finalize(data, kept, dirn[sel], stop[sel], None, spec.target, spec.side)


# ------------------------------------------------------------------------------------------ metadata
def _swings(data: FamilyData, i: int) -> dict[str, float | None]:
    """Last two-sided (2 left / 2 right) confirmed swing pivots at or before bar ``i-2`` inside the contiguous segment."""
    seg = int(segment_start(data)[i])
    lo_i = max(seg, i - SWING_LOOKBACK)
    sh = sl = None
    for j in range(i - 2, lo_i + 1, -1):
        if sh is None and data.h[j] > max(data.h[j - 2], data.h[j - 1], data.h[j + 1], data.h[j + 2]):
            sh = float(data.h[j])
        if sl is None and data.l[j] < min(data.l[j - 2], data.l[j - 1], data.l[j + 1], data.l[j + 2]):
            sl = float(data.l[j])
        if sh is not None and sl is not None:
            break
    return {"swing_high": sh, "swing_low": sl}


def structure_levels(data: FamilyData, spec: STRUCTSpec, decision_idx: int, direction: int) -> dict[str, Any]:
    """Optional, additive metadata for the exit-plan producer: the range edges of the decision's own event and the
    last confirmed swings (all known at the decision bar close)."""
    ev = _events(data, spec)
    hit = np.flatnonzero((ev["dec"] == decision_idx) & (ev["dir"] == direction))
    if len(hit) == 0:
        return {}
    e = int(hit[0])
    atr = float(data.atr[decision_idx])
    out: dict[str, Any] = {
        "kind": "range", "mode": spec.mode, "n_range": spec.n_range,
        "range_high": float(ev["hi"][e]), "range_low": float(ev["lo"][e]),
        "range_width_atr": float((ev["hi"][e] - ev["lo"][e]) / atr) if atr > 0 else None,
        "break_bar_offset": int(decision_idx - ev["brk"][e]),
        "stop_buffer_atr": STOP_BUFFER_ATR, "constants_version": CONSTANTS_VERSION,
        "phase": PHASE_TAG, "alpha_status": ALPHA_STATUS,
    }
    out.update(_swings(data, decision_idx))
    return out


__all__ = (
    "ALPHA_STATUS", "CONSTANTS_VERSION", "COOLDOWN", "MODES", "PHASE_TAG", "STRUCTSpec", "constants", "fit", "generate",
    "grid", "segment_start", "structure_levels",
)
