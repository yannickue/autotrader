# ruff: noqa: E501
"""Chronological (expanding-window, purged, embargoed) validation and calibration metrics.

Numpy only. Ideas reused from `alpha.metalabel.cv` (day-block folds, purge by label availability,
embargo) but parameterised for the small demo dataset: fold k tests one contiguous block of DAYS;
its training rows are those whose signal day AND label-availability day are strictly before
`test_start - embargo_days`. Nothing after a test block is ever trained on; nothing is shuffled.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from demo.learning.dataset import Dataset, head_arrays

MIN_EMBARGO_DAYS = 1


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Fold:
    index: int
    train: np.ndarray  # row indices into the dataset
    test: np.ndarray
    test_start_day: int
    test_end_day: int  # exclusive
    embargo_days: int


def chronological_folds(
    ds: Dataset,
    n_folds: int = 4,
    embargo_days: int = MIN_EMBARGO_DAYS,
    min_train_days: int = 5,
    initial_fraction: float = 0.4,
) -> list[Fold]:
    if embargo_days < MIN_EMBARGO_DAYS:
        raise ValidationError(f"embargo_days must be >= {MIN_EMBARGO_DAYS}")
    day, lday = ds.day, ds.label_day
    days = np.unique(day)
    first = max(min_train_days + embargo_days, int(len(days) * initial_fraction))
    if n_folds < 1 or len(days) - first < n_folds:
        raise ValidationError(f"{len(days)} distinct days cannot host {n_folds} folds")
    folds: list[Fold] = []
    for k, blk in enumerate(np.array_split(days[first:], n_folds)):
        if not len(blk):
            continue
        s, e = int(blk[0]), int(blk[-1]) + 1
        tr = np.flatnonzero((day < s - embargo_days) & (lday < s - embargo_days))
        te = np.flatnonzero((day >= s) & (day < e))
        if len(tr) and len(te):
            folds.append(Fold(k, tr, te, s, e, embargo_days))
    if not folds:
        raise ValidationError("no usable fold")
    assert_chronological(folds, ds)
    return folds


def assert_chronological(folds: list[Fold], ds: Dataset) -> None:
    """Raise AssertionError if any fold trains on future / overlapping / embargoed rows."""
    day, lday = ds.day, ds.label_day
    prev_end = -(10**9)
    for f in folds:
        assert not np.intersect1d(f.train, f.test).size, "row in both train and test"
        assert day[f.train].max() <= f.test_start_day - f.embargo_days - 1, "train row inside embargo/test"
        assert lday[f.train].max() <= f.test_start_day - f.embargo_days - 1, "train label not yet available"
        assert ds.ts[f.train].max() < ds.ts[f.test].min(), "train row not strictly before test"
        assert ds.label_ts[f.train].max() < ds.ts[f.test].min(), "train label overlaps test horizon"
        assert day[f.test].min() >= f.test_start_day and day[f.test].max() < f.test_end_day
        assert f.test_start_day >= prev_end, "test blocks overlap or are out of order"
        prev_end = f.test_end_day


# ---------------------------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------------------------
def auc(y: np.ndarray, s: np.ndarray) -> float:
    y = np.asarray(y).astype(bool)
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    srt = np.sort(s)
    rank = (np.searchsorted(srt, s, side="left") + np.searchsorted(srt, s, side="right") + 1) / 2.0
    return float((rank[y].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def brier(y: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y, dtype=float)
    return float(np.mean((np.asarray(p, dtype=float) - y) ** 2)) if len(y) else float("nan")


def log_loss(y: np.ndarray, p: np.ndarray, eps: float = 1e-12) -> float:
    y = np.asarray(y, dtype=float)
    p = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))) if len(y) else float("nan")


def reliability(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> list[dict[str, float]]:
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, n_bins - 1)
    out = []
    for b in range(n_bins):
        m = idx == b
        if m.any():
            out.append({"bin": b, "n": int(m.sum()), "mean_pred": float(p[m].mean()),
                        "frac_pos": float(y[m].mean())})
    return out


def ece(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> float:
    n = len(y)
    if n == 0:
        return float("nan")
    return float(sum(r["n"] / n * abs(r["mean_pred"] - r["frac_pos"]) for r in reliability(y, p, n_bins)))


def calibration_report(y: np.ndarray, p: np.ndarray) -> dict[str, Any]:
    y = np.asarray(y, dtype=float)
    base = float(y.mean()) if len(y) else float("nan")
    return {
        "n": len(y),
        "base_rate": base,
        "auc": auc(y, p),
        "brier": brier(y, p),
        "brier_base_rate": brier(y, np.full(len(y), base)) if len(y) else float("nan"),
        "log_loss": log_loss(y, p),
        "ece": ece(y, p),
        "reliability": reliability(y, p),
    }


def permutation_baseline(y: np.ndarray, p: np.ndarray, n_perm: int = 200, seed: int = 0) -> dict[str, float]:
    """AUC of the real scores vs AUC when labels are shuffled (seeded). One-sided p-value."""
    y = np.asarray(y, dtype=float)
    obs = auc(y, p)
    if not math.isfinite(obs):
        return {"observed_auc": obs, "null_mean_auc": float("nan"), "p_value": float("nan"), "n_perm": 0}
    rng = np.random.default_rng(seed)
    null = np.array([auc(rng.permutation(y), p) for _ in range(n_perm)])
    return {
        "observed_auc": obs,
        "null_mean_auc": float(np.nanmean(null)),
        "p_value": float((1 + np.sum(null >= obs)) / (n_perm + 1)),
        "n_perm": n_perm,
    }


# ---------------------------------------------------------------------------------------------
# cross-validation driver
# ---------------------------------------------------------------------------------------------
class Learner(Protocol):
    def fit(self, X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> Any: ...
    def predict(self, X: np.ndarray) -> np.ndarray: ...


def cross_validate(
    factory: Callable[[], Learner],
    ds: Dataset,
    head: str,
    folds: list[Fold],
    *,
    min_train_rows: int = 20,
) -> dict[str, Any]:
    """Out-of-fold predictions for one head. Rows without a label for `head` are skipped."""
    ok_idx, _X, y_all, _w = head_arrays(ds, head)
    pos = {int(i): k for k, i in enumerate(ok_idx)}
    oof = np.full(len(ds), np.nan)
    used = []
    for f in folds:
        tr = np.array([i for i in f.train if int(i) in pos], dtype=np.int64)
        te = np.array([i for i in f.test if int(i) in pos], dtype=np.int64)
        if len(tr) < min_train_rows or not len(te):
            continue
        ytr = y_all[[pos[int(i)] for i in tr]]
        if head != "expected_r" and len(np.unique(ytr)) < 2:
            continue
        m = factory()
        m.fit(ds.X[tr], ytr, ds.weight[tr])
        oof[te] = m.predict(ds.X[te])
        used.append(f.index)
    have = np.flatnonzero(np.isfinite(oof))
    res: dict[str, Any] = {"head": head, "folds_used": used, "n_oof": len(have), "oof": oof}
    if len(have):
        y = y_all[[pos[int(i)] for i in have]]
        p = oof[have]
        if head == "expected_r":
            res["rmse"] = float(np.sqrt(np.mean((p - y) ** 2)))
            res["corr"] = float(np.corrcoef(p, y)[0, 1]) if len(y) > 2 and np.std(p) > 0 and np.std(y) > 0 else None
        else:
            res["calibration"] = calibration_report(y, p)
    return res


def selection_evidence(ds: Dataset, oof: np.ndarray, threshold: float = 0.5) -> dict[str, Any]:
    """Promotion-gate inputs from out-of-fold scores on REAL trades only (counterfactual R is
    hypothetical and never counts as realised expectancy)."""
    have = np.flatnonzero(np.isfinite(oof) & ~ds.is_cf)
    sel = have[oof[have] >= threshold]
    r = ds.r
    out: dict[str, Any] = {
        "n_scored_real": len(have),
        "n_selected": len(sel),
        "baseline_expectancy_r": float(r[have].mean()) if len(have) else None,
        "net_expectancy_r": float(r[sel].mean()) if len(sel) else None,
    }
    if len(sel):
        cum = np.cumsum(r[sel][np.argsort(ds.ts[sel], kind="stable")])
        peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
        out["max_drawdown_r"] = float(np.max(peak - cum))
    else:
        out["max_drawdown_r"] = None
    for name, keys in (("market", ds.market), ("session", ds.session), ("family", ds.family)):
        sums: dict[str, float] = {}
        for i in sel:
            sums[keys[i]] = sums.get(keys[i], 0.0) + float(r[i])
        pos = {k: v for k, v in sums.items() if v > 0}
        tot = sum(pos.values())
        out[f"max_{name}_share"] = (max(pos.values()) / tot) if tot > 0 else (1.0 if len(sel) else None)
    return out
