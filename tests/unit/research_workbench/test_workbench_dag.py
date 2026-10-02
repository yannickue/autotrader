# ruff: noqa: E501
"""Artifact store: atomic publish, verified load, status handling, stage keys (fast; no feature build)."""

from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytest

from alpha.common.sim import COST_SCENARIOS
from research_workbench import dag
from tests.unit.research_workbench.test_workbench_helpers import experiment, strategy

KEY = "ab" * 32


def _publish(store: dag.ArtifactStore, key: str = KEY, **kwargs):
    files = {"payload.npz": dag.npz_bytes({"x": np.arange(10)})}
    return store.publish(
        "M", "SIGNALS", key, files, experiment_id="exp-1", code="c0de", runtime_s=0.5, **kwargs
    )


def test_publish_writes_manifest_last_with_required_fields(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    manifest = _publish(store)
    for field in (
        "experiment_id", "market", "stage", "fingerprint", "artifact_sha256", "code_hash",
        "runtime_s", "created_utc", "complete",
    ):  # fmt: skip
        assert field in manifest
    assert manifest["complete"] is True
    lookup = store.lookup("M", "SIGNALS", KEY)
    assert lookup.hit and lookup.reason == "HIT"
    assert np.array_equal(
        dag.load_npz(store.stage_dir("M", "SIGNALS", KEY) / "payload.npz")["x"], np.arange(10)
    )
    assert not list(tmp_path.rglob("*.tmp-*"))  # no temp leftovers


def test_interrupted_publish_never_looks_complete(tmp_path, monkeypatch) -> None:
    store = dag.ArtifactStore(tmp_path)

    def boom(path, obj):
        raise KeyboardInterrupt

    monkeypatch.setattr(dag, "atomic_write_json", boom)  # dies exactly at the commit marker
    with pytest.raises(KeyboardInterrupt):
        _publish(store)
    monkeypatch.undo()
    lookup = store.lookup("M", "SIGNALS", KEY)
    assert not lookup.hit and lookup.reason == "NO_MANIFEST"
    # payload files exist (they were published first) but without the marker they are never served


def test_ctrl_c_during_compute_leaves_failed_status_and_no_complete(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)

    def compute():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        store.compute_and_publish("exp-1", "M", "SIGNALS", KEY, compute, code="c")
    status = store.read_status("exp-1", "M", "SIGNALS")
    assert status["status"] == "FAILED" and status["interrupted"] is True
    assert not store.lookup("M", "SIGNALS", KEY).hit


def test_status_goes_running_then_complete_only_after_the_manifest(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    seen = []

    def compute():
        seen.append(store.read_status("exp-1", "M", "SIMULATION")["status"])
        return {"a.npz": dag.npz_bytes({"x": np.zeros(3)})}, {}, "value"

    value, _runtime, manifest = store.compute_and_publish(
        "exp-1", "M", "SIMULATION", KEY, compute, code="c"
    )
    assert seen == ["RUNNING"] and value == "value" and manifest["complete"] is True
    assert store.read_status("exp-1", "M", "SIMULATION")["status"] == "COMPLETE"
    assert {s.value for s in dag.StageStatus} == {"PENDING", "RUNNING", "COMPLETE", "FAILED"}


@pytest.mark.parametrize(
    "damage", ["truncate", "flip", "delete", "bad_manifest", "wrong_key", "incomplete"]
)
def test_any_damage_is_a_miss_never_an_error(tmp_path, damage) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    directory = store.stage_dir("M", "SIGNALS", KEY)
    payload = directory / "payload.npz"
    manifest = directory / "manifest.json"
    if damage == "truncate":
        payload.write_bytes(payload.read_bytes()[:20])
    elif damage == "flip":
        data = bytearray(payload.read_bytes())
        data[-1] ^= 0xFF
        payload.write_bytes(bytes(data))
    elif damage == "delete":
        payload.unlink()
    elif damage == "bad_manifest":
        manifest.write_text("{not json", encoding="utf-8")
    elif damage == "wrong_key":
        data = json.loads(manifest.read_text("utf-8"))
        data["fingerprint"] = "00" * 32
        manifest.write_text(json.dumps(data), encoding="utf-8")
    elif damage == "incomplete":
        data = json.loads(manifest.read_text("utf-8"))
        data["complete"] = False
        manifest.write_text(json.dumps(data), encoding="utf-8")
    assert not store.lookup("M", "SIGNALS", KEY).hit


def test_uncertainty_is_a_miss(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    assert store.lookup("M", "SIGNALS", KEY, cacheable=False).reason == "UNCACHEABLE"
    assert not store.lookup("OTHER", "SIGNALS", KEY).hit
    assert not store.lookup("M", "SIMULATION", KEY).hit
    assert not store.lookup("M", "SIGNALS", "cd" * 32).hit


def _keys(exp, **kwargs):
    defaults = {"metric_version": "v1", "gate": {"g": 1}, "fastrun_digest": "d"}
    return dag.compute_keys(exp, "M", "dataset-hash", **{**defaults, **kwargs})


def test_stage_keys_chain_and_change_only_downstream(tmp_path) -> None:
    exp = experiment(str(tmp_path))
    base = _keys(exp)
    assert base.cacheable
    assert _keys(exp) == base
    cost = _keys(replace(exp, cost_model=COST_SCENARIOS["SPREAD_STRESS"]))
    assert (cost.features, cost.signals) == (base.features, base.signals)
    assert cost.simulation != base.simulation and cost.metrics != base.metrics
    rule = _keys(replace(exp, strategy_spec=strategy(threshold=30.0)))
    assert rule.features == base.features
    assert rule.signals != base.signals and rule.simulation != base.simulation
    metric = _keys(exp, metric_version="v2")
    assert (metric.features, metric.signals, metric.simulation) == (
        base.features,
        base.signals,
        base.simulation,
    )
    assert metric.metrics != base.metrics
    data = dag.compute_keys(
        exp, "M", "other-dataset", metric_version="v1", gate={"g": 1}, fastrun_digest="d"
    )
    assert data.features != base.features and data.signals != base.signals


def test_code_hash_uses_import_closure_and_is_cacheable() -> None:
    for entries in (dag.FEATURE_CODE, dag.SIGNAL_CODE, dag.SIM_CODE, dag.METRIC_CODE):
        value = dag.code_hash(entries)
        assert len(value) == 64 and not value.startswith(dag.UNCACHEABLE), (entries, value)


def test_fidelity_and_report_keys_depend_on_their_inputs() -> None:
    a = dag.fidelity_key("s1", COST_SCENARIOS["BASE"], {"x": 1}, "1.0")
    assert a != dag.fidelity_key("s2", COST_SCENARIOS["BASE"], {"x": 1}, "1.0")
    assert a != dag.fidelity_key("s1", COST_SCENARIOS["BASE"], {"x": 1}, "1.1")
    assert dag.report_key({"A": "1"}) != dag.report_key({"A": "2"})
