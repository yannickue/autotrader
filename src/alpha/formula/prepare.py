# ruff: noqa: E501
"""Build the Train-only view the formula search works on (chronological split -> contiguous Train slice).

The Train partition of a chronological ``SplitPlan`` is a contiguous block of bars.  ``make_train_view``
cuts EXACTLY that block out of the development frame (``FormulaData`` and ``MarketArrays``) and hands the
search a view that physically contains no bar of any later partition, so no search-time code path (factor
evaluation, forward labels ``i+h``, threshold fitting, simulation) can read one.  ``run_start`` is re-based
to the slice.  The later partitions are reachable only through the separate sealed accessor module.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from alpha.common.protocol import SplitPlan
from alpha.fast.sim import MarketArrays
from alpha.formula.data import FormulaData


@dataclass(frozen=True)
class TrainView:
    data: FormulaData
    market: MarketArrays
    dates: np.ndarray  # Berlin date per Train bar
    n_days: int  # Berlin days with market data in Train (zero-trade days count)
    start: int  # index of the first Train bar in the development frame
    stop: int  # one past the last Train bar


def make_train_view(data: FormulaData, market: MarketArrays, dates: np.ndarray, plan: SplitPlan) -> TrainView:
    dates = np.asarray(dates).astype("datetime64[D]")
    if not (len(dates) == len(data) == len(market.o)):
        raise ValueError("data, market and dates must have one row per bar")
    tm = plan.mask(dates, plan.train)
    idx = np.flatnonzero(tm)
    if len(idx) == 0:
        raise ValueError("no Train bars")
    a, b = int(idx[0]), int(idx[-1]) + 1
    if not tm[a:b].all():
        raise ValueError("Train bars are not contiguous")
    sl = slice(a, b)
    rs = np.maximum(data.run_start[sl] - a, 0)
    d = FormulaData(
        data.o[sl], data.h[sl], data.l[sl], data.c[sl], data.atr[sl], rs, data.minute[sl],
        {k: v[sl] for k, v in data.arrays.items()},
    )
    cn = market.contig_next[sl].copy()
    cn[-1] = False  # there is no bar after the last Train bar inside this view
    m = MarketArrays(market.o[sl], market.h[sl], market.l[sl], market.c[sl], market.spread[sl],
                     market.minute[sl], market.day[sl], cn)
    return TrainView(d, m, dates[sl], len(np.unique(market.day[sl])), a, b)


__all__ = ("TrainView", "make_train_view")
