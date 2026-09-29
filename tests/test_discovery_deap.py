from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha.discovery import deap_driver
from alpha.discovery.compile import TrialLedger, canonical_hash
from alpha.discovery.deap_driver import evolve_structures, lineage_family
from alpha.discovery.evaluate import GenomeEvaluator
from alpha.discovery.genome import is_valid
from alpha.fast.store import FeatureStore

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "research/configs/ad1_discovery.json"
CACHE = REPO / "data/feature_store/ad1_bench_gram"


@pytest.fixture(scope="module")
def env():
    from alpha.common.dataset import POINT, load_research_dataset
    from alpha.discovery.catalog import FeaturePool
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


def _ev(env, tmp_path):
    return GenomeEvaluator(env["features"], env["market"], env["dates"], env["plan"], env["cfg"],
                           tmp_path, TrialLedger())


def test_lineage_family():
    assert lineage_family("MUT:BREAKOUT") == "BREAKOUT"
    assert lineage_family("X:A|B") == "A"
    assert lineage_family("MUT:X:A|B") == "A"
    assert lineage_family("HYBRID") == "HYBRID"


def test_no_validation_access():
    src = Path(deap_driver.__file__).read_text(encoding="utf-8")
    for token in ("validation_gate_view", "_validation", "ValidationView"):
        assert token not in src


@pytest.fixture(scope="module")
def run_a(env, tmp_path_factory):
    return evolve_structures(_ev(env, tmp_path_factory.mktemp("a")), env["pool"], 40, 6, seed=7)


def test_end_to_end_improves_and_is_valid(run_a):
    assert len(run_a.stats) == 7
    assert run_a.stats[-1].best > run_a.stats[0].best
    assert run_a.stats[-1].best >= max(s.best for s in run_a.stats) - 1e-12  # elitist
    for c in run_a.population + run_a.hall_of_fame:
        assert is_valid(c.genome) and not c.evaluation.rejected
    assert all(s.unique_hashes == sum(s.lineage_histogram.values()) for s in run_a.stats)
    hof = run_a.hof_hashes
    assert len(set(hof)) == len(hof)
    fits = [c.fitness for c in run_a.hall_of_fame]
    assert fits == sorted(fits, reverse=True)


def test_lineage_cap_respected(run_a):
    cap = -(-40 * deap_driver.LINEAGE_CAP_FRACTION // 1)
    for s in run_a.stats:
        if not s.cap_relaxed:
            assert max(s.lineage_histogram.values()) <= cap
    assert run_a.stats[0].unique_hashes == sum(run_a.stats[0].lineage_histogram.values())


def test_determinism(env, tmp_path, run_a):
    other = evolve_structures(_ev(env, tmp_path), env["pool"], 40, 6, seed=7)
    assert other.hof_hashes == run_a.hof_hashes
    assert [s.best for s in other.stats] == [s.best for s in run_a.stats]


def test_budget_respected(env, tmp_path):
    ev = _ev(env, tmp_path)
    res = evolve_structures(ev, env["pool"], 20, 10, seed=3, max_evaluations=45)
    assert res.evaluations_used <= 45 and res.budget_exhausted
    assert ev.ledger.structural_trials <= 45
    assert len({canonical_hash(c.genome) for c in res.scored.values()}) <= 45
