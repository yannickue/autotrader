# ruff: noqa: E501
"""Synthetic frames shared by the FormulaAlpha tests (plus a few sanity checks of the helpers)."""

from __future__ import annotations

import numpy as np

from alpha.common.protocol import Partition, SplitPlan
from alpha.fast.sim import MarketArrays
from alpha.formula.data import FormulaData, build_formula_data

BARS_PER_DAY = 96


def run_starts(lengths) -> np.ndarray:
    out = []
    s = 0
    for L in lengths:
        out.extend([s] * L)
        s += L
    return np.asarray(out, dtype=np.int64)


def random_ohlc(n: int, seed: int, lengths=None):
    rng = np.random.default_rng(seed)
    if lengths is None:
        lengths = []
        tot = 0
        while tot < n:
            L = int(rng.choice([130, 40, 200, 5, 125, 60]))
            lengths.append(min(L, n - tot))
            tot += lengths[-1]
    rs = run_starts(lengths)[:n]
    c = 100 + np.cumsum(rng.normal(0, 1.0, n))
    o = np.r_[c[0], c[:-1]] + rng.normal(0, 0.2, n)
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.5, n))
    lo = np.minimum(o, c) - np.abs(rng.normal(0, 0.5, n))
    return o, h, lo, c, rs


def day_structure(n_days: int, bars_per_day: int = BARS_PER_DAY):
    """run_start / berlin_minute for ``n_days`` days of ``bars_per_day`` M5 bars starting 08:00."""
    n = n_days * bars_per_day
    idx = np.arange(n)
    rs = (idx // bars_per_day * bars_per_day).astype(np.int64)
    minute = (8 * 60 + (idx % bars_per_day) * 5).astype(np.int64)
    day = (idx // bars_per_day).astype(np.int64)
    return rs, minute, day


def planted_data(n_days: int = 40, seed: int = 0, beta: float = 0.35, noise_feats: int = 3, step: float = 8.0):
    """Random-walk M5 bars whose NEXT return depends on a trailing z-score of the close.

    r[t+1] = -beta * z[t] * s + eps, z[t] = Zscore(c, 10)[t] (computed on the path as it is built), so the
    planted formula ``Zscore(c,10)`` (mean reversion, IC < 0) predicts the forward return; any function of
    bars <= t only.  Returns (FormulaData, MarketArrays, dates).
    """
    rng = np.random.default_rng(seed)
    n = n_days * BARS_PER_DAY
    rs, minute, day = day_structure(n_days)
    s = step
    c = np.empty(n)
    c[0] = 10000.0
    for t in range(n):
        if t == rs[t]:
            c[t] = c[t - 1] + rng.normal(0, s) if t > 0 else 10000.0
            continue
        # z of the previous close (index t-1) over the last <=10 closes ending at t-1
        w = c[max(rs[t], t - 10):t]
        z = 0.0
        if len(w) == 10 and w.std() > 1e-9:
            z = (w[-1] - w.mean()) / w.std()
        c[t] = c[t - 1] - beta * s * z + rng.normal(0, s)
    o = np.r_[c[0], c[:-1]]
    o = np.where(rs == np.arange(n), c, o)
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.375 * s, n))
    lo_ = np.minimum(o, c) - np.abs(rng.normal(0, 0.375 * s, n))
    from alpha.formula import ops

    atr14 = ops.atr(h, lo_, c, rs, 14)
    atr14 = np.where(np.isfinite(atr14), atr14, np.nan)
    extra = {f"noise{k}": rng.normal(size=n) for k in range(noise_feats)}
    fd = build_formula_data(o, h, lo_, c, atr14, rs, minute, None, extra=extra)
    contig = np.ones(n, dtype=bool)
    contig[:-1] = rs[1:] == rs[:-1]
    contig[-1] = False
    market = MarketArrays(o, h, lo_, c, np.full(n, 1.0), minute, day, contig)
    dates = (np.datetime64("2025-03-03") + day.astype("timedelta64[D]")).astype("datetime64[D]")
    return fd, market, dates


def split_plan_for(dates: np.ndarray, train_frac: float = 0.6) -> SplitPlan:
    days = np.unique(dates)
    k = len(days)
    a, b = int(k * train_frac), int(k * 0.8)
    return SplitPlan(
        Partition("train", str(days[0]), str(days[a - 1])),
        Partition("validation", str(days[a]), str(days[b - 1])),
        Partition("oos", str(days[b]), str(days[-1])),
        embargo_days=1,
    )


def test_planted_helpers_shapes():
    fd, market, dates = planted_data(6, 1)
    assert isinstance(fd, FormulaData) and len(fd) == len(market.o) == len(dates) == 6 * BARS_PER_DAY
    assert "noise0" in fd.arrays and "b_ret1" in fd.arrays
