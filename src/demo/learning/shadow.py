# ruff: noqa: E501
"""SHADOW predictor / trainer. Nothing here influences a live decision (champion = static demo policy).

Runner integration (import lazily, only from shadow/analysis tooling, never from the hot path):

    predictor, trainer = build_shadow(model_dir="artifacts/demo_models", seed=0)
    ...
    # per opportunity, AFTER the snapshot is persisted and BEFORE the decision:
    preds = predictor.predict(snapshot)                      # {model_name: prediction dict}
    for name, pred in preds.items():
        store.record_shadow_prediction(snapshot.opportunity_id, name, pred)
    # or simply: predictor.predict_and_store(snapshot, store)
    ...
    # periodically / on close (never inside the decision path):
    report = trainer.update(store)

Untrained / unavailable / incompatible models yield explicit ``None`` probabilities with a
``status`` field, never a fake number. Prediction failures are contained (status ``error``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from demo.learning.dataset import Dataset, build_dataset, target_hit
from demo.learning.features import FEATURE_VERSION, extract_features, feature_vector
from demo.learning.models import BatchModel
from demo.learning.online import OnlineChallenger, OnlineLearningError
from demo.learning.validation import (
    ValidationError,
    chronological_folds,
    cross_validate,
)
from demo.store import DemoStore, parse_utc

BATCH_MODELS = ("logreg_baseline", "lgbm_batch")
ONLINE_MODEL = "river_online"
ROLE = "challenger"


@dataclass
class ModelSet:
    logreg: BatchModel = field(default_factory=lambda: BatchModel("logreg_baseline"))
    lgbm: BatchModel = field(default_factory=lambda: BatchModel("lgbm_batch"))
    online: OnlineChallenger = field(default_factory=OnlineChallenger)
    versions: dict[str, str | None] = field(default_factory=dict)
    trained_rows: int = 0

    def batch(self) -> dict[str, BatchModel]:
        return {"logreg_baseline": self.logreg, "lgbm_batch": self.lgbm}


# ---------------------------------------------------------------------------------------------
# versioned persistence (local files only; pickles are only ever read from our own directory)
# ---------------------------------------------------------------------------------------------
def _dir(model_dir: Path, name: str) -> Path:
    d = Path(model_dir) / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _next_version(d: Path, suffix: str) -> str:
    n = 1 + max([int(p.stem[1:]) for p in d.glob(f"v*{suffix}") if p.stem[1:].isdigit()], default=0)
    return f"v{n:04d}"


def save_model(model_dir: Path, name: str, obj: Any, meta: dict[str, Any]) -> str:
    import joblib

    d = _dir(model_dir, name)
    ver = _next_version(d, ".joblib")
    joblib.dump(obj, d / f"{ver}.joblib")
    (d / f"{ver}.json").write_text(json.dumps({**meta, "version": ver}, sort_keys=True, default=str))
    return ver


def load_latest(model_dir: Path, name: str) -> tuple[Any, str] | None:
    import joblib

    d = Path(model_dir) / name
    files = sorted(d.glob("v*.joblib")) if d.exists() else []
    if not files:
        return None
    return joblib.load(files[-1]), files[-1].stem


def load_model_set(model_dir: str | Path | None) -> ModelSet:
    ms = ModelSet()
    if model_dir is None:
        return ms
    for name in BATCH_MODELS:
        got = load_latest(Path(model_dir), name)
        if got is not None:
            obj, ver = got
            if getattr(obj, "feature_version", None) == FEATURE_VERSION:
                setattr(ms, "logreg" if name == "logreg_baseline" else "lgbm", obj)
                ms.versions[name] = ver
    got = load_latest(Path(model_dir), ONLINE_MODEL)
    if got is not None and getattr(got[0], "feature_version", None) == FEATURE_VERSION:
        ms.online, ms.versions[ONLINE_MODEL] = got[0], got[1]
    return ms


# ---------------------------------------------------------------------------------------------
# predictor
# ---------------------------------------------------------------------------------------------
def _null_pred(name: str, status: str, version: str | None, heads: tuple[str, ...]) -> dict[str, Any]:
    p: dict[str, Any] = {"model": name, "role": ROLE, "feature_version": FEATURE_VERSION,
                         "model_version": version, "status": status}
    for h in heads:
        p[_key(h)] = None
    return p


def _key(head: str) -> str:
    return {"target_before_stop": "p_target_before_stop", "net_positive": "p_net_positive",
            "expected_r": "expected_r"}[head]


class ShadowPredictor:
    def __init__(self, models: ModelSet | None = None, model_dir: str | Path | None = None) -> None:
        self.models = models if models is not None else load_model_set(model_dir)

    def predict(self, snapshot: Any, predicted_utc: str | None = None) -> dict[str, dict[str, Any]]:
        """{model_name: prediction dict}. Pure w.r.t. the store; registers a River `pending` entry."""
        feats = extract_features(snapshot)  # raises LeakageGuardError for non-snapshots
        vec = feature_vector(feats)
        now = predicted_utc or datetime.now(UTC).isoformat()
        out: dict[str, dict[str, Any]] = {}
        for name, model in self.models.batch().items():
            heads = tuple(model.heads) or ("target_before_stop", "net_positive")
            ver = self.models.versions.get(name)
            try:
                if not model.trained or model.feature_version != FEATURE_VERSION:
                    out[name] = _null_pred(name, "untrained", ver, heads)
                    continue
                pred = _null_pred(name, "ok", ver, tuple(model.heads))
                for h, v in model.predict_row(vec).items():
                    pred[_key(h)] = v
                out[name] = pred
            except Exception as exc:  # shadow must never break the runner
                out[name] = {**_null_pred(name, "error", ver, heads), "error": type(exc).__name__}
        on = self.models.online
        try:
            p = on.predict_one(snapshot.opportunity_id, feats, now)
            pred = _null_pred(ONLINE_MODEL, "ok" if p is not None else "untrained",
                              self.models.versions.get(ONLINE_MODEL), ("target_before_stop",))
            pred["p_target_before_stop"] = p
            out[ONLINE_MODEL] = pred
        except Exception as exc:
            out[ONLINE_MODEL] = {**_null_pred(ONLINE_MODEL, "error", None, ("target_before_stop",)),
                                 "error": type(exc).__name__}
        return out

    def predict_and_store(self, snapshot: Any, store: DemoStore) -> dict[str, dict[str, Any]]:
        preds = self.predict(snapshot)
        for name, pred in preds.items():
            store.record_shadow_prediction(snapshot.opportunity_id, name, pred)
        return preds


# ---------------------------------------------------------------------------------------------
# trainer
# ---------------------------------------------------------------------------------------------
class ShadowTrainer:
    def __init__(
        self,
        models: ModelSet,
        model_dir: str | Path | None = None,
        *,
        seed: int = 0,
        min_rows: int = 30,
        retrain_every: int = 10,
        include_counterfactual: bool = False,
        cf_weight: float = 0.5,
        phase: str | None = None,
        tracker: Any | None = None,
    ) -> None:
        self.models = models
        self.model_dir = None if model_dir is None else Path(model_dir)
        self.seed, self.min_rows, self.retrain_every = seed, min_rows, retrain_every
        self.include_cf, self.cf_weight, self.phase = include_counterfactual, cf_weight, phase
        self.tracker = tracker

    def update(self, store: DemoStore) -> dict[str, Any]:
        ds = build_dataset(store, phase=self.phase, include_counterfactual=self.include_cf,
                           cf_weight=self.cf_weight)
        report: dict[str, Any] = {"n_rows": len(ds), "n_cf": int(ds.is_cf.sum()) if len(ds) else 0,
                                  "feature_version": FEATURE_VERSION, "batch": {}}
        need = len(ds) >= self.min_rows and (
            not (self.models.logreg.trained or self.models.lgbm.trained)
            or len(ds) - self.models.trained_rows >= self.retrain_every
        )
        if need:
            report["batch"] = self._retrain(ds)
        elif len(ds) < self.min_rows:
            report["batch"] = {"skipped": f"fewer than {self.min_rows} closed rows"}
        report["online"] = self._feed_online(store)
        return report

    # -- batch ---------------------------------------------------------------------------------
    def _retrain(self, ds: Dataset) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, model in self.models.batch().items():
            model.seed = self.seed
            model.fit(ds)
            model.meta = {"window": ds.window(), "n_rows": len(ds), "seed": self.seed,
                          "include_counterfactual": self.include_cf}
            cv: dict[str, Any] = {}
            try:
                folds = chronological_folds(ds)
                for head in model.heads:
                    if model.heads[head] is None:
                        continue
                    res = cross_validate(lambda n=name, h=head: _factory(n, h, self.seed), ds, head, folds)
                    res.pop("oof", None)
                    cv[head] = res
            except ValidationError as exc:
                cv["skipped"] = str(exc)
            model.meta["cv"] = cv
            ver = None
            if self.model_dir is not None:
                ver = save_model(self.model_dir, name, model, model.meta)
                self.models.versions[name] = ver
            if self.tracker is not None and model.trained:
                self._track(name, ver or "unsaved", ds, cv)
            out[name] = {"trained": model.trained, "version": ver,
                         "heads": {h: (m is not None) for h, m in model.heads.items()}}
        self.models.trained_rows = len(ds)
        return out

    def _track(self, name: str, ver: str, ds: Dataset, cv: dict[str, Any]) -> None:
        metrics: dict[str, Any] = {"n_rows": len(ds)}
        for head, res in cv.items():
            cal = res.get("calibration") if isinstance(res, dict) else None
            if cal:
                metrics.update({f"{head}_auc": cal["auc"], f"{head}_brier": cal["brier"],
                                f"{head}_ece": cal["ece"], f"{head}_log_loss": cal["log_loss"]})
        self.tracker.log_run(model_name=name, model_version=ver, feature_version=FEATURE_VERSION,
                             window=ds.window(), params={"seed": self.seed, "cf": self.include_cf},
                             metrics=metrics, role="challenger")

    # -- online --------------------------------------------------------------------------------
    def _feed_online(self, store: DemoStore) -> dict[str, Any]:
        on = self.models.online
        priors = {p["opportunity_id"]: p for p in store.list_shadow_predictions()
                  if p["model_name"] == ONLINE_MODEL}
        items: list[tuple[str, bool, str]] = []  # (opportunity_id, y, available_utc)
        for _iid, opp, out in store.list_outcomes(self.phase):
            items.append((opp, target_hit(out), out.closed_utc))
        if self.include_cf:
            for lab in store.list_counterfactuals(self.phase):
                if lab.target_before_stop is not None:
                    items.append((lab.opportunity_id, bool(lab.target_before_stop),
                                  max((lab.horizon_end_utc, lab.labelled_utc), key=parse_utc)))
        items.sort(key=lambda t: (t[2], t[0]))
        learned = skipped = 0
        for opp, y, avail in items:
            if opp in on.learned_ids:
                continue
            prior = priors.get(opp)
            if prior is None or not avail:  # never learn without a stored prior prediction
                skipped += 1
                continue
            if opp not in on.pending:
                snap = store.get_snapshot(opp)
                if snap is None:
                    skipped += 1
                    continue
                on.register_prior(opp, extract_features(snap), prior["prediction"].get("p_target_before_stop"),
                                  prior["created_utc"])
            try:
                on.learn_one(opp, y, avail)
                learned += 1
            except OnlineLearningError:
                skipped += 1
        ver = None
        if self.model_dir is not None and learned:
            ver = save_model(self.model_dir, ONLINE_MODEL, on, {"n_learned": on.n_learned})
            self.models.versions[ONLINE_MODEL] = ver
        return {"learned": learned, "skipped_no_prior": skipped, "version": ver, **on.metrics()}


def _factory(name: str, head: str, seed: int):
    from demo.learning.models import make_learner

    return make_learner(name, head, seed)


def build_shadow(model_dir: str | Path | None = None, seed: int = 0, **trainer_kwargs: Any
                 ) -> tuple[ShadowPredictor, ShadowTrainer]:
    models = load_model_set(model_dir)
    return (ShadowPredictor(models),
            ShadowTrainer(models, model_dir, seed=seed, **trainer_kwargs))
