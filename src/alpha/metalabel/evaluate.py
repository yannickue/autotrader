# ruff: noqa: E501
"""Purged out-of-fold evaluation, honest nulls and the meta-label verdict (research only).

``oof_predict`` trains ONLY on rows strictly earlier than each fold's test block (see ``cv``); the
Platt scaler / early stopping use a chronological inner calibration block of the same training rows.
Nulls re-run the identical pipeline on outcomes (R, y1, y3 jointly) that were

* permuted WITHIN day blocks (``block_permutation``: label-permutation null), or
* circularly rotated within day blocks against the (time-ordered) feature rows
  (``block_rotation``: features-shuffled-in-time null; keeps the cluster structure of the outcomes).

Verdict (``verdict``): USEFUL only if the OOF AUC day-cluster CI excludes 0.5 from below AND the
top-decile mean R exceeds the all-trade mean with day-clustered t > 2 AND both nulls are beaten on both
statistics (AUC, top-decile mean R) at ``alpha / n_models`` (Bonferroni over the compared model families).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from alpha.metalabel import cv, metrics
from alpha.metalabel.models import GBDT, LogisticL2, PlattScaler, Preprocessor, Ridge

TOP_FRAC = 0.10
THR_QUANTILE = 0.70


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str  # logit | ridge | gbdt
    label: str  # y1 | y2 | y3
    params: dict = field(default_factory=dict)


@dataclass
class Outcomes:
    """Trade outcomes that move together under the nulls."""

    r: np.ndarray
    y1: np.ndarray
    y3: np.ndarray

    def take(self, idx: np.ndarray) -> Outcomes:
        return Outcomes(self.r[idx], self.y1[idx], self.y3[idx])

    def label(self, name: str) -> np.ndarray:
        return {"y1": self.y1, "y2": self.r, "y3": self.y3}[name]


def block_permutation(day: np.ndarray, block_days: int, rng: np.random.Generator) -> np.ndarray:
    """Row permutation that shuffles rows only within blocks of ``block_days`` consecutive days."""
    blk = np.asarray(day) // block_days
    order = np.arange(len(day))
    out = order.copy()
    for b in np.unique(blk):
        idx = np.flatnonzero(blk == b)
        out[idx] = rng.permutation(idx)
    return out


def block_rotation(day: np.ndarray, block_days: int, rng: np.random.Generator) -> np.ndarray:
    """Row map that circularly rotates the row order inside each day block by a random offset in [20%, 80%]."""
    blk = np.asarray(day) // block_days
    out = np.arange(len(day))
    for b in np.unique(blk):
        idx = np.flatnonzero(blk == b)
        m = len(idx)
        if m < 5:
            continue
        s = int(rng.integers(max(int(0.2 * m), 1), max(int(0.8 * m), 2)))
        out[idx] = np.roll(idx, s)
    return out


class _Fitted:
    """A trained fold model exposing ``score(X)`` and (classifiers) calibrated ``prob(score)``."""

    def __init__(self, spec: ModelSpec, x_fit: np.ndarray, y_fit: np.ndarray, x_cal: np.ndarray | None,
                 y_cal: np.ndarray | None, seed: int) -> None:
        self.spec = spec
        self.pre = Preprocessor().fit(x_fit)
        xf = self.pre.transform(x_fit)
        p = spec.params
        self.platt: PlattScaler | None = None
        if spec.kind == "logit":
            self.m = LogisticL2(p.get("l2", 10.0)).fit(xf, y_fit)
            self._score = self.m.decision_function
        elif spec.kind == "ridge":
            self.m = Ridge(p.get("alpha", 100.0)).fit(xf, y_fit)
            self._score = self.m.predict
        elif spec.kind == "gbdt":
            xc = self.pre.transform(x_cal) if x_cal is not None else None
            self.m = GBDT(loss=p.get("loss", "logistic"), n_trees=p.get("n_trees", 200), lr=p.get("lr", 0.05),
                          depth=p.get("depth", 3), subsample=p.get("subsample", 0.5),
                          colsample=p.get("colsample", 0.7), min_leaf=p.get("min_leaf", 100),
                          lam=p.get("lam", 10.0), patience=p.get("patience", 20), seed=seed).fit(xf, y_fit, xc, y_cal)
            self._score = self.m.decision_function
        else:
            raise ValueError(spec.kind)
        if spec.kind in ("logit", "gbdt") and x_cal is not None:
            self.platt = PlattScaler().fit(self._score(self.pre.transform(x_cal)), y_cal)

    def score(self, x: np.ndarray) -> np.ndarray:
        return self._score(self.pre.transform(x))

    def prob(self, score: np.ndarray) -> np.ndarray:
        if self.platt is None:
            raise ValueError("not a calibrated classifier")
        return self.platt.transform(score)


def oof_predict(x: np.ndarray, out: Outcomes, folds: list[cv.DayFold], day: np.ndarray, exit_day: np.ndarray,
                spec: ModelSpec, seed: int = 0, keep_models: bool = False) -> dict:
    """Out-of-fold scores.  ``score``/``prob``/``thr``/``base`` are NaN outside the OOF (test) rows."""
    n = len(x)
    y = out.label(spec.label)
    score = np.full(n, np.nan)
    prob = np.full(n, np.nan)
    thr = np.full(n, np.nan)
    base = np.full(n, np.nan)
    fold_of = np.full(n, -1, dtype=np.int64)
    models = []
    for f in folds:
        if spec.kind == "ridge":
            fit_rows, cal_rows = f.train, None
        else:
            fit_rows, cal_rows = cv.inner_split(day, exit_day, f.train)
        m = _Fitted(spec, x[fit_rows], y[fit_rows], None if cal_rows is None else x[cal_rows],
                    None if cal_rows is None else y[cal_rows], seed + f.index)
        s = m.score(x[f.test])
        score[f.test] = s
        fold_of[f.test] = f.index
        if spec.label != "y2":
            prob[f.test] = m.prob(s)
            thr[f.test] = np.quantile(m.prob(m.score(x[cal_rows])), THR_QUANTILE)
            base[f.test] = y[f.train].mean()
        else:
            thr[f.test] = np.quantile(m.score(x[f.train]), THR_QUANTILE)
            base[f.test] = y[f.train].mean()
        if keep_models:
            models.append(m)
    return {"score": score, "prob": prob, "thr": thr, "base": base, "fold": fold_of, "models": models}


def oof_stats(res: dict, out: Outcomes, spec: ModelSpec) -> tuple[float, float]:
    """(OOF AUC vs y1, top-decile mean R) - the two statistics the nulls are built on."""
    m = np.isfinite(res["score"])
    s = res["score"][m]
    a = metrics.auc(out.y1[m], s)
    top = metrics.top_decile_mask(s, TOP_FRAC)
    return a, float(out.r[m][top].mean())


def summarize_oof(res: dict, out: Outcomes, spec: ModelSpec, day: np.ndarray, seed: int = 0,
                  n_boot: int = 500) -> dict:
    """Full point metrics of one model's OOF predictions (day-clustered uncertainty)."""
    m = np.isfinite(res["score"])
    s, r, d = res["score"][m], out.r[m], day[m]
    y1, y3 = out.y1[m], out.y3[m]
    lab = out.label(spec.label)[m]
    a = metrics.auc(y1, s)
    lo, hi = metrics.cluster_boot_auc(y1, s, d, n_boot=n_boot, seed=seed)
    top = metrics.top_decile_mask(s, TOP_FRAC)
    row: dict = {
        "model": spec.name, "kind": spec.kind, "label": spec.label, "n_oof": int(m.sum()),
        "n_oof_days": len(np.unique(d)), "auc_y1": a, "auc_y1_ci95": [lo, hi],
        "auc_y3": metrics.auc(y3, s),
        "mean_r_all": float(r.mean()), "hit_rate_all": float(y1.mean()),
        "top_decile": {**metrics.cluster_boot_diff(r, d, top, seed=seed), "frac": TOP_FRAC,
                       "clustered_mean_t": metrics.day_clustered_mean(r[top], d[top])["t"]},
        "deciles": metrics.deciles(s, r, y1),
    }
    if spec.label != "y2":
        p = res["prob"][m]
        cal = metrics.calibration(lab, p)
        b = res["base"][m]
        row["calibration"] = cal
        row["brier"] = metrics.brier(lab, p)
        row["brier_base_rate"] = metrics.brier(lab, b)
        row["brier_skill"] = 1.0 - row["brier"] / max(row["brier_base_rate"], 1e-12)
        taken = p > res["thr"][m]
        row["taken_p_gt_thr"] = {**metrics.cluster_boot_diff(r, d, taken, seed=seed + 1),
                                 "share_taken": float(taken.mean()), "hit_rate_taken": float(y1[taken].mean()) if taken.any() else None}
    else:
        taken = s > res["thr"][m]
        row["corr_pred_r"] = float(np.corrcoef(s, r)[0, 1]) if np.std(s) > 0 else None
        row["taken_p_gt_thr"] = {**metrics.cluster_boot_diff(r, d, taken, seed=seed + 1), "share_taken": float(taken.mean())}
    return row


