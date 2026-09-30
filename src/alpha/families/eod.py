# ruff: noqa: E501
"""FAMILY 7 - EOD: last-hour continuation / reversal before the session end.

HYPOTHESIS.  Into the final ``window_min`` minutes before the session end ``T = min(cash_close, entry_end)`` (the
last enterable point of the market's entry window) the day's direction either continues (index trend days,
closing-imbalance flow: mode ``continue``) or is faded (profit-taking / mean reversion: mode ``reverse``).  The
trend measure is the move since the cash OPEN (``trend_ref='open'``) or over the last 2 hours (``'last2h'``),
in ATR units at the decision bar; the trigger is ``|move| >= `` the TRAIN-ONLY quantile ``q`` of the same measure
at the same clock bar.  ONE decision per day at the close of the bar opening at ``T - window_min - 5``; the
position is closed at the clock ``exit_clock``: ``'T'`` (= the session end T) or the forced flat.

FAILURE MODES.  One decision per day (few trades, best-of-grid inflation); the "last hour" of a CFD on the entry
window of these calendars is not the exchange closing auction (NAS100/SPX500 entries stop at 15:00 New York, GER40
at cash close 17:30 vs entry_end 20:00: T is min of both); the day-trend measure is autocorrelated with the gap.

CAUSALITY.  The decision bar is a fixed clock bar; the move uses ``c[i]``, the cash-session open (known since the
open) or ``c[i-24]`` inside the same run, ATR at ``i`` and a frozen Train quantile.  LONG/SHORT mirror exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np

from alpha.families.common import Thr, empty_candidates, entry_mask, finalize, train_quantile
from alpha.families.data import FamilyData
from alpha.families.spec import EffectiveWindow, FamilySpec, MarketCalendar, check, thin_grid
from alpha.fast.sim import CandidateArrays

LAST2H_BARS = 24


def session_end(cal: MarketCalendar) -> int:
    return min(cal.cash_close_min, cal.entry_end_min)


@dataclass(frozen=True)
class EODSpec(FamilySpec):
    FAMILY = "EOD"
    window_min: int = 60
    trend_ref: str = "open"  # open | last2h
    q: float = 0.75
    mode: str = "continue"  # continue | reverse
    stop_atr: float = 2.0
    target_r: float = 1.5
    exit_clock: str = "T"  # T | flat
    side: str = "both"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        check(self.window_min >= 5 and self.window_min % 5 == 0 and self.window_min <= 240, "window_min")
        check(self.trend_ref in ("open", "last2h") and self.mode in ("continue", "reverse"), "trend_ref/mode")
        check(0.0 <= self.q < 1.0 and self.stop_atr > 0 and self.target_r > 0, "q/stop/target")
        check(self.exit_clock in ("T", "flat") and self.side in ("both", "long", "short"), "exit/side")

    @property
    def complexity(self) -> int:
        return 3 + int(self.trend_ref == "last2h") + int(self.exit_clock == "T")

    def exit_min(self, cal: MarketCalendar) -> int:
        return session_end(cal) if self.exit_clock == "T" else cal.flat_min

    def effective_window(self, cal: MarketCalendar) -> EffectiveWindow:
        ex = self.exit_min(cal)
        end = min(cal.entry_end_min, ex)
        if end <= cal.entry_start_min:
            raise ValueError("exit clock is not after the entry start")
        return EffectiveWindow(cal.entry_start_min, end, ex)


def grid(max_n: int | None = 400) -> list[FamilySpec]:
    out = [EODSpec(w, t, q, m, s, r, ex) for w, t, q, m, s, r, ex in product(
        (30, 60, 90), ("open", "last2h"), (0.5, 0.75, 0.9), ("continue", "reverse"), (1.5, 2.5), (1.0, 2.0), ("T", "flat"))]
    return thin_grid(out, max_n)


def _decision_minute(data: FamilyData, spec: EODSpec) -> int:
    return session_end(data.cal) - spec.window_min - 5  # OPEN minute of the decision bar (entry at T - window)


def _move(data: FamilyData, spec: EODSpec) -> np.ndarray:
    n = len(data)
    with np.errstate(invalid="ignore", divide="ignore"):
        if spec.trend_ref == "open":
            return (data.c - data.cash["sess_open_cash"]) / data.atr
        m = np.full(n, np.nan)
        if n > LAST2H_BARS:
            m[LAST2H_BARS:] = (data.c[LAST2H_BARS:] - data.c[:-LAST2H_BARS]) / data.atr[LAST2H_BARS:]
        return np.where(data.run_start <= np.arange(n) - LAST2H_BARS, m, np.nan)


def _sel(data: FamilyData, spec: EODSpec) -> np.ndarray:
    ok = entry_mask(data, spec.effective_window(data.cal))
    return ok & (data.minute == _decision_minute(data, spec))


def fit(train: FamilyData, spec: EODSpec) -> Thr:
    return Thr((train_quantile(np.abs(_move(train, spec))[_sel(train, spec)], spec.q),))


def generate(data: FamilyData, spec: EODSpec, thr: Thr) -> CandidateArrays:
    if not thr.ok or len(data) == 0:
        return empty_candidates()
    mv = _move(data, spec)
    with np.errstate(invalid="ignore"):
        sel = _sel(data, spec) & np.isfinite(mv) & (np.abs(mv) >= thr.values[0]) & (mv != 0)
    idx = np.flatnonzero(sel)
    if len(idx) == 0:
        return empty_candidates()
    s = np.sign(mv[idx]).astype(np.int8)
    dirn = s if spec.mode == "continue" else (-s).astype(np.int8)
    stop = data.c[idx] - dirn * spec.stop_atr * data.atr[idx]
    return finalize(data, idx, dirn, stop, None, spec.target_r, spec.side)


__all__ = ("LAST2H_BARS", "EODSpec", "fit", "generate", "grid", "session_end")
