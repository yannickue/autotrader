# ruff: noqa: E501
"""Out-of-fold metrics with day-clustered uncertainty (research only)."""

from __future__ import annotations

import numpy as np

from alpha.metalabel.models import PlattScaler, logit


def auc(y: np.ndarray, s: np.ndarray) -> float:
    """Mann-Whitney AUC with tie-averaged ranks; NaN if one class is missing."""
    y = np.asarray(y).astype(bool)
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    srt = np.sort(s)
    rank = (np.searchsorted(srt, s, side="left") + np.searchsorted(srt, s, side="right") + 1) / 2.0
    return float((rank[y].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def _day_groups(day: np.ndarray) -> tuple[np.ndarray, list[np.ndarray]]:
    order = np.argsort(day, kind="stable")
    ds = day[order]
    days, start = np.unique(ds, return_index=True)
    return days, np.split(order, start[1:])


def cluster_boot_auc(y: np.ndarray, s: np.ndarray, day: np.ndarray, n_boot: int = 500, seed: int = 0,
                     alpha: float = 0.05) -> tuple[float, float]:
    """Percentile CI of the AUC under a day-cluster bootstrap (whole days resampled with replacement)."""
    _, groups = _day_groups(day)
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(groups), size=len(groups))
        idx = np.concatenate([groups[k] for k in pick])
        a = auc(y[idx], s[idx])
        if np.isfinite(a):
            vals.append(a)
    if len(vals) < 10:
        return float("nan"), float("nan")
    return float(np.quantile(vals, alpha / 2)), float(np.quantile(vals, 1 - alpha / 2))


def day_clustered_mean(r: np.ndarray, day: np.ndarray) -> dict:
    """Mean of ``r`` with a cluster-robust (by day) standard error of the ratio estimator, and t = mean / se."""
    r = np.asarray(r, dtype=np.float64)
    if len(r) == 0:
        return {"n": 0, "n_days": 0, "mean": None, "se": None, "t": None}
    days, groups = _day_groups(day)
    sums = np.array([r[g].sum() for g in groups])
    cnt = np.array([len(g) for g in groups], dtype=np.float64)
    m = r.mean()
    g = len(days)
    resid = sums - m * cnt
    if g < 2:
        return {"n": len(r), "n_days": g, "mean": float(m), "se": None, "t": None}
    var = g / (g - 1) * np.sum(resid ** 2) / (len(r) ** 2)
    se = float(np.sqrt(var))
    return {"n": len(r), "n_days": int(g), "mean": float(m), "se": se, "t": float(m / se) if se > 0 else None}


def cluster_boot_diff(r: np.ndarray, day: np.ndarray, sel: np.ndarray, n_boot: int = 1000, seed: int = 0) -> dict:
    """mean(r | sel) - mean(r) with a day-cluster bootstrap SE (whole days resampled); t = diff / se."""
    r = np.asarray(r, dtype=np.float64)
    sel = np.asarray(sel, dtype=bool)
    days, groups = _day_groups(day)
    s_all = np.array([r[g].sum() for g in groups])
    n_all = np.array([len(g) for g in groups], dtype=np.float64)
    s_sel = np.array([r[g][sel[g]].sum() for g in groups])
    n_sel = np.array([sel[g].sum() for g in groups], dtype=np.float64)
    if sel.sum() == 0 or len(days) < 2:
        return {"n_sel": int(sel.sum()), "mean_sel": None, "mean_all": float(r.mean()) if len(r) else None,
                "diff": None, "se": None, "t": None}
    rng = np.random.default_rng(seed)
    w = rng.multinomial(len(days), np.full(len(days), 1.0 / len(days)), size=n_boot).astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        d = (w @ s_sel) / (w @ n_sel) - (w @ s_all) / (w @ n_all)
    d = d[np.isfinite(d)]
    diff = float(s_sel.sum() / n_sel.sum() - s_all.sum() / n_all.sum())
    se = float(d.std(ddof=1)) if len(d) > 5 else float("nan")
    return {"n_sel": int(sel.sum()), "mean_sel": float(s_sel.sum() / n_sel.sum()), "mean_all": float(s_all.sum() / n_all.sum()),
            "diff": diff, "se": se, "t": float(diff / se) if se and se > 0 else None}


def cluster_boot_contrast(r: np.ndarray, day: np.ndarray, a: np.ndarray, b: np.ndarray, n_boot: int = 1000,
                          seed: int = 0) -> dict:
    """mean(r | a) - mean(r | b) for two disjoint row masks, day-cluster bootstrap SE and t."""
    r = np.asarray(r, dtype=np.float64)
    a = np.asarray(a, dtype=bool)
    b = np.asarray(b, dtype=bool)
    if a.sum() == 0 or b.sum() == 0:
        return {"n_a": int(a.sum()), "n_b": int(b.sum()), "mean_a": None, "mean_b": None, "diff": None, "se": None, "t": None}
    days, groups = _day_groups(day)
    sa = np.array([r[g][a[g]].sum() for g in groups])
    na = np.array([a[g].sum() for g in groups], dtype=np.float64)
    sb = np.array([r[g][b[g]].sum() for g in groups])
    nb = np.array([b[g].sum() for g in groups], dtype=np.float64)
    rng = np.random.default_rng(seed)
    w = rng.multinomial(len(days), np.full(len(days), 1.0 / len(days)), size=n_boot).astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        d = (w @ sa) / (w @ na) - (w @ sb) / (w @ nb)
    d = d[np.isfinite(d)]
    ma, mb = float(sa.sum() / na.sum()), float(sb.sum() / nb.sum())
    se = float(d.std(ddof=1)) if len(d) > 5 else float("nan")
    return {"n_a": int(a.sum()), "n_b": int(b.sum()), "mean_a": ma, "mean_b": mb, "diff": ma - mb, "se": se,
            "t": float((ma - mb) / se) if se and se > 0 else None}


def calibration(y: np.ndarray, p: np.ndarray) -> dict:
    """Calibration slope/intercept: logistic regression of y on logit(p) (slope 1, intercept 0 = perfect)."""
    sc = PlattScaler().fit(logit(np.asarray(p, dtype=np.float64)), np.asarray(y, dtype=np.float64))
    return {"slope": sc.a, "intercept": sc.b}


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def deciles(score: np.ndarray, r: np.ndarray, y: np.ndarray | None = None, n_bins: int = 10) -> list[dict]:
    """Lift table: rows sorted by ``score`` into equal-count bins (bin 0 = lowest)."""
    order = np.argsort(score, kind="stable")
    out = []
    for b, idx in enumerate(np.array_split(order, n_bins)):
        row = {"decile": b + 1, "n": len(idx), "mean_score": float(np.mean(score[idx])) if len(idx) else None,
               "mean_r": float(np.mean(r[idx])) if len(idx) else None}
        if y is not None:
            row["hit_rate"] = float(np.mean(y[idx])) if len(idx) else None
        out.append(row)
    return out


def top_decile_mask(score: np.ndarray, frac: float = 0.10) -> np.ndarray:
    k = max(round(len(score) * frac), 1)
    order = np.argsort(-score, kind="stable")
    m = np.zeros(len(score), dtype=bool)
    m[order[:k]] = True
    return m


__all__ = ("auc", "brier", "calibration", "cluster_boot_auc", "cluster_boot_contrast", "cluster_boot_diff", "day_clustered_mean",
           "deciles", "top_decile_mask")
