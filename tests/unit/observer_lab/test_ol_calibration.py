from __future__ import annotations

import numpy as np
import pytest

from coverage_analysis.observer_lab import calibration as C


def logit(p):
    return np.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


@pytest.fixture(scope="module")
def truth():
    rng = np.random.default_rng(11)
    p = rng.uniform(0.05, 0.95, 20000)
    y = (rng.random(p.size) < p).astype(float)
    return p, y


def test_brier_and_log_loss_known_values():
    p, y = np.array([0.8, 0.2]), np.array([1.0, 0.0])
    assert C.brier_score(p, y) == pytest.approx(0.04)
    assert C.log_loss(p, y) == pytest.approx(-np.log(0.8))
    # clipping keeps a certain-but-wrong forecast finite
    assert np.isfinite(C.log_loss(np.array([0.0]), np.array([1.0])))


def test_auc_known_and_degenerate():
    assert C.auc(np.array([0.1, 0.4, 0.35, 0.8]), np.array([0, 0, 1, 1.0])) == pytest.approx(0.75)
    assert C.auc(np.array([0.1, 0.2, 0.8, 0.9]), np.array([0, 0, 1, 1.0])) == 1.0
    assert C.auc(np.array([0.5, 0.5]), np.array([0, 1.0])) == 0.5  # ties count half
    assert np.isnan(C.auc(np.array([0.1, 0.2]), np.array([1.0, 1.0])))


def test_slope_intercept_calibrated_overconfident_and_shifted(truth):
    p, y = truth
    a, b = C.calibration_slope_intercept(p, y)
    assert abs(a) < 0.08 and abs(b - 1) < 0.08
    a2, b2 = C.calibration_slope_intercept(sigmoid(2 * logit(p)), y)  # overconfident forecaster: true slope 0.5
    assert b2 == pytest.approx(0.5, abs=0.06) and abs(a2) < 0.08
    a3, b3 = C.calibration_slope_intercept(sigmoid(logit(p) + 1.0), y)  # over-forecasting by 1 logit unit
    assert a3 == pytest.approx(-1.0, abs=0.08) and b3 == pytest.approx(1.0, abs=0.08)


def test_slope_intercept_is_safe_for_separation_and_single_class():
    p = np.array([0.1, 0.2, 0.8, 0.9] * 5)
    y = np.array([0, 0, 1, 1.0] * 5)  # perfectly separable: finite thanks to the ridge
    a, b = C.calibration_slope_intercept(p, y)
    assert np.isfinite(a) and np.isfinite(b) and b > 1
    a, b = C.calibration_slope_intercept(np.array([0.3, 0.4]), np.array([1.0, 1.0]))
    assert np.isnan(a) and np.isnan(b)


def test_reliability_table_equal_count_and_ece(truth):
    p, y = truth
    tab = C.reliability_table(p, y, n_bins=10)
    assert len(tab) == 10 and max(r["n"] for r in tab) - min(r["n"] for r in tab) <= 1
    assert sum(r["n"] for r in tab) == len(p)
    assert all(r["mean_p"] <= r2["mean_p"] for r, r2 in zip(tab, tab[1:], strict=False))
    assert all(r["ci_low"] <= r["mean_y"] <= r["ci_high"] for r in tab)
    assert C.expected_calibration_error(p, y) < 0.03
    assert C.expected_calibration_error(sigmoid(logit(p) + 1.0), y) > 0.1
    # hand-checkable: two bins
    t2 = C.reliability_table(np.array([0.1, 0.2, 0.8, 0.9]), np.array([0, 1, 1, 1.0]), n_bins=2)
    assert [r["n"] for r in t2] == [2, 2] and t2[0]["mean_y"] == 0.5 and t2[1]["mean_y"] == 1.0
    assert C.expected_calibration_error(np.array([0.1, 0.2, 0.8, 0.9]), np.array([0, 1, 1, 1.0]), n_bins=2) == pytest.approx(
        0.5 * abs(0.15 - 0.5) + 0.5 * abs(0.85 - 1.0)
    )


def test_evaluate_forecast_block_bootstrap_intervals(truth):
    p, y = (a[:6000] for a in truth)
    day = np.arange(len(p)) // 20  # 300 day blocks of 20 rows
    out = C.evaluate_forecast(p, y, day, B=100, seed=3)
    for k in ("brier", "log_loss", "ece", "slope", "intercept", "auc"):
        assert out[k]["ci_low"] <= out[k]["value"] <= out[k]["ci_high"], k
    assert out["slope"]["ci_low"] < 1.0 < out["slope"]["ci_high"] or abs(out["slope"]["value"] - 1) < 0.1
    again = C.evaluate_forecast(p, y, day, B=100, seed=3)
    assert again["brier"] == out["brier"]
    assert out["n"] == len(p) and out["n_blocks"] == 300
    assert out["auc"]["role"] == "supplementary"


def test_brier_skill_vs_baseline_detects_informative_forecast(truth):
    p, y = (a[:6000] for a in truth)
    day = np.arange(len(p)) // 20
    base = np.full_like(p, y.mean())
    r = C.brier_skill_vs_baseline(p, y, base, day, B=200, seed=1)
    assert r["improvement"] > 0.03 and r["ci_low"] > 0  # model beats the constant base rate
    noise = np.random.default_rng(5).uniform(0.05, 0.95, len(p))
    r2 = C.brier_skill_vs_baseline(noise, y, base, day, B=200, seed=1)
    assert r2["improvement"] < 0 and r2["ci_high"] < 0
