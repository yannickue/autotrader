# ruff: noqa: E501
"""River online challenger with prequential (predict-then-learn) discipline enforced by the API.

  * `predict_one(opportunity_id, features, predicted_utc)` stores a `pending` entry (features + the
    prediction that was made) BEFORE any label is known.
  * `learn_one(opportunity_id, y, outcome_available_utc)` learns ONLY from the stored pending entry's
    features (the caller cannot pass different ones) and raises `OnlineLearningError` when there is
    no stored prior prediction, when the id was already learned, or when the outcome is stamped
    earlier than the prediction.
  * Streaming metrics (rolling log-loss / Brier / AUC) are scored on the stored prior prediction;
    an ADWIN detector on the absolute error emits drift events.

SHADOW ONLY: nothing here influences a live decision.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from demo.learning.features import FEATURE_NAMES, FEATURE_VERSION
from demo.learning.validation import auc, brier, log_loss
from demo.store import parse_utc


class OnlineLearningError(RuntimeError):
    pass


@dataclass
class _Pending:
    features: dict[str, float]
    p: float | None
    predicted_ts: float


@dataclass
class OnlineChallenger:
    name: str = "river_online"
    min_samples: int = 20  # below this (or with a single class seen) the prediction is an explicit None
    window: int = 200
    adwin_delta: float = 0.002
    feature_version: str = FEATURE_VERSION
    pending: dict[str, _Pending] = field(default_factory=dict)
    learned_ids: set[str] = field(default_factory=set)
    n_learned: int = 0
    class_counts: list[int] = field(default_factory=lambda: [0, 0])
    drift_events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        from river import compose, drift, linear_model, optim, preprocessing

        self._model = compose.Pipeline(
            preprocessing.StandardScaler(),
            linear_model.LogisticRegression(optimizer=optim.SGD(0.05), l2=0.001),
        )
        self._adwin = drift.ADWIN(delta=self.adwin_delta)
        self._recent: deque[tuple[float, int]] = deque(maxlen=self.window)
        self._all_y: list[int] = []
        self._all_p: list[float] = []

    # -- helpers -------------------------------------------------------------------------------
    @staticmethod
    def _clean(features: dict[str, float]) -> dict[str, float]:
        return {k: (0.0 if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v))
                for k, v in features.items() if k in FEATURE_NAMES}

    @property
    def ready(self) -> bool:
        return self.n_learned >= self.min_samples and min(self.class_counts) > 0

    # -- prequential API -----------------------------------------------------------------------
    def predict_one(self, opportunity_id: str, features: dict[str, float], predicted_utc: str) -> float | None:
        if opportunity_id in self.learned_ids:
            raise OnlineLearningError(f"{opportunity_id} already learned; cannot predict again")
        if opportunity_id in self.pending:
            return self.pending[opportunity_id].p  # idempotent: the first prediction stands
        x = self._clean(features)
        p = float(self._model.predict_proba_one(x).get(True, 0.5)) if self.ready else None
        self.pending[opportunity_id] = _Pending(x, p, parse_utc(predicted_utc).timestamp())
        return p

    def register_prior(self, opportunity_id: str, features: dict[str, float], p: float | None,
                       predicted_utc: str) -> None:
        """Re-create a pending entry from a STORED prior prediction (restart recovery). The
        probability is the one that was persisted before the decision, never a recomputed one."""
        if opportunity_id in self.learned_ids or opportunity_id in self.pending:
            return
        self.pending[opportunity_id] = _Pending(self._clean(features), p, parse_utc(predicted_utc).timestamp())

    def learn_one(self, opportunity_id: str, y: bool, outcome_available_utc: str) -> None:
        if opportunity_id in self.learned_ids:
            raise OnlineLearningError(f"{opportunity_id} was already learned")
        pend = self.pending.get(opportunity_id)
        if pend is None:
            raise OnlineLearningError(f"no stored prior prediction for {opportunity_id}: predict_one first")
        avail = parse_utc(outcome_available_utc).timestamp()
        if avail < pend.predicted_ts:
            raise OnlineLearningError("outcome available before the prediction was made")
        yi = int(bool(y))
        if pend.p is not None:  # score the PRIOR prediction, then learn
            self._recent.append((pend.p, yi))
            self._all_p.append(pend.p)
            self._all_y.append(yi)
            self._adwin.update(abs(pend.p - yi))
            if self._adwin.drift_detected:
                self.drift_events.append({"n_learned": self.n_learned + 1, "opportunity_id": opportunity_id,
                                          "utc": outcome_available_utc})
        self._model.learn_one(pend.features, bool(yi))
        self.class_counts[yi] += 1
        self.n_learned += 1
        self.learned_ids.add(opportunity_id)
        del self.pending[opportunity_id]

    # -- metrics -------------------------------------------------------------------------------
    def metrics(self) -> dict[str, Any]:
        def _m(pairs_p: list[float], pairs_y: list[int]) -> dict[str, Any]:
            if not pairs_y:
                return {"n": 0, "log_loss": None, "brier": None, "auc": None}
            p, y = np.asarray(pairs_p), np.asarray(pairs_y)
            a = auc(y, p)
            return {"n": len(y), "log_loss": log_loss(y, p), "brier": brier(y, p),
                    "auc": None if math.isnan(a) else a}

        rp = [p for p, _ in self._recent]
        ry = [y for _, y in self._recent]
        return {"rolling": _m(rp, ry), "cumulative": _m(self._all_p, self._all_y),
                "n_learned": self.n_learned, "n_pending": len(self.pending),
                "n_drift_events": len(self.drift_events)}
