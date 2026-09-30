# ruff: noqa: E501
"""Small statistics helpers for the raw edge scan (no scipy dependency).

Day clustering: every trade stream of the scan has AT MOST one trade per (cell, day); a "day" is
therefore one observation and the plain t-stat over daily net returns is the day-clustered t-stat.
``cluster_t`` implements the general definition (aggregate per day first) and is what the tests use
to prove the matrix engine agrees with it.
"""

from __future__ import annotations

import math

import numpy as np

_erfc = np.frompyfunc(math.erfc, 1, 1)


def cluster_t(values: np.ndarray, day_ids: np.ndarray) -> tuple[float, float, int]:
    """(mean over days, t-stat, n_days): observations are first averaged within a day."""
    v = np.asarray(values, dtype=float)
    d = np.asarray(day_ids)
    ok = np.isfinite(v)
    v, d = v[ok], d[ok]
    if len(v) == 0:
        return float("nan"), float("nan"), 0
    uniq, inv = np.unique(d, return_inverse=True)
    sums = np.bincount(inv, weights=v)
    cnt = np.bincount(inv)
    day_mean = sums / cnt
    n = len(uniq)
    m = float(day_mean.mean())
    if n < 2:
        return m, float("nan"), n
    sd = float(day_mean.std(ddof=1))
    if sd <= 0.0:
        return m, float("nan"), n
    return m, m / (sd / math.sqrt(n)), n


def t_from_moments(n: np.ndarray, s1: np.ndarray, s2: np.ndarray, min_n: int) -> tuple:
    """Vectorised (mean, t) from counts, sums and sums of squares; NaN where n < min_n or sd == 0."""
    n = np.asarray(n, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = s1 / n
        var = (s2 - n * mean * mean) / (n - 1.0)
        var = np.where(var > 1e-18, var, np.nan)
        t = mean / np.sqrt(var / n)
    bad = n < max(min_n, 2)
    return np.where(bad, np.nan, mean), np.where(bad, np.nan, t)


def t_to_z(t: np.ndarray, df: np.ndarray) -> np.ndarray:
    """Normal deviate matching a Student-t (Abramowitz-Stegun 26.7.8); accurate to ~1e-3 for df>=10."""
    t = np.asarray(t, dtype=float)
    df = np.maximum(np.asarray(df, dtype=float), 1.0)
    return t * (1.0 - 1.0 / (4.0 * df)) / np.sqrt(1.0 + t * t / (2.0 * df))


def norm_sf(z: np.ndarray) -> np.ndarray:
    z = np.asarray(z, dtype=float)
    return 0.5 * _erfc(z / math.sqrt(2.0)).astype(float)


def p_one_sided(t: np.ndarray, n: np.ndarray) -> np.ndarray:
    """P(T >= t): the 'a positive net edge exists' p-value. NaN t -> NaN."""
    return norm_sf(t_to_z(t, np.asarray(n, dtype=float) - 1.0))


def bh_qvalues(p: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values (q-values); NaN p stay NaN and are not counted in m."""
    p = np.asarray(p, dtype=float)
    q = np.full(p.shape, np.nan)
    ok = np.isfinite(p)
    m = int(ok.sum())
    if m == 0:
        return q
    pv = p[ok]
    order = np.argsort(pv, kind="mergesort")
    ranked = pv[order] * m / (np.arange(m) + 1.0)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(ranked, 1.0)
    q[ok] = out
    return q


def percentiles(a: np.ndarray, qs=(50, 95, 99)) -> dict[str, float]:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    if len(a) == 0:
        return {f"p{q}": float("nan") for q in qs}
    return {f"p{q}": float(np.percentile(a, q)) for q in qs}
