# ruff: noqa: E501
"""Own NumPy/numba models for meta-labeling (deterministic, seeded; no scikit-learn / lightgbm).

* ``Preprocessor``: in-fold median imputation, winsorising and standardisation (fit on training rows only).
* ``LogisticL2``: L2-regularised logistic regression by Newton/IRLS (unpenalised intercept).
* ``Ridge``: closed-form ridge regression (unpenalised intercept).
* ``PlattScaler``: 2-parameter sigmoid calibration ``p = sigmoid(a * score + b)`` (Newton), fitted on a
  held-out chronological calibration block INSIDE each CV fold.
* ``GBDT``: small gradient-boosted decision trees (depth <= 3, shrinkage, row/column subsampling,
  histogram splits in numba) with early stopping on a held-out validation block.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from numba import njit

WINSOR = 5.0


def sigmoid(z: np.ndarray) -> np.ndarray:
    z = np.clip(z, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-z))


def logit(p: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    p = np.clip(p, eps, 1.0 - eps)
    return np.log(p / (1.0 - p))


# --------------------------------------------------------------------------- preprocessing
class Preprocessor:
    """median-impute -> winsorise at +-WINSOR robust sd -> standardise; parameters from the training rows only."""

    def fit(self, x: np.ndarray) -> Preprocessor:
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            self.med = np.nanmedian(x, axis=0)
        self.med = np.where(np.isfinite(self.med), self.med, 0.0)
        xi = np.where(np.isfinite(x), x, self.med)
        self.mu = xi.mean(axis=0)
        sd = xi.std(axis=0)
        self.sd = np.where(sd > 1e-12, sd, 1.0)
        return self

    def transform(self, x: np.ndarray) -> np.ndarray:
        xi = np.where(np.isfinite(x), x, self.med)
        return np.clip((xi - self.mu) / self.sd, -WINSOR, WINSOR)

    def fit_transform(self, x: np.ndarray) -> np.ndarray:
        return self.fit(x).transform(x)


# --------------------------------------------------------------------------- logistic regression (IRLS)
def _nll(x1: np.ndarray, y: np.ndarray, beta: np.ndarray, l2: float) -> float:
    z = x1 @ beta
    return float(np.sum(np.logaddexp(0.0, z) - y * z) + 0.5 * l2 * np.dot(beta[1:], beta[1:]))


class LogisticL2:
    """Binary logistic regression, ``0.5 * l2 * ||beta||^2`` penalty (intercept excluded), Newton steps."""

    def __init__(self, l2: float = 10.0, max_iter: int = 60, tol: float = 1e-9) -> None:
        self.l2, self.max_iter, self.tol = float(l2), int(max_iter), float(tol)

    def fit(self, x: np.ndarray, y: np.ndarray) -> LogisticL2:
        n, p = x.shape
        x1 = np.column_stack([np.ones(n), x])
        beta = np.zeros(p + 1)
        pen = np.full(p + 1, self.l2)
        pen[0] = 0.0
        prev = _nll(x1, y, beta, self.l2)
        for _ in range(self.max_iter):
            pr = sigmoid(x1 @ beta)
            w = np.maximum(pr * (1.0 - pr), 1e-9)
            g = x1.T @ (y - pr) - pen * beta
            h = (x1 * w[:, None]).T @ x1 + np.diag(pen + 1e-9)
            step = np.linalg.solve(h, g)
            t = 1.0
            while t > 1e-4:  # step halving keeps the objective monotone
                cand = beta + t * step
                cur = _nll(x1, y, cand, self.l2)
                if cur <= prev + 1e-12:
                    break
                t *= 0.5
            else:
                break
            beta, done = cand, prev - cur < self.tol * max(1.0, abs(prev))
            prev = cur
            if done:
                break
        self.beta = beta
        return self

    def decision_function(self, x: np.ndarray) -> np.ndarray:
        return self.beta[0] + x @ self.beta[1:]

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        return sigmoid(self.decision_function(x))


class Ridge:
    def __init__(self, alpha: float = 100.0) -> None:
        self.alpha = float(alpha)

    def fit(self, x: np.ndarray, y: np.ndarray) -> Ridge:
        self.y0 = float(np.mean(y))
        xm = x.mean(axis=0)
        xc = x - xm
        a = xc.T @ xc + self.alpha * np.eye(x.shape[1])
        self.coef = np.linalg.solve(a, xc.T @ (y - self.y0))
        self.xm = xm
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.y0 + (x - self.xm) @ self.coef


@dataclass
class PlattScaler:
    """p = sigmoid(a * score + b); Newton fit of the two parameters (a = calibration slope, b = intercept)."""

    a: float = 1.0
    b: float = 0.0

    def fit(self, score: np.ndarray, y: np.ndarray, max_iter: int = 50) -> PlattScaler:
        s = np.asarray(score, dtype=np.float64)
        yy = np.asarray(y, dtype=np.float64)
        if len(s) < 10 or yy.min() == yy.max():
            self.a, self.b = 1.0, 0.0
            return self
        beta = np.array([0.0, 1.0])  # [b, a]
        x1 = np.column_stack([np.ones(len(s)), s])
        prev = _nll(x1, yy, beta, 0.0)
        for _ in range(max_iter):
            pr = sigmoid(x1 @ beta)
            w = np.maximum(pr * (1.0 - pr), 1e-9)
            g = x1.T @ (yy - pr)
            h = (x1 * w[:, None]).T @ x1 + 1e-9 * np.eye(2)
            step = np.linalg.solve(h, g)
            t = 1.0
            while t > 1e-4:
                cand = beta + t * step
                cur = _nll(x1, yy, cand, 0.0)
                if cur <= prev + 1e-12:
                    break
                t *= 0.5
            else:
                break
            beta = cand
            done = prev - cur < 1e-10
            prev = cur
            if done:
                break
        self.b, self.a = float(beta[0]), float(beta[1])
        return self

    def transform(self, score: np.ndarray) -> np.ndarray:
        return sigmoid(self.a * np.asarray(score, dtype=np.float64) + self.b)


# --------------------------------------------------------------------------- GBDT
MAX_DEPTH = 3
N_NODES = 2 ** (MAX_DEPTH + 1) - 1  # heap layout: children of node k are 2k+1 / 2k+2
N_BINS = 32


@njit(cache=True)
def _grow_tree(xb: np.ndarray, g: np.ndarray, h: np.ndarray, rows: np.ndarray, colmask: np.ndarray,
               depth: int, lam: float, min_leaf: int, min_gain: float, n_bins: int,
               feat: np.ndarray, thr: np.ndarray, val: np.ndarray) -> None:
    """Level-wise histogram tree on the sampled ``rows``; fills heap arrays ``feat/thr/val`` in place.

    ``xb`` is (n_features, n_rows) uint8 bin codes.  A node with feat < 0 is a leaf (value in ``val``)."""
    n_feat = xb.shape[0]
    n_nodes = feat.shape[0]
    node_of = np.zeros(len(rows), dtype=np.int64)
    feat[:] = -1
    thr[:] = 0
    val[:] = 0.0
    for level in range(depth + 1):
        first = 2 ** level - 1
        n_lvl = 2 ** level
        gs = np.zeros(n_lvl)
        hs = np.zeros(n_lvl)
        cs = np.zeros(n_lvl, dtype=np.int64)
        for t in range(len(rows)):
            nd = node_of[t] - first
            if nd < 0 or nd >= n_lvl:
                continue
            r = rows[t]
            gs[nd] += g[r]
            hs[nd] += h[r]
            cs[nd] += 1
        for nd in range(n_lvl):
            if cs[nd] > 0:
                val[first + nd] = -gs[nd] / (hs[nd] + lam)
        if level == depth:
            break
        hg = np.zeros((n_lvl, n_feat, n_bins))
        hh = np.zeros((n_lvl, n_feat, n_bins))
        hc = np.zeros((n_lvl, n_feat, n_bins), dtype=np.int64)
        for t in range(len(rows)):
            nd = node_of[t] - first
            if nd < 0 or nd >= n_lvl:
                continue
            r = rows[t]
            gr = g[r]
            hr = h[r]
            for f in range(n_feat):
                if colmask[f]:
                    b = xb[f, r]
                    hg[nd, f, b] += gr
                    hh[nd, f, b] += hr
                    hc[nd, f, b] += 1
        for nd in range(n_lvl):
            if cs[nd] < 2 * min_leaf:
                continue
            gt = gs[nd]
            ht = hs[nd]
            base = gt * gt / (ht + lam)
            best_gain = min_gain
            best_f = -1
            best_b = 0
            for f in range(n_feat):
                if not colmask[f]:
                    continue
                gl = 0.0
                hl = 0.0
                cl = 0
                for b in range(n_bins - 1):
                    gl += hg[nd, f, b]
                    hl += hh[nd, f, b]
                    cl += hc[nd, f, b]
                    cr = cs[nd] - cl
                    if cl < min_leaf or cr < min_leaf:
                        continue
                    gr_ = gt - gl
                    hr_ = ht - hl
                    gain = gl * gl / (hl + lam) + gr_ * gr_ / (hr_ + lam) - base
                    if gain > best_gain:
                        best_gain = gain
                        best_f = f
                        best_b = b
            if best_f >= 0:
                k = first + nd
                feat[k] = best_f
                thr[k] = best_b
        # route rows one level down
        for t in range(len(rows)):
            k = node_of[t]
            if k < n_nodes and feat[k] >= 0 and k >= first:
                r = rows[t]
                node_of[t] = 2 * k + 1 if xb[feat[k], r] <= thr[k] else 2 * k + 2
        # rows in nodes that did not split keep node_of (they are out of range for the next level)


@njit(cache=True)
def _tree_predict(xb: np.ndarray, feat: np.ndarray, thr: np.ndarray, val: np.ndarray) -> np.ndarray:
    n = xb.shape[1]
    out = np.empty(n)
    for r in range(n):
        k = 0
        while feat[k] >= 0:
            k = 2 * k + 1 if xb[feat[k], r] <= thr[k] else 2 * k + 2
        out[r] = val[k]
    return out


class GBDT:
    """Gradient-boosted depth-<=3 trees.  ``loss`` = 'logistic' (binary y) or 'squared'.

    Scores are margins (log-odds for 'logistic').  Deterministic given ``seed``."""

    def __init__(self, loss: str = "logistic", n_trees: int = 200, lr: float = 0.05, depth: int = 3,
                 subsample: float = 0.5, colsample: float = 0.7, min_leaf: int = 100, lam: float = 10.0,
                 patience: int = 20, seed: int = 0) -> None:
        if loss not in ("logistic", "squared"):
            raise ValueError(loss)
        if not 1 <= depth <= MAX_DEPTH:
            raise ValueError(f"depth must be in [1, {MAX_DEPTH}]")
        self.loss, self.n_trees, self.lr, self.depth = loss, n_trees, lr, depth
        self.subsample, self.colsample, self.min_leaf, self.lam = subsample, colsample, min_leaf, lam
        self.patience, self.seed = patience, seed

    # -- binning
    def _fit_bins(self, x: np.ndarray) -> None:
        self.edges: list[np.ndarray] = []
        qs = np.linspace(0, 1, N_BINS + 1)[1:-1]
        for j in range(x.shape[1]):
            col = x[:, j]
            e = np.unique(np.quantile(col[np.isfinite(col)], qs)) if np.isfinite(col).any() else np.zeros(0)
            self.edges.append(e)

    def _bin(self, x: np.ndarray) -> np.ndarray:
        out = np.empty((x.shape[1], x.shape[0]), dtype=np.uint8)
        for j, e in enumerate(self.edges):
            col = np.where(np.isfinite(x[:, j]), x[:, j], self.fill[j])
            out[j] = np.searchsorted(e, col, side="right")
        return out

    def _grad(self, y: np.ndarray, m: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.loss == "logistic":
            p = sigmoid(m)
            return p - y, np.maximum(p * (1.0 - p), 1e-6)
        return m - y, np.ones_like(y)

    def _loss(self, y: np.ndarray, m: np.ndarray) -> float:
        if self.loss == "logistic":
            return float(np.mean(np.logaddexp(0.0, m) - y * m))
        return float(np.mean((m - y) ** 2))

    def fit(self, x: np.ndarray, y: np.ndarray, x_val: np.ndarray | None = None,
            y_val: np.ndarray | None = None) -> GBDT:
        rng = np.random.default_rng(self.seed)
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        self.fill = np.array([np.nanmedian(x[:, j]) if np.isfinite(x[:, j]).any() else 0.0 for j in range(x.shape[1])])
        self._fit_bins(np.where(np.isfinite(x), x, self.fill))
        xb = self._bin(x)
        n, p = x.shape
        if self.loss == "logistic":
            mean = float(np.clip(y.mean(), 1e-4, 1 - 1e-4))
            self.base = float(np.log(mean / (1 - mean)))
        else:
            self.base = float(y.mean())
        m = np.full(n, self.base)
        use_val = x_val is not None and len(x_val) > 0
        if use_val:
            xvb = self._bin(np.asarray(x_val, dtype=np.float64))
            yv = np.asarray(y_val, dtype=np.float64)
            mv = np.full(len(yv), self.base)
            best, best_it = self._loss(yv, mv), 0
        feats, thrs, vals = [], [], []
        k_rows = max(int(self.subsample * n), 1)
        for it in range(self.n_trees):
            g, h = self._grad(y, m)
            rows = np.sort(rng.choice(n, size=k_rows, replace=False)).astype(np.int64)
            cmask = rng.random(p) < self.colsample
            if not cmask.any():
                cmask[rng.integers(p)] = True
            feat = np.empty(N_NODES, dtype=np.int64)
            thr = np.empty(N_NODES, dtype=np.int64)
            val = np.empty(N_NODES)
            _grow_tree(xb, g, h, rows, cmask, self.depth, self.lam, self.min_leaf, 1e-9, N_BINS, feat, thr, val)
            val = val * self.lr
            feats.append(feat)
            thrs.append(thr)
            vals.append(val)
            m += _tree_predict(xb, feat, thr, val)
            if use_val:
                mv += _tree_predict(xvb, feat, thr, val)
                cur = self._loss(yv, mv)
                if cur < best - 1e-9:
                    best, best_it = cur, it + 1
                elif it + 1 - best_it >= self.patience:
                    break
        keep = best_it if use_val else len(feats)
        keep = max(keep, 1) if not use_val else keep
        self.feats, self.thrs, self.vals = feats[:keep], thrs[:keep], vals[:keep]
        self.n_used = len(self.feats)
        return self

    def decision_function(self, x: np.ndarray) -> np.ndarray:
        xb = self._bin(np.asarray(x, dtype=np.float64))
        m = np.full(xb.shape[1], self.base)
        for f, t, v in zip(self.feats, self.thrs, self.vals, strict=True):
            m += _tree_predict(xb, f, t, v)
        return m

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        return sigmoid(self.decision_function(x))

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.decision_function(x)


__all__ = ("GBDT", "LogisticL2", "PlattScaler", "Preprocessor", "Ridge", "logit", "sigmoid")
