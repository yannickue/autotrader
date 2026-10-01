# ruff: noqa: E501
"""Segment store (commit / lookup / invalidation reasons / atomic writes), scheduler determinism and failure handling, status file."""

from __future__ import annotations

import json
import time

import pytest

from research_speed import parallel as P
from research_speed.artifact import ArtifactFingerprint
from research_speed.progress import StatusFile
from research_speed.scheduler import Task, run_dag
from research_speed.segments import SegmentStore, atomic_write_text
from research_speed.testing import read_env, set_env, square, write_marker


@pytest.fixture
def real_workers(monkeypatch):
    """Memory-guard parameters that let ``jobs > 1`` really start worker processes whatever the free memory of the machine is."""
    monkeypatch.setenv("RESEARCH_SPEED_RESERVE_MB", "0")
    monkeypatch.setenv("RESEARCH_SPEED_PER_WORKER_MB", "50")
    monkeypatch.setattr(P, "available_memory_mb", lambda: 100000.0)  # fail-closed guard: independent of this machine's free RAM


FP = ArtifactFingerprint("data1", "feat1", "ctl1", "cfg1", "lab1", "pre1")


def _store_with_segment(tmp_path):
    (tmp_path / "M").mkdir()
    (tmp_path / "M" / "a.parquet").write_bytes(b"AAAA")
    (tmp_path / "M" / "b.json").write_text("{}")
    s = SegmentStore(tmp_path)
    s.commit("M/events", FP, ["M/a.parquet", "M/b.json"], wall_s=1.5)
    return s


def test_identical_fingerprint_is_a_cache_hit(tmp_path):
    s = _store_with_segment(tmp_path)
    lk = s.lookup("M/events", FP)
    assert lk.hit and lk.reason == "HIT" and lk.artifact_id == FP.artifact_id


@pytest.mark.parametrize(
    "component",
    [
        "data_hash",
        "feature_code_hash",
        "control_code_hash",
        "config_hash",
        "label_version",
        "prereg_version",
    ],
)
def test_any_changed_component_is_a_miss_that_names_it(tmp_path, component):
    s = _store_with_segment(tmp_path)
    changed = ArtifactFingerprint(**{**FP.components(), component: "different"})
    lk = s.lookup("M/events", changed)
    assert not lk.hit and lk.reason == f"FINGERPRINT_CHANGED:{component}"


def test_missing_manifest_missing_file_and_tampered_file_are_misses(tmp_path):
    s = _store_with_segment(tmp_path)
    assert s.lookup("M/other", FP).reason == "NO_MANIFEST"
    (tmp_path / "M" / "a.parquet").write_bytes(
        b"AAAB"
    )  # same size, different content: sha256 catches it
    assert s.lookup("M/events", FP).reason == "FILE_CHANGED:M/a.parquet"
    (tmp_path / "M" / "a.parquet").unlink()
    assert s.lookup("M/events", FP).reason == "FILE_MISSING:M/a.parquet"


def test_incomplete_or_corrupt_manifest_is_never_a_hit(tmp_path):
    s = _store_with_segment(tmp_path)
    m = json.loads(s.path("M/events").read_text())
    m["status"] = "RUNNING"
    s.path("M/events").write_text(json.dumps(m))
    assert s.lookup("M/events", FP).reason == "NOT_COMPLETE"
    s.path("M/events").write_text("{ half written")
    assert s.lookup("M/events", FP).reason == "NO_MANIFEST"


def test_atomic_write_leaves_no_temp_file_and_replaces_whole(tmp_path):
    p = tmp_path / "x" / "f.json"
    atomic_write_text(p, "one")
    atomic_write_text(p, "two")
    assert p.read_text() == "two" and [q.name for q in p.parent.iterdir()] == ["f.json"]


def test_clamp_jobs_never_exceeds_three_or_the_task_count_or_memory():
    assert P.MAX_WORKERS == 3
    assert P.clamp_jobs(8, 100, avail_mb=100000) == 3
    assert P.clamp_jobs(3, 2, avail_mb=100000) == 2
    assert P.clamp_jobs(3, 100, avail_mb=2100) == 2  # (2100 - 1500) // 300
    assert P.clamp_jobs(3, 100, avail_mb=1800) == 1  # reserve + exactly one worker
    with pytest.raises(P.InsufficientMemoryError):  # fail closed: never rounded up to one worker
        P.clamp_jobs(3, 100, avail_mb=1700)
    with pytest.raises(ValueError):
        P.clamp_jobs(0, 5)


