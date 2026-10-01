# ruff: noqa: E501
"""Lane R addendum: the NORMAL daily task cannot launch an unfinished / unapproved system (STOP wins, deployment-approval gate,
exit code 30, never restarted); the EOD recovery is NOT gated.  Pure / patched process launch: no MT5, no scheduler."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.autostart import supervisor as sup

gate = sup.deploy_gate  # the module object the supervisor itself uses (top-level import via scripts/autostart on sys.path)
BERLIN = ZoneInfo("Europe/Berlin")
AUTOSTART = Path(sup.HERE)
SHA = "a" * 40
OTHER = "b" * 40


def fake_git(head: str = SHA, dirty: str = ""):
    def run(repo, args):
        if list(args)[:2] == ["rev-parse", "HEAD"]:
            return head + "\n"
        if list(args)[:2] == ["status", "--porcelain"]:
            return dirty
        raise AssertionError(args)

    return run


def approve(art: Path, sha: str = SHA) -> None:
    art.mkdir(parents=True, exist_ok=True)
    (art / "deploy_approved.json").write_text(json.dumps({"sha": sha, "approved_utc": "2026-10-02T08:00:00+00:00"}), encoding="utf-8")


# ------------------------------------------------------------------------------------------ gate verdicts
def test_gate_not_approved_without_a_file_or_without_a_full_sha(tmp_path):
    art = tmp_path / "a"
    assert gate.check_deploy_approval(art, tmp_path, fake_git()).verdict == gate.NOT_APPROVED
    art.mkdir()
    (art / "deploy_approved.json").write_text("not json", encoding="utf-8")
    assert gate.check_deploy_approval(art, tmp_path, fake_git()).verdict == gate.NOT_APPROVED
    (art / "deploy_approved.json").write_text(json.dumps({"sha": "abc123"}), encoding="utf-8")  # short sha is not an approval
    assert gate.check_deploy_approval(art, tmp_path, fake_git()).verdict == gate.NOT_APPROVED


def test_gate_sha_mismatch_dirty_checkout_git_unavailable_and_ok(tmp_path):
    art = tmp_path / "a"
    approve(art)
    r = gate.check_deploy_approval(art, tmp_path, fake_git(head=OTHER))
    assert r.verdict == gate.SHA_MISMATCH and r.head == OTHER and r.approved_sha == SHA
    r = gate.check_deploy_approval(art, tmp_path, fake_git(dirty=" M src/demo/runner.py\n"))
    assert r.verdict == gate.DIRTY_CHECKOUT and "runner.py" in r.detail

    def broken(repo, args):
        raise OSError("git missing")

    assert gate.check_deploy_approval(art, tmp_path, broken).verdict == gate.GIT_UNAVAILABLE
    ok = gate.check_deploy_approval(art, tmp_path, fake_git())
    assert ok.ok and ok.verdict == gate.OK


def test_gate_runs_git_with_tracked_files_only_status(tmp_path):
    seen: list[list[str]] = []

    def spy(repo, args):
        seen.append(list(args))
        return SHA + "\n" if args[0] == "rev-parse" else ""

    approve(tmp_path / "a")
    gate.check_deploy_approval(tmp_path / "a", tmp_path, spy)
    assert ["status", "--porcelain", "--untracked-files=no"] in seen  # tracked files only (artifacts / data junctions do not count)


def test_approve_writes_the_current_head_refuses_a_dirty_tree_and_revoke_removes_it(tmp_path):
    art = tmp_path / "a"
    assert gate.write_approval(art, tmp_path, fake_git(dirty=" M x")).verdict == gate.DIRTY_CHECKOUT
    assert not (art / "deploy_approved.json").exists()
    res = gate.write_approval(art, tmp_path, fake_git(), now=datetime(2026, 10, 2, 8, 0, tzinfo=UTC))
    body = json.loads((art / "deploy_approved.json").read_text(encoding="utf-8"))
    assert res.ok and body == {"sha": SHA, "approved_utc": "2026-10-02T08:00:00+00:00"}
    assert gate.check_deploy_approval(art, tmp_path, fake_git()).ok
    assert gate.revoke_approval(art) is True and gate.revoke_approval(art) is False
    assert gate.check_deploy_approval(art, tmp_path, fake_git()).verdict == gate.NOT_APPROVED


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_gate_against_a_real_git_checkout_head_dirty_and_new_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def g(*a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True, env=env).stdout

    g("init", "-q")
    (repo / "f.txt").write_text("1", encoding="utf-8")
    g("add", "f.txt")
    g("commit", "-q", "-m", "one")
    art = tmp_path / "art"
    assert gate.write_approval(art, repo).ok
    assert gate.check_deploy_approval(art, repo).ok
    (repo / "untracked.txt").write_text("x", encoding="utf-8")  # untracked files do not dirty the gate
    assert gate.check_deploy_approval(art, repo).ok
    (repo / "f.txt").write_text("2", encoding="utf-8")  # a modified tracked file does
    assert gate.check_deploy_approval(art, repo).verdict == gate.DIRTY_CHECKOUT
    g("commit", "-q", "-am", "two")  # new commit: the approval is stale
    assert gate.check_deploy_approval(art, repo).verdict == gate.SHA_MISMATCH


def test_gate_cli_exit_codes(tmp_path, capsys):
    art = tmp_path / "a"
    assert gate.main(["--artifacts", str(art), "--check"]) == gate.EXIT_NOT_APPROVED == 30
    assert json.loads(capsys.readouterr().out)["verdict"] == "NOT_APPROVED"
    assert gate.main(["--artifacts", str(art), "--revoke"]) == 0


# ------------------------------------------------------------------------------------------ supervisor
def _alert(art: Path) -> dict:
    return json.loads((art / "watchdog_alert.json").read_text(encoding="utf-8"))["latest"]


def test_exit_code_30_is_distinct_and_documented():
    codes = {sup.SUP_OK, sup.SUP_UNHEALTHY_RUNNER_PRESENT, sup.SUP_GAVE_UP, sup.SUP_NOT_APPROVED}
    assert len(codes) == 4 and sup.SUP_NOT_APPROVED == 30
    assert "SUP_NOT_APPROVED" in (sup.__doc__ or "") and "30" in (sup.__doc__ or "") and "never" in (sup.__doc__ or "").lower()
    assert sup.SUP_NOT_APPROVED not in (sup.RUN_OK, sup.RUN_BAD_ARGS, sup.RUN_NO_HEARTBEAT, sup.RUN_FAIL_CLOSED, sup.RUN_UNAVAILABLE, sup.RUN_ALREADY_RUNNING)


@pytest.mark.parametrize("approval,verdict", [(None, "NOT_APPROVED"), (OTHER, "SHA_MISMATCH")])
def test_supervisor_main_refuses_the_real_runner_without_a_matching_approval(tmp_path, monkeypatch, approval, verdict):
    art = tmp_path / "a"
    if approval:
        approve(art, approval)
    monkeypatch.setattr(gate, "run_git", fake_git(head=SHA))
    launched: list[int] = []
    monkeypatch.setattr(sup.Supervisor, "_launch", lambda self: launched.append(1))
    code = sup.main(["--artifacts", str(art), "--ignore-operating-day", "--log-dir", str(tmp_path / "l")])
    assert code == sup.SUP_NOT_APPROVED == 30 and launched == []
    latest = _alert(art)
    assert latest["severity"] == "CRITICAL" and verdict in latest["reason"] and latest["gate"] == verdict
    assert not (art / "supervisor.lock").exists()  # refused before any lock / runner handling


def test_supervisor_main_refuses_a_dirty_checkout(tmp_path, monkeypatch):
    art = tmp_path / "a"
    approve(art)
    monkeypatch.setattr(gate, "run_git", fake_git(dirty=" M scripts/autostart/supervisor.py"))
    assert sup.main(["--artifacts", str(art), "--ignore-operating-day", "--log-dir", str(tmp_path / "l")]) == 30
    assert "DIRTY_CHECKOUT" in _alert(art)["reason"]


def test_stop_file_wins_the_normal_task_does_not_start_while_stop_exists(tmp_path, monkeypatch):
    art = tmp_path / "a"
    approve(art)  # even a fully approved checkout does not start with STOP present
    (art / "STOP").write_text("", encoding="utf-8")
    monkeypatch.setattr(gate, "run_git", fake_git())
    marker = tmp_path / "launched.txt"
    cmd = json.dumps([sys.executable, "-c", f"open(r'{marker}', 'a').write('x')"])
    assert sup.main(["--artifacts", str(art), "--ignore-operating-day", "--runner-cmd-json", cmd, "--log-dir", str(tmp_path / "l")]) == 0
    assert not marker.exists()
    # and with the REAL runner command: STOP is checked before the gate: exit 0, no gate alert
    assert sup.main(["--artifacts", str(art), "--ignore-operating-day", "--log-dir", str(tmp_path / "l2")]) == 0
    assert not (art / "watchdog_alert.json").exists()


def _log_text(d: Path) -> str:
    return " ".join(f.read_text(encoding="utf-8") for f in sorted(d.glob("*.log")))


def test_an_approved_clean_checkout_passes_the_gate_and_dry_run_reports_it(tmp_path, monkeypatch):
    art = tmp_path / "a"
    approve(art)
    monkeypatch.setattr(gate, "run_git", fake_git())
    assert sup.main(["--artifacts", str(art), "--dry-run", "--log-dir", str(tmp_path / "l")]) == 0
    assert '"verdict": "OK"' in _log_text(tmp_path / "l")


def test_dry_run_never_fails_on_the_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "run_git", fake_git())
    assert sup.main(["--artifacts", str(tmp_path / "a"), "--dry-run", "--log-dir", str(tmp_path / "l")]) == 0
    assert "NOT_APPROVED" in _log_text(tmp_path / "l")


def test_the_gate_is_rechecked_before_every_restart_and_a_refusal_ends_the_loop(tmp_path, monkeypatch):
    art = tmp_path / "a"
    log = sup.RunLog(tmp_path / "l" / "s.log", echo=None)
    results = iter([gate.GateResult(gate.OK, "ok"), gate.GateResult(gate.DIRTY_CHECKOUT, "modified meanwhile")])
    s = sup.Supervisor(art, ["x"], tmp_path, log, sup.Policy(), ignore_operating_day=True,
                       now_fn=lambda: datetime(2026, 10, 26, 10, 0, tzinfo=BERLIN), gate=lambda: next(results))
    runs: list[int] = []
    monkeypatch.setattr(s, "_run_once", lambda: runs.append(1) or (1, 100.0))  # the runner crashes once
    monkeypatch.setattr(s, "_sleep", lambda sec: None)
    assert s.run() == 30
    assert runs == [1]  # launched once, the restart was refused: no restart loop on the gate
    assert "DIRTY_CHECKOUT" in _alert(art)["reason"]
    log.close()


def test_without_a_gate_the_class_behaves_as_before(tmp_path, monkeypatch):
    log = sup.RunLog(tmp_path / "l" / "s.log", echo=None)
    s = sup.Supervisor(tmp_path / "a", ["x"], tmp_path, log, sup.Policy(), ignore_operating_day=True,
                       now_fn=lambda: datetime(2026, 10, 26, 10, 0, tzinfo=BERLIN))
    assert s.gate is None
    monkeypatch.setattr(s, "_run_once", lambda: (0, 100.0))
    monkeypatch.setattr(sup, "_heartbeat", lambda a, n=None: {"status": {"stop_reason": "eod_flat_shutdown", "updated_utc": datetime.now(UTC).isoformat()}})
    assert s.run() == 0
    log.close()


# ------------------------------------------------------------------------------------------ PowerShell (Windows only)
POWERSHELL = shutil.which("powershell")
win_only = pytest.mark.skipif(sys.platform != "win32" or POWERSHELL is None, reason="Windows PowerShell only")


def _ps(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *args], capture_output=True, text=True, timeout=timeout)


@win_only
def test_register_dry_run_with_disabled_and_the_enable_disable_approve_helpers():
    cp = _ps(str(AUTOSTART / "register_task.ps1"), "-DryRun", "-Disabled")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "registered DISABLED" in cp.stdout and "-Disabled" in cp.stdout and "DRY RUN: nothing registered" in cp.stdout
    for script, text in (("enable_task.ps1", "Enable-ScheduledTask"), ("disable_task.ps1", "Disable-ScheduledTask")):
        cp = _ps(str(AUTOSTART / script), "-DryRun")
        assert cp.returncode == 0, cp.stderr
        assert "is not registered" in cp.stdout or text in cp.stdout  # nothing is registered on the dev box
    cp = _ps(str(AUTOSTART / "approve_deploy.ps1"), "-DryRun", "-Uv", "C:\\fake\\uv.exe")
    assert cp.returncode == 0 and "deploy_gate.py" in cp.stdout and "--approve" in cp.stdout
    cp = _ps(str(AUTOSTART / "approve_deploy.ps1"), "-DryRun", "-Revoke", "-Uv", "C:\\fake\\uv.exe")
    assert "--revoke" in cp.stdout


def test_register_script_registers_disabled_via_disable_scheduledtask_only_after_registration():
    body = (AUTOSTART / "register_task.ps1").read_text(encoding="utf-8")
    assert body.index("Register-ScheduledTask -TaskName $t.Name") < body.index("Disable-ScheduledTask -TaskName $t.Name")
    for name in ("enable_task.ps1", "disable_task.ps1", "approve_deploy.ps1"):
        text = (AUTOSTART / name).read_text(encoding="utf-8").lower()
        assert "register-scheduledtask" not in text and "stop-process" not in text and "terminal64" not in text
