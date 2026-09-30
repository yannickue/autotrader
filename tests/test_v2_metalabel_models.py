# ruff: noqa: E501
"""Own models: planted linear signal, noise, GBDT XOR vs logistic, ridge, calibration correctness, determinism."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.metalabel import metrics
from alpha.metalabel.models import (
    GBDT,
    LogisticL2,
    PlattScaler,
    Preprocessor,
    Ridge,
    logit,
    sigmoid,
)


def _split(n=24000, p=12, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, p))
    return rng, x, slice(0, 16000), slice(16000, n)


def test_logistic_recovers_planted_linear_signal():
    rng, x, tr, te = _split()
    y = (rng.random(len(x)) < sigmoid(1.2 * x[:, 0] - 0.8 * x[:, 1] + 0.4 * x[:, 2])).astype(float)
    m = LogisticL2(10.0).fit(x[tr], y[tr])
    assert metrics.auc(y[te], m.decision_function(x[te])) > 0.8
    assert m.beta[1] > 0.8 and m.beta[2] < -0.5  # signs / magnitudes recovered


def test_logistic_on_noise_is_near_chance():
    aucs = []
    for seed in range(4):
        rng, x, tr, te = _split(seed=seed)
        y = (rng.random(len(x)) < 0.4).astype(float)
        aucs.append(metrics.auc(y[te], LogisticL2(10.0).fit(x[tr], y[tr]).decision_function(x[te])))
    assert 0.47 < float(np.mean(aucs)) < 0.53 and max(abs(a - 0.5) for a in aucs) < 0.04


def test_gbdt_learns_xor_interaction_logistic_cannot():
    rng, x, tr, te = _split()
    y = ((x[:, 0] > 0) ^ (x[:, 1] > 0)).astype(float)
    y = np.where(rng.random(len(y)) < 0.10, 1 - y, y)
    lg = LogisticL2(10.0).fit(x[tr], y[tr])
    gb = GBDT(seed=1).fit(x[tr], y[tr], x[12000:16000], y[12000:16000])
    a_lg = metrics.auc(y[te], lg.decision_function(x[te]))
    a_gb = metrics.auc(y[te], gb.decision_function(x[te]))
    assert a_lg < 0.6 and a_gb > 0.8


def test_gbdt_is_deterministic_and_respects_depth_limit():
    rng, x, tr, te = _split(n=8000)
    y = (rng.random(len(x)) < sigmoid(x[:, 0])).astype(float)
    a = GBDT(seed=3, n_trees=30).fit(x[tr], y[tr]).decision_function(x[te])
    b = GBDT(seed=3, n_trees=30).fit(x[tr], y[tr]).decision_function(x[te])
    assert np.array_equal(a, b)
    with pytest.raises(ValueError):
        GBDT(depth=4)


def test_ridge_recovers_coefficients():
    rng, x, _, _ = _split(n=6000, p=5)
    beta = np.array([2.0, -1.0, 0.0, 0.5, 0.0])
    y = x @ beta + 0.3 + rng.normal(0, 0.5, len(x))
    m = Ridge(1.0).fit(x, y)
    assert np.allclose(m.coef, beta, atol=0.05) and abs(m.y0 - 0.3) < 0.05


def test_preprocessor_imputes_with_train_statistics_only():
    x = np.array([[1.0, np.nan], [2.0, 4.0], [3.0, 6.0], [np.nan, 8.0]])
    pre = Preprocessor().fit(x)
    assert np.isfinite(pre.transform(x)).all()
    z = pre.transform(np.array([[np.nan, np.nan]]))
    assert np.allclose(z, 0.0, atol=1e-9)  # NaN -> train median -> standardised ~ 0 for symmetric columns


def test_platt_scaling_repairs_overconfident_scores():
    rng = np.random.default_rng(0)
    n = 40000
    true_logit = rng.normal(0, 1.0, n)
    y = (rng.random(n) < sigmoid(true_logit)).astype(float)
    over = 3.0 * true_logit + 0.7  # overconfident and shifted scores
    cal_before = metrics.calibration(y, sigmoid(over))
    assert cal_before["slope"] < 0.5 and abs(cal_before["intercept"]) > 0.15
    sc = PlattScaler().fit(over[:20000], y[:20000])
    cal_after = metrics.calibration(y[20000:], sc.transform(over[20000:]))
    assert abs(cal_after["slope"] - 1.0) < 0.06 and abs(cal_after["intercept"]) < 0.06
    assert abs(sc.a - 1 / 3) < 0.03  # recovers the compression factor
    assert metrics.brier(y[20000:], sc.transform(over[20000:])) < metrics.brier(y[20000:], sigmoid(over[20000:]))


def test_calibration_of_perfectly_calibrated_probabilities():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.05, 0.95, 60000)
    y = (rng.random(len(p)) < p).astype(float)
    cal = metrics.calibration(y, p)
    assert abs(cal["slope"] - 1) < 0.05 and abs(cal["intercept"]) < 0.05
    assert metrics.brier(y, p) < metrics.brier(y, np.full_like(p, y.mean()))
    assert np.allclose(logit(sigmoid(np.array([-2.0, 0.0, 3.0]))), [-2.0, 0.0, 3.0], atol=1e-5)


def test_auc_ties_and_bootstrap_ci_covers_truth():
    y = np.array([0, 1, 0, 1])
    assert metrics.auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    assert metrics.auc(y, np.array([0.1, 0.9, 0.2, 0.8])) == 1.0
    rng = np.random.default_rng(0)
    day = np.repeat(np.arange(100), 30)
    s = rng.normal(size=len(day))
    yy = (rng.random(len(day)) < sigmoid(1.0 * s)).astype(float)
    lo, hi = metrics.cluster_boot_auc(yy, s, day, n_boot=200, seed=1)
    assert lo < metrics.auc(yy, s) < hi
