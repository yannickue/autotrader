"""Selection-aware statistics for mass search + overlap clustering + verdict rule.  Research only.

Pure stdlib/NumPy (scipy is not installed): the normal cdf/ppf come from ``statistics.NormalDist``.

IMPORTANT honesty note.  ``expected_max_null_t`` and the Bonferroni p-value are ROUGH
extreme-value / union-bound devices, not proofs: trials are strongly dependent (parameter
neighbours, shared signals), so N is neither the number of independent tests nor is the null
Gaussian.  They are conservative screens that stop a lucky maximum of thousands of trials from
being read as a discovery.  The deflated-Sharpe probability follows Bailey & Lopez de Prado
(2014) with per-trade R Sharpe, skew/kurtosis adjustment and an N-trial expected-maximum
benchmark; it ignores day-clustering (trades on one day are not independent), which makes it
mildly optimistic, while the day-clustered pooled t used for the null-bound comparison is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

import numpy as np

from alpha.discovery.evaluate import ADVERSE_COST, cluster_se

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329
VERDICT_YES = "YES"
VERDICT_NO = "NO"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"


def expected_max_null_t(n_trials: int) -> float:
    """Rough bound on the max of N standard-normal t-stats: sqrt(2 ln N) (0 for N <= 1).

    Extreme-value/Bonferroni style, NOT a proof; see module docstring.
    """
    if n_trials <= 1:
        return 0.0
    return math.sqrt(2.0 * math.log(n_trials))


def bonferroni_p(t_stat: float | None, n_effective: int) -> float | None:
    """One-sided Bonferroni-adjusted p for a t-stat (normal approximation), N = unique specs."""
    if t_stat is None:
        return None
    return float(min(1.0, max(1, n_effective) * (1.0 - _N.cdf(t_stat))))


def _expected_max_sharpe(n_trials: int, n_obs: int) -> float:
    """BLdP expected max of N null per-trade Sharpes; null SR std ~ 1/sqrt(n_obs - 1)."""
    if n_trials <= 1 or n_obs < 3:
        return 0.0
    sigma = 1.0 / math.sqrt(n_obs - 1)
    g = EULER_GAMMA
    return sigma * ((1 - g) * _N.inv_cdf(1 - 1.0 / n_trials)
                    + g * _N.inv_cdf(1 - 1.0 / (n_trials * math.e)))


def deflated_sharpe_probability(r: np.ndarray, n_trials: int) -> dict[str, float | None]:
    """Probability that the true per-trade Sharpe exceeds the N-trial null maximum (BLdP)."""
    n = len(r)
    if n < 3 or float(np.std(r, ddof=1)) == 0.0:
        return {"sharpe": None, "skew": None, "kurtosis": None, "sr0": None, "dsr": None}
    mean, sd = float(r.mean()), float(np.std(r, ddof=1))
    sr = mean / sd
    z = (r - mean) / float(np.std(r))
    skew, kurt = float((z**3).mean()), float((z**4).mean())  # kurt is NON-excess
    sr0 = _expected_max_sharpe(n_trials, n)
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return {"sharpe": sr, "skew": skew, "kurtosis": kurt, "sr0": sr0, "dsr": None}
    dsr = _N.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom))
    return {"sharpe": sr, "skew": skew, "kurtosis": kurt, "sr0": sr0, "dsr": float(dsr)}


@dataclass
class SelectionStats:
    n_trades: int
    pooled_t: float | None  # day-clustered t of pooled Train+Validation (COMBINED_ADVERSE)
    null_bound_t: float  # expected_max_null_t(N_total)
    exceeds_null: bool
    validation_t: float | None
    validation_bonferroni_p: float | None  # N_effective = unique canonical specs
    sharpe_per_trade: float | None
    skew: float | None
    kurtosis: float | None
    sr0_null_max: float | None
    deflated_sharpe_prob: float | None
    n_total_trials: int
    n_unique_specs: int


def selection_stats(pooled_r: np.ndarray, pooled_days: np.ndarray, validation_t: float | None,
                    n_total_trials: int, n_unique_specs: int) -> SelectionStats:
    r = np.asarray(pooled_r, dtype=float)
    se = cluster_se(r, pooled_days) if len(r) else None
    t = None if not se else float(r.mean() / se)
    bound = expected_max_null_t(n_total_trials)
    d = deflated_sharpe_probability(r, n_total_trials)
    return SelectionStats(
        len(r), t, bound, bool(t is not None and t > bound), validation_t,
        bonferroni_p(validation_t, n_unique_specs), d["sharpe"], d["skew"], d["kurtosis"],
        d["sr0"], d["dsr"], int(n_total_trials), int(n_unique_specs))


# --------------------------------------------------------------------------- overlap clusters
@dataclass
class OverlapCluster:
    representative: Any  # the CandidateResult with the best train fitness
    members: list[Any]

    @property
    def size(self) -> int:
        return len(self.members)


def entry_set(pooled_trades: Any) -> frozenset[int]:
    """Set of (entry bar, side) keys of a PooledTrades object."""
    tr = pooled_trades.trades
    if tr is None:
        return frozenset()
    return frozenset(int(i) * 2 + (1 if s > 0 else 0) for i, s in zip(tr.entry_idx, tr.side,
                                                                       strict=True))


def jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def cluster_entry_sets(items: list[tuple[Any, float, frozenset[int]]], threshold: float
                       ) -> list[OverlapCluster]:
    """Greedy clustering: items sorted by (-fitness, key order); join the first cluster whose
    representative has Jaccard >= threshold, else start a new one."""
    order = sorted(range(len(items)), key=lambda i: (-items[i][1], i))
    clusters: list[tuple[frozenset[int], OverlapCluster]] = []
    for i in order:
        obj, _fit, es = items[i]
        for rep_set, cluster in clusters:
            if jaccard(es, rep_set) >= threshold:
                cluster.members.append(obj)
                break
        else:
            clusters.append((es, OverlapCluster(obj, [obj])))
    return [c for _, c in clusters]


def overlap_clusters(survivors: list[Any], ev: Any, threshold: float = 0.6, sim: Any = None
                     ) -> list[OverlapCluster]:
    """Cluster survivors by Jaccard overlap of Train+Validation entry sets (COMBINED_ADVERSE)."""
    from alpha.discovery.stages import PooledSim

    sim = sim or PooledSim(ev)
    items = []
    for s in survivors:
        fit = s.train_fitness if s.train_fitness is not None else -math.inf
        items.append((s, fit, entry_set(sim.pooled(s.genome, ADVERSE_COST))))
    return cluster_entry_sets(items, threshold)


# --------------------------------------------------------------------------- verdict
VERDICT_RULE = (
    "YES only if at least one overlap-cluster representative passes ALL stages (C, D, E) AND its "
    "pooled day-clustered t exceeds the selection null bound sqrt(2 ln N_total_trials); "
    "INCONCLUSIVE if some representative passes all stages but none exceeds the null bound; "
    "otherwise NO."
)


def robust_verdict(representatives: list[Any]) -> str:
    """``representatives``: CandidateResult-like objects with ``passed_all`` and
    ``selection.exceeds_null``."""
    passing = [c for c in representatives if c.passed_all]
    if any(c.selection is not None and c.selection.exceeds_null for c in passing):
        return VERDICT_YES
    return VERDICT_INCONCLUSIVE if passing else VERDICT_NO


__all__ = (
    "VERDICT_RULE",
    "OverlapCluster",
    "SelectionStats",
    "bonferroni_p",
    "cluster_entry_sets",
    "deflated_sharpe_probability",
    "entry_set",
    "expected_max_null_t",
    "jaccard",
    "overlap_clusters",
    "robust_verdict",
    "selection_stats",
)
