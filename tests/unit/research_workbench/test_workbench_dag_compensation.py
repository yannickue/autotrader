# ruff: noqa: E501
"""Compensating install check, lock-timeout policy, bounded lock registry, cache-write-skipped visibility."""

from __future__ import annotations

import contextlib
import gc
import shutil

import numpy as np

from research_workbench import dag
from research_workbench.fastrun import plan_experiment, run_market
from research_workbench.report import build_report, render_markdown
from tests.unit.research_workbench.test_workbench_dag_transaction import (
    KEY,
    _corrupt,
    _leftovers,
    _publish,
    _snapshot,
)
from tests.unit.research_workbench.test_workbench_helpers import LOOSE_GATE, experiment


def test_valid_publication_installed_in_the_gap_before_the_move_is_restored(
    tmp_path, monkeypatch
) -> None:
    """The seam fires AFTER the second verification: the target is invalid when checked, valid when moved."""
    donor = dag.ArtifactStore(tmp_path / "donor")
    _publish(donor)
    valid_dir = donor.stage_dir("M", "SIGNALS", KEY)
    store = dag.ArtifactStore(tmp_path / "root")
    _publish(store)
    _corrupt(store)
    final = store.stage_dir("M", "SIGNALS", KEY)
    fired = []

    def concurrent_valid_install(self, target):
        fired.append(True)
        shutil.rmtree(target)
        shutil.copytree(valid_dir, target)  # another publisher's valid, COMPLETE publication

    monkeypatch.setattr(dag.ArtifactStore, "_before_move", concurrent_valid_install)
    manifest = _publish(store)
    monkeypatch.undo()
    assert fired == [True] and manifest["complete"] is True
    assert store.lookup("M", "SIGNALS", KEY).hit  # restored: the valid publication survived
    assert _snapshot(final) == _snapshot(valid_dir)  # no data lost
    assert not _leftovers(store)  # nothing left aside, our temp directory discarded


def test_lock_timeout_never_touches_an_existing_target(tmp_path, monkeypatch) -> None:
    store = dag.ArtifactStore(tmp_path)
    _publish(store)
    _corrupt(store)
    final = store.stage_dir("M", "SIGNALS", KEY)
    before = _snapshot(final)

    @contextlib.contextmanager
    def timed_out(_parent):
        yield False

    monkeypatch.setattr(dag, "_publish_lock", timed_out)

    def compute():
        return {"payload.npz": dag.npz_bytes({"x": np.arange(10)})}, {}, "fresh"

    value, _rt, manifest = store.compute_and_publish(
        "exp-1", "M", "SIGNALS", KEY, compute, code="c"
    )
    assert value == "fresh" and manifest is None
    status = store.read_status("exp-1", "M", "SIGNALS")
    assert status["status"] == "COMPLETE" and status["cache_write_skipped"].endswith("lock_timeout")
    assert _snapshot(final) == before and not [n for n in _leftovers(store) if ".tmp." in n]
    # no existing target: the atomic rename still installs safely without the lock
    fresh = dag.ArtifactStore(tmp_path / "fresh")
    _publish(fresh)
    assert fresh.lookup("M", "SIGNALS", KEY).hit


def test_thread_lock_registry_does_not_grow(tmp_path) -> None:
    for i in range(20):
        _publish(dag.ArtifactStore(tmp_path / f"r{i}"))
    gc.collect()
    assert len(dag._THREAD_LOCKS) == 0


def test_skipped_cache_write_is_visible_in_report_and_plan(tmp_path, monkeypatch) -> None:
    real_publish = dag.ArtifactStore.publish

    def publish(self, market, stage, key, files, **kw):
        if stage == "METRICS":
            raise dag.CacheWriteSkipped("lock_timeout")
        return real_publish(self, market, stage, key, files, **kw)

    monkeypatch.setattr(dag.ArtifactStore, "publish", publish)
    exp = experiment(str(tmp_path))
    record = run_market(exp, "SYN_A", gate=LOOSE_GATE)
    assert record["stages"]["METRICS"]["cache_write_skipped"].endswith("lock_timeout")
    assert record["metrics_inline"]["status"] == record["status"]
    store = dag.ArtifactStore(tmp_path)
    assert not store.lookup("SYN_A", "METRICS", record["keys"]["METRICS"]).hit
    report = build_report(exp, store, {"SYN_A": record})
    market = report["MARKETS"]["SYN_A"]
    assert (
        market["FAST_STATUS"] == record["status"]
    )  # still readable although the artifact was not written
    assert market["ARTIFACTS"]["METRICS"]["state"].startswith("COMPLETE (cache write skipped: ")
    assert market["ARTIFACTS"]["SIGNALS"]["state"] in {"HIT", "MISS"}
    assert "METRICS:COMPLETE (cache write skipped:" in render_markdown(report)
    plan = plan_experiment(exp, gate=LOOSE_GATE)
    assert plan["markets"]["SYN_A"]["cache_write_skipped"]["METRICS"].endswith("lock_timeout")