def test_memory_guard_parameters_can_be_overridden_by_env_for_controlled_benchmarks(monkeypatch):
    with pytest.raises(P.InsufficientMemoryError):  # default reserve 1500: 700 MB free => no run at all
        P.clamp_jobs(3, 100, avail_mb=700)
    monkeypatch.setenv("RESEARCH_SPEED_RESERVE_MB", "0")
    assert P.clamp_jobs(3, 100, avail_mb=700) == 2  # 700 // 300
    monkeypatch.setenv("RESEARCH_SPEED_PER_WORKER_MB", "200")
    assert P.clamp_jobs(3, 100, avail_mb=700) == 3
    assert P.clamp_jobs(8, 100, avail_mb=100000) == 3  # the hard cap is not overridable


def test_ordered_map_is_input_ordered_and_independent_of_jobs(real_workers):
    xs = list(range(9))
    assert P.ordered_map(square, xs, 1) == P.ordered_map(square, xs, 3) == [x * x for x in xs]


def _tasks(d, fail=None):
    def mk(i, deps=(), **kw):
        return Task(i, write_marker, {"id": i, "dir": str(d), "value": len(i), **kw}, tuple(deps))

    return [
        mk("A/events", sleep=0.3),
        mk("B/events"),
        mk("A/ctl", ["A/events"]),
        mk("B/ctl", ["B/events"], fail=(fail == "B/ctl")),
        mk("C/events", fail=(fail == "C/events")),
        mk("C/ctl", ["C/events"]),
    ]


def _strip(res):
    return {k: {x: y for x, y in v.items() if x != "pid"} for k, v in res.items()}


def test_scheduler_results_do_not_depend_on_worker_count(tmp_path, real_workers):
    (tmp_path / "s").mkdir()
    (tmp_path / "p").mkdir()
    a, ea = run_dag(_tasks(tmp_path / "s"), 1)
    b, eb = run_dag(_tasks(tmp_path / "p"), 3)
    assert not ea and not eb
    assert _strip(a) == _strip(b) and set(a) == {t.id for t in _tasks(tmp_path)}
    names = sorted(p.name for p in (tmp_path / "s").iterdir())
    assert names == sorted(p.name for p in (tmp_path / "p").iterdir())
    assert all((tmp_path / "s" / n).read_text() == (tmp_path / "p" / n).read_text() for n in names)


def test_a_failure_skips_only_its_dependants(tmp_path, real_workers):
    for j in (1, 3):
        d = tmp_path / f"j{j}"
        d.mkdir()
        res, err = run_dag(_tasks(d, fail="C/events"), j)
        assert (
            set(err) == {"C/events", "C/ctl"}
            and "boom" in err["C/events"]
            and err["C/ctl"].startswith("SKIPPED")
        )
        assert set(res) == {"A/events", "B/events", "A/ctl", "B/ctl"}
        assert not (d / "C__ctl.txt").exists()


def test_unknown_dependency_and_duplicate_ids_are_rejected():
    with pytest.raises(ValueError):
        run_dag([Task("x", write_marker, {}, ("nope",))], 1)
    with pytest.raises(ValueError):
        run_dag([Task("x", write_marker, {}), Task("x", write_marker, {})], 1)


def test_status_file_tracks_segments_and_heartbeat(tmp_path):
    p = tmp_path / "_status.json"
    with StatusFile(p, "run1", ["a", "b"], interval_s=0.05, warn_after_s=0.0) as st:
        st.start("a")
        time.sleep(0.3)
        snap = json.loads(p.read_text())
        assert (
            snap["segments"]["a"]["state"] == "running"
            and snap["segments"]["b"]["state"] == "pending"
            and "a" in snap["long_running"]
        )
        hb1 = snap["heartbeat_utc"]
        st.finish("a")
        assert st.snapshot()["segments_finished"] == 1
    final = json.loads(p.read_text())
    assert (
        final["segments"]["a"]["state"] == "done"
        and final["segments"]["a"]["wall_s"] is not None
        and hb1 <= final["heartbeat_utc"]
    )


def test_worker_initializer_runs_before_the_tasks_in_every_worker(real_workers):
    tasks = [Task(f"t{i}", read_env, {"name": "RS_INIT_PROBE"}) for i in range(4)]
    res, err = run_dag(tasks, 2, initializer=set_env, initargs=("RS_INIT_PROBE", "ready"))
    assert not err and {r["value"] for r in res.values()} == {"ready"}
