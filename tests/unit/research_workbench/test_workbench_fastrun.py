# ruff: noqa: E501
"""FAST path: speed invariants, cold==warm, parity with the direct API, market isolation, plan purity.

HEAVY (builds real FeatureStore arrays + numba kernels, ~1-2 min): should be designated slow in tests/conftest.py.
"""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace

import numpy as np
import pytest

from alpha.common.sim import COST_SCENARIOS
from alpha.fast.sim import simulate_fast
from alpha.fast.spec import evaluate_spec
from alpha.fast.store import FeatureStore
from research_workbench import dag, fastrun
from research_workbench.experiment import DatasetRef, load_market_frame, normalise_frame
from research_workbench.fastrun import (
    FastGate,
    market_arrays,
    market_from_features,
    plan_experiment,
    run_fast,
    run_market,
)
from tests.unit.research_workbench.test_workbench_helpers import (
    LOOSE_GATE,
    experiment,
    stage_cache,
    stage_computed,
    strategy,
)

ALL_HIT = {"FEATURES": "HIT", "SIGNALS": "HIT", "SIMULATION": "HIT", "METRICS": "HIT"}


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    root = tmp_path_factory.mktemp("wb_base")
    exp = experiment(str(root))
    cold = run_market(exp, "SYN_A", gate=LOOSE_GATE)
    return root, exp, cold


def _copy(base_root, tmp_path):
    target = tmp_path / "root"
    shutil.copytree(base_root, target)
    return target


def _metrics_file(root, record, market="SYN_A"):
    path = dag.ArtifactStore(root).stage_dir(market, "METRICS", record["keys"]["METRICS"])
    return json.loads((path / "metrics.json").read_text("utf-8"))


def _trades(root, record, market="SYN_A"):
    path = dag.ArtifactStore(root).stage_dir(market, "SIMULATION", record["keys"]["SIMULATION"])
    return dag.load_npz(path / "trades.npz")


def test_cold_run_computes_everything_and_warm_run_is_all_hit(base) -> None:
    root, exp, cold = base
    assert all(cold["stages"][s]["computed"] for s in ("SIGNALS", "SIMULATION", "METRICS"))
    warm = run_market(exp, "SYN_A", gate=LOOSE_GATE)
    assert stage_cache(warm) == ALL_HIT
    assert not any(stage_computed(warm).values())
    # cold == warm: identical keys, status and stored metrics
    assert warm["keys"] == cold["keys"] and warm["status"] == cold["status"]
    assert _metrics_file(root, warm) == _metrics_file(root, cold)
    for name, value in _trades(root, cold).items():
        assert np.array_equal(value, _trades(root, warm)[name], equal_nan=True)


def test_wrapper_results_equal_direct_api_bit_identical(base) -> None:
    root, exp, cold = base
    frame = load_market_frame(exp, "SYN_A")
    features = FeatureStore.build(frame, exp.feature_config)
    candidates = evaluate_spec(features, exp.strategy_spec)
    direct = simulate_fast(
        market_arrays(market_from_features(features)), candidates, exp.cost_model, exp.sizing,
        exp.rules, exp.window,
    )  # fmt: skip
    stored = _trades(root, cold)
    assert len(direct) > 0
    for name in direct.__dataclass_fields__:
        assert np.array_equal(getattr(direct, name), stored[name], equal_nan=True), name
        assert getattr(direct, name).dtype == stored[name].dtype
    cand_dir = dag.ArtifactStore(root).stage_dir("SYN_A", "SIGNALS", cold["keys"]["SIGNALS"])
    stored_cand = dag.load_npz(cand_dir / "candidates.npz")
    assert np.array_equal(stored_cand["decision_idx"], candidates.decision_idx)
    assert np.array_equal(stored_cand["stop"], candidates.stop, equal_nan=True)


