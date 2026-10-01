# ruff: noqa: E501
"""Calibration / proper scoring metrics for ANY probability forecast ``p_hat`` vs binary outcome ``y`` (OFFLINE, no model training here).

Primary metrics are proper scoring rules (Brier, log loss) and calibration (reliability table with EQUAL-COUNT bins, expected
calibration error, logistic recalibration slope/intercept). AUC is SUPPLEMENTARY only (rank metric; blind to calibration).
All headline numbers get day-block bootstrap intervals (``stats.block_bootstrap_ci``: whole local days resampled).
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from coverage_analysis.observer_lab.stats import DEFAULT_ALPHA, block_bootstrap_ci, wilson_interval

EPS = 1e-12
RIDGE = 1e-2  # pulls (intercept, slope) towards (0, 1); negligible for n >> 1/RIDGE, keeps the fit finite under separation


def _arr(p, y) -> tuple[np.ndarray, np.ndarray]:
    p, y = np.asarray(p, float), np.asarray(y, float)
    if p.shape != y.shape:
        raise ValueError("p and y must have the same shape")
    return p, y


def brier_score(p, y) -> float:
    p, y = _arr(p, y)
    return float(np.mean((p - y) ** 2))


def log_loss(p, y, eps: float = EPS) -> float:
    p, y = _arr(p, y)
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def auc(p, y) -> float:
    """Mann-Whitney AUC with ties counted half (SUPPLEMENTARY metric). NaN with a single class."""
    p, y = _arr(p, y)
    pos = y == 1
    n1, n0 = int(pos.sum()), int((~pos).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p))
    ps = p[order]
    # average ranks for ties
    start = 0
    while start < len(ps):
        end = start
        while end + 1 < len(ps) and ps[end + 1] == ps[start]:
            end += 1
        ranks[order[start : end + 1]] = (start + end) / 2 + 1
        start = end + 1
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def reliability_table(p, y, n_bins: int = 10) -> list[dict]:
    """Equal-COUNT bins over the sorted forecasts (sizes differ by at most one). Wilson interval on the observed rate."""
    p, y = _arr(p, y)
    order = np.argsort(p, kind="mergesort")
    rows = []
    for k, idx in enumerate(np.array_split(order, min(n_bins, len(p)))):
        if len(idx) == 0:
            continue
        lo, hi = wilson_interval(int(y[idx].sum()), len(idx))
        rows.append({
            "bin": k, "n": len(idx), "mean_p": float(p[idx].mean()), "mean_y": float(y[idx].mean()), "ci_low": lo, "ci_high": hi,
        })
    return rows


def expected_calibration_error(p, y, n_bins: int = 10) -> float:
    p, y = _arr(p, y)
    tab = reliability_table(p, y, n_bins)
    return float(sum(r["n"] * abs(r["mean_p"] - r["mean_y"]) for r in tab) / len(p))


def calibration_slope_intercept(p, y, ridge: float = RIDGE) -> tuple[float, float]:
    """Logistic recalibration ``P(y=1) = sigmoid(a + b * logit(p_hat))`` -> (intercept a, slope b). Perfect: (0, 1).
    Newton / IRLS with step halving and a small ridge towards (0, 1). NaN pair when only one outcome class is present."""
    p, y = _arr(p, y)
    if len(y) == 0 or y.min() == y.max():
        return float("nan"), float("nan")
    z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
    X = np.column_stack([np.ones_like(z), z])
    prior = np.array([0.0, 1.0])
    theta = prior.copy()

    def nll(t: np.ndarray) -> float:
        eta = X @ t
        return float(np.sum(np.logaddexp(0, eta) - y * eta) + 0.5 * ridge * np.sum((t - prior) ** 2))

    cur = nll(theta)
    for _ in range(60):
        mu = 1 / (1 + np.exp(-(X @ theta)))
        grad = X.T @ (mu - y) + ridge * (theta - prior)
        hess = (X * (mu * (1 - mu))[:, None]).T @ X + ridge * np.eye(2)
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        t = 1.0
        while t > 1e-8:
            cand = theta - t * step
            val = nll(cand)
            if val <= cur:
                break
            t /= 2
        else:
            break
        theta, delta, cur = cand, abs(cur - val), val
        if delta < 1e-10 and np.max(np.abs(step)) < 1e-8:
            break
    return float(theta[0]), float(theta[1])


def _with_ci(fn: Callable[[np.ndarray, np.ndarray], float], p, y, day, *, B: int, seed: int, alpha: float, **extra) -> dict:
    lo, hi, _ = block_bootstrap_ci((p, y), day, fn, B=B, seed=seed, alpha=alpha)
    return {"value": float(fn(p, y)), "ci_low": lo, "ci_high": hi, **extra}


def evaluate_forecast(p, y, day, *, n_bins: int = 10, B: int = 500, seed: int = 0, alpha: float = DEFAULT_ALPHA) -> dict:
    """All metrics with day-block bootstrap intervals. ``day`` = local-day id per row (the independent block)."""
    p, y = _arr(p, y)
    day = np.asarray(day)
    kw = {"B": B, "seed": seed, "alpha": alpha}
    return {
        "n": len(p), "n_blocks": len(np.unique(day)), "base_rate": float(np.mean(y)),
        "brier": _with_ci(brier_score, p, y, day, **kw),
        "log_loss": _with_ci(log_loss, p, y, day, **kw),
        "ece": _with_ci(lambda a, b: expected_calibration_error(a, b, n_bins), p, y, day, **kw),
        "intercept": _with_ci(lambda a, b: calibration_slope_intercept(a, b)[0], p, y, day, **kw),
        "slope": _with_ci(lambda a, b: calibration_slope_intercept(a, b)[1], p, y, day, **kw),
        "auc": _with_ci(auc, p, y, day, role="supplementary", **kw),
        "reliability": reliability_table(p, y, n_bins),
    }


def brier_skill_vs_baseline(p, y, baseline, day, *, B: int = 500, seed: int = 0, alpha: float = DEFAULT_ALPHA) -> dict:
    """Improvement = Brier(baseline) - Brier(model) (> 0: the model is better), paired, with a day-block bootstrap interval.
    Typical baselines: the training base rate, or the matched-control event probability."""
    p, y = _arr(p, y)
    baseline = np.asarray(baseline, float)
    lo, hi, _ = block_bootstrap_ci(
        (p, y, baseline), day, lambda a, b, c: brier_score(c, b) - brier_score(a, b), B=B, seed=seed, alpha=alpha,
    )
    bs_model, bs_base = brier_score(p, y), brier_score(baseline, y)
    return {
        "brier_model": bs_model, "brier_baseline": bs_base, "improvement": bs_base - bs_model,
        "skill_score": 1 - bs_model / bs_base if bs_base > 0 else float("nan"), "ci_low": lo, "ci_high": hi,
    }
