# ruff: noqa: E501
"""Lane L: single-instance lock, exit-code policy, backoff, operating-day guard, supervisor behaviour
with fake runner commands, task XML and PowerShell dry-runs.  Never touches MT5 or the real scheduler."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import demo_trader
from scripts.autostart import supervisor as sup

BERLIN = ZoneInfo("Europe/Berlin")
AUTOSTART = Path(sup.HERE)
SUPERVISOR_PY = AUTOSTART / "supervisor.py"
lockmod = sup.instance_lock


def _ct(alive: dict[int, float]):
    return lambda pid: alive.get(pid)


# ------------------------------------------------------------------------------ instance lock
def test_lock_acquire_release_and_payload(tmp_path: Path) -> None:
    lk = lockmod.InstanceLock(tmp_path / "runner.lock", "runner", pid=111, create_time=_ct({111: 1000.0}))
    lk.acquire()
    data = json.loads((tmp_path / "runner.lock").read_text())
    assert data["pid"] == 111 and data["create_time"] == 1000.0 and data["role"] == "runner"
    lk.release()
    assert not (tmp_path / "runner.lock").exists()


def test_second_instance_refused_while_holder_alive(tmp_path: Path) -> None:
    ct = _ct({111: 1000.0, 222: 2000.0})
    first = lockmod.InstanceLock(tmp_path / "runner.lock", "runner", pid=111, create_time=ct)
    first.acquire()
    with pytest.raises(lockmod.LockHeld) as err:
        lockmod.InstanceLock(tmp_path / "runner.lock", "runner", pid=222, create_time=ct).acquire()
    assert "111" in str(err.value)
    assert json.loads((tmp_path / "runner.lock").read_text())["pid"] == 111  # untouched


def test_stale_lock_dead_pid_is_recovered(tmp_path: Path) -> None:
    (tmp_path / "runner.lock").write_text(json.dumps({"pid": 999, "create_time": 500.0, "role": "runner"}))
    lk = lockmod.InstanceLock(tmp_path / "runner.lock", "runner", pid=222, create_time=_ct({222: 2000.0}))
    lk.acquire()  # 999 is not alive -> stale
    assert json.loads((tmp_path / "runner.lock").read_text())["pid"] == 222


def test_stale_lock_pid_reuse_is_recovered(tmp_path: Path) -> None:
    (tmp_path / "runner.lock").write_text(json.dumps({"pid": 999, "create_time": 500.0, "role": "runner"}))
    # pid 999 is alive again but a DIFFERENT process (other creation time)
    lk = lockmod.InstanceLock(tmp_path / "runner.lock", "runner", pid=222, create_time=_ct({222: 2000.0, 999: 9000.0}))
    lk.acquire()
    assert json.loads((tmp_path / "runner.lock").read_text())["pid"] == 222


def test_unreadable_old_lock_is_stale(tmp_path: Path) -> None:
    p = tmp_path / "runner.lock"
    p.write_text("{not json")
    os.utime(p, (time.time() - 60, time.time() - 60))
    lockmod.InstanceLock(p, "runner", pid=222, create_time=_ct({222: 2000.0})).acquire()
    assert json.loads(p.read_text())["pid"] == 222


def test_release_never_deletes_someone_elses_lock(tmp_path: Path) -> None:
    p = tmp_path / "runner.lock"
    lk = lockmod.InstanceLock(p, "runner", pid=111, create_time=_ct({111: 1.0}))
    lk.acquire()
    p.write_text(json.dumps({"pid": 333, "create_time": 3.0, "role": "runner"}))  # taken over meanwhile
    lk.release()
    assert json.loads(p.read_text())["pid"] == 333


def test_real_process_create_time_matches_self() -> None:
    a = lockmod.process_create_time(os.getpid())
    assert a is not None and abs(a - time.time()) < 10 * 24 * 3600  # sane, not garbage
    assert lockmod.process_create_time(1_852_516_352) is None or sys.platform != "win32"


# ------------------------------------------------------------------------------ demo_trader guard
def test_runner_refuses_second_instance_same_artifacts(tmp_path: Path, monkeypatch, capsys) -> None:
    held = demo_trader.instance_lock.InstanceLock(tmp_path / "runner.lock", "runner:demo-auto")
    held.acquire()  # this very (live) test process is the first runner
    monkeypatch.setattr(demo_trader, "_run", lambda *a, **k: pytest.fail("second runner must never start"))
    code = demo_trader.main(["--shadow", "--artifacts", str(tmp_path)])
    assert code == demo_trader.instance_lock.EXIT_ALREADY_RUNNING == 9
    assert "REFUSED" in capsys.readouterr().err
    held.release()


def test_runner_lock_held_during_run_and_released_after(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, bool] = {}

    def fake_run(args, mode):
        seen["locked"] = (args.artifacts / "runner.lock").exists()
        return 0

    monkeypatch.setattr(demo_trader, "_run", fake_run)
    assert demo_trader.main(["--shadow", "--artifacts", str(tmp_path)]) == 0
    assert seen["locked"] and not (tmp_path / "runner.lock").exists()


def test_runner_lock_released_on_crash(tmp_path: Path, monkeypatch) -> None:
    def boom(args, mode):
        raise RuntimeError("crash")

    monkeypatch.setattr(demo_trader, "_run", boom)
    with pytest.raises(RuntimeError):
        demo_trader.main(["--shadow", "--artifacts", str(tmp_path)])
    assert not (tmp_path / "runner.lock").exists()


def test_stale_runner_lock_from_crash_does_not_block(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "runner.lock").write_text(json.dumps({"pid": 1_852_516_352, "create_time": 1.0}))
    monkeypatch.setattr(demo_trader, "_run", lambda a, m: 0)
    assert demo_trader.main(["--shadow", "--artifacts", str(tmp_path)]) == 0


def test_status_and_analyze_unaffected_by_lock(tmp_path: Path, capsys) -> None:
    held = demo_trader.instance_lock.InstanceLock(tmp_path / "runner.lock", "runner")
    held.acquire()
    assert demo_trader.main(["--status", "--artifacts", str(tmp_path)]) == 3  # no heartbeat, NOT the lock code
    assert demo_trader.main(["--analyze", "--artifacts", str(tmp_path)]) == 3  # no store
    held.release()


def test_demo_auto_without_confirmation_refused_before_lock(tmp_path: Path) -> None:
    assert demo_trader.main(["--demo-auto", "--artifacts", str(tmp_path)]) == 2
    assert not (tmp_path / "runner.lock").exists()


# ------------------------------------------------------------------------------ exit-code policy
MON_10 = datetime(2026, 10, 26, 10, 0, tzinfo=BERLIN)
MON_2230 = datetime(2026, 10, 26, 22, 30, tzinfo=BERLIN)
POLICY = sup.Policy()


def _d(code, *, stop=False, now=MON_10, state=None, ran=60.0, policy=POLICY):
    return sup.decide(code, stop_file_exists=stop, now=now, state=state or sup.WatchdogState(date="x"),
                      policy=policy, ran_s=ran)


def test_policy_exit0_done_no_restart() -> None:
    d = _d(0)
    assert d.action == "done" and d.exit_code == 0 and not d.alert


def test_policy_exit2_never_restarts_and_alerts() -> None:
    d = _d(2)
    assert d.action == "no_restart" and d.alert and d.exit_code == 2


def test_policy_exit9_duplicate_runner_no_restart() -> None:
    assert _d(9).action == "done"


@pytest.mark.parametrize("code", [0, 1, 7, 8, 139, -9])
def test_policy_stop_file_never_restarts(code) -> None:
    assert _d(code, stop=True).action == "done"


@pytest.mark.parametrize("code", [1, 7, 8, 139])
def test_policy_after_hours_never_restarts(code) -> None:
    d = _d(code, now=MON_2230)
    assert d.action == "no_restart" and "after operating-day end" in d.reason and d.alert


def test_policy_weekend_never_restarts() -> None:
    sat = datetime(2026, 10, 24, 10, 0, tzinfo=BERLIN)
    assert _d(1, now=sat).action == "no_restart"


def test_policy_crash_restarts_with_backoff_and_7_is_bounded_and_alerting() -> None:
    crash = _d(1)
    assert crash.action == "restart" and crash.delay_s == 30 and not crash.alert
    seven = _d(7)
    assert seven.action == "restart" and seven.alert  # surfaced immediately
    st = sup.WatchdogState(date="x", restarts_today=3, fail_closed_restarts_today=3, consecutive_failures=3)
    assert _d(7, state=st).action == "give_up"
    assert _d(1, state=st).action == "restart"  # a plain crash still has overall budget left
    full = sup.WatchdogState(date="x", restarts_today=8, consecutive_failures=7)
    g = _d(1, state=full)
    assert g.action == "give_up" and g.exit_code == sup.SUP_GAVE_UP and g.alert


def test_policy_unavailable_8_restarts_bounded() -> None:
    assert _d(8).action == "restart" and _d(8).alert
    assert _d(8, state=sup.WatchdogState(date="x", restarts_today=8)).action == "give_up"


def test_policy_stable_run_resets_consecutive_streak() -> None:
    st = sup.WatchdogState(date="x", restarts_today=2, consecutive_failures=4)
    assert _d(1, state=st, ran=10.0).delay_s == 600  # streak 5 -> saturated 600
    d = _d(1, state=st, ran=POLICY.stable_reset_s + 1)
    assert d.consecutive == 1 and d.delay_s == 30


def test_backoff_schedule_saturates() -> None:
    sched = sup.DEFAULT_BACKOFF_S
    assert sched == (30, 60, 120, 300, 600)
    assert [sup.backoff_for(n, sched) for n in range(1, 9)] == [30, 60, 120, 300, 600, 600, 600, 600]
    assert sup.backoff_for(1, ()) == 0.0


def test_watchdog_state_rolls_over_at_new_day(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    sup.WatchdogState(date="2026-10-26", restarts_today=5, consecutive_failures=3).save(p)
    assert sup.WatchdogState.load(p, "2026-10-26").restarts_today == 5
    fresh = sup.WatchdogState.load(p, "2026-10-27")
    assert fresh.restarts_today == 0 and fresh.consecutive_failures == 0


# ------------------------------------------------------------------------------ operating day
EOD = dtime(22, 15)


@pytest.mark.parametrize(("when", "ok"), [
    (datetime(2026, 10, 26, 8, 30, tzinfo=BERLIN), True),  # Monday
    (datetime(2026, 10, 30, 22, 14, tzinfo=BERLIN), True),  # Friday last minute
    (datetime(2026, 10, 26, 22, 15, tzinfo=BERLIN), False),  # cut-off is exclusive
    (datetime(2026, 10, 24, 10, 0, tzinfo=BERLIN), False),  # Saturday
    (datetime(2026, 10, 25, 10, 0, tzinfo=BERLIN), False),  # Sunday
])
def test_operating_day_guard(when, ok) -> None:
    assert sup.operating_day(when, EOD)[0] is ok


def test_operating_day_override() -> None:
    assert sup.operating_day(datetime(2026, 10, 24, 23, 0, tzinfo=BERLIN), EOD, ignore=True)[0]


def test_operating_day_uses_berlin_wall_clock_across_dst_change() -> None:
    # 2026-10-25 02:00-03:00 repeats (CEST -> CET); Monday 08:30 after it is still 08:30 local = 07:30 UTC
    mon_winter = datetime(2026, 10, 26, 8, 30, tzinfo=BERLIN)
    mon_summer = datetime(2026, 10, 19, 8, 30, tzinfo=BERLIN)
    assert mon_winter.astimezone(UTC).hour == 7 and mon_summer.astimezone(UTC).hour == 6
    assert sup.operating_day(mon_winter, EOD)[0] and sup.operating_day(mon_summer, EOD)[0]


# ------------------------------------------------------------------------------ runner command / daily
def test_production_runner_command() -> None:
    cmd = sup.build_runner_cmd("PY", Path("R"), Path("A"), "ALPHA_EXECUTION_DISCOVERY", daily=False)
    assert cmd == ["PY", str(Path("R") / "scripts" / "demo_trader.py"), "--demo-auto",
                   "--confirm-demo-auto=I-AUTHORIZE-ACTIVTRADES-DEMO-TRADING-ONLY", "--artifacts", "A",
                   "--account-phase", "ALPHA_EXECUTION_DISCOVERY"]
    assert sup.build_runner_cmd("PY", Path("R"), Path("A"), "X", daily=True)[-1] == "--daily"


def test_daily_detection_via_help() -> None:
    assert sup.runner_supports_daily([sys.executable, "-c", "import sys;print('--daily')"], Path.cwd())
    assert not sup.runner_supports_daily([sys.executable, "-c", "print('x')"], Path.cwd())
    # current checkout: Lane P has not landed the flag yet, detection must simply reflect the help text
    help_txt = subprocess.run([sys.executable, str(sup.REPO_ROOT / "scripts" / "demo_trader.py"), "--help"],
                              capture_output=True, text=True, check=True).stdout
    assert sup.runner_supports_daily([sys.executable, str(sup.REPO_ROOT / "scripts" / "demo_trader.py")],
                                     sup.REPO_ROOT) == ("--daily" in help_txt)


# ------------------------------------------------------------------------------ presence (no duplicate)
def _hb(verdict, pid=None, reason="x"):
    return lambda art: {"verdict": verdict, "age_s": 5.0, "reason": reason, "status": None if pid is None else {"pid": pid}}


def test_presence_healthy_when_fresh_heartbeat_and_live_pid(tmp_path: Path) -> None:
    kind, _ = sup.runner_presence(tmp_path, create_time=_ct({77: 1.0}), heartbeat=_hb("RUNNING", 77))
    assert kind == "healthy"


def test_presence_none_when_heartbeat_fresh_but_pid_dead(tmp_path: Path) -> None:
    kind, _ = sup.runner_presence(tmp_path, create_time=_ct({}), heartbeat=_hb("RUNNING", 77))
    assert kind == "none"


def test_presence_unhealthy_when_lock_alive_but_no_fresh_heartbeat(tmp_path: Path) -> None:
    (tmp_path / "runner.lock").write_text(json.dumps({"pid": 88, "create_time": 5.0}))
    kind, detail = sup.runner_presence(tmp_path, create_time=_ct({88: 5.0}), heartbeat=_hb("NOT RUNNING", None, "stale"))
    assert kind == "unhealthy" and "88" in detail


# ------------------------------------------------------------------------------ supervisor with fake runners
def _fake_runner(tmp_path: Path, script: str) -> str:
    f = tmp_path / "fake_runner.py"
    f.write_text(
        "import sys, pathlib\n"
        f"m = pathlib.Path({str(tmp_path / 'launches.txt')!r})\n"
        "m.open('a').write('x\\n')\n"
        f"n = len(m.read_text().split())\n{script}\n",
        encoding="utf-8")
    return json.dumps([sys.executable, str(f)])


def _run_sup(tmp_path: Path, runner_json: str, *extra: str) -> tuple[int, int]:
    art = tmp_path / "art"
    cp = subprocess.run(
        [sys.executable, str(SUPERVISOR_PY), "--artifacts", str(art), "--runner-cmd-json", runner_json,
         "--backoff", "0", "--poll-s", "0.05", "--log-dir", str(tmp_path / "logs"), *extra],
        capture_output=True, text=True, timeout=120, cwd=sup.REPO_ROOT)
    launches = tmp_path / "launches.txt"
    return cp.returncode, len(launches.read_text().split()) if launches.exists() else 0


def test_sup_exit0_day_finished_no_restart(tmp_path: Path) -> None:
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, "sys.exit(0)"), "--ignore-operating-day")
    assert (code, n) == (0, 1)


def test_sup_exit2_no_restart_with_alert(tmp_path: Path) -> None:
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, "sys.exit(2)"), "--ignore-operating-day")
    assert (code, n) == (2, 1)
    alert = json.loads((tmp_path / "art" / "watchdog_alert.json").read_text())
    assert alert["latest"]["severity"] == "CRITICAL" and alert["latest"]["exit_code"] == 2


def test_sup_crash_then_recovery_reconciles_via_restart(tmp_path: Path) -> None:
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, "sys.exit(1 if n == 1 else 0)"), "--ignore-operating-day")
    assert (code, n) == (0, 2)


def test_sup_crash_loop_is_bounded(tmp_path: Path) -> None:
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, "sys.exit(1)"), "--ignore-operating-day",
                       "--max-restarts-per-day", "2")
    assert (code, n) == (sup.SUP_GAVE_UP, 3)  # initial + 2 restarts, then give up
    assert (tmp_path / "art" / "watchdog_alert.json").exists()
    state = json.loads((tmp_path / "art" / "watchdog_state.json").read_text())
    assert state["restarts_today"] == 2


def test_sup_fail_closed_7_bounded_and_surfaced(tmp_path: Path) -> None:
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, "sys.exit(7)"), "--ignore-operating-day",
                       "--max-fail-closed-restarts-per-day", "1")
    assert (code, n) == (sup.SUP_GAVE_UP, 2)
    assert json.loads((tmp_path / "art" / "watchdog_alert.json").read_text())["latest"]["severity"] == "CRITICAL"


def test_sup_budget_persists_across_supervisor_restarts(tmp_path: Path) -> None:
    runner = _fake_runner(tmp_path, "sys.exit(1)")
    assert _run_sup(tmp_path, runner, "--ignore-operating-day", "--max-restarts-per-day", "1") == (sup.SUP_GAVE_UP, 2)
    # Task Scheduler restarts the supervisor: the day's budget is already spent -> one launch, then give up
    assert _run_sup(tmp_path, runner, "--ignore-operating-day", "--max-restarts-per-day", "1") == (sup.SUP_GAVE_UP, 3)


def test_sup_stop_file_blocks_start(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    (tmp_path / "art" / "STOP").write_text("stop")
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, "sys.exit(0)"), "--ignore-operating-day")
    assert (code, n) == (0, 0)


def test_sup_stop_file_created_while_running_prevents_restart(tmp_path: Path) -> None:
    art = tmp_path / "art"
    body = f"import pathlib; pathlib.Path({str(art / 'STOP')!r}).write_text('s'); sys.exit(1)"
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, body), "--ignore-operating-day")
    assert (code, n) == (0, 1)


def test_sup_after_hours_and_weekend_guard_blocks_start(tmp_path: Path) -> None:
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, "sys.exit(0)"), "--end-of-day", "00:00")
    assert (code, n) == (0, 0)


def test_sup_second_supervisor_exits_without_launch(tmp_path: Path) -> None:
    art = tmp_path / "art"
    held = lockmod.InstanceLock(art / "supervisor.lock", "supervisor")
    held.acquire()
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, "sys.exit(0)"), "--ignore-operating-day")
    held.release()
    assert (code, n) == (0, 0)


def test_sup_healthy_runner_present_no_duplicate(tmp_path: Path) -> None:
    art = tmp_path / "art"
    art.mkdir()
    (art / "heartbeat.json").write_text(json.dumps({
        "updated_utc": datetime.now(UTC).isoformat(), "process_alive": True, "pid": os.getpid()}))
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, "sys.exit(0)"), "--ignore-operating-day")
    assert (code, n) == (0, 0)


def test_sup_unhealthy_runner_present_not_duplicated_and_alerts(tmp_path: Path) -> None:
    art = tmp_path / "art"
    held = lockmod.InstanceLock(art / "runner.lock", "runner")  # live (this test process), no heartbeat
    held.acquire()
    code, n = _run_sup(tmp_path, _fake_runner(tmp_path, "sys.exit(0)"), "--ignore-operating-day")
    held.release()
    assert (code, n) == (sup.SUP_UNHEALTHY_RUNNER_PRESENT, 0)
    assert (art / "watchdog_alert.json").exists()


def test_sup_missed_start_launches_immediately_reconcile_is_runners_job(tmp_path: Path) -> None:
    """Started late (PC was off at 08:30): operating day -> launch at once, no waiting, no chasing logic here."""
    log = sup.RunLog(tmp_path / "l.log", echo=None)
    cmd = json.loads(_fake_runner(tmp_path, "sys.exit(0)"))
    s = sup.Supervisor(tmp_path / "art", cmd, sup.REPO_ROOT, log, sup.Policy(backoff_s=(0,)), poll_s=0.05,
                       now_fn=lambda: datetime(2026, 10, 26, 10, 40, tzinfo=BERLIN))
    assert s.run() == 0
    assert (tmp_path / "launches.txt").read_text().split() == ["x"]
    s2 = sup.Supervisor(tmp_path / "art2", cmd, sup.REPO_ROOT, log, sup.Policy(backoff_s=(0,)), poll_s=0.05,
                        now_fn=lambda: datetime(2026, 10, 26, 22, 40, tzinfo=BERLIN))
    assert s2.run() == 0
    assert (tmp_path / "launches.txt").read_text().split() == ["x"]  # after hours: nothing more
    log.close()


# ------------------------------------------------------------------------------ task XML
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
TEMPLATE = AUTOSTART / "AutoTrader-DemoDaily.task.xml"


def _render(wake: str = "false") -> ET.Element:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = (text.replace("@@USER_ID@@", "PC\\user").replace("@@REPO_ROOT@@", "C:\\repo")
            .replace("@@START_DATE@@", "2026-10-02").replace("@@WAKE@@", wake))
    return ET.fromstring(text)


def test_task_xml_trigger_is_local_0830_weekdays() -> None:
    root = _render()
    trig = root.find("t:Triggers/t:CalendarTrigger", NS)
    assert trig is not None
    start = trig.findtext("t:StartBoundary", namespaces=NS) or ""
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT08:30:00", start), start  # no Z / offset => local wall clock (DST-safe)
    days = {c.tag.split("}")[1] for c in trig.find("t:ScheduleByWeek/t:DaysOfWeek", NS)}
    assert days == {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
    assert trig.findtext("t:ScheduleByWeek/t:WeeksInterval", namespaces=NS) == "1"


def test_task_xml_settings_and_action() -> None:
    root = _render("true")
    s = root.find("t:Settings", NS)
    assert s is not None
    g = lambda tag: s.findtext(f"t:{tag}", namespaces=NS)  # noqa: E731
    assert g("StartWhenAvailable") == "true"
    assert g("MultipleInstancesPolicy") == "IgnoreNew"
    assert g("RunOnlyIfNetworkAvailable") == "false"
    assert g("WakeToRun") == "true"
    assert g("ExecutionTimeLimit") == "PT15H"
    assert g("DisallowStartIfOnBatteries") == "false" and g("StopIfGoingOnBatteries") == "false"
    assert s.findtext("t:RestartOnFailure/t:Count", namespaces=NS) == "1"  # conservative, bounded
    assert root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=NS) == "InteractiveToken"
    assert root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=NS) == "LeastPrivilege"
    ex = root.find("t:Actions/t:Exec", NS)
    assert ex is not None and ex.findtext("t:WorkingDirectory", namespaces=NS) == "C:\\repo"
    assert "run_trader_day.ps1" in (ex.findtext("t:Arguments", namespaces=NS) or "")
    assert "mt5" not in ET.tostring(root, encoding="unicode").lower().replace("never touches mt5", "")


def test_no_autostart_script_touches_mt5_or_orders() -> None:
    for f in AUTOSTART.iterdir():
        if f.suffix in {".py", ".ps1"}:
            body = f.read_text(encoding="utf-8").lower()
            assert "order_send" not in body and "import metatrader5" not in body
            assert "terminal64" not in body and "stop-process" not in body
            assert "register-scheduledtask" not in body.replace("unregister-scheduledtask", "") or f.name == "register_task.ps1"


# ------------------------------------------------------------------------------ PowerShell (Windows only)
POWERSHELL = shutil.which("powershell")
win_only = pytest.mark.skipif(sys.platform != "win32" or POWERSHELL is None, reason="Windows PowerShell only")


def _ps(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *args],
                          capture_output=True, text=True, timeout=timeout)


@win_only
def test_ps_launcher_dry_run_prints_production_command(tmp_path: Path) -> None:
    cp = _ps(str(AUTOSTART / "run_trader_day.ps1"), "-DryRun", "-Uv", "C:\\fake\\uv.exe")
    assert cp.returncode == 0, cp.stderr
    out = cp.stdout
    assert "supervisor.py" in out and "--daily auto" in out and "ALPHA_EXECUTION_DISCOVERY" in out
    assert "--end-of-day 22:15" in out and "demo_100k" in out and "Global\\AutoTrader-DemoDaily-" in out
    assert str(sup.REPO_ROOT) in out  # working directory = this checkout


@win_only
def test_ps_launcher_propagates_exit_code_and_single_mutex(tmp_path: Path) -> None:
    fake = tmp_path / "uv.cmd"
    fake.write_text("@echo off\r\nping -n 6 127.0.0.1 >nul\r\nexit /b 5\r\n")
    art = tmp_path / "art"
    base = [str(AUTOSTART / "run_trader_day.ps1"), "-Uv", str(fake), "-ArtifactsDir", str(art)]
    first = subprocess.Popen([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *base],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 30
    launcher_log = art / "logs" / "autostart_launcher.log"
    while time.time() < deadline and not (launcher_log.exists() and "start repo" in launcher_log.read_text()):
        time.sleep(0.2)
    second = _ps(*base)  # while the first one still holds the mutex
    assert second.returncode == 0 and "already holds" in second.stdout
    out, _ = first.communicate(timeout=60)
    assert first.returncode == 5 and "supervisor exit code 5" in out


@win_only
def test_ps_register_task_dry_run_validates_xml_without_registering() -> None:
    cp = _ps(str(AUTOSTART / "register_task.ps1"), "-DryRun")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "XML valid" in cp.stdout and "StartWhenAvailable=True" in cp.stdout
    assert "DRY RUN: nothing registered" in cp.stdout and "register_task.ps1" in cp.stdout
    assert "T08:30:00" in cp.stdout
    lst = subprocess.run([POWERSHELL, "-NoProfile", "-Command",
                          "(Get-ScheduledTask -TaskName 'AutoTrader-DemoDaily' -ErrorAction SilentlyContinue | Measure-Object).Count"],
                         capture_output=True, text=True, timeout=60)
    assert lst.stdout.strip() == "0"