def test_identical_run_hits_every_stage_and_metric_only_change_recomputes_only_metrics(
    base, tmp_path
) -> None:
    root, exp, _ = base
    copy = _copy(root, tmp_path)
    # (a) metric-only: a different metric_version (same trades, same everything upstream)
    rec = run_market(
        replace(exp, artifact_root=str(copy)),
        "SYN_A",
        gate=LOOSE_GATE,
        metric_version="wb-metrics-X",
    )
    assert stage_cache(rec) == {
        "FEATURES": "HIT",
        "SIGNALS": "HIT",
        "SIMULATION": "HIT",
        "METRICS": "MISS",
    }
    assert stage_computed(rec) == {
        "FEATURES": False,
        "SIGNALS": False,
        "SIMULATION": False,
        "METRICS": True,
    }
    # (b) metric-only: the split (windows of train/validation) is a metrics input, not a simulation input
    split = replace(exp.split, embargo_days=1)
    rec2 = run_market(replace(exp, artifact_root=str(copy), split=split), "SYN_A", gate=LOOSE_GATE)
    assert stage_cache(rec2) == {
        "FEATURES": "HIT",
        "SIGNALS": "HIT",
        "SIMULATION": "HIT",
        "METRICS": "MISS",
    }
    assert not any(v for s, v in stage_computed(rec2).items() if s != "METRICS")


def test_cost_only_change_hits_features_and_signals_but_misses_simulation(base, tmp_path) -> None:
    root, exp, _ = base
    copy = _copy(root, tmp_path)
    rec = run_market(
        replace(exp, artifact_root=str(copy), cost_model=COST_SCENARIOS["SPREAD_STRESS"]),
        "SYN_A",
        gate=LOOSE_GATE,
    )
    assert stage_cache(rec)["FEATURES"] == "HIT" and stage_cache(rec)["SIGNALS"] == "HIT"
    assert stage_cache(rec)["SIMULATION"] == "MISS" and stage_cache(rec)["METRICS"] == "MISS"
    assert stage_computed(rec) == {
        "FEATURES": False,
        "SIGNALS": False,
        "SIMULATION": True,
        "METRICS": True,
    }


def test_entry_rule_change_hits_features_but_misses_signals_and_simulation(base, tmp_path) -> None:
    root, exp, _ = base
    copy = _copy(root, tmp_path)
    rec = run_market(
        replace(exp, artifact_root=str(copy), strategy_spec=strategy(threshold=30.0)),
        "SYN_A",
        gate=LOOSE_GATE,
    )
    cache = stage_cache(rec)
    assert cache["FEATURES"] == "HIT"
    assert (
        cache["SIGNALS"] == "MISS" and cache["SIMULATION"] == "MISS" and cache["METRICS"] == "MISS"
    )
    assert (
        stage_computed(rec)["FEATURES"] is False
    )  # loaded from the FeatureStore cache, nothing rebuilt


def test_corrupt_artifact_is_a_miss_and_is_recomputed_identically(base, tmp_path) -> None:
    root, exp, cold = base
    copy = _copy(root, tmp_path)
    store = dag.ArtifactStore(copy)
    victim = store.stage_dir("SYN_A", "SIMULATION", cold["keys"]["SIMULATION"]) / "trades.npz"
    victim.write_bytes(victim.read_bytes()[:50])  # truncated => size/sha mismatch
    assert not store.lookup("SYN_A", "SIMULATION", cold["keys"]["SIMULATION"]).hit
    # METRICS still valid on its own, but remove it so the corrupted SIMULATION must be rebuilt
    shutil.rmtree(store.stage_dir("SYN_A", "METRICS", cold["keys"]["METRICS"]))
    rec = run_market(replace(exp, artifact_root=str(copy)), "SYN_A", gate=LOOSE_GATE)
    assert stage_cache(rec)["SIMULATION"] == "MISS" and stage_computed(rec)["SIMULATION"] is True
    for name, value in _trades(copy, rec).items():
        assert np.array_equal(value, _trades(root, cold)[name], equal_nan=True)
    assert store.lookup("SYN_A", "SIMULATION", rec["keys"]["SIMULATION"]).hit


def test_fast_outcome_is_only_reject_or_promote_with_explicit_reasons(base) -> None:
    _, exp, cold = base
    assert cold["status"] in {"REJECT_FAST", "PROMOTE_TO_FIDELITY"}
    assert cold["status"] == "PROMOTE_TO_FIDELITY" and cold["reasons"] == []  # loose gate
    strict = run_market(exp, "SYN_A", gate=FastGate(min_train_trades=10_000))
    assert strict["status"] == "REJECT_FAST"
    assert "too_few_trades_train" in strict["reasons"]


