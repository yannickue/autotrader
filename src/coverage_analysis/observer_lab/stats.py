# ruff: noqa: E501
"""Statistics of the observer lab (OFFLINE; OBSERVATION_ONLY / NOT_ALPHA_VALIDATED).

* Effect size = P(outcome | event) - P(outcome | matched control).
* Uncertainty = DAY-BLOCK bootstrap: whole local days (events AND controls of a day together) are resampled with replacement (seeded,
  ``B`` configurable). Intraday observations of one day are strongly dependent; resampling single events would be anti-conservative.
  Percentile interval; the two-sided bootstrap p-value is the share of resamples on the other side of 0 (consistent with the CI).
* Single rates: Wilson interval (``coverage_analysis.control.wilson`` is reused, not re-implemented).
* Predeclared minimum evidence (``DEFAULT_MIN_EVIDENCE``): fewer events/controls/day-blocks/clusters => ``INSUFFICIENT_EVIDENCE``
  (the numbers are still reported, but no verdict and no adjusted p-value is attached).
* Multiple testing: Holm (family-wise) and Benjamini-Hochberg (FDR) over a REGISTERED hypothesis list. ``HypothesisRegistry`` counts
  every registered hypothesis (also those not evaluable) and refuses results for unregistered ones.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from coverage_analysis.control import (
    wilson as wilson_interval,  # noqa: F401  (re-exported on purpose)
)

INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
NOT_SIGNIFICANT = "NOT_SIGNIFICANT"
SIGNIFICANT_ADJUSTED = "SIGNIFICANT_ADJUSTED"  # adjusted p < alpha AND the block-bootstrap CI excludes 0. Still NOT an edge claim.
OK = "OK"
DEFAULT_ALPHA = 0.05
DEFAULT_B = 2000


@dataclass(frozen=True)
class MinEvidence:
    """Predeclared before looking at results (n per cell == ``demo.entry_exit_quality.SMALL_N``, clusters == ``MIN_CLUSTERS``)."""

    min_events: int = 30
    min_controls: int = 30
    min_blocks: int = 20  # independent local days
    min_clusters: int = 20  # independent event clusters (structure_event_id), checked only when cluster ids exist


DEFAULT_MIN_EVIDENCE = MinEvidence()


def evidence_status(
    *, n_event: int, n_control: int, n_blocks: int, n_clusters: int | None = None, min_evidence: MinEvidence = DEFAULT_MIN_EVIDENCE,
) -> str:
    if n_event < min_evidence.min_events or n_control < min_evidence.min_controls or n_blocks < min_evidence.min_blocks:
        return INSUFFICIENT_EVIDENCE
    if n_clusters is not None and n_clusters < min_evidence.min_clusters:
        return INSUFFICIENT_EVIDENCE
    return OK


def final_status(status: str, adjusted_p: float | None, ci_low: float, ci_high: float, alpha: float = DEFAULT_ALPHA) -> str:
    if status == INSUFFICIENT_EVIDENCE:
        return INSUFFICIENT_EVIDENCE
    if adjusted_p is not None and adjusted_p < alpha and (ci_low > 0 or ci_high < 0):
        return SIGNIFICANT_ADJUSTED
    return NOT_SIGNIFICANT


# ---------------------------------------------------------------------------------------------- block bootstrap
@dataclass(frozen=True)
class DeltaEstimate:
    n_event: int
    n_control: int
    k_event: int
    k_control: int
    p_event: float
    p_control: float
    delta: float
    ci_low: float
    ci_high: float
    p_boot: float
    n_blocks: int
    n_clusters: int | None
    B: int
    alpha: float


Arm = tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]  # (y_event, day_event, y_control, day_control)


def _clean(y: np.ndarray, day: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    y = np.asarray(y, dtype=float)
    day = np.asarray(day)
    if len(y) != len(day):
        raise ValueError("outcome and day arrays differ in length")
    keep = ~np.isnan(y)
    return y[keep], day[keep], keep


def _arm_counts(arm: Arm, day_index: dict, D: int) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    out = np.zeros((D, 4))
    ns = []
    for col, (y, d) in enumerate(((arm[0], arm[1]), (arm[2], arm[3]))):
        yy, dd, _ = _clean(y, d)
        idx = np.fromiter((day_index[x] for x in dd), dtype=np.int64, count=len(dd))
        out[:, 2 * col] = np.bincount(idx, weights=yy, minlength=D)  # successes
        out[:, 2 * col + 1] = np.bincount(idx, minlength=D)  # n
        ns.append((int(yy.sum()), len(yy)))
    return out, (ns[0][0], ns[0][1], ns[1][0], ns[1][1])


def _rate(k: np.ndarray, n: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(n > 0, k / n, np.nan)


def _bootstrap_sums(counts: np.ndarray, B: int, seed: int, chunk: int = 200):
    D = counts.shape[0]
    rng = np.random.default_rng(seed)
    for lo in range(0, B, chunk):
        m = min(chunk, B - lo)
        idx = rng.integers(0, D, size=(m, D))
        yield counts[idx].sum(axis=1)  # (m, columns)


def _summarise(draws: np.ndarray, alpha: float) -> tuple[float, float, float]:
    d = draws[~np.isnan(draws)]
    if len(d) == 0:
        return float("nan"), float("nan"), float("nan")
    lo, hi = np.percentile(d, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    le, ge = int((d <= 0).sum()), int((d >= 0).sum())
    p = min(1.0, 2.0 * (min(le, ge) + 1) / (len(d) + 1))
    return float(lo), float(hi), float(p)


def block_bootstrap_delta(
    y_event, day_event, y_control, day_control, *, B: int = DEFAULT_B, seed: int = 0, alpha: float = DEFAULT_ALPHA,
    cluster_event=None,
) -> DeltaEstimate:
    """delta = P(y|event) - P(y|control); y in {0, 1, NaN}: NaN (censored / undefined) outcomes are excluded from both rates."""
    arm: Arm = (np.asarray(y_event, float), np.asarray(day_event), np.asarray(y_control, float), np.asarray(day_control))
    days = np.unique(np.concatenate([_clean(arm[0], arm[1])[1], _clean(arm[2], arm[3])[1]])) if (len(arm[0]) + len(arm[2])) else np.array([])
    index = {d: i for i, d in enumerate(days.tolist())}
    D = len(days)
    counts, (ke, ne, kc, nc) = _arm_counts(arm, index, D)
    pe = ke / ne if ne else float("nan")
    pc = kc / nc if nc else float("nan")
    draws = np.concatenate([_rate(s[:, 0], s[:, 1]) - _rate(s[:, 2], s[:, 3]) for s in _bootstrap_sums(counts, B, seed)]) if D else np.array([])
    lo, hi, p = _summarise(draws, alpha)
    ncl = None
    if cluster_event is not None:
        cl = np.asarray(cluster_event)
        ncl = len(set(cl[~np.isnan(arm[0])].tolist()))
    return DeltaEstimate(ne, nc, ke, kc, pe, pc, pe - pc, lo, hi, p, D, ncl, B, alpha)


def block_bootstrap_contrast(arm_a: Arm, arm_b: Arm, *, B: int = DEFAULT_B, seed: int = 0, alpha: float = DEFAULT_ALPHA) -> DeltaEstimate:
    """(delta of arm A) - (delta of arm B), whole days resampled JOINTLY for both arms (paired: arm A is typically a subset of arm B)."""
    days = np.unique(np.concatenate([_clean(a[0], a[1])[1] for a in (arm_a, arm_b)] + [_clean(a[2], a[3])[1] for a in (arm_a, arm_b)]))
    index = {d: i for i, d in enumerate(days.tolist())}
    D = len(days)
    ca, (ke, ne, kc, nc) = _arm_counts(arm_a, index, D)
    cb, (kbe, nbe, kbc, nbc) = _arm_counts(arm_b, index, D)
    counts = np.concatenate([ca, cb], axis=1)
    draws = np.concatenate([
        (_rate(s[:, 0], s[:, 1]) - _rate(s[:, 2], s[:, 3])) - (_rate(s[:, 4], s[:, 5]) - _rate(s[:, 6], s[:, 7])) for s in _bootstrap_sums(counts, B, seed)
    ])
    lo, hi, p = _summarise(draws, alpha)
    pe, pc = (ke / ne if ne else float("nan")), (kc / nc if nc else float("nan"))
    pbe, pbc = (kbe / nbe if nbe else float("nan")), (kbc / nbc if nbc else float("nan"))
    return DeltaEstimate(ne, nc, ke, kc, pe, pc, (pe - pc) - (pbe - pbc), lo, hi, p, D, None, B, alpha)


def block_bootstrap_ci(
    arrays: Sequence[np.ndarray], day: np.ndarray, stat: Callable[..., float], *, B: int = 1000, seed: int = 0, alpha: float = DEFAULT_ALPHA,
) -> tuple[float, float, np.ndarray]:
    """Generic day-block bootstrap of ``stat(*arrays)`` (rows of one day always stay together). Returns (lo, hi, draws)."""
    day = np.asarray(day)
    arrays = [np.asarray(a) for a in arrays]
    order = np.argsort(day, kind="stable")
    sorted_day = day[order]
    _, start = np.unique(sorted_day, return_index=True)
    groups = np.split(order, start[1:])
    rng = np.random.default_rng(seed)
    draws = np.empty(B)
    for b in range(B):
        pick = rng.integers(0, len(groups), size=len(groups))
        idx = np.concatenate([groups[k] for k in pick])
        try:
            draws[b] = stat(*(a[idx] for a in arrays))
        except (ValueError, FloatingPointError, ZeroDivisionError):
            draws[b] = np.nan
    d = draws[~np.isnan(draws)]
    if len(d) == 0:
        return float("nan"), float("nan"), draws
    lo, hi = np.percentile(d, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi), draws


# ---------------------------------------------------------------------------------------------- multiple testing
def holm_adjust(p: Sequence[float], m: int | None = None) -> list[float]:
    """Holm step-down adjusted p-values. ``m`` = size of the registered family (>= len(p); unevaluated hypotheses count as p = 1)."""
    arr = np.asarray(p, float)
    m = len(arr) if m is None else m
    if m < len(arr):
        raise ValueError("m must be >= the number of p-values")
    order = np.argsort(arr, kind="stable")
    adj = np.empty(len(arr))
    running = 0.0
    for rank, k in enumerate(order):
        running = max(running, (m - rank) * arr[k])
        adj[k] = min(1.0, running)
    return adj.tolist()


def bh_adjust(p: Sequence[float], m: int | None = None) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (FDR); ``m`` as in ``holm_adjust``."""
    arr = np.asarray(p, float)
    m = len(arr) if m is None else m
    if m < len(arr):
        raise ValueError("m must be >= the number of p-values")
    order = np.argsort(arr, kind="stable")
    adj = np.empty(len(arr))
    running = 1.0
    for rank in range(len(arr) - 1, -1, -1):
        k = order[rank]
        running = min(running, m * arr[k] / (rank + 1))
        adj[k] = min(1.0, running)
    return adj.tolist()


