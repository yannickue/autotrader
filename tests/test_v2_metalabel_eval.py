# ruff: noqa: E501
"""OOF pipeline, honest nulls and the verdict on planted signal vs pure noise."""

from __future__ import annotations

import numpy as np

from alpha.metalabel import cv
from alpha.metalabel import evaluate as ev

N_DAYS = 120
PER = 60


def _data(planted: bool, seed: int, p: int = 8):
    rng = np.random.default_rng(seed)
    day = np.repeat(np.arange(N_DAYS), PER)
    n = len(day)
    x = rng.normal(size=(n, p))
    if planted:
        y1 = (rng.random(n) < 1 / (1 + np.exp(-(0.9 * x[:, 0] - 0.6 * x[:, 1])))).astype(float)
    else:
        y1 = (rng.random(n) < 0.42).astype(float)
    r = np.where(y1 > 0, 1.2, -1.0) + rng.normal(0, 0.4, n)
    out = ev.Outcomes(r, y1, y1.copy())
    folds = cv.rolling_origin_folds(day, day.copy(), N_DAYS, 4, 3, 40)
    return x, out, folds, day


def _run(planted, seed, draws, kind="logit", label="y1"):
    x, out, folds, day = _data(planted, seed)
    spec = ev.ModelSpec("m", kind, label, {"alpha": 100.0} if kind == "ridge" else {})
    res = ev.oof_predict(x, out, folds, day, day.copy(), spec, seed=seed)
    row = ev.summarize_oof(res, out, spec, day, seed=seed, n_boot=200)
    a_obs, t_obs = ev.oof_stats(res, out, spec)
    nulls = {k: ev.null_distribution(x, out, folds, day, day.copy(), spec, k, draws, seed + 5, blk)
             for k, blk in (("perm", 5), ("shift", 10))}
    p_perm = {"auc": ev.p_value(a_obs, nulls["perm"]["auc"]), "top_r": ev.p_value(t_obs, nulls["perm"]["top_r"])}
    p_shift = {"auc": ev.p_value(a_obs, nulls["shift"]["auc"]), "top_r": ev.p_value(t_obs, nulls["shift"]["top_r"])}
    return row, p_perm, p_shift


def test_planted_signal_is_useful_and_beats_both_nulls():
    row, pp, ps = _run(True, 0, draws=39)
    assert row["auc_y1"] > 0.65 and row["auc_y1_ci95"][0] > 0.5
    assert row["top_decile"]["diff"] > 0 and row["top_decile"]["t"] > 2
    assert max(pp.values()) <= 0.05 and max(ps.values()) <= 0.05
    v = ev.verdict(row, pp, ps, n_models=1)
    assert v["verdict"] == "USEFUL"
    assert 0.9 < row["calibration"]["slope"] < 1.15  # Platt-calibrated OOF probabilities


def test_pure_noise_is_not_useful_and_null_p_values_are_spread():
    ps_auc = []
    for seed in range(8):
        row, pp, ps = _run(False, 100 + seed, draws=19)
        ps_auc += [pp["auc"], ps["auc"]]
        assert ev.verdict(row, pp, ps, n_models=1)["verdict"] in ("NOT USEFUL", "INCONCLUSIVE")
        assert ev.verdict(row, pp, ps, n_models=1)["verdict"] != "USEFUL"
    ps_auc = np.asarray(ps_auc)
    assert 0.25 < ps_auc.mean() < 0.75  # roughly uniform, not concentrated near 0
    assert np.mean(ps_auc <= 0.1) <= 0.35
    # most noise datasets are cleanly NOT USEFUL
    verdicts = []
    for s_ in range(4):
        row, pp, ps = _run(False, 200 + s_, draws=9)
        verdicts.append(ev.verdict(row, pp, ps, 1)["verdict"])
    assert verdicts.count("NOT USEFUL") >= 3


def test_nulls_preserve_marginals_and_structure():
    _, out, _, day = _data(True, 3)
    rng = np.random.default_rng(0)
    perm = ev.block_permutation(day, 5, rng)
    assert sorted(perm) == list(range(len(day)))
    assert np.array_equal(day[perm] // 5, day // 5)  # rows only move inside their day block
    rot = ev.block_rotation(day, 10, rng)
    assert sorted(rot) == list(range(len(day)))
    assert np.array_equal(day[rot] // 10, day // 10)
    assert np.array_equal(np.sort(out.take(rot).r), np.sort(out.r))
    # the rotation keeps runs of adjacent rows together (cluster structure), the permutation destroys them
    seq = np.arange(len(day))
    adj_rot = np.mean(np.abs(np.diff(rot[seq])) == 1)
    adj_perm = np.mean(np.abs(np.diff(perm[seq])) == 1)
    assert adj_rot > 0.9 and adj_perm < 0.1


def test_oof_rows_come_only_from_test_blocks_after_purge():
    x, out, folds, day = _data(True, 5)
    spec = ev.ModelSpec("m", "logit", "y1")
    res = ev.oof_predict(x, out, folds, day, day.copy(), spec)
    m = np.isfinite(res["score"])
    assert m.sum() == sum(len(f.test) for f in folds)
    assert day[m].min() >= folds[0].test_start
    # first 40+3 days can never be scored (no model may be trained without history)
    assert not m[day < 43].any()


def test_ridge_expected_r_model_on_planted_signal():
    row, _, _ = _run(True, 1, draws=19, kind="ridge", label="y2")
    assert row["auc_y1"] > 0.65 and row["corr_pred_r"] > 0.25


def test_overall_verdict_aggregation():
    assert ev.overall_verdict({"a": {"verdict": "NOT USEFUL"}, "b": {"verdict": "INCONCLUSIVE"}}) == "INCONCLUSIVE"
    assert ev.overall_verdict({"a": {"verdict": "NOT USEFUL"}}) == "NOT USEFUL"
    assert ev.overall_verdict({"a": {"verdict": "USEFUL"}, "b": {"verdict": "NOT USEFUL"}}) == "USEFUL"


def test_gbdt_meta_model_finds_planted_interaction_that_logit_misses_oof():
    rng = np.random.default_rng(7)
    day = np.repeat(np.arange(N_DAYS), PER)
    n = len(day)
    x = rng.normal(size=(n, 8))
    y1 = ((x[:, 0] > 0) ^ (x[:, 1] > 0)).astype(float)
    y1 = np.where(rng.random(n) < 0.12, 1 - y1, y1)
    out = ev.Outcomes(np.where(y1 > 0, 1.0, -1.0), y1, y1.copy())
    folds = cv.rolling_origin_folds(day, day.copy(), N_DAYS, 4, 3, 40)
    a = {}
    for kind in ("logit", "gbdt"):
        spec = ev.ModelSpec(kind, kind, "y1", {"n_trees": 80} if kind == "gbdt" else {})
        res = ev.oof_predict(x, out, folds, day, day.copy(), spec)
        a[kind] = ev.oof_stats(res, out, spec)[0]
    assert a["logit"] < 0.56 and a["gbdt"] > 0.75
