# ruff: noqa: E501
import pytest
from demo_factories import make_decision, make_snapshot
from learning_helpers import seed_store, snap_i

from demo.learning.features import FEATURE_VERSION, LeakageGuardError
from demo.learning.shadow import (
    ONLINE_MODEL,
    ModelSet,
    ShadowPredictor,
    build_shadow,
)
from demo.store import DemoStore, LeakageError

NAMES = ("logreg_baseline", "lgbm_batch", ONLINE_MODEL)


def prob_fields(pred):
    return {k: v for k, v in pred.items() if k.startswith("p_") or k == "expected_r"}


def test_untrained_models_yield_explicit_null_never_a_number():
    preds = ShadowPredictor().predict(make_snapshot())
    assert set(preds) == set(NAMES)
    for name, pred in preds.items():
        assert pred["status"] == "untrained" and pred["role"] == "challenger"
        assert pred["feature_version"] == FEATURE_VERSION
        fields = prob_fields(pred)
        assert fields and all(v is None for v in fields.values()), name


def test_predict_rejects_non_snapshot():
    with pytest.raises(LeakageGuardError):
        ShadowPredictor().predict(make_decision(make_snapshot()))


def test_predict_and_store_precedes_decision_and_is_immutable(tmp_path):
    snap = make_snapshot()
    store = DemoStore(tmp_path / "s.db", clock=lambda: snap.created_utc)
    store.record_snapshot(snap)
    preds = ShadowPredictor().predict_and_store(snap, store)
    rows = store.list_shadow_predictions(snap.opportunity_id)
    assert {r["model_name"] for r in rows} == set(NAMES) and len(preds) == 3
    store.record_decision(make_decision(snap, True))
    assert all(r["created_utc"] <= store.get_decision(snap.opportunity_id).decided_utc for r in rows)


def test_store_rejects_shadow_prediction_after_outcome(tmp_path):
    store = DemoStore(tmp_path / "s.db")
    seed_store(store, 1, seed=1)
    snap, _ = snap_i(0, 1)
    with pytest.raises(LeakageError):
        ShadowPredictor().predict_and_store(snap, store)  # outcome already exists


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    d = tmp_path_factory.mktemp("shadow")
    store = DemoStore(d / "s.db")
    seed_store(store, 60, seed=11, shadow_names=(ONLINE_MODEL,), shadow_p=None)
    predictor, trainer = build_shadow(d / "models", seed=4, min_rows=30)
    report = trainer.update(store)
    return d, store, predictor, trainer, report


def test_trainer_trains_versions_and_feeds_river_with_priors(trained):
    d, _store, predictor, _trainer, rep = trained
    assert rep["n_rows"] == 60 and rep["n_cf"] == 0
    assert rep["batch"]["lgbm_batch"]["trained"] and rep["batch"]["logreg_baseline"]["trained"]
    assert rep["batch"]["lgbm_batch"]["version"] == "v0001"
    assert rep["online"]["learned"] == 60 and rep["online"]["skipped_no_prior"] == 0
    for name in ("logreg_baseline", "lgbm_batch", ONLINE_MODEL):
        assert (d / "models" / name / "v0001.joblib").exists()
    assert (d / "models" / "lgbm_batch" / "v0001.json").exists()
    preds = predictor.predict(snap_i(500, 99)[0])
    for name in ("logreg_baseline", "lgbm_batch"):
        assert preds[name]["status"] == "ok" and preds[name]["p_target_before_stop"] is not None
        assert 0.0 <= preds[name]["p_target_before_stop"] <= 1.0
    assert preds["lgbm_batch"]["expected_r"] is not None
    assert preds[ONLINE_MODEL]["status"] == "ok" and 0 <= preds[ONLINE_MODEL]["p_target_before_stop"] <= 1


def test_cv_metadata_is_chronological_and_recorded(trained):
    _d, _s, predictor, _t, _r = trained
    cv = predictor.models.lgbm.meta["cv"]
    assert "target_before_stop" in cv and cv["target_before_stop"]["n_oof"] > 0
    assert "calibration" in cv["target_before_stop"]


def test_reload_from_disk_reproduces_predictions(trained):
    d, _s, predictor, _t, _r = trained
    p2, _ = build_shadow(d / "models", seed=4)
    snap = snap_i(503, 98)[0]
    a = predictor.predict(snap, "2026-10-30T00:00:00+00:00")
    b = p2.predict(snap, "2026-10-30T00:00:00+00:00")
    for name in ("logreg_baseline", "lgbm_batch"):
        assert a[name] == b[name]


