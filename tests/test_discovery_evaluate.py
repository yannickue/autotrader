from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from alpha.discovery.archetypes import random_genome
from alpha.discovery.catalog import FeaturePool
from alpha.discovery.compile import TrialLedger, canonical_hash, canonicalize
from alpha.discovery.evaluate import GenomeEval, GenomeEvaluator, validation_gate_view
from alpha.discovery.fitness import train_fitness
from alpha.discovery.optuna_driver import (
    MAX_TRIALS_PER_STRUCTURE,
    optimize_structure,
    run_optuna_campaign,
)
from alpha.discovery.search import param_space, with_params
from alpha.fast.store import FeatureStore

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "research/configs/ad1_discovery.json"
CACHE = REPO / "data/feature_store/ad1_bench_gram"


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


def _genomes(env, n, seed=3):
    rng = np.random.default_rng(seed)
    return [random_genome(rng, env["pool"]) for _ in range(n)]


def _good(env, ev, n=60):
    for g in _genomes(env, n):
        if not ev.evaluate(g).rejected:
            return g
    pytest.skip("no Stage-A survivor among random genomes")


def test_evaluate_roundtrip_cache_and_ledger(env, tmp_path):
    ledger = TrialLedger()
    ev = _evaluator(env, tmp_path, ledger)
    g = _good(env, ev)
    total, hits = ledger.total_trials, ledger.cache_hits
    a = ev.evaluate(g, kind="param")
    assert (ledger.total_trials, ledger.cache_hits) == (total + 1, hits + 1)
    assert ledger.param_trials == 1
    # lineage-only difference maps to the same canonical hash -> cache hit, same numbers
    b = ev.evaluate(replace(g, lineage="OTHER"))
    assert b.lineage == "OTHER" and b.train == a.train
    assert canonical_hash(g) == a.genome_hash
    assert ledger.unique <= ledger.total_trials
    assert GenomeEval.from_dict(json.loads(a.to_json())).train == a.train
    # fresh evaluator over the same on-disk cache: identical results, zero simulations
    ev2 = _evaluator(env, tmp_path)
    c = ev2.evaluate(g)
    assert c.train == a.train and validation_gate_view(c) == validation_gate_view(a)
    assert ev2.sim_count == 0 and ev2.ledger.cache_hits == 1


def test_fingerprint_depends_on_split_embargo(env, tmp_path):
    from alpha.common.protocol import SplitPlan

    plan = env["plan"]
    other = SplitPlan(plan.train, plan.validation, plan.oos, embargo_days=plan.embargo_days + 2)
    ev1 = _evaluator(env, tmp_path)
    ev2 = GenomeEvaluator(env["features"], env["market"], env["dates"], other, env["cfg"],
                          tmp_path, TrialLedger())
    assert ev1.fingerprint("x") != ev2.fingerprint("x")


def test_stage_a_reject_and_deterministic(env, tmp_path):
    ev = _evaluator(env, tmp_path)
    results = [ev.evaluate(g) for g in _genomes(env, 40)]
    rejected = [r for r in results if r.rejected]
    ok = [r for r in results if not r.rejected]
    assert rejected and ok
    assert all(train_fitness(r.train) < 0 for r in rejected if r.train.adverse.screen.n_trades < 30)
    assert all(r.train.adverse.screen.n_trades >= 30 for r in ok)
    again = [_evaluator(env, tmp_path / "fresh").evaluate(g) for g in _genomes(env, 5)]
    assert [x.train for x in again] == [x.train for x in results[:5]]


def test_train_chunks_and_stats_present(env, tmp_path):
    ev = _evaluator(env, tmp_path)
    r = ev.evaluate(_good(env, ev))
    side = r.train.adverse
    assert len(side.chunk_expectancy) == 3 and sum(side.chunk_trades) == side.screen.n_trades
    assert side.se_r is not None and side.ci_lo < side.screen.expectancy_r < side.ci_hi
    assert side.screen.expectancy_r <= r.train.base.screen.expectancy_r + 1e-12
    assert validation_gate_view(r).adverse.screen.n_trades >= 0


def test_optuna_reproducible_bounded_and_train_only(env, tmp_path):
    g = _good(env, _evaluator(env, tmp_path / "s"))
    runs = []
    for i in range(2):
        ledger = TrialLedger()
        res = optimize_structure(_evaluator(env, tmp_path / f"r{i}", ledger), g, 12, seed=7)
        runs.append((res, ledger))
    (a, la), (b, _lb) = runs
    assert a.trial_hashes == b.trial_hashes and len(a.trial_hashes) == 12
    assert canonical_hash(a.best_genome) == canonical_hash(b.best_genome)
    assert a.best_fitness == b.best_fitness
    assert la.param_trials == 12 == a.n_trials
    assert len(a.top) <= 5
    assert len({c.genome_hash for c in a.top}) == len(a.top)
    assert [c.fitness for c in a.top] == sorted((c.fitness for c in a.top), reverse=True)
    assert a.trial_hashes[0] == canonical_hash(canonicalize(g))  # trial 0 = the structure
    with pytest.raises(AssertionError):
        optimize_structure(_evaluator(env, tmp_path / "x"), g, MAX_TRIALS_PER_STRUCTURE + 1, 1)
    other = optimize_structure(_evaluator(env, tmp_path / "o"), g, 12, seed=8)
    assert other.trial_hashes != a.trial_hashes


def test_campaign_pools_distinct_candidates(env, tmp_path):
    ev = _evaluator(env, tmp_path)
    structures = [g for g in _genomes(env, 60) if not ev.evaluate(g).rejected][:2]
    res = run_optuna_campaign(ev, structures, 8, seed=1)
    assert len(res.studies) == len(structures) and all(s.n_trials == 8 for s in res.studies)
    hashes = [c.genome_hash for c in res.candidates]
    assert len(hashes) == len(set(hashes))
    fits = [c.fitness for c in res.candidates]
    assert fits == sorted(fits, reverse=True)
    assert len(res.top_k(3)) <= 3


def test_param_kind_evaluation(env, tmp_path):
    ev = _evaluator(env, tmp_path)
    g = canonicalize(_good(env, ev))
    mid = {n: (lo + hi) / 2 for n, lo, hi, _ in param_space(g)}
    r = ev.evaluate(with_params(g, mid), kind="param")
    assert r.genome_hash and ev.ledger.param_trials == 1


def test_stage_a_min_train_trades_is_60_and_version_bumped(env, tmp_path):
    from alpha.discovery.evaluate import EVALUATOR_VERSION, MIN_TRAIN_TRADES

    assert MIN_TRAIN_TRADES == 60 and EVALUATOR_VERSION == "ad1-genome-eval-v3"
    ev = _evaluator(env, tmp_path)
    assert ev.min_trades == 60  # research/configs/ad1_discovery.json sample_rules


def test_min_train_trades_parameter_changes_cache_fingerprint(env, tmp_path):
    a = _evaluator(env, tmp_path / "a")
    b = GenomeEvaluator(env["features"], env["market"], env["dates"], env["plan"], env["cfg"],
                        tmp_path / "b", TrialLedger(), min_train_trades=250)
    assert a.min_trades == 60 and b.min_trades == 250
    assert a._fp_static != b._fp_static
    g = _genomes(env, 1)[0]
    assert a.fingerprint("h") != b.fingerprint("h")
    assert b.evaluate(g).rejected or b.evaluate(g).train.adverse.screen.n_trades >= 250
