# ruff: noqa: E501
"""Confluence analysis: clustering, reference outcomes (multi-pass == single trade), planted edge vs none."""

from __future__ import annotations

import numpy as np

from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, MarketArrays, SimWindow, simulate_fast
from alpha.metalabel import confluence as cf

N_DAYS = 80
BPD = 100


def _scenario(planted: bool, seed: int, edge: float = 0.7):
    """3 independent clusters (2 identical strategies each); reference R per bar with an optional
    planted edge at (bar, dir) where >= 2 clusters fire."""
    rng = np.random.default_rng(seed)
    n = N_DAYS * BPD
    day = np.arange(n) // BPD
    minute = (540 + 5 * (np.arange(n) % BPD)).astype(np.int64)
    train = np.ones(n, dtype=bool)
    fire = [np.flatnonzero(rng.random(n) < 0.05) for _ in range(3)]  # cluster firings (long only)
    counts = np.zeros(n, dtype=int)
    for f in fire:
        counts[f] += 1
    ref_long = rng.normal(-0.1, 1.0, n)
    if planted:
        ref_long = ref_long + np.where(counts >= 2, edge, 0.0)
    ref_short = rng.normal(-0.1, 1.0, n)
    rows_bar, rows_spec = [], []
    codes = []
    for s in range(6):  # two identical strategies per cluster
        f = fire[s // 2]
        rows_bar.append(f)
        rows_spec.append(np.full(len(f), s))
        codes.append(np.unique(f * 2 + 1))
    data = cf.ConfluenceData(np.concatenate(rows_bar), np.ones(sum(len(b) for b in rows_bar), dtype=np.int64),
                             np.concatenate(rows_spec), np.array([0, 0, 1, 1, 2, 2]), codes, None, ref_long, ref_short,
                             day, minute, train)
    return data


def test_clustering_merges_identical_and_separates_independent():
    rng = np.random.default_rng(0)
    a = np.unique(rng.integers(0, 5000, 300)) * 2 + 1
    b = np.unique(rng.integers(0, 5000, 300)) * 2 + 1
    cid, info = cf.cluster_specs([a, a.copy(), b, b.copy()], None)
    assert cid[0] == cid[1] and cid[2] == cid[3] and cid[0] != cid[2] and info["n_clusters"] == 2
    # high overlap (Jaccard > 0.5) merges, low overlap does not
    c = np.r_[a[:200], b[:20]]
    cid2, _ = cf.cluster_specs([a, np.unique(c), b], None)
    assert cid2[0] == cid2[1] and cid2[0] != cid2[2]


def test_clustering_links_by_daily_return_correlation():
    a = np.arange(0, 400, 2) * 2 + 1
    b = np.arange(1, 400, 2) * 2 + 1  # disjoint decision bars -> Jaccard 0
    d = np.random.default_rng(0).normal(size=(3, 60))
    daily = np.vstack([d[0], d[0] * 2 + 0.01, d[1]])
    cid, info = cf.cluster_specs([a, b, a + 1000], daily)
    assert cid[0] == cid[1] and cid[0] != cid[2] and info["corr_only_links"] >= 1


def test_planted_confluence_edge_is_positive():
    res = cf.analyse(_scenario(True, 1), n_null=100, seed=3)
    assert res["clusters"]["n_clusters"] == 3
    c = res["by_clusters"]
    assert c["ge2_vs_1"]["diff"] > 0.4 and c["ge2_vs_1"]["t"] > 2
    assert res["null_day_shift"]["p_upper"] <= 0.05
    assert res["verdict"]["verdict"] == "CONFLUENCE POSITIVE"


def test_no_edge_is_inconclusive_and_t_is_calibrated():
    res = [cf.analyse(_scenario(False, 30 + s), n_null=60, seed=s) for s in range(12)]
    verdicts = [r["verdict"]["verdict"] for r in res]
    t = np.array([r["by_clusters"]["ge2_vs_1"]["t"] for r in res])
    assert verdicts.count("CONFLUENCE POSITIVE") + verdicts.count("CONFLUENCE NEGATIVE") <= 1  # ~1% false-positive rate
    assert verdicts.count("INCONCLUSIVE") >= 11
    assert abs(t.mean()) < 1.0 and 0.5 < t.std() < 1.8  # day-clustered t is roughly N(0, 1) under no edge


def test_planted_negative_confluence():
    res = cf.analyse(_scenario(True, 4, edge=-0.7), n_null=100, seed=5)
    assert res["verdict"]["verdict"] == "CONFLUENCE NEGATIVE"


def test_reference_outcomes_multi_pass_equals_single_trade():
    rng = np.random.default_rng(0)
    n = 1500
    c = 100 + np.cumsum(rng.normal(0, 0.4, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.15, n))
    lo = np.minimum(o, c) - np.abs(rng.normal(0, 0.15, n))
    minute = (540 + 5 * (np.arange(n) % BPD)).astype(np.int64)
    day = np.arange(n) // BPD
    mkt = MarketArrays(o, h, lo, c, np.full(n, 0.1), minute, day, np.r_[np.ones(n - 1, dtype=bool), False])
    atr = np.full(n, 1.5)
    cost = COST_SCENARIOS["COMBINED_ADVERSE"]
    sizing = SizingSpec(min_risk_pts=0.5, max_risk_pts=60.0)
    rules = SimRules()
    win = SimWindow(540, 1000, 1040)
    dec = np.arange(20, 1400, 3)
    ref = cf.reference_outcomes(mkt, atr, dec, 1, cost, sizing, rules, win)
    free = SimRules(max_trades_per_day=10 ** 6, max_entry_spread_pts=rules.max_entry_spread_pts)
    checked = 0
    for i in dec[::7]:
        d = np.array([1], dtype=np.int8)
        cand = CandidateArrays(np.array([i]), d, c[[i]] - 1.5, np.array([np.nan]), np.array([1.5]), np.array([EXIT_FIXED_R], dtype=np.int8))
        tr = simulate_fast(mkt, cand, cost, sizing, free, win)
        if len(tr):
            assert ref[i] == tr.r_multiple[0]
            checked += 1
        else:
            assert np.isnan(ref[i])
    assert checked > 20
    # overlapping decisions all received an outcome (no occupancy censoring)
    inside = dec[(minute[dec + 1] >= 540) & (minute[dec + 1] < 1000) & (day[dec] == day[dec + 1])]
    assert np.isfinite(ref[inside]).mean() > 0.9