def test_same_seed_same_predictions(tmp_path):
    outs = []
    for i in range(2):
        store = DemoStore(tmp_path / f"s{i}.db")
        seed_store(store, 50, seed=12, shadow_names=(ONLINE_MODEL,))
        pr, tr = build_shadow(None, seed=9, min_rows=30)
        tr.update(store)
        outs.append(pr.predict(snap_i(501, 77)[0], "2026-10-30T00:00:00+00:00"))
    assert outs[0] == outs[1]


def test_river_never_learns_without_stored_prior(tmp_path):
    store = DemoStore(tmp_path / "s.db")
    seed_store(store, 40, seed=13)  # no shadow predictions stored at all
    _p, trainer = build_shadow(None, seed=0, min_rows=100)
    rep = trainer.update(store)
    assert rep["online"]["learned"] == 0 and rep["online"]["skipped_no_prior"] == 40
    assert rep["batch"] == {"skipped": "fewer than 100 closed rows"}
    assert trainer.models.online.n_learned == 0


def test_counterfactual_rows_are_opt_in(tmp_path):
    store = DemoStore(tmp_path / "s.db")
    seed_store(store, 30, n_rejected=20, seed=14, shadow_names=(ONLINE_MODEL,))
    off = build_shadow(None, min_rows=10)[1].update(store)
    on = build_shadow(None, min_rows=10, include_counterfactual=True, cf_weight=0.3)[1].update(store)
    assert off["n_rows"] == 30 and on["n_rows"] == 50 and on["n_cf"] == 20
    assert off["online"]["learned"] == 30 and on["online"]["learned"] == 50


def test_feature_version_mismatch_is_untrained(trained):
    _d, _s, predictor, _t, _r = trained
    ms = ModelSet(logreg=predictor.models.logreg, lgbm=predictor.models.lgbm)
    ms.lgbm = type(ms.lgbm)(**{**ms.lgbm.__dict__, "feature_version": "demo-features-0"})
    pred = ShadowPredictor(ms).predict(snap_i(502, 55)[0])
    assert pred["lgbm_batch"]["status"] == "untrained"
    assert pred["lgbm_batch"]["p_target_before_stop"] is None


def test_prediction_errors_are_contained(trained, monkeypatch):
    _d, _s, predictor, _t, _r = trained
    ms = ModelSet(logreg=predictor.models.logreg, lgbm=predictor.models.lgbm)
    monkeypatch.setattr(ms.lgbm, "predict_row", lambda x: 1 / 0)
    pred = ShadowPredictor(ms).predict(snap_i(504, 56)[0])
    assert pred["lgbm_batch"]["status"] == "error" and pred["lgbm_batch"]["p_target_before_stop"] is None
    assert pred["logreg_baseline"]["status"] == "ok"


def test_trainer_logs_mlflow_lineage(tmp_path):
    from demo.learning.tracking import LearningTracker

    store = DemoStore(tmp_path / "s.db")
    seed_store(store, 40, seed=15, shadow_names=(ONLINE_MODEL,))
    tracker = LearningTracker(root=tmp_path / "mlflow", commit="cafe1234")
    _p, trainer = build_shadow(tmp_path / "m", seed=1, min_rows=30, tracker=tracker)
    trainer.update(store)
    rid = tracker.latest_run("lgbm_batch", "challenger")
    tags = tracker.get_tags(rid)
    assert tags["git_commit"] == "cafe1234" and tags["feature_version"] == FEATURE_VERSION
    assert tags["window_n"] == "40" and tags["window_start_utc"] != "None"


def test_importing_learning_modules_loads_no_ml_library():
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, 'src');"
        "import demo.learning.features, demo.learning.dataset, demo.learning.validation, "
        "demo.learning.models, demo.learning.online, demo.learning.promotion, demo.learning.shadow, "
        "demo.learning.tracking;"
        "bad = [m for m in ('sklearn', 'lightgbm', 'river', 'mlflow') if m in sys.modules];"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_hot_path_packages_do_not_import_learning_stack():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[4] / "src"
    pat = re.compile(r"^\s*(?:from|import)\s+(demo\.learning|sklearn|lightgbm|river|mlflow)\b", re.M)
    hits = []
    for pkg in ("demo/execution", "demo/opportunity", "nautilus_mt5", "risk"):
        for f in (root / pkg).rglob("*.py") if (root / pkg).exists() else []:
            if pat.search(f.read_text(encoding="utf-8")):
                hits.append(str(f))
    assert not hits