def permutation_importance(x: np.ndarray, out: Outcomes, folds: list[cv.DayFold], res: dict, names: list[str],
                           seed: int = 0, top: int = 15) -> list[dict]:
    """Out-of-fold permutation importance: AUC(y1) drop when one test column is shuffled inside each test block."""
    rng = np.random.default_rng(seed)
    n_f = x.shape[1]
    drops = np.zeros(n_f)
    for f, m in zip(folds, res["models"], strict=True):
        xt = x[f.test]
        y = out.y1[f.test]
        base = metrics.auc(y, m.score(xt))
        for j in range(n_f):
            keep = xt[:, j].copy()
            xt[:, j] = keep[rng.permutation(len(keep))]
            drops[j] += base - metrics.auc(y, m.score(xt))
            xt[:, j] = keep
    drops /= max(len(folds), 1)
    order = np.argsort(-drops)[:top]
    return [{"feature": names[j], "auc_drop": float(drops[j])} for j in order]


def null_distribution(x: np.ndarray, out: Outcomes, folds: list[cv.DayFold], day: np.ndarray, exit_day: np.ndarray,
                      spec: ModelSpec, kind: str, n_draws: int, seed: int, block_days: int) -> dict:
    """``kind`` = 'perm' (label permutation within blocks) or 'shift' (feature rows rotated in time)."""
    rng = np.random.default_rng(seed)
    aucs, tops = [], []
    for k in range(n_draws):
        perm = (block_permutation if kind == "perm" else block_rotation)(day, block_days, rng)
        o = out.take(perm)
        res = oof_predict(x, o, folds, day, exit_day, spec, seed=seed + 1000 + k)
        a, t = oof_stats(res, o, spec)
        aucs.append(a)
        tops.append(t)
    return {"auc": np.asarray(aucs), "top_r": np.asarray(tops)}


