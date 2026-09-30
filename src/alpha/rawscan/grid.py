# ruff: noqa: E501
"""Day x 5-minute-slot grid of a market's M5 bars in the market's LOCAL calendar timezone.

Rows are local calendar dates (Mon-Fri only), columns are 288 local 5-minute slots (minute-of-day of
the bar OPEN // 5).  Missing bars are NaN.  ``ATR[d, s]`` is the causal ATR(14) known when bar (d, s)
OPENS, i.e. computed from bars strictly before it (so it is a legitimate feature of a trade entered
at that bar's open).  ``SP`` is the recorded spread in PRICE units (spread_pts * point_size).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from alpha.common.market_data import assert_no_forward_holdout

SLOT_MIN = 5
N_SLOTS = 24 * 60 // SLOT_MIN


class GridError(ValueError):
    """The frame cannot be arranged on the day x slot grid."""


@dataclass(frozen=True)
class DayGrid:
    dates: np.ndarray  # datetime64[D], one per row (local date)
    dow: np.ndarray  # int8 0=Mon..4=Fri
    O: np.ndarray  # noqa: E741
    H: np.ndarray
    L: np.ndarray
    C: np.ndarray
    SP: np.ndarray
    ATR: np.ndarray
    tz: str

    @property
    def n_days(self) -> int:
        return len(self.dates)


def causal_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:  # noqa: E741
    """``out[i]`` = Wilder ATR(n) using bars <= i-1 only (NaN until n bars exist)."""
    h, l, c = (np.asarray(x, dtype=float) for x in (h, l, c))  # noqa: E741
    pc = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr = pd.Series(tr).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()
    out = np.full(len(atr), np.nan)
    out[1:] = atr[:-1]
    return out


def build_day_grid(
    frame: pd.DataFrame, tz: str, point_size: float, *, atr_n: int = 14
) -> DayGrid:
    assert_no_forward_holdout(frame)
    if frame.empty:
        raise GridError("empty frame")
    ts = pd.DatetimeIndex(frame["ts"])
    local = ts.tz_convert(tz)
    minute = np.asarray(local.hour * 60 + local.minute)
    if (minute % SLOT_MIN).any():
        raise GridError("bars are not on the 5-minute grid in the local timezone")
    slot = minute // SLOT_MIN
    ldate = local.normalize().tz_localize(None).to_numpy().astype("datetime64[D]")
    wk = (ldate.astype("int64") + 3) % 7  # 1970-01-01 was a Thursday -> Mon=0
    keep = wk < 5
    days = np.unique(ldate[keep])
    row = np.searchsorted(days, ldate)
    o, h, lo, c = (frame[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    sp = frame["spread_pts"].to_numpy(float) * point_size
    atr = causal_atr(h, lo, c, atr_n)
    shape = (len(days), N_SLOTS)
    arrs = {k: np.full(shape, np.nan) for k in ("O", "H", "L", "C", "SP", "ATR")}
    r, s = row[keep], slot[keep]
    for name, src in (("O", o), ("H", h), ("L", lo), ("C", c), ("SP", sp), ("ATR", atr)):
        arrs[name][r, s] = src[keep]
    dow = ((days.astype("int64") + 3) % 7).astype(np.int8)
    return DayGrid(dates=days, dow=dow, tz=tz, **arrs)


def slot_of(minute_of_day: int) -> int:
    if minute_of_day % SLOT_MIN:
        raise GridError(f"minute {minute_of_day} not on the {SLOT_MIN}-minute grid")
    return minute_of_day // SLOT_MIN