def test_market_a_complete_survives_market_b_failure_and_rerun_hits(tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    a = normalise_frame(DatasetRef(kind="synthetic", seed=5, days=30, start="2024-03-04").load("A"))
    a.to_csv(data / "A.csv", index=False)  # B.csv deliberately missing
    root = tmp_path / "art"
    exp = experiment(
        str(root),
        markets=("A", "B"),
        dataset=DatasetRef(kind="csv", path=str(data / "{market}.csv")),
    )
    first = run_fast(exp, jobs=1, gate=LOOSE_GATE)
    assert first["markets"]["A"]["status"] == "PROMOTE_TO_FIDELITY"
    assert first["markets"]["B"]["status"] == "FAILED" and first["n_failed"] == 1
    store = dag.ArtifactStore(root)
    for stage in ("FEATURES", "SIGNALS", "SIMULATION", "METRICS"):
        assert store.lookup("A", stage, first["markets"]["A"]["keys"][stage]).hit
    second = run_fast(exp, jobs=1, gate=LOOSE_GATE)
    assert stage_cache(second["markets"]["A"]) == ALL_HIT
    assert not any(stage_computed(second["markets"]["A"]).values())
    assert second["markets"]["B"]["status"] == "FAILED"


@contextmanager
def _thread_pool(
    n, initializer=None, initargs=()
):  # stands in for the process pool (RAM-safe in tests)
    with ThreadPoolExecutor(max_workers=n) as ex:
        yield ex


def test_jobs_one_equals_jobs_two_outputs(tmp_path, monkeypatch) -> None:
    """The scheduler's n>1 path (completion order independent) with a thread stand-in for the process pool."""
    monkeypatch.setattr("research_speed.scheduler.managed_pool", _thread_pool)
    monkeypatch.setattr(
        "research_speed.scheduler.clamp_jobs", lambda jobs, n_tasks: min(jobs, n_tasks)
    )
    monkeypatch.setattr(fastrun, "clamp_jobs", lambda jobs, n_tasks: min(jobs, n_tasks))
    results = {}
    for jobs in (1, 2):
        exp = experiment(str(tmp_path / f"j{jobs}"), markets=("SYN_A", "SYN_B"))
        out = run_fast(exp, jobs=jobs, gate=LOOSE_GATE)
        assert out["jobs_effective"] == jobs
        results[jobs] = out
    for market in ("SYN_A", "SYN_B"):
        r1, r2 = results[1]["markets"][market], results[2]["markets"][market]
        assert (r1["status"], r1["reasons"], r1["keys"], r1["dataset_hash"]) == (
            r2["status"], r2["reasons"], r2["keys"], r2["dataset_hash"],
        )  # fmt: skip
        m1 = _metrics_file(tmp_path / "j1", r1, market)
        m2 = _metrics_file(tmp_path / "j2", r2, market)
        assert m1 == m2


def test_plan_performs_no_simulation_and_no_writes(base, tmp_path, monkeypatch) -> None:
    root, exp, _cold = base
    copy = _copy(root, tmp_path)
    before = sorted(str(p.relative_to(copy)) for p in copy.rglob("*"))

    def boom(*_a, **_k):
        raise AssertionError("simulation/feature build during plan")

    monkeypatch.setattr(fastrun, "simulate_fast", boom)
    monkeypatch.setattr(FeatureStore, "build", staticmethod(boom))
    monkeypatch.setattr(FeatureStore, "load_or_build", staticmethod(boom))
    plan = plan_experiment(replace(exp, artifact_root=str(copy)), jobs=1, gate=LOOSE_GATE)
    assert {s: v["cache"] for s, v in plan["markets"]["SYN_A"]["stages"].items()} == ALL_HIT
    assert plan["markets"]["SYN_A"]["expected_segments"] == []
    assert plan["classification"] == "LIGHT"
    assert sorted(str(p.relative_to(copy)) for p in copy.rglob("*")) == before
    # a cold plan reports MISS everywhere and still writes nothing (not even the artifact root)
    cold_root = tmp_path / "never_created"
    cold_plan = plan_experiment(replace(exp, artifact_root=str(cold_root)), gate=LOOSE_GATE)
    assert all(v["cache"] == "MISS" for v in cold_plan["markets"]["SYN_A"]["stages"].values())
    assert not cold_root.exists()


def test_same_size_corruption_of_the_feature_cache_is_a_miss(base, tmp_path) -> None:
    root, exp, _ = base
    copy = _copy(root, tmp_path)
    exp = replace(exp, artifact_root=str(copy))
    store = dag.ArtifactStore(copy)
    _, _, keys = fastrun._prepare(exp, "SYN_A", fastrun.METRIC_VERSION, LOOSE_GATE)
    assert fastrun._features_lookup(store, "SYN_A", keys).hit
    npz = next((copy / "features").glob("*/features.npz"))
    data = bytearray(npz.read_bytes())
    data[len(data) // 2] ^= 0xFF  # same size, different content
    npz.write_bytes(bytes(data))
    lookup = fastrun._features_lookup(store, "SYN_A", keys)
    assert not lookup.hit and lookup.reason == "FEATURE_CACHE_CHANGED"


def test_embargo_and_gap_bars_are_loaded_but_excluded_from_metrics(tmp_path) -> None:
    from alpha.common.protocol import Partition, SplitPlan

    split = SplitPlan(
        Partition("TRAIN", "2024-03-04", "2024-03-15"),
        Partition("VALIDATION", "2024-03-25", "2024-04-05"),
        Partition("OOS", "2024-04-08", "2024-04-30"),
        embargo_days=3,
    )
    exp = experiment(str(tmp_path), split=split)
    record = run_market(exp, "SYN_A", gate=LOOSE_GATE)
    scope = record["data_scope"]
    assert scope["gap_or_embargo_bars"] > 0  # gap Mar16-24 + embargo Mar25-27 were loaded...
    assert scope["bars_loaded_but_not_in_metrics"] >= scope["gap_or_embargo_bars"]
    assert scope["partitions_feeding_metrics"] == ["TRAIN", "VALIDATION"]
    assert "limitation" in scope["causality"].lower()
    store = dag.ArtifactStore(tmp_path)
    market = dag.load_npz(
        store.stage_dir("SYN_A", "SIGNALS", record["keys"]["SIGNALS"]) / "market.npz"
    )
    trades = _trades(tmp_path, record)
    entry_dates = market["date_days"][trades["entry_idx"]].astype("datetime64[D]")
    in_train = split.mask(entry_dates, split.train)
    in_val = split.mask(entry_dates, split.validation)  # excludes the 3 embargo days
    outside = ~(in_train | in_val)
    assert outside.any(), "test data must contain trades in gap/embargo days"
    metrics = _metrics_file(tmp_path, record)
    assert metrics["contract"]["trade_count"] == int((in_train | in_val).sum())
    assert metrics["train"]["n_trades"] == int(in_train.sum())
    assert metrics["validation"]["n_trades"] == int(in_val.sum())
    assert metrics["contract"]["trade_count"] < len(trades["entry_idx"])


def test_real_process_pool_jobs_one_equals_jobs_two(tmp_path) -> None:
    from research_speed.parallel import available_memory_mb, clamp_jobs

    free = available_memory_mb()
    if free is None or free < 1500:
        pytest.skip(f"real process pool needs >= 1.5 GB free RAM (free: {free} MB)")
    try:
        clamp_jobs(2, 2)
    except Exception as exc:  # InsufficientMemoryError: reserve + worker not available
        pytest.skip(f"RAM guard refuses 2 workers: {exc}")
    tiny = DatasetRef(kind="synthetic", seed=3, days=12, start="2024-03-04")
    results = {}
    for jobs in (1, 2):
        exp = experiment(str(tmp_path / f"p{jobs}"), markets=("SYN_A", "SYN_B"), dataset=tiny)
        results[jobs] = run_fast(exp, jobs=jobs, gate=LOOSE_GATE)
    assert results[2]["jobs_effective"] == 2
    for market in ("SYN_A", "SYN_B"):
        r1, r2 = results[1]["markets"][market], results[2]["markets"][market]
        assert (r1["status"], r1["keys"], r1["dataset_hash"]) == (
            r2["status"],
            r2["keys"],
            r2["dataset_hash"],
        )
        assert _metrics_file(tmp_path / "p1", r1, market) == _metrics_file(
            tmp_path / "p2", r2, market
        )