def p_value(obs: float, null: np.ndarray) -> float:
    """One-sided empirical p (obs large is 'good'): (1 + #{null >= obs}) / (1 + n)."""
    null = np.asarray(null)
    null = null[np.isfinite(null)]
    return float((1 + np.sum(null >= obs)) / (1 + len(null)))


def verdict(summary: dict, p_perm: dict, p_shift: dict, n_models: int, alpha: float = 0.05) -> dict:
    """USEFUL / INCONCLUSIVE / NOT USEFUL for one model (see module docstring)."""
    lo = summary["auc_y1_ci95"][0]
    td = summary["top_decile"]
    lift_ok = td["diff"] is not None and td["diff"] > 0 and td["t"] is not None and td["t"] > 2.0
    auc_ok = lo is not None and np.isfinite(lo) and lo > 0.5
    a_adj = alpha / max(n_models, 1)
    nulls_ok = all(p <= a_adj for p in (p_perm["auc"], p_perm["top_r"], p_shift["auc"], p_shift["top_r"]))
    if auc_ok and lift_ok and nulls_ok:
        v = "USEFUL"
    elif not auc_ok and not lift_ok:
        v = "NOT USEFUL"
    else:
        v = "INCONCLUSIVE"
    return {"verdict": v, "auc_ci_excludes_0.5": bool(auc_ok), "top_decile_lift_t_gt_2": bool(lift_ok),
            "beats_both_nulls": bool(nulls_ok), "alpha_adjusted": a_adj}


def overall_verdict(per_model: dict[str, dict]) -> str:
    vs = [m["verdict"] for m in per_model.values()]
    if "USEFUL" in vs:
        return "USEFUL"
    if "INCONCLUSIVE" in vs:
        return "INCONCLUSIVE"
    return "NOT USEFUL"


__all__ = ("ModelSpec", "Outcomes", "block_permutation", "block_rotation", "null_distribution", "oof_predict",
           "oof_stats", "overall_verdict", "p_value", "permutation_importance", "summarize_oof", "verdict")
