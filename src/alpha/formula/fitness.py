# ruff: noqa: E501
"""Train-only factor fitness: pooled rank information coefficient against a FORWARD return label.

The label is the ONLY place in the formula package that looks at bars AFTER the factor bar, and it is
used for scoring only:

    y_h[i] = (c[i+h] - c[i]) / atr[i]      valid iff i+h is inside the same run (day) and atr[i] > 0

The factor at bar ``i`` (built from ``FormulaData``, which has no label field) uses bars ``<= i``.  The
scorer is constructed from a ``FormulaData`` that the caller has already truncated to the Train slice, so
no bar of any later partition exists in memory here; ``i+h`` never leaves that slice.

fitness = LCB(rank-IC) * (0.5 + 0.5 * stability) - lambda * complexity
  rank-IC   = Spearman correlation between the factor and y_h pooled over ALL Train bars (global ranks);
  LCB       = |IC| - 1 cluster-robust standard error (clusters = days/runs: the per-day sums of the
              centred rank products), floored at 0, at the best horizon h.  Slow (day-constant) factors
              have a huge cluster se and score ~0 unless the relation is real;
  stability = fraction of ``n_chunks`` contiguous groups of Train days whose IC has the same sign as
              the overall IC;
  sign / best horizon are fitted on Train and returned so the signal layer never re-derives them.
A per-day (within-day) IC was tried first and rejected: ranking a level-like factor inside one day's own
sample is biased negative (day-mean reversion) on a pure random walk.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from alpha.formula import ops
from alpha.formula.data import FormulaData

HORIZONS: tuple[int, ...] = (6, 12, 24)


def forward_labels(data: FormulaData, horizons: Sequence[int] = HORIZONS) -> dict[int, np.ndarray]:
    """ATR-scaled forward close-to-close return per horizon.  USES FUTURE BARS BY DESIGN (label only)."""
    n = len(data)
    rs = data.run_start
    out: dict[int, np.ndarray] = {}
    idx = np.arange(n)
    for h in horizons:
        y = np.full(n, np.nan)
        j = idx + h
        ok = j < n
        jj = np.where(ok, j, 0)
        ok &= rs[jj] == rs  # same run (day) => the window never crosses a session boundary
        ok &= np.isfinite(data.atr) & (data.atr > 0)
        y[ok] = (data.c[jj[ok]] - data.c[ok]) / data.atr[ok]
        out[h] = y
    return out


def rank_avg(x: np.ndarray) -> np.ndarray:
    """Average ranks (ties share the mean rank), float64, 1-based."""
    order = np.argsort(x, kind="stable")
    xs = x[order]
    ranks = np.empty(len(x))
    n = len(x)
    if n == 0:
        return ranks
    new = np.r_[True, xs[1:] != xs[:-1]]
    grp = np.cumsum(new) - 1
    starts = np.flatnonzero(new)
    ends = np.r_[starts[1:], n]
    mid = (starts + ends + 1) / 2.0  # mean of 1-based positions start+1 .. end
    ranks[order] = mid[grp]
    return ranks


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 30:
        return float("nan")
    a = a - a.mean()
    b = b - b.mean()
    den = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / den) if den > 0 else float("nan")


def pooled_ic(f: np.ndarray, y: np.ndarray, day: np.ndarray, n_days: int) -> tuple[float, float, np.ndarray, np.ndarray] | None:
    """(rank-IC, cluster-robust se, per-day product sums, mask) over bars where both are finite."""
    m = np.isfinite(f) & np.isfinite(y)
    if int(m.sum()) < 200:
        return None
    u = rank_avg(f[m])
    v = rank_avg(y[m])
    u -= u.mean()
    v -= v.mean()
    den = float(np.sqrt((u * u).sum() * (v * v).sum()))
    if den <= 0:
        return None
    prod = u * v
    s_d = np.bincount(day[m], prod, n_days)
    has = np.bincount(day[m], minlength=n_days) > 0
    if int(has.sum()) < 3:
        return None
    ic = float(prod.sum() / den)
    se = float(np.sqrt(int(has.sum())) * s_d[has].std(ddof=1) / den)
    return ic, se, s_d, has


@dataclass(frozen=True)
class FactorScore:
    valid: bool
    reason: str  # "" when valid
    fitness: float  # raw Train fitness (complexity-penalised); -1.0 when invalid
    ic: dict[int, float]  # horizon -> pooled Train rank-IC
    best_h: int
    sign: int  # +1 / -1: sign of the Train IC at best_h (the fitted direction), 0 if invalid
    stability: float
    chunk_ic: tuple[float, ...]
    finite_frac: float
    n_used: int  # number of Train days (clusters) contributing to the IC at best_h
    t_stat: float = 0.0

    def to_dict(self) -> dict:
        return {
            "valid": self.valid, "reason": self.reason, "fitness": round(self.fitness, 6),
            "ic": {str(h): round(v, 6) for h, v in self.ic.items()}, "best_h": self.best_h,
            "sign": self.sign, "stability": round(self.stability, 4), "t_stat": round(self.t_stat, 3),
            "chunk_ic": [round(v, 5) for v in self.chunk_ic], "finite_frac": round(self.finite_frac, 4),
            "n_days_used": self.n_used,
        }


def _invalid(reason: str, finite_frac: float = 0.0) -> FactorScore:
    return FactorScore(False, reason, -1.0, {}, 0, 0, 0.0, (), finite_frac, 0)


class FactorScorer:
    def __init__(
        self, data: FormulaData, horizons: Sequence[int] = HORIZONS, *, n_chunks: int = 4,
        min_finite_frac: float = 0.4, complexity_penalty: float = 0.0001, level_corr_max: float = 0.97,
        signature_stride: int = 7, min_days: int = 20,
    ) -> None:
        self.data = data
        self.horizons = tuple(horizons)
        self.labels = forward_labels(data, self.horizons)
        n = len(data)
        self.day_of = np.unique(data.run_start, return_inverse=True)[1].astype(np.int64)
        self.n_days = int(self.day_of.max()) + 1 if n else 0
        self.n_chunks = n_chunks
        self.min_finite_frac = min_finite_frac
        self.lam = complexity_penalty
        self.level_corr_max = level_corr_max
        self.min_days = min_days
        self.label_ok = np.zeros(n, dtype=bool)
        for y in self.labels.values():
            self.label_ok |= np.isfinite(y)
        self.sig_idx = np.flatnonzero(self.label_ok)[:: max(1, signature_stride)]

    def score_array(self, f: np.ndarray, cx: int, labels: dict[int, np.ndarray] | None = None) -> FactorScore:
        fin = np.isfinite(f)
        frac = float(fin.mean()) if len(f) else 0.0
        if frac < self.min_finite_frac:
            return _invalid("too_few_finite", frac)
        vals = f[fin]
        if float(vals.max() - vals.min()) < 1e-12:
            return _invalid("constant", frac)
        if len(np.unique(vals[:: max(1, len(vals) // 2000)])) < 3:
            return _invalid("near_constant", frac)
        if abs(_pearson(vals, self.data.c[fin])) > self.level_corr_max:
            return _invalid("price_level", frac)
        labs = labels if labels is not None else self.labels
        best = None
        ics: dict[int, float] = {}
        for h in self.horizons:
            r = pooled_ic(f, labs[h], self.day_of, self.n_days)
            if r is None or int(r[3].sum()) < self.min_days:
                continue
            ic, se, s_d, has = r
            ics[h] = ic
            lcb = max(abs(ic) - se, 0.0)
            if best is None or (lcb, -h) > (best[0], -best[1]):
                best = (lcb, h, ic, se, s_d, has)
        if best is None:
            return _invalid("no_ic", frac)
        lcb, best_h, ic, se, s_d, has = best
        sign = 1 if ic > 0 else -1
        days = np.flatnonzero(has)
        chunks = [float(s_d[c].sum() / max(1e-12, s_d[has].sum() / ic)) for c in np.array_split(days, self.n_chunks) if len(c)]
        agree = [c for c in chunks if c != 0 and (c > 0) == (sign > 0)]
        stability = len(agree) / max(1, len(chunks))
        fit = lcb * (0.5 + 0.5 * stability) - self.lam * cx
        t = ic / se if se > 0 else 0.0
        return FactorScore(True, "", float(fit), ics, best_h, sign, float(stability), tuple(chunks), frac, len(days), float(t))

    def signature(self, f: np.ndarray) -> np.ndarray:
        """Rank vector on a fixed subsample of Train bars (NaN -> neutral), for decorrelation tests."""
        v = f[self.sig_idx]
        fin = np.isfinite(v)
        out = np.full(len(v), 0.5)
        if fin.sum() >= 30:
            r = rank_avg(v[fin])
            out[fin] = (r - 0.5) / len(r)
        out = out - out.mean()
        nrm = float(np.sqrt((out * out).sum()))
        return (out / nrm).astype(np.float32) if nrm > 0 else out.astype(np.float32)


def shifted_labels(scorer: FactorScorer, rng: np.random.Generator, min_shift_frac: float = 0.1) -> dict[int, np.ndarray]:
    """Labels circularly shifted by a whole number of runs: same marginal + autocorrelation, factor relation gone."""
    n = len(scorer.data)
    starts = np.unique(scorer.data.run_start)
    lo, hi = int(n * min_shift_frac), n - int(n * min_shift_frac)
    cand = starts[(starts >= lo) & (starts <= hi)]
    s = int(rng.choice(cand)) if len(cand) else int(rng.integers(max(1, lo), max(2, hi)))
    return {h: np.roll(y, s) for h, y in scorer.labels.items()}


def op_family_of(name: str) -> str:
    return ops.OPS[name].family


__all__ = (
    "HORIZONS", "FactorScore", "FactorScorer", "forward_labels", "op_family_of", "pooled_ic", "rank_avg",
    "shifted_labels",
)
