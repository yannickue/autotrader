# ruff: noqa: E501
"""Directory-transaction publish: crash/interrupt safety, concurrency, stale handling (fast, no feature build)."""

from __future__ import annotations

import json
import os
import threading
import time

import numpy as np
import pytest

from research_workbench import dag

KEY = "ab" * 32


def _publish(store: dag.ArtifactStore, key: str = KEY):
    files = {"payload.npz": dag.npz_bytes({"x": np.arange(10)})}
    return store.publish(
        "M", "SIGNALS", key, files, experiment_id="exp-1", code="c0de", runtime_s=0.5
    )


def _snapshot(directory):
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


def _leftovers(store, key=KEY):
    parent = store.stage_dir("M", "SIGNALS", key).parent
    return [p.name for p in parent.iterdir() if ".tmp." in p.name or ".stale." in p.name]


def _corrupt(store):
    payload = store.stage_dir("M", "SIGNALS", KEY) / "payload.npz"
    data = bytearray(payload.read_bytes())
    data[-1] ^= 0xFF
    payload.write_bytes(bytes(data))


def _boom(*_a, **_k):
    raise KeyboardInterrupt


def test_valid_publication_is_a_noop_and_never_rewritten(tmp_path, monkeypatch) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    before = _snapshot(store.stage_dir("M", "SIGNALS", KEY))
    monkeypatch.setattr(dag, "atomic_write_bytes", _boom)
    monkeypatch.setattr(dag, "atomic_write_json", _boom)
    manifest = _publish(store)  # no write is even attempted
    monkeypatch.undo()
    assert manifest["complete"] is True
    assert _snapshot(store.stage_dir("M", "SIGNALS", KEY)) == before
    assert store.lookup("M", "SIGNALS", KEY).hit and not _leftovers(store)


@pytest.mark.parametrize("where", ["payload", "manifest", "install"])
def test_interrupt_leaves_the_previous_state_untouched(tmp_path, monkeypatch, where) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    _corrupt(store)  # previous publication is INVALID, so a new publish really tries to replace it
    before = _snapshot(store.stage_dir("M", "SIGNALS", KEY))
    target = {
        "payload": (dag, "atomic_write_bytes"),
        "manifest": (dag, "atomic_write_json"),
        "install": (dag.ArtifactStore, "_install"),
    }[where]
    monkeypatch.setattr(*target, _boom)
    with pytest.raises(KeyboardInterrupt):
        _publish(store)
    monkeypatch.undo()
    assert _snapshot(store.stage_dir("M", "SIGNALS", KEY)) == before  # target untouched
    assert not _leftovers(store)  # temp directory discarded
    assert not store.lookup("M", "SIGNALS", KEY).hit  # still invalid, never half-valid


def test_interrupt_never_destroys_a_valid_previous_publication(tmp_path, monkeypatch) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    before = _snapshot(store.stage_dir("M", "SIGNALS", KEY))
    monkeypatch.setattr(dag.ArtifactStore, "_install", _boom)
    monkeypatch.setattr(dag, "atomic_write_json", _boom)
    _publish(store)  # valid previous => no-op, the interrupt hooks are never reached
    monkeypatch.undo()
    assert _snapshot(store.stage_dir("M", "SIGNALS", KEY)) == before
    assert store.lookup("M", "SIGNALS", KEY).hit


def test_invalid_existing_target_is_replaced_via_stale_aside(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    _corrupt(store)
    assert not store.lookup("M", "SIGNALS", KEY).hit
    _publish(store)
    assert store.lookup("M", "SIGNALS", KEY).hit
    assert np.array_equal(
        dag.load_npz(store.stage_dir("M", "SIGNALS", KEY) / "payload.npz")["x"], np.arange(10)
    )
    assert any(".stale." in n for n in _leftovers(store))  # moved aside, never deleted in place


def test_corrupt_then_recompute_flow_through_compute_and_publish(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    _corrupt(store)

    def compute():
        return {"payload.npz": dag.npz_bytes({"x": np.arange(10)})}, {}, "fresh"

    value, _rt, manifest = store.compute_and_publish(
        "exp-1", "M", "SIGNALS", KEY, compute, code="c"
    )
    assert value == "fresh" and manifest["complete"] is True
    assert store.lookup("M", "SIGNALS", KEY).hit
    assert store.read_status("exp-1", "M", "SIGNALS")["status"] == "COMPLETE"


def test_concurrent_publishers_of_the_same_key_end_with_one_valid_publication(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    manifests: list[dict] = []

    def worker():
        try:
            barrier.wait()
            manifests.append(_publish(store))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(manifests) == n and all(m["complete"] for m in manifests)
    assert store.lookup("M", "SIGNALS", KEY).hit
    parent = store.stage_dir("M", "SIGNALS", KEY).parent
    assert [p.name for p in parent.iterdir() if p.name.startswith(KEY[:40])] == [KEY[:40]]


def test_concurrent_publishers_over_an_invalid_target(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    _corrupt(store)
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait()
            _publish(store)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert store.lookup("M", "SIGNALS", KEY).hit


def test_old_leftover_directories_are_cleaned_young_ones_kept(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    parent = store.stage_dir("M", "SIGNALS", KEY).parent
    parent.mkdir(parents=True)
    old, young = parent / f"{KEY[:40]}.tmp.old", parent / f"{KEY[:40]}.stale.young"
    old.mkdir()
    young.mkdir()
    ancient = time.time() - dag.LEFTOVER_MAX_AGE_S - 60
    os.utime(old, (ancient, ancient))
    _publish(store)
    assert not old.exists() and young.exists()


def test_running_status_with_a_dead_owner_is_reported_stale(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    store.set_status("exp-1", "M", "SIGNALS", dag.StageStatus.RUNNING)
    assert (
        store.read_status("exp-1", "M", "SIGNALS")["status"] == "RUNNING"
    )  # live owner (this process)
    path = store.run_dir("exp-1") / "status" / "M__SIGNALS.json"
    data = json.loads(path.read_text("utf-8"))
    data["pid"] = 2_000_000_000  # no such process
    path.write_text(json.dumps(data), encoding="utf-8")
    status = store.read_status("exp-1", "M", "SIGNALS")
    assert status["status"] == "STALE" and status["was"] == "RUNNING"
    assert not store.lookup("M", "SIGNALS", KEY).hit  # status never creates a cache hit
