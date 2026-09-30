# ruff: noqa: E501
"""Pareto selection, correlation clustering, freezing and the small statistics helpers of v2_select."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from alpha.discovery import v2_select as vs


def test_non_dominated_fronts_hand_case():
    obj = np.array([[1, 1], [2, 2], [3, 0], [0, 3], [1.5, 1.5]], dtype=float)
    front = vs.non_dominated_fronts(obj)
    assert front.tolist() == [2, 0, 0, 0, 1]


def test_duplicates_share_a_front_and_order_is_total_and_deterministic():
    obj = np.array([[1.0, 1.0], [1.0, 1.0], [0.0, 2.0], [2.0, 0.0]])
    hashes = ["b", "a", "c", "d"]
    order, front, crowd = vs.pareto_order(obj, hashes)
    assert front.tolist() == [0, 0, 0, 0]
    assert sorted(order.tolist()) == [0, 1, 2, 3]
    order2, _, _ = vs.pareto_order(obj.copy(), hashes)
    assert order.tolist() == order2.tolist()
    assert np.isinf(crowd[2]) and np.isinf(crowd[3])  # boundary points of the front


def test_corr_matrix_constant_rows_and_signs():
    x = np.array([[1, 2, 3, 4.0], [2, 4, 6, 8.0], [4, 3, 2, 1.0], [5, 5, 5, 5.0]])
    c = vs.corr_matrix(x)
    assert c[0, 1] == pytest.approx(1.0) and c[0, 2] == pytest.approx(-1.0)
    assert c[3, 0] == 0.0 and c[3, 3] == 1.0


def test_leader_clusters_keep_pareto_best_per_cluster():
    x = np.array([[1, 2, 3, 4.0], [1, 2, 3, 4.1], [4, 1, 3, 2.0]])
    corr = vs.corr_matrix(x)
    cluster, leaders = vs.leader_clusters(np.array([1, 0, 2]), corr, 0.8)
    assert leaders == [1, 2] and cluster.tolist() == [0, 0, 1]


def _row(h, e, t, payoff=1.5, tpd=0.5, shock=None, n=100, passer=True):
    return {"hash": h, "passer": passer, "n_trades": n, "e_adv": e, "t_day": t, "payoff": payoff, "tpd": tpd,
            "e_shock": e - 0.02 if shock is None else shock}


def _daily(seed, n=60):
    return np.random.default_rng(seed).normal(0, 1, n)


def test_select_finalists_dedup_grades_and_eligibility():
    d_a, d_b, d_c = _daily(1), _daily(2), _daily(3)
    daily = {"a1": d_a, "a2": d_a * 1.0 + 1e-3 * _daily(9), "b": d_b, "c": d_c, "low": _daily(4), "x": _daily(5)}
    rows = [_row("a1", 0.10, 2.0), _row("a2", 0.05, 1.0),  # a2 = near-copy of a1, dominated
            _row("b", 0.20, 1.0, payoff=3.0), _row("c", -0.10, 0.5, payoff=1.0, tpd=1.2),  # negative E stays eligible
            _row("low", 0.30, 3.0, n=39),  # too few trades: not eligible
            _row("x", 0.30, 3.0, passer=False)]  # not a passer
    sel = vs.select_finalists(rows, daily, vs.SelectConfig(k=10))
    hs = [f["hash"] for f in sel["finalists"]]
    assert "low" not in hs and "x" not in hs
    assert ("a1" in hs) != ("a2" in hs) and "a1" in hs  # only the Pareto-best of the near-duplicate cluster
    assert "c" in hs  # graded selection: no binary sign gate
    a1 = next(f for f in sel["finalists"] if f["hash"] == "a1")
    assert a1["cluster_size"] == 2 and sel["n_eligible"] == 4 and sel["n_clusters"] == 3
    assert len({f["cluster_id"] for f in sel["finalists"]}) == len(hs)
    sel_k = vs.select_finalists(rows, daily, vs.SelectConfig(k=2))
    assert len(sel_k["finalists"]) == 2 and [f["rank"] for f in sel_k["finalists"]] == [0, 1]
    assert sel == vs.select_finalists(rows, daily, vs.SelectConfig(k=10))  # deterministic


def test_tpd_cap_and_payoff_handling_in_objectives():
    cfg = vs.SelectConfig()
    rows = [_row("a", 0.1, 1.0, tpd=5.0), _row("b", 0.1, 1.0, tpd=1.5), _row("c", 0.1, 1.0, payoff=None)]
    obj = vs.objective_matrix(rows, np.ones(3), cfg)
    assert obj[0, 3] == obj[1, 3] == 1.5 and obj[2, 2] == 0.0
    rows2 = [_row("a", 0.1, 1.0, payoff=math.inf)]
    assert vs.objective_matrix(rows2, np.ones(1), cfg)[0, 2] == vs.PAYOFF_CAP


def test_preregistered_defaults_are_fixed():
    c = vs.SelectConfig()
    assert (c.k, c.tpd_cap, c.corr_threshold, c.min_trades) == (30, 1.5, 0.8, 40)
    assert vs.OBJECTIVES == ("e_adv", "t_day", "payoff", "tpd", "e_shock", "novelty")


def _sel(hashes):
    return {"config": vs.SelectConfig().to_dict(), "n_eligible": len(hashes), "n_clusters": len(hashes),
            "finalists": [{"rank": i, "hash": h, "cluster_id": i, "objectives": {}} for i, h in enumerate(hashes)]}


def test_freeze_is_immutable_and_hash_covers_order_and_config(tmp_path):
    p1 = vs.build_frozen_payload({"M": _sel(["h1", "h2"])})
    p2 = vs.build_frozen_payload({"M": _sel(["h2", "h1"])})
    assert p1["finalist_hash"] != p2["finalist_hash"]
    path = tmp_path / "frozen.json"
    vs.freeze_selection(path, p1)
    assert vs.freeze_selection(path, p1)["finalist_hash"] == p1["finalist_hash"]  # idempotent
    with pytest.raises(vs.FrozenSelectionError):
        vs.freeze_selection(path, p2)
    assert vs.load_frozen(path)["finalist_hash"] == p1["finalist_hash"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["markets"]["M"]["finalists"][0]["hash"] = "evil"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(vs.FrozenSelectionError):
        vs.load_frozen(path)
    cfg2 = vs.SelectConfig(k=5).to_dict()
    assert vs.finalist_hash({"M": ["h1"]}, cfg2) != vs.finalist_hash({"M": ["h1"]}, vs.SelectConfig().to_dict())


def test_day_clustered_t_matches_formula_and_shrinks_with_clustering():
    r = np.array([1.0, 1.0, -0.5, 2.0, 0.5, 1.5])
    day = np.array([0, 1, 2, 3, 4, 5])
    n, mean = len(r), r.mean()
    se_iid_cr = math.sqrt(np.sum((r - mean) ** 2)) / n * math.sqrt(6 / 5)
    assert vs.day_clustered_t(r, day) == pytest.approx(mean / se_iid_cr)
    rr = np.array([1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0, 2.0])
    t_ind = vs.day_clustered_t(rr, np.arange(8))
    t_clu = vs.day_clustered_t(rr, np.array([0, 0, 0, 0, 1, 1, 1, 2]))
    assert abs(t_clu) < abs(t_ind)
    assert vs.day_clustered_t(np.array([1.0]), np.array([0])) is None
    assert vs.day_clustered_t(np.ones(4), np.array([0, 0, 1, 1])) is None  # zero variance


def test_trade_stats_and_daily_vector_hash():
    st = vs.trade_stats(np.array([2.0, -1.0, 1.0, -1.0]), np.array([0, 0, 1, 1]))
    assert st["payoff"] == pytest.approx(1.5) and st["profit_factor"] == pytest.approx(1.5)
    assert vs.trade_stats(np.zeros(0), np.zeros(0))["n_trades"] == 0
    v = vs.daily_vector(np.array([1.0, 2.0, 3.0]), np.array([5, 5, 7]), np.array([4, 5, 6, 7]))
    assert v.tolist() == [0.0, 3.0, 0.0, 3.0]
    assert vs.daily_hash(v) == vs.daily_hash(v.copy()) and vs.daily_hash(v) != vs.daily_hash(v * 2)


def test_effective_trials_counts_behaviours_and_dsr_is_deflated_by_more_trials():
    base = [_daily(s) for s in (1, 2, 3)]
    mat = np.stack([b + 1e-3 * _daily(10 + i) for b in base for i in range(4)])  # 3 behaviours x 4 copies
    eff = vs.effective_independent_trials(mat, np.arange(len(mat)), 0.8)
    assert eff["n_strategies"] == 12 and eff["n_clusters"] == 3
    x = _daily(7) + 0.15
    hi = vs.deflated_sharpe(x, 0.01, 10)["dsr"]
    lo = vs.deflated_sharpe(x, 0.01, 1000)["dsr"]
    assert lo < hi
    assert vs.deflated_sharpe(np.ones(20), 0.01, 10)["dsr"] is None
    assert vs.expected_max_sharpe(0.01, 1) == 0.0 and vs.expected_max_sharpe(0.01, 100) > vs.expected_max_sharpe(0.01, 10)
