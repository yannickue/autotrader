# ruff: noqa: E501
"""Batch challengers: regularised LogisticRegression baseline and LightGBM meta-label challenger.

SHADOW ONLY. sklearn / lightgbm are imported lazily inside constructors so importing this module
is cheap and never happens on the trading hot path. All randomness is seeded and single-threaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from demo.learning.dataset import Dataset, head_arrays
from demo.learning.features import FEATURE_VERSION

HEADS = ("target_before_stop", "net_positive", "expected_r")
MIN_CLASS_ROWS = 3


class LogisticLearner:
    """Imputer -> scaler -> L2 LogisticRegression with class balancing."""

    def __init__(self, c: float = 0.5, seed: int = 0) -> None:
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        self.pipe = Pipeline([
            ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(C=c, class_weight="balanced", max_iter=500, random_state=seed)),
        ])

    def fit(self, X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> LogisticLearner:
        self.pipe.fit(X, y.astype(int), clf__sample_weight=w)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return self.pipe.predict_proba(X)[:, 1]


class LGBMLearner:
    """Small, regularised, deterministic LightGBM (classifier or regressor)."""

    def __init__(self, kind: str = "clf", seed: int = 0, n_estimators: int = 80) -> None:
        import lightgbm as lgb

        params = dict(
            n_estimators=n_estimators, learning_rate=0.05, num_leaves=7, max_depth=4,
            min_child_samples=10, subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
            reg_lambda=5.0, random_state=seed, deterministic=True, force_row_wise=True,
            n_jobs=1, verbose=-1,
        )
        self.kind = kind
        self.model = lgb.LGBMClassifier(**params) if kind == "clf" else lgb.LGBMRegressor(**params)

    def fit(self, X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> LGBMLearner:
        self.model.fit(X, y.astype(int) if self.kind == "clf" else y, sample_weight=w)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.kind == "clf":
            return self.model.predict_proba(X)[:, 1]
        return self.model.predict(X)


def make_learner(name: str, head: str, seed: int = 0):
    if name == "logreg_baseline":
        if head == "expected_r":
            raise ValueError("logistic baseline has no expected_r head")
        return LogisticLearner(seed=seed)
    if name == "lgbm_batch":
        return LGBMLearner("reg" if head == "expected_r" else "clf", seed=seed)
    raise ValueError(f"unknown model {name!r}")


MODEL_HEADS: dict[str, tuple[str, ...]] = {
    "logreg_baseline": ("target_before_stop", "net_positive"),
    "lgbm_batch": HEADS,
}


@dataclass
class BatchModel:
    """A fitted batch challenger: one learner per head (None = not trainable with the data)."""

    name: str
    feature_version: str = FEATURE_VERSION
    seed: int = 0
    heads: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def trained(self) -> bool:
        return any(v is not None for v in self.heads.values())

    def fit(self, ds: Dataset) -> BatchModel:
        self.heads = {}
        for head in MODEL_HEADS[self.name]:
            idx, X, y, w = head_arrays(ds, head)
            ok = len(idx) >= 2 * MIN_CLASS_ROWS
            if ok and head != "expected_r":
                ok = min(int((y == 1).sum()), int((y == 0).sum())) >= MIN_CLASS_ROWS
            if not ok:
                self.heads[head] = None
                continue
            self.heads[head] = make_learner(self.name, head, self.seed).fit(X, y, w)
        return self

    def predict_row(self, x: list[float]) -> dict[str, float | None]:
        """Per-head prediction for one feature vector; untrained head -> explicit None."""
        arr = np.asarray([x], dtype=np.float64)
        out: dict[str, float | None] = {}
        for head in MODEL_HEADS[self.name]:
            m = self.heads.get(head)
            out[head] = None if m is None else float(m.predict(arr)[0])
        return out
