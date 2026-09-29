"""GER40_MOMENTUM_V1 - volatility-adjusted directional impulse continuation (research rules).

Idea: when the last L closed bars moved unusually far in one direction relative to normal
volatility, the move tends to continue for a while (short-horizon intraday momentum).

  z_i = (close_i - close_{i-L}) / (ATR14_i * sqrt(L))        (same Berlin day only)
  LONG  when z crosses UP through +thr on bar i and bar i closed up   (fresh impulse only)
  SHORT when z crosses DOWN through -thr on bar i and bar i closed down
  stop = close_i -/+ stop_mult * ATR14_i

Parameters: lookback L, threshold thr (z units), stop_mult.  No EMA cross, no other filters.
"""

from __future__ import annotations

import itertools

import numpy as np

from alpha.common.frame import Frame
from alpha.common.sim import Signals

NAME = "GER40_MOMENTUM_V1"
GRID: list[dict] = [
    {"lookback": lb, "thr": th, "stop_mult": sm}
    for lb, th, sm in itertools.product((12, 24, 48), (1.0, 1.5, 2.0), (1.5, 2.5))
]


def signals(fr: Frame, p: dict) -> Signals:
    atr = fr.atr(14)
    lb = int(p["lookback"])
    thr = float(p["thr"])
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (fr.c - fr.lagged(fr.c, lb)) / (atr * np.sqrt(lb))
        zprev = np.concatenate([[np.nan], z[:-1]])
        up = (z >= thr) & (zprev < thr) & (fr.c > fr.o)
        dn = (z <= -thr) & (zprev > -thr) & (fr.c < fr.o)
    side = np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)
    stop = np.where(
        up, fr.c - p["stop_mult"] * atr, np.where(dn, fr.c + p["stop_mult"] * atr, np.nan)
    )
    return Signals(side=side, stop=stop, tag={"z": z})
