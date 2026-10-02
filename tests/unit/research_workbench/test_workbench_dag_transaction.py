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


def test_invalid_existing_target_is_replaced_after_a_verified_move_aside(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    _corrupt(store)
    assert not store.lookup("M", "SIGNALS", KEY).hit
    _publish(store)
    assert store.lookup("M", "SIGNALS", KEY).hit
    assert np.array_equal(
        dag.load_npz(store.stage_dir("M", "SIGNALS", KEY) / "payload.npz")["x"], np.arange(10)
    )
    assert not _leftovers(
        store
    )  # the verified-invalid aside copy is removed after the successful install


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


def _mk_tmp(parent, suffix, pid=None, age_s=0.0):
    d = parent / f"{KEY[:40]}.tmp.{suffix}"
    d.mkdir(parents=True)
    if pid is not None:
        (d / ".owner").write_text(json.dumps({"pid": pid, "token": "t"}), encoding="utf-8")
    if age_s:
        old = time.time() - age_s
        os.utime(d, (old, old))
    return d


def test_leftover_cleanup_respects_ownership_and_age(tmp_path) -> None:
    store = dag.ArtifactStore(tmp_path)
    parent = store.stage_dir("M", "SIGNALS", KEY).parent
    parent.mkdir(parents=True)
    live_old = _mk_tmp(
        parent, "live", pid=os.getpid(), age_s=dag.LEFTOVER_MAX_AGE_S + 600
    )  # live owner, older than 30 min
    dead = _mk_tmp(parent, "dead", pid=2_000_000_000)  # dead owner, brand new
    no_marker_young = _mk_tmp(parent, "nomarker")
    no_marker_ancient = _mk_tmp(parent, "ancient", age_s=dag.TMP_MAX_AGE_S + 60)
    stale_young = parent / f"{KEY[:40]}.stale.young"
    stale_old = parent / f"{KEY[:40]}.stale.old"
    stale_young.mkdir()
    stale_old.mkdir()
    old = time.time() - dag.LEFTOVER_MAX_AGE_S - 60
    os.utime(stale_old, (old, old))
    result = dag.ArtifactStore._cleanup_leftovers(parent, KEY[:40])
    assert (
        live_old.exists() and no_marker_young.exists() and stale_young.exists()
    )  # a live slow publisher is never hit
    assert not dead.exists() and not no_marker_ancient.exists() and not stale_old.exists()
    assert sorted(result["removed"]) == sorted([dead.name, no_marker_ancient.name, stale_old.name])
    assert result["failed"] == []


def test_leftover_removal_failure_is_reported_not_claimed(tmp_path, monkeypatch) -> None:
    store = dag.ArtifactStore(tmp_path)
    parent = store.stage_dir("M", "SIGNALS", KEY).parent
    parent.mkdir(parents=True)
    dead = _mk_tmp(parent, "dead", pid=2_000_000_000)
    monkeypatch.setattr(dag.shutil, "rmtree", lambda *a, **k: None)  # removal silently fails
    monkeypatch.setattr(dag.time, "sleep", lambda *_: None)
    result = dag.ArtifactStore._cleanup_leftovers(parent, KEY[:40])
    assert dead.exists() and result["failed"] == [dead.name] and result["removed"] == []


def test_interleaved_valid_install_between_check_and_move_survives(tmp_path, monkeypatch) -> None:
    """Deterministic interleaving: A saw an INVALID target; before A moves it aside, B installs a VALID publication."""
    donor = dag.ArtifactStore(tmp_path / "donor")
    _publish(donor)
    valid_dir = donor.stage_dir("M", "SIGNALS", KEY)
    store = dag.ArtifactStore(tmp_path / "root")
    _publish(store)
    _corrupt(store)  # A's view: the target is invalid
    final = store.stage_dir("M", "SIGNALS", KEY)
    fired = []

    def b_installs(self, target):
        import shutil

        fired.append(True)
        shutil.rmtree(target)  # B replaces the invalid target by its valid publication
        shutil.copytree(valid_dir, target)

    monkeypatch.setattr(dag.ArtifactStore, "_before_move_aside", b_installs)
    manifest = _publish(store)  # A
    monkeypatch.undo()
    assert fired == [True]
    assert manifest["complete"] is True
    assert store.lookup("M", "SIGNALS", KEY).hit  # B's publication survived
    assert _snapshot(final) == _snapshot(valid_dir)
    assert not _leftovers(store)  # A moved nothing aside and discarded its temp dir


def test_failing_install_degrades_to_uncached_and_restores_the_old_target(
    tmp_path, monkeypatch
) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    _corrupt(store)
    final = store.stage_dir("M", "SIGNALS", KEY)
    before = _snapshot(final)
    real_rename = os.rename

    def failing(src, dst, *a, **k):
        if str(dst).endswith(KEY[:40]):
            raise PermissionError("sharing violation")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(dag.os, "rename", failing)
    monkeypatch.setattr(dag, "INSTALL_BUDGET_S", 0.3)

    def compute():
        return {"payload.npz": dag.npz_bytes({"x": np.arange(10)})}, {}, "fresh"

    value, _rt, manifest = store.compute_and_publish(
        "exp-1", "M", "SIGNALS", KEY, compute, code="c"
    )
    monkeypatch.undo()
    assert value == "fresh" and manifest is None  # computed result returned, no exception leaked
    status = store.read_status("exp-1", "M", "SIGNALS")
    assert status["status"] == "COMPLETE" and "cache_write_skipped" in status
    assert _snapshot(final) == before  # the previous target was restored, not destroyed
    assert not [n for n in _leftovers(store) if ".tmp." in n]


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
