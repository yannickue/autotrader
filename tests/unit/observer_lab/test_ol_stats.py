# ruff: noqa: E501
from __future__ import annotations

import numpy as np
import pytest

from coverage_analysis.observer_lab import stats as ST


def _shifted(p, shift):
    return 1 / (1 + np.exp(-(np.log(p / (1 - p)) + shift)))


def sim(rng, n_days=60, per_day=5, p_e=0.4, p_c=0.4, day_sd=0.5):
    """Events + controls sharing a day-level random effect (the clustering the block bootstrap must respect)."""
    ye, de, yc, dc = [], [], [], []
    for d in range(n_days):
        shift = rng.normal(0, day_sd)
        ye += list((rng.random(per_day) < _shifted(p_e, shift)).astype(float))
        yc += list((rng.random(per_day) < _shifted(p_c, shift)).astype(float))
        de += [d] * per_day
        dc += [d] * per_day
    return np.array(ye), np.array(de), np.array(yc), np.array(dc)


def test_wilson_known_values():
    assert ST.wilson_interval(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-3)
    lo, hi = ST.wilson_interval(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    assert ST.wilson_interval(50, 100) == pytest.approx((0.4038, 0.5962), abs=1e-3)
    assert np.isnan(ST.wilson_interval(0, 0)[0])


def test_delta_point_estimate_counts_and_blocks():
    ye = np.array([1, 1, 0, np.nan, 1.0])
    de = np.array([0, 0, 1, 1, 2])
    yc = np.array([0, 1, 0.0])
    dc = np.array([0, 1, 3])
    est = ST.block_bootstrap_delta(ye, de, yc, dc, B=50, seed=1, cluster_event=np.array(["a", "a", "b", "b", "c"]))
    assert (est.n_event, est.n_control, est.k_event, est.k_control) == (4, 3, 3, 1)  # NaN (= censored) outcome dropped
    assert est.p_event == pytest.approx(0.75) and est.p_control == pytest.approx(1 / 3)
    assert est.delta == pytest.approx(0.75 - 1 / 3)
    assert est.n_blocks == 4  # distinct days over events and controls: {0, 1, 2, 3}
    assert est.n_clusters == 3  # distinct event clusters among the used events


def test_bootstrap_is_seeded_and_reproducible():
    ye, de, yc, dc = sim(np.random.default_rng(0))
    a = ST.block_bootstrap_delta(ye, de, yc, dc, B=400, seed=7)
    b = ST.block_bootstrap_delta(ye, de, yc, dc, B=400, seed=7)
    c = ST.block_bootstrap_delta(ye, de, yc, dc, B=400, seed=8)
    assert (a.ci_low, a.ci_high, a.p_boot) == (b.ci_low, b.ci_high, b.p_boot)
    assert (a.ci_low, a.ci_high) != (c.ci_low, c.ci_high)


def test_block_bootstrap_null_coverage_and_planted_effect():
    cover = 0
    sims = 150
    for s in range(sims):
        ye, de, yc, dc = sim(np.random.default_rng(1000 + s))
        est = ST.block_bootstrap_delta(ye, de, yc, dc, B=300, seed=s)
        cover += est.ci_low <= 0.0 <= est.ci_high
    assert 0.88 <= cover / sims <= 0.995  # nominal 0.95, loose tolerance for B=300 / 150 sims
    ye, de, yc, dc = sim(np.random.default_rng(5), p_e=0.6, p_c=0.4)
    est = ST.block_bootstrap_delta(ye, de, yc, dc, B=1000, seed=1)
    assert est.ci_low > 0 and est.p_boot < 0.01 and est.delta == pytest.approx(0.2, abs=0.08)


def test_contrast_of_two_arms_detects_planted_difference():
    rng = np.random.default_rng(2)
    base = sim(rng, p_e=0.5, p_c=0.5)
    boosted = sim(np.random.default_rng(3), p_e=0.7, p_c=0.5)
    c = ST.block_bootstrap_contrast(boosted, base, B=800, seed=0)
    assert c.delta == pytest.approx((boosted[0].mean() - boosted[2].mean()) - (base[0].mean() - base[2].mean()))
    assert c.ci_low > 0 or c.p_boot < 0.05


def test_generic_block_bootstrap_resamples_whole_days():
    day = np.repeat(np.arange(30), 4)
    x = (day % 2).astype(float)  # a whole day is constant
    lo, hi, draws = ST.block_bootstrap_ci((x,), day, lambda v: float(v.mean()), B=200, seed=0)
    assert 0.0 < lo < 0.5 < hi < 1.0 and len(draws) == 200


def test_holm_and_bh_known_vectors():
    p = [0.01, 0.04, 0.03, 0.005]
    assert ST.holm_adjust(p) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert ST.bh_adjust(p) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    # untested registered hypotheses (m > len(p)) make the correction stricter
    assert ST.holm_adjust(p, m=10) == pytest.approx([0.09, 0.28, 0.24, 0.05])
    assert ST.bh_adjust(p, m=10) == pytest.approx([0.05, 0.10, 0.10, 0.05])
    assert ST.holm_adjust([0.5, 0.9]) == pytest.approx([1.0, 1.0])
    with pytest.raises(ValueError):
        ST.holm_adjust(p, m=2)


def test_registry_counts_and_refuses_unregistered_results():
    reg = ST.HypothesisRegistry("lane-d1-smoke")
    reg.register_many(["h1", "h2", "h3"])
    with pytest.raises(ValueError):
        reg.register("h1")
    with pytest.raises(KeyError):
        reg.record("nope", 0.01)
    reg.record("h1", 0.001)
    reg.record("h2", None)  # registered but not evaluable (insufficient evidence) still counts
    with pytest.raises(ValueError):
        reg.record("h1", 0.5)
    assert reg.n_hypotheses == 3 and reg.n_evaluated == 1
    adj = reg.adjust("holm")
    assert adj["h1"] == pytest.approx(0.003) and adj["h2"] is None and adj["h3"] is None
    assert reg.adjust("bh")["h1"] == pytest.approx(0.003)
    assert reg.summary()["n_hypotheses"] == 3
    with pytest.raises(ValueError):
        reg.adjust("bonferroni-ish")


def test_insufficient_evidence_rules():
    ev = ST.DEFAULT_MIN_EVIDENCE
    assert (ev.min_events, ev.min_controls, ev.min_blocks, ev.min_clusters) == (30, 30, 20, 20)
    ok = dict(n_event=40, n_control=40, n_blocks=25, n_clusters=None)
    assert ST.evidence_status(**ok) == "OK"
    assert ST.evidence_status(**{**ok, "n_event": 29}) == ST.INSUFFICIENT_EVIDENCE
    assert ST.evidence_status(**{**ok, "n_control": 29}) == ST.INSUFFICIENT_EVIDENCE
    assert ST.evidence_status(**{**ok, "n_blocks": 19}) == ST.INSUFFICIENT_EVIDENCE
    assert ST.evidence_status(**{**ok, "n_clusters": 19}) == ST.INSUFFICIENT_EVIDENCE
    assert ST.evidence_status(**{**ok, "n_clusters": 20}) == "OK"


def test_final_status_uses_adjusted_p_and_ci():
    assert ST.final_status(ST.INSUFFICIENT_EVIDENCE, 0.0, 0.1, 0.2) == ST.INSUFFICIENT_EVIDENCE
    assert ST.final_status("OK", 0.001, 0.05, 0.2) == ST.SIGNIFICANT_ADJUSTED
    assert ST.final_status("OK", 0.20, 0.05, 0.2) == ST.NOT_SIGNIFICANT
    assert ST.final_status("OK", 0.001, -0.05, 0.2) == ST.NOT_SIGNIFICANT  # CI includes 0
    assert ST.final_status("OK", None, 0.05, 0.2) == ST.NOT_SIGNIFICANT


def test_enrichment_result_fields():
    r = ST.EnrichmentResult(
        feature="f_levels__x", group="levels", label="y_fav050_before_adv050", cell="Q3", n_event=40, n_control=40, p_event=0.6,
        p_control=0.5, delta=0.1, ci_low=-0.1, ci_high=0.3, n_blocks=25, adjusted_p=None, status="OK", versions={"levels": "mso-levels-1"},
    )
    assert r.kind == "single" and r.n_clusters is None
