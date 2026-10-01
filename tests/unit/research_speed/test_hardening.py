# ruff: noqa: E501
"""Process hardening of the research tooling: it must never starve the live trader (priority, threads, fail-closed memory, live-process
detection, job object), never leave orphan workers behind, never lose finished results, never run twice on the same output."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import coverage_analysis  # noqa: F401  (the src package must be imported before scripts/ is on sys.path: scripts/coverage_analysis.py would shadow it)
from research_speed import parallel as P
from research_speed.progress import StatusFile
from research_speed.runlock import RunLock, RunLockError
from research_speed.scheduler import Task, run_dag
from research_speed.testing import die, sleeper, write_marker

_REAL_HARDEN = P.harden_process  # captured at import (collection) time: other test directories patch the module attribute for the CLI tests
SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))  # appended: scripts/coverage_analysis.py must not shadow the src package


def _harden(jobs=3, *, avail_mb=100000.0, live=False, env=None, k32=None, platform="win32", **kw):
    env = {} if env is None else env
    k32 = MagicMock() if k32 is None else k32
    out = _REAL_HARDEN(jobs, avail_mb=avail_mb, cmdlines=(lambda: ["python scripts/demo_trader.py --x"]) if live else (lambda: ["python -m pytest"]), env=env, kernel32=k32, platform=platform, **kw)
    return out, env, k32


# ---------------------------------------------------------------------------------------------- HIGH-1
def test_low_memory_is_fail_closed_with_a_nonzero_exit_and_nothing_started():
    k32 = MagicMock()
    with pytest.raises(SystemExit) as e:
        _harden(avail_mb=1700, k32=k32)  # needs reserve 1500 + one worker 300
    assert e.value.code not in (0, None)
    _out, _env, _ok = _harden(avail_mb=1800)
    assert _out == 1  # (1800 - 1500) // 300 == 1
    assert _harden(avail_mb=2400)[0] == 3
    with pytest.raises(P.InsufficientMemoryError):
        P.clamp_jobs(3, 10, avail_mb=1700)  # clamp_jobs no longer rounds up to one worker


def test_unknown_memory_means_one_worker_not_three(monkeypatch):
    monkeypatch.setattr(P, "available_memory_mb", lambda: None)
    assert P.clamp_jobs(3, 10) == 1
    out = _REAL_HARDEN(3, cmdlines=lambda: [], env={}, kernel32=MagicMock(), platform="win32")
    assert out == 1


def test_parameters_are_validated():
    with pytest.raises(ValueError):
        P.clamp_jobs(2, 5, avail_mb=9000, per_worker_mb=10)
    with pytest.raises(ValueError):
        P.clamp_jobs(2, 5, avail_mb=9000, reserve_mb=-1)
    with pytest.raises(SystemExit) as e:
        _harden(per_worker_mb=10)
    assert e.value.code not in (0, None)


def test_harden_lowers_the_priority_class_of_the_process_on_windows():
    _out, _env, k32 = _harden()
    assert k32.SetPriorityClass.call_args.args[1] == P.BELOW_NORMAL_PRIORITY_CLASS == 0x4000
    assert k32.SetPriorityClass.call_args.args[0] is k32.GetCurrentProcess.return_value
    _out, _env, k32 = _harden(platform="linux")
    assert not k32.SetPriorityClass.called  # Windows only


def test_harden_pins_the_numeric_library_threads_before_any_pool_exists():
    _out, env, _k = _harden(env={"OMP_NUM_THREADS": "8"})
    assert {env[k] for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")} == {"1"}


def test_a_detected_live_trader_forces_one_job_unless_explicitly_overridden(capsys):
    assert _harden(3, live=True)[0] == 1
    assert "live" in capsys.readouterr().err.lower()
    assert _harden(3, live=True, env={P.ALLOW_LIVE_ENV: "1"})[0] == 3
    assert _harden(3, live=True, env={P.ALLOW_LIVE_ENV: "0"})[0] == 1  # only the explicit "1" overrides


def test_a_failing_live_process_check_is_conservative():
    def boom():
        raise OSError("no powershell")

    assert _REAL_HARDEN(3, avail_mb=100000.0, cmdlines=boom, env={}, kernel32=MagicMock(), platform="win32") == 1


def test_live_detection_matches_the_trader_and_supervisor_command_lines():
    assert P.live_trader_running(lambda: ["C:\\py\\python.exe scripts\\demo_trader.py --demo"]) is True
    assert P.live_trader_running(lambda: ["python C:/x/supervisor.py"]) is True
    assert P.live_trader_running(lambda: ["python -m pytest tests", "python scripts/observer_backfill.py"]) is False
    assert P.live_trader_running(lambda: []) is False


# ---------------------------------------------------------------------------------------------- HIGH-2
def test_harden_puts_the_process_into_a_kill_on_close_job_object():
    import ctypes

    _out, _env, k32 = _harden()
    assert k32.CreateJobObjectW.called and k32.AssignProcessToJobObject.called
    args = k32.SetInformationJobObject.call_args.args
    assert args[1] == 9  # JobObjectExtendedLimitInformation
    info = args[2]._obj if hasattr(args[2], "_obj") else args[2]
    assert info.BasicLimitInformation.LimitFlags & 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert args[3] == ctypes.sizeof(info)


def test_an_exception_in_the_parent_leaves_no_worker_processes_behind():
    procs = []
    with pytest.raises(RuntimeError, match="parent failed"), P.managed_pool(2) as ex:
        futs = [ex.submit(sleeper, 60) for _ in range(4)]
        deadline = time.monotonic() + 30
        while len(getattr(ex, "_processes", {})) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        procs = list(ex._processes.values())
        assert procs and futs
        raise RuntimeError("parent failed")
    t0 = time.monotonic()
    while any(p.is_alive() for p in procs) and time.monotonic() - t0 < 15:
        time.sleep(0.1)
    assert not any(p.is_alive() for p in procs)


def test_ordered_map_still_returns_input_ordered_results():
    assert P.ordered_map(abs, [-3, 2, -1], 1) == [3, 2, 1]


# ---------------------------------------------------------------------------------------------- MEDIUM-3
def test_a_worker_that_dies_marks_running_and_open_tasks_failed_without_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_SPEED_RESERVE_MB", "0")
    monkeypatch.setenv("RESEARCH_SPEED_PER_WORKER_MB", "50")
    monkeypatch.setattr(P, "available_memory_mb", lambda: 100000.0)
    tasks = [Task("crash", die, {}), Task("a", write_marker, {"id": "a", "dir": str(tmp_path), "sleep": 1}), Task("b", write_marker, {"id": "b", "dir": str(tmp_path)}), Task("c", write_marker, {"id": "c", "dir": str(tmp_path)}, ("b",))]
    with StatusFile(tmp_path / "_status.json", "r1", [t.id for t in tasks]) as st:
        results, errors = run_dag(tasks, 2, st)
    assert "crash" in errors and set(results) | set(errors) == {"crash", "a", "b", "c"}
    assert all(k in errors for k in set(errors))
    assert "BrokenProcessPool" in errors["crash"] or "terminated abruptly" in errors["crash"]


# ---------------------------------------------------------------------------------------------- MEDIUM-4
def test_gate_b_persists_each_market_result_before_a_later_market_fails(tmp_path, monkeypatch):
    import observer_gate_b_report as GB

    for m in ("M1", "M2"):
        (tmp_path / m).mkdir()

    def fake(m, root, p2, a, cached):
        if m == "M2":
            raise RuntimeError("market 2 exploded")
        return {"verdict": "PASS", "blocking": [], "gate_b_seconds": 1}

    monkeypatch.setattr(GB, "gate_b_market", fake)
    plan = {"M1": ("todo", None), "M2": ("todo", None)}
    with pytest.raises(RuntimeError, match="market 2 exploded"):
        GB.compute_and_persist(["M1", "M2"], tmp_path, None, SimpleNamespace(), plan, jobs=1)
    assert (tmp_path / "M1" / "gate_b.json").is_file()
    assert not (tmp_path / "M2" / "gate_b.json").exists()


# ---------------------------------------------------------------------------------------------- MEDIUM-5
def test_run_lock_is_exclusive_stale_aware_and_released(tmp_path):
    p = tmp_path / "_run.lock"
    with RunLock(p):
        assert p.is_file()
        with pytest.raises(RunLockError):
            RunLock(p).acquire()
    assert not p.exists()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    p.write_text(str(dead.pid))  # a lock left behind by a dead process is taken over
    with RunLock(p):
        assert p.read_text().strip() == str(__import__("os").getpid())
    p.write_text("not-a-pid")
    with pytest.raises(RunLockError):  # unreadable owner => conservative: held
        RunLock(p).acquire()


def test_entry_points_harden_the_process_and_the_output_dirs_are_locked():
    root = Path(__file__).resolve().parents[3]
    for name in ("observer_backfill.py", "observer_gate_b_report.py", "observer_gate_c.py"):
        src = (root / "scripts" / name).read_text(encoding="utf-8")
        assert "harden_process(" in src, name
    for name in ("observer_backfill.py", "observer_gate_b_report.py"):
        assert "RunLock(" in (root / "scripts" / name).read_text(encoding="utf-8"), name
