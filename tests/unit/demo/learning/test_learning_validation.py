# ruff: noqa: E501
import dataclasses
from itertools import pairwise

import numpy as np
import pytest
from learning_helpers import seed_store

from demo.learning.dataset import build_dataset
from demo.learning.models import LGBMLearner, LogisticLearner
from demo.learning.validation import (
    Fold,
    ValidationError,
    assert_chronological,
    auc,
    brier,
    calibration_report,
    chronological_folds,
    cross_validate,
    ece,
    permutation_baseline,
    selection_evidence,
)
from demo.store import DemoStore


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    st = DemoStore(tmp_path_factory.mktemp("v") / "s.db")
    seed_store(st, 120, seed=5)
    return build_dataset(st)


def test_folds_have_no_future_rows_in_train(ds):
    folds = chronological_folds(ds, n_folds=4, embargo_days=1)
    assert len(folds) == 4
    for f in folds:
        assert ds.ts[f.train].max() < ds.ts[f.test].min()
        assert ds.label_ts[f.train].max() < ds.ts[f.test].min()
        assert ds.day[f.train].max() <= f.test_start_day - 1 - 1  # embargo of one full day
        assert not set(f.train) & set(f.test)
    # expanding window: train sets only grow, test blocks are disjoint and ordered
    sizes = [len(f.train) for f in folds]
    assert sizes == sorted(sizes)
    for a, b in pairwise(folds):
        assert a.test_end_day <= b.test_start_day


def test_embargo_must_be_positive(ds):
    with pytest.raises(ValidationError):
        chronological_folds(ds, embargo_days=0)
    with pytest.raises(ValidationError):
        chronological_folds(ds, n_folds=500)


def test_tampered_fold_is_detected(ds):
    f = chronological_folds(ds, n_folds=3)[0]
    bad = dataclasses.replace(f, train=np.concatenate([f.train, f.test[:1]]))
    with pytest.raises(AssertionError):
        assert_chronological([bad], ds)
    future = Fold(0, np.array([len(ds) - 1]), f.test, f.test_start_day, f.test_end_day, f.embargo_days)
    with pytest.raises(AssertionError):
        assert_chronological([future], ds)


def test_cross_validate_oof_only_on_test_rows_and_learns_signal(ds):
    folds = chronological_folds(ds, n_folds=4)
    res = cross_validate(lambda: LogisticLearner(seed=0), ds, "target_before_stop", folds)
    oof = res["oof"]
    test_rows = np.concatenate([f.test for f in folds])
    assert set(np.flatnonzero(np.isfinite(oof))) <= set(test_rows)
    assert res["calibration"]["auc"] > 0.8  # quality drives the label in the synthetic data
    res_r = cross_validate(lambda: LGBMLearner("reg", seed=0), ds, "expected_r", folds)
    assert res_r["n_oof"] > 0 and res_r["rmse"] >= 0


def test_metrics_basic():
    y = np.array([0, 0, 1, 1])
    assert auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0
    assert np.isnan(auc(np.ones(4), np.arange(4)))
    assert brier(y, y.astype(float)) == 0.0
    rng = np.random.default_rng(0)
    p = rng.random(20000)
    yy = (rng.random(20000) < p).astype(float)
    assert ece(yy, p) < 0.02  # calibrated
    assert ece(yy, np.clip(p * 0.5, 0, 1)) > 0.1  # miscalibrated
    rep = calibration_report(yy, p)
    assert rep["brier"] < rep["brier_base_rate"]


def test_permutation_baseline_separates_signal_from_noise():
    rng = np.random.default_rng(1)
    y = (rng.random(300) < 0.5).astype(float)
    good = y + rng.normal(0, 0.6, 300)
    noise = rng.random(300)
    assert permutation_baseline(y, good, 100, seed=0)["p_value"] < 0.05
    assert permutation_baseline(y, noise, 100, seed=0)["p_value"] > 0.05
    assert permutation_baseline(y, good, 50, seed=3) == permutation_baseline(y, good, 50, seed=3)


def test_selection_evidence_real_rows_only(ds):
    folds = chronological_folds(ds, n_folds=3)
    oof = cross_validate(lambda: LogisticLearner(seed=0), ds, "target_before_stop", folds)["oof"]
    ev = selection_evidence(ds, oof, 0.5)
    assert ev["n_selected"] > 0 and ev["net_expectancy_r"] > ev["baseline_expectancy_r"]
    assert ev["max_drawdown_r"] >= 0
    assert 0 < ev["max_market_share"] <= 1
