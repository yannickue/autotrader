"""Stages C-E, selection statistics, overlap clustering and the verdict rule (research only)."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pytest

from alpha.discovery import selection as sel
from alpha.discovery import stages as st
from alpha.discovery.archetypes import random_genome
from alpha.discovery.catalog import FeaturePool
from alpha.discovery.compile import TrialLedger, canonical_hash, canonicalize
from alpha.discovery.evaluate import (
    GenomeEval,
    GenomeEvaluator,
    SideMetrics,
    TrainView,
    ValidationView,
)
from alpha.discovery.fitness import train_fitness
from alpha.fast.screen import PartitionScreen
from alpha.fast.store import FeatureStore

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "research/configs/ad1_discovery.json"
CACHE = REPO / "data/feature_store/ad1_bench_gram"
CFG = st.PipelineConfig()


# --------------------------------------------------------------------------- synthetic fixtures
def _screen(n=30, e=0.1, pf=1.3, top3=0.4):
    return PartitionScreen(n, e, pf, 0.5, 1.0, -1.0, 1.0, 5.0, 3, 0.5, 0.5, 0.05, top3, 1.0, 1.0)


def _side(n=30, e=0.1, pf=1.3, top3=0.4, se=0.05):
    return SideMetrics(_screen(n, e, pf, top3), se, None, None)


def _eval(v_base, v_adv, reject=None):
    empty = _side(0, None, None, None, None)
    return GenomeEval("h", "L", 1, 100, reject, TrainView("h", 1, empty, empty),
                      ValidationView(v_base, v_adv))


def test_stage_c_thresholds():
    ok = st.judge_stage_c(_eval(_side(), _side(e=0.05)), CFG)
    assert ok.passed and ok.reasons == [] and ok.t_adverse == pytest.approx(1.0)
    assert CFG.c_min_trades == 25 and CFG.validation_min_t == 1.0
    bad_n = st.judge_stage_c(_eval(_side(n=24), _side(n=24)), CFG)
    assert not bad_n.passed and any("trades 24" in r for r in bad_n.reasons)
    assert st.judge_stage_c(_eval(_side(n=25), _side(n=25)), CFG).passed
    assert not st.judge_stage_c(_eval(_side(e=0.0), _side()), CFG).passed
    assert not st.judge_stage_c(_eval(_side(), _side(e=-0.01)), CFG).passed  # adverse <= 0
    assert not st.judge_stage_c(_eval(_side(pf=1.0), _side()), CFG).passed  # PF must be > 1
    assert st.judge_stage_c(_eval(_side(pf=None), _side()), CFG).passed  # no losers -> inf
    assert st.judge_stage_c(_eval(_side(top3=0.6), _side()), CFG).passed
    assert not st.judge_stage_c(_eval(_side(top3=0.61), _side()), CFG).passed
    assert not st.judge_stage_c(_eval(_side(), _side(), reject="too_few_trades"), CFG).passed


def test_stage_c_validation_t_boundary():
    # adverse expectancy > 0 but t = e/SE just below / at / above the 1.0 floor
    below = st.judge_stage_c(_eval(_side(), _side(e=0.0499, se=0.05)), CFG)
    assert not below.passed and any("validation adverse t" in r for r in below.reasons)
    assert below.t_adverse == pytest.approx(0.998)
    assert st.judge_stage_c(_eval(_side(), _side(e=0.05, se=0.05)), CFG).passed  # t == 1.0
    assert st.judge_stage_c(_eval(_side(), _side(e=0.0501, se=0.05)), CFG).passed
    # configurable through the 'pipeline' key
    strict = st.PipelineConfig.from_dict({"validation_min_t": 2.0, "c_min_trades": 40})
    assert not st.judge_stage_c(_eval(_side(), _side(e=0.05)), strict).passed
    assert not st.judge_stage_c(_eval(_side(n=39), _side(n=39, e=0.2)), strict).passed
    # undefined SE (no dispersion / no trades) cannot pass the t condition
    assert not st.judge_stage_c(_eval(_side(), _side(se=None)), CFG).passed


def _ps(e, se, **kw):
    base = dict(n_trades=100, expectancy_r=e, se_r=se, lower_bound_r=None if e is None else e - se,
                t_stat=None, profit_factor=1.1, top3_share=0.3, max_dd_r=8.0, max_loss_streak=5,
                trades_per_day=0.4, zero_trade_day_frac=0.7, cost_burden_r=0.1)
    base.update(kw)
    return st.PooledStats(**base)


def test_stage_d_thresholds():
    by = {"BASE": _ps(0.20, 0.05), "SPREAD_STRESS": _ps(0.1, 0.05),
          "SLIPPAGE_STRESS": _ps(0.1, 0.05), "COMBINED_ADVERSE": _ps(0.05, 0.05)}
    d = st.judge_stage_d(by, CFG)
    assert d.passed and d.r_lost_base_to_adverse == pytest.approx(0.15)
    by["COMBINED_ADVERSE"] = _ps(0.05, 0.08)  # lower bound -0.03 <= -0.02
    assert not st.judge_stage_d(by, CFG).passed
    by["COMBINED_ADVERSE"] = _ps(0.05, 0.06)  # lower bound -0.01 > -0.02
    assert st.judge_stage_d(by, CFG).passed
    by["COMBINED_ADVERSE"] = _ps(-0.01, 0.001)
    assert not st.judge_stage_d(by, CFG).passed


def test_neighborhood_thresholds():
    def nb(exps):
        return [st.NeighborResult(f"g{i}", "+1", e, 50) for i, e in enumerate(exps)]

    assert st.judge_neighborhood(nb([0.1] * 6 + [-0.1] * 4), 0, CFG).passed  # 60% positive
    r = st.judge_neighborhood(nb([0.1] * 5 + [-0.1] * 5), 0, CFG)
    assert not r.passed and r.frac_positive == 0.5
    r = st.judge_neighborhood(nb([0.1] * 9 + [-0.21]), 0, CFG)  # worst below -0.20
    assert not r.passed and any("worst" in x for x in r.reasons)
    assert st.judge_neighborhood(nb([0.1] * 9 + [-0.2]), 0, CFG).passed
    assert not st.judge_neighborhood(nb([0.1] * 5 + [None] * 5), 0, CFG).passed  # None not > 0
    assert not st.judge_neighborhood([], 3, CFG).passed


def test_regime_thresholds():
    def tab(sums, n=20):
        return {k: {"n": n, "sum_r": v, "mean_r": v / n} for k, v in sums.items()}

    sums = [1, 1, 1, 1, 1, 1, -1, -1, -1, -1]
    months = tab({f"2025-{m:02d}": v for m, v in zip(range(1, 11), sums, strict=True)})
    tables = {"regime_direction": tab({"UP": 5, "DOWN": 4}), "month": months}
    assert st.judge_regime(tables, CFG).passed  # 60% months >= 0, shares 0.56
    tables["regime_direction"] = tab({"UP": 8.1, "DOWN": 1.9})  # 0.81 > 0.80
    r = st.judge_regime(tables, CFG)
    assert not r.passed and any("regime_direction" in x for x in r.reasons)
    tables["regime_direction"] = tab({"UP": 8.0, "DOWN": 2.0})
    assert st.judge_regime(tables, CFG).passed
    tables["regime_direction"] = tab({"UP": 8.0, "DOWN": -2.0})  # negative bucket -> 100% share
    assert not st.judge_regime(tables, CFG).passed
    tables["regime_direction"] = tab({"UP": 8.0})  # single bucket: by construction, not judged
    assert st.judge_regime(tables, CFG).passed
    sums = [1, 1, 1, 1, 1, -1, -1, -1, -1, -1]
    weak = tab({f"2025-{m:02d}": v for m, v in zip(range(1, 11), sums, strict=True)})
    assert not st.judge_regime({"month": weak}, CFG).passed  # 50% < 60%
    assert not st.judge_regime({"month": tab({"2025-01": 1.0}, n=4)}, CFG).passed  # no eligible


def test_concentration_thresholds():
    assert st.judge_concentration(_ps(0.1, 0.05, top3_share=0.45), CFG).passed
    assert not st.judge_concentration(_ps(0.1, 0.05, top3_share=0.46), CFG).passed
    assert not st.judge_concentration(_ps(0.1, 0.05, max_loss_streak=13), CFG).passed
    assert st.judge_concentration(_ps(0.1, 0.05, max_loss_streak=12), CFG).passed
    assert not st.judge_concentration(_ps(0.1, 0.05, max_dd_r=25.5), CFG).passed
    assert st.judge_concentration(_ps(0.1, 0.05, max_dd_r=25.0), CFG).passed


def test_config_from_dict_ignores_unknown():
    c = st.PipelineConfig.from_dict({"c_min_trades": 20, "bogus": 1})
    assert c.c_min_trades == 20 and c.e_top3_share_max == 0.45


# --------------------------------------------------------------------------- selection
def test_null_bound_bonferroni_and_dsr():
    assert sel.expected_max_null_t(1) == 0.0
    assert sel.expected_max_null_t(1000) == pytest.approx(np.sqrt(2 * np.log(1000)))
    assert sel.expected_max_null_t(10_000) > sel.expected_max_null_t(100)
    assert sel.bonferroni_p(0.0, 10) == 1.0
    assert sel.bonferroni_p(3.0, 100) == pytest.approx(100 * 0.0013498980316, rel=1e-3)
    assert sel.bonferroni_p(None, 5) is None
    rng = np.random.default_rng(1)
    noise = rng.normal(0.0, 1.0, 400)
    strong = rng.normal(0.4, 1.0, 400)
    d_noise = sel.deflated_sharpe_probability(noise, 5000)
    d_strong = sel.deflated_sharpe_probability(strong, 5000)
    assert d_noise["dsr"] < 0.5 < 0.99 < d_strong["dsr"]
    # more trials -> harder to clear
    assert (sel.deflated_sharpe_probability(strong, 10)["dsr"]
            >= sel.deflated_sharpe_probability(strong, 10**6)["dsr"])
    days = np.arange(400)
    s = sel.selection_stats(strong, days, 3.0, 5000, 800)
    assert s.exceeds_null and s.pooled_t > s.null_bound_t and s.n_unique_specs == 800
    s2 = sel.selection_stats(noise, days, 0.2, 5000, 800)
    assert not s2.exceeds_null and s2.validation_bonferroni_p == 1.0


@dataclass
class _Fake:
    passed_all: bool
    selection: object | None
    train_fitness: float = 0.0


def test_verdict_rule_yes_no_inconclusive():
    hit = sel.SelectionStats(100, 5.0, 4.0, True, 3.0, 0.1, .1, 0, 3, 0, 0.99, 1000, 500)
    miss = replace(hit, pooled_t=2.0, exceeds_null=False)
    assert sel.robust_verdict([_Fake(True, hit)]) == "YES"
    assert sel.robust_verdict([_Fake(False, hit), _Fake(True, miss)]) == "INCONCLUSIVE"
    assert sel.robust_verdict([_Fake(True, miss)]) == "INCONCLUSIVE"
    assert sel.robust_verdict([_Fake(False, hit)]) == "NO"  # exceeding null without stages: NO
    assert sel.robust_verdict([]) == "NO"
    assert sel.robust_verdict([_Fake(True, None)]) == "INCONCLUSIVE"


def test_cluster_entry_sets_greedy_and_dedupe():
    a, b, c = frozenset(range(10)), frozenset(range(9)), frozenset(range(100, 110))
    items = [("a", 0.1, a), ("b", 0.3, b), ("c", 0.2, c), ("a2", 0.05, a)]
    clusters = sel.cluster_entry_sets(items, 0.6)
    assert [x.representative for x in clusters] == ["b", "c"]  # best fitness leads its cluster
    assert [x.size for x in clusters] == [3, 1]
    assert sel.jaccard(a, b) == pytest.approx(0.9)
    assert len(sel.cluster_entry_sets(items, 0.95)) == 3  # a/b no longer merge, a2 joins a


# --------------------------------------------------------------------------- real-data integration
@pytest.fixture(scope="module")
def env():
    from alpha.common.dataset import POINT, load_research_dataset
    from research.runners import ar2_fast

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    root = REPO / cfg["dataset_root"]
    if not root.exists():
        pytest.skip(f"dev dataset {root} not present (data/ar1_ger40 must be copied)")
    plan = ar2_fast._plan(cfg)
    dev = ar2_fast.dev_frame(load_research_dataset(root).frame, plan)
    features = FeatureStore.load_or_build(dev, {"point_size": POINT}, CACHE)
    return {"features": features, "plan": plan, "cfg": cfg,
            "market": ar2_fast._market(features), "dates": ar2_fast._dates(features),
            "pool": FeaturePool.from_features(features)}


def _evaluator(env, tmp_path, ledger=None):
    return GenomeEvaluator(env["features"], env["market"], env["dates"], env["plan"], env["cfg"],
                           tmp_path, ledger if ledger is not None else TrialLedger())


def _build_pool(env, ev, n, seed=11):
    rng = np.random.default_rng(seed)
    cands = []
    for _ in range(n):
        g = random_genome(rng, env["pool"])
        e = ev.evaluate(g)
        cands.append({"genome": g.to_dict(), "canonical_hash": e.genome_hash,
                      "train_fitness": train_fitness(e.train), "origin": "random",
                      "lineage": g.lineage})
    led = json.loads(ev.ledger.to_json())
    meta = {"ledger": {"total_trials": led["total_trials"], "unique_specs": led["unique"],
                       "param_trials": led["param_trials"],
                       "structural_trials": led["structural_trials"],
                       "duplicate_rejects": led["duplicate_rejects"],
                       "invalid_rejects": led["invalid_rejects"], "cache_hits": led["cache_hits"]},
            "oos_touched": False}
    return {"meta": meta, "candidates": cands}


def _stage_a_survivor(env, ev, n=80):
    rng = np.random.default_rng(5)
    for _ in range(n):
        g = random_genome(rng, env["pool"])
        if not ev.evaluate(g).rejected:
            return canonicalize(g)
    pytest.skip("no Stage-A survivor")


def test_pipeline_runs_counts_monotone_and_deterministic(env, tmp_path):
    ev = _evaluator(env, tmp_path)
    pool = _build_pool(env, ev, 30)
    before = json.loads(ev.ledger.to_json())
    res = st.run_pipeline(pool, ev, CFG)
    after = json.loads(ev.ledger.to_json())
    c = res.counts
    assert c["pool"] <= 30 and c["stage_a"] <= c["pool"]
    assert c["C"] <= c["train_and_validation_positive"] or c["C"] <= c["stage_a"]
    assert c["E"] <= c["D"] <= c["C"] <= c["stage_a"]
    # bookkeeping re-evaluation of the pool is ledger-neutral (only neighbour trials may add)
    assert after["structural_trials"] == before["structural_trials"]
    assert after["param_trials"] - before["param_trials"] >= 0
    plain = json.dumps(st.to_plain(res.candidates), sort_keys=True, allow_nan=False)
    ev2 = _evaluator(env, tmp_path / "again")
    res2 = st.run_pipeline(pool, ev2, CFG)
    assert res2.counts == res.counts
    assert json.dumps(st.to_plain(res2.candidates), sort_keys=True, allow_nan=False) == plain
    with pytest.raises(ValueError):
        st.run_pipeline({"meta": {"oos_touched": True}, "candidates": []}, ev, CFG)


def test_unique_specs_accounting_regression(env, tmp_path):
    """The ledger dict emitted by TrialLedger.to_json carries the REAL unique count under BOTH
    keys; a pool meta that only has the raw ``unique`` key (as the campaign wrote) must still
    yield N_eff = unique (was 0 -> report 'None')."""
    ev = _evaluator(env, tmp_path)
    pool = _build_pool(env, ev, 12)
    raw = json.loads(ev.ledger.to_json())
    raw.pop("seen")
    assert raw["unique"] == raw["unique_specs"] == ev.ledger.unique > 0
    assert sel.ledger_unique({"unique": 7}) == 7 and sel.ledger_unique({"unique_specs": 8}) == 8
    assert sel.ledger_unique({}) is None
    pool["meta"]["ledger"] = {k: v for k, v in raw.items() if k != "unique_specs"}
    res = st.run_pipeline(pool, ev, CFG)
    assert res.accounting["campaign_unique_specs"] == raw["unique"]
    assert res.accounting["n_unique_specs"] == raw["unique"]
    # cumulative N: prior trials/unique specs add on top; neighbour trials stay separate
    res2 = st.run_pipeline(pool, ev, CFG, prior_trials=1000, prior_unique_specs=500)
    a = res2.accounting
    assert a["n_total_trials"] == raw["total_trials"] + 1000
    assert a["n_unique_specs"] == raw["unique"] + 500 and a["prior_trials"] == 1000
    for c in res2.candidates:
        if c.selection is not None:
            assert c.selection.n_unique_specs == a["n_unique_specs"] > 0
            assert c.selection.n_total_trials == a["n_total_trials"]
            assert c.selection.null_bound_t == pytest.approx(
                sel.expected_max_null_t(a["n_total_trials"]))
    pool["meta"]["prior_trials"], pool["meta"]["prior_unique_specs"] = 40, 30  # via pool meta
    assert st.run_pipeline(pool, ev, CFG).accounting["n_total_trials"] == raw["total_trials"] + 40


def test_stage_c_and_d_on_real_candidate_consistent_with_eval(env, tmp_path):
    ev = _evaluator(env, tmp_path)
    g = _stage_a_survivor(env, ev)
    before = json.loads(ev.ledger.to_json())
    c = st.stage_c_validation(g, ev, CFG)
    assert json.loads(ev.ledger.to_json()) == before  # ledger-neutral
    assert c.n_trades == st.validation_gate_view(ev.evaluate(g)).base.screen.n_trades
    d = st.stage_d_cost_stress(g, ev, CFG)
    assert set(d.by_cost) == set(st.STRESS_COSTS)
    # cost ordering: BASE is never worse than the stressed scenarios (same signals, higher costs)
    e = {k: v.expectancy_r for k, v in d.by_cost.items() if v.expectancy_r is not None}
    if len(e) == 4:
        assert e["BASE"] >= e["SPREAD_STRESS"] - 1e-9 and e["BASE"] >= e["SLIPPAGE_STRESS"] - 1e-9
        assert e["SPREAD_STRESS"] >= e["COMBINED_ADVERSE"] - 1e-9
        assert d.r_lost_base_to_adverse == pytest.approx(e["BASE"] - e["COMBINED_ADVERSE"])
    pooled_n = d.by_cost["BASE"].n_trades
    ev_view = ev.evaluate(g)
    # embargo respected: pooled trades == Train + post-embargo Validation
    val_n = st.validation_gate_view(ev_view).base.screen.n_trades
    assert pooled_n == ev_view.train.base.screen.n_trades + val_n


def test_stage_e_counts_neighbours_on_ledger_and_is_seeded(env, tmp_path):
    ledger = TrialLedger()
    ev = _evaluator(env, tmp_path, ledger)
    g = _stage_a_survivor(env, ev)
    p0 = ledger.param_trials
    e1 = st.stage_e_stability(g, ev, CFG)
    added = ledger.param_trials - p0
    hood = e1.neighborhood
    assert added == hood.n_neighbors > 0
    assert hood.n_neighbors <= 2 * len(st.param_space(g)) + CFG.e_joint_perturbations
    e2 = st.stage_e_stability(g, _evaluator(env, tmp_path / "b"), CFG)
    assert [n.expectancy_r for n in e2.neighborhood.neighbors] == [
        n.expectancy_r for n in hood.neighbors]
    for dim in ("regime_direction", "regime_vol_state", "phase", "hour", "month"):
        if e1.regime.tables:
            assert dim in e1.regime.tables
    if e1.regime.tables:
        n_pooled = sum(v["n"] for v in e1.regime.tables["month"].values())
        assert n_pooled == sum(v["n"] for v in e1.regime.tables["hour"].values())


def test_overlap_clusters_dedupe_identical_entry_sets(env, tmp_path):
    ev = _evaluator(env, tmp_path)
    g = _stage_a_survivor(env, ev)
    other = st.PipelineConfig()
    twin = replace(g, lineage="TWIN")  # same canonical spec -> identical entry set
    mk = lambda gen, fit: st.CandidateResult(  # noqa: E731
        canonical_hash(canonicalize(gen)), canonicalize(gen), fit, None, gen.lineage, True, True)
    a, b = mk(g, 0.2), mk(twin, 0.1)
    clusters = sel.overlap_clusters([b, a], ev, 0.6, sim=st.PooledSim(ev, other))
    assert len(clusters) == 1 and clusters[0].size == 2 and clusters[0].representative is a


def test_runner_cli_writes_survivors_and_report(env, tmp_path):
    from research.runners import ad1_survivors as runner

    ev = _evaluator(env, tmp_path / "gen")
    pool = _build_pool(env, ev, 30)
    pool_path = tmp_path / "candidate_pool.json"
    pool_path.write_text(json.dumps(pool), encoding="utf-8")
    out = tmp_path / "out"
    assert runner.main(["--pool", str(pool_path), "--config", str(CONFIG), "--out-dir", str(out),
                        "--cache-dir", str(CACHE)]) == 0
    data = json.loads((out / "survivors.json").read_text(encoding="utf-8"))
    report = (out / "report.md").read_text(encoding="utf-8")
    assert data["summary"]["oos_touched"] is False
    assert "OOS TOUCHED: NO" in report
    assert f"{runner.VERDICT_PREFIX}: {data['summary']['verdict']}" in report
    assert data["summary"]["verdict"] in ("YES", "NO", "INCONCLUSIVE")
    assert data["summary"]["counts"]["pool"] <= 30
    # an OOS-touched pool is refused
    bad = json.loads(pool_path.read_text(encoding="utf-8"))
    bad["meta"]["oos_touched"] = True
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(SystemExit):
        runner.main(["--pool", str(tmp_path / "bad.json"), "--config", str(CONFIG),
                     "--out-dir", str(tmp_path / "o2"), "--cache-dir", str(CACHE)])


def test_derive_weaknesses_flags_null_bound_and_stage_failures():
    from research.runners import ad1_survivors as runner

    stats = _ps(0.05, 0.05, n_trades=60)
    d = st.StageDResult(False, ["pooled x"], {"BASE": _ps(0.2, 0.05), "COMBINED_ADVERSE": stats},
                        0.05, 0.0, 0.1, 0.15)
    c_res = st.StageCResult(True, [], 20, 0.1, 0.01, 1.2, 0.3, 0.05, 0.2)
    s = sel.SelectionStats(50, 1.0, 4.0, False, 0.2, 0.9, 0.05, 0, 3, 0, 0.3, 1000, 500)
    c = st.CandidateResult("h", None, 0.1, None, None, True, True, c_res, d, None, s)
    w = runner.derive_weaknesses(c, 0.2)
    joined = " | ".join(w)
    for needle in ("stage D FAIL", "does not exceed null bound", "Bonferroni", "deflated-Sharpe",
                   "thin Validation sample", "small pooled sample", "cost sensitive", "< 25%"):
        assert needle in joined, needle