@dataclass
class HypothesisRegistry:
    """Every hypothesis must be registered BEFORE its result is recorded; the count is the multiplicity of the family."""

    name: str
    _names: list[str] = field(default_factory=list)
    _p: dict[str, float | None] = field(default_factory=dict)

    def register(self, hypothesis: str) -> None:
        if hypothesis in self._names:
            raise ValueError(f"hypothesis already registered: {hypothesis}")
        self._names.append(hypothesis)

    def register_many(self, hypotheses: Sequence[str]) -> None:
        for h in hypotheses:
            self.register(h)

    def record(self, hypothesis: str, p: float | None) -> None:
        if hypothesis not in self._names:
            raise KeyError(f"result for an unregistered hypothesis: {hypothesis}")
        if hypothesis in self._p:
            raise ValueError(f"result already recorded: {hypothesis}")
        self._p[hypothesis] = None if p is None or np.isnan(p) else float(p)

    @property
    def n_hypotheses(self) -> int:
        return len(self._names)

    @property
    def n_evaluated(self) -> int:
        return sum(v is not None for v in self._p.values())

    def adjust(self, method: str = "holm") -> dict[str, float | None]:
        fn = {"holm": holm_adjust, "bh": bh_adjust}.get(method)
        if fn is None:
            raise ValueError("method must be 'holm' or 'bh'")
        names = [n for n in self._names if self._p.get(n) is not None]
        adj = dict(zip(names, fn([self._p[n] for n in names], m=self.n_hypotheses), strict=True)) if names else {}
        return {n: adj.get(n) for n in self._names}

    def summary(self) -> Mapping[str, int | str]:
        return {"registry": self.name, "n_hypotheses": self.n_hypotheses, "n_evaluated": self.n_evaluated}


# ---------------------------------------------------------------------------------------------- result record
@dataclass(frozen=True)
class EnrichmentResult:
    feature: str  # column (f_<group>__<name>) or ablation arm description
    group: str | None
    label: str  # y_* column
    cell: str  # cell definition (quantile range / category / conjunction)
    n_event: int
    n_control: int
    p_event: float
    p_control: float
    delta: float  # kind == "single": P(y|event) - P(y|control); "incremental": delta(base + cell) - delta(base)
    ci_low: float
    ci_high: float
    n_blocks: int
    adjusted_p: float | None
    status: str
    versions: Mapping[str, str]
    n_clusters: int | None = None
    p_boot: float | None = None
    kind: str = "single"
    base_delta: float | None = None
    hypothesis: str | None = None
