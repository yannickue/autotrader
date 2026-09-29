"""Audit-fix regressions: canonical fixed point, behaviour uniqueness, provenance, OOS banner."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import numpy as np
import pytest

from alpha.discovery import provenance as prov
from alpha.discovery.archetypes import random_genome
from alpha.discovery.audit import q_points
from alpha.discovery.catalog import FeaturePool
from alpha.discovery.compile import (
    ThresholdResolver,
    TrialLedger,
    behavior_key,
    canonical_hash,
    canonicalize,
    compile_genome,
)
from alpha.discovery.evaluate import (
    EVALUATOR_VERSION,
    GenomeEvaluator,
    _library_versions,
    _source_hashes,
)
from alpha.discovery.genome import Clause, Genome, StopGene
from alpha.discovery.search import crossover, mutate_structure
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
    dates = ar2_fast._dates(features)
    return {"features": features, "plan": plan, "dates": dates, "cfg": cfg,
            "market": ar2_fast._market(features),
            "resolver": ThresholdResolver.from_plan(features, plan, dates),
            "pool": FeaturePool.from_features(features)}


def _g(trigger, or_group=()):
    return Genome("LONG", trigger=tuple(trigger), or_group=tuple(or_group),
                  stop=StopGene("atr_multiple", 1.5), target_r=2.0)


def test_auditor_repro_reaches_fixed_point():
    g = _g([Clause("m5_rsi14", ">", 0.6, ())],
           [Clause("m5_rsi14", ">", 0.7, ()), Clause("m5_rsi14", ">", 0.7, ())])
    c1 = canonicalize(g)
    assert canonicalize(c1) == c1 and canonical_hash(c1) == canonical_hash(g)
    assert [(c.feature, c.q) for c in c1.trigger] == [("m5_rsi14", 0.7)] and not c1.or_group


def test_canonicalize_is_idempotent_on_random_and_mutated(env):
    rng = np.random.default_rng(2024)
    pool, n = env["pool"], 0
    gs = [random_genome(rng, pool) for _ in range(1000)]
    for g in gs:
        m = mutate_structure(g, rng, pool)
        x, _ = crossover(g, gs[int(rng.integers(len(gs)))], rng)
        for cand in (g, m, x):
            c = canonicalize(cand)
            assert canonicalize(c) == c
            assert canonical_hash(cand) == canonical_hash(c)
            n += 1
    assert n == 3000


def test_ledger_counts_unique_specs_and_behaviours(env, tmp_path):
    res, feat = env["resolver"], env["features"]
    pair = None
    for name, entry in env["pool"].entries.items():
        if entry.kind not in ("continuous", "signed") or entry.ops[0] != ">":
            continue
        pts = q_points(entry)
        for qa, qb in itertools.pairwise(pts):
            if res.value(entry.feature, qa, entry.floor) == res.value(entry.feature, qb,
                                                                       entry.floor):
                pair = (name, qa, qb)
                break
        if pair:
            break
    assert pair, "no degenerate quantile pair found in the catalog (adapt the candidates)"
    a = _g([Clause(pair[0], ">", pair[1], ())])
    b = _g([Clause(pair[0], ">", pair[2], ())])
    assert canonical_hash(a) != canonical_hash(b)
    assert behavior_key(compile_genome(a, res)) == behavior_key(compile_genome(b, res))
    ev = GenomeEvaluator(feat, env["market"], env["dates"], env["plan"], env["cfg"], tmp_path,
                         TrialLedger())
    ev.evaluate(a)
    ev.evaluate(b)
    ev.evaluate(a)  # duplicate
    led = ev.ledger
    assert led.unique == 2 and led.unique_behaviors == 1 and led.duplicate_rejects == 1
    raw = json.loads(led.to_json())
    assert raw["unique_behaviors"] == 1 and raw["unique"] == raw["unique_specs"] == 2
    assert TrialLedger.from_json(led.to_json()).unique_behaviors == 1


def test_fingerprint_sources_and_versions():
    assert EVALUATOR_VERSION == "ad1-genome-eval-v3"
    hashes = _source_hashes()
    for mod in ("alpha.common.frame", "alpha.common.sim", "alpha.common.protocol",
                "alpha.discovery.genome"):
        assert mod in hashes
    libs = _library_versions()
    assert set(libs) == {"numba", "numpy", "pandas", "talib"} and libs["numpy"] != "absent"


def test_check_pool_provenance_logic():
    exp = {"config_hash": "a", "splits": {"x": 1}, "embargo_days": 5, "dataset_hash": "d",
           "feature_key": "k", "evaluator_version": "v3", "evaluator_fingerprint": "f",
           "min_train_trades": 60}
    cfg = {"sample_rules": {"min_trades_flag": 60}}
    meta = dict(exp)
    del meta["min_train_trades"]  # old pool: falls back to the config default
    assert prov.check_pool_provenance(meta, exp, cfg) == ([], [])
    for k, v in (("config_hash", "b"), ("embargo_days", 7), ("splits", {"x": 2}),
                 ("dataset_hash", "z"), ("feature_key", "q"), ("min_train_trades", 250)):
        errs, _ = prov.check_pool_provenance({**meta, k: v}, exp, cfg)
        assert errs and k in errs[0]
    errs, _ = prov.check_pool_provenance({k: v for k, v in meta.items() if k != "feature_key"},
                                         exp, cfg)
    assert errs == ["pool meta lacks feature_key"]
    stale = {**meta, "evaluator_version": "v2", "evaluator_fingerprint": "old"}
    assert len(prov.check_pool_provenance(stale, exp, cfg)[0]) == 2
    errs, warns = prov.check_pool_provenance(stale, exp, cfg, allow_evaluator_mismatch=True)
    assert not errs and len(warns) == 2


def test_oos_bars_in_a_frame_are_refused(env):
    plan, dates = env["plan"], env["dates"]
    prov.assert_oos_untouched(plan, dates)  # the real dev frame is clean
    oos_day = np.datetime64(plan.oos.start)
    with pytest.raises(AssertionError, match="OOS"):
        prov.assert_oos_untouched(plan, np.concatenate([dates, [oos_day]]).astype("datetime64[D]"))
    late = np.datetime64(plan.oos.end) + np.timedelta64(400, "D")  # beyond OOS end: still refused
    with pytest.raises(AssertionError):
        prov.assert_oos_untouched(plan, np.concatenate([dates, [late]]).astype("datetime64[D]"))


def test_oos_status_reads_ar1_log_and_tolerates_missing(tmp_path):
    txt = prov.oos_status()
    assert "NOT a clean holdout" in txt and "2026-05-01..2026-08-31" in txt
    log = json.loads(prov.AR1_OOS_LOG.read_text(encoding="utf-8"))
    n = sum(1 for e in log if e["kind"] == "evaluation")
    assert f"{n} evaluations" in txt
    (tmp_path / "l.json").write_text(json.dumps([{"kind": "evaluation"}] * 5), encoding="utf-8")
    assert "5 evaluations" in prov.oos_status(tmp_path / "l.json")
    assert "unknown number" in prov.oos_status(tmp_path / "missing.json")
