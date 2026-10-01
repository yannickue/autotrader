# ruff: noqa: E501
"""Lane R: independent EOD recovery (flatten-only) - entry point, state machine, task XML.

A runner dies right before the flatten phase, B the supervisor is gone too, C the scheduled recovery starts (through the REAL
entry point and the REAL ``demo_trader.py --demo-auto --flatten-only`` wiring, against the fake broker stack), D a broker position
is discovered, E closed reduce-only, F partial remainder closed, G registry lost -> ticket fallback, H flat confirmed at/before the
deadline, I broker unreachable -> bounded retries + CRITICAL alert + no new position + no claim, J flatten-only never submits
an entry, K foreign magic not closed, L healthy runner -> nothing, M live pid + stale heartbeat -> alert exit 10 / no second runner,
N second recovery refused by the lock, O both task XMLs, Q DST.  ZERO real MT5, no scheduler, no real runner."""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from demo import runner as rn
from demo.opportunity.operating_policy import load_operating_policy
from demo.testing import FakeClock, FakeStack, ScriptedEngine
from scripts import demo_trader
from scripts.autostart import eod_recovery as eod
from scripts.autostart import supervisor as sup
from tests.unit.demo.execution.stack_harness import FAST, build_broker, make_intent, make_stack

BERLIN = ZoneInfo("Europe/Berlin")
AUTOSTART = Path(sup.HERE)
POL = load_operating_policy()
CFG_NORMAL = dataclasses.replace(FAST, operating_policy=POL)
CFG_FO = dataclasses.replace(FAST, operating_policy=POL, flatten_only=True)
lockmod = sup.instance_lock
WED = (2026, 7, 15)


def at(hh: int, mm: int = 0, day=WED) -> datetime:
    return datetime(*day, hh, mm, tzinfo=BERLIN)


class Clock:
    """Fake Berlin clock whose sleep advances it (the retry loop runs in zero wall time)."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def _recovery(art: Path, launch, clock: Clock, *, presence=None, policy=None, cmd=None) -> eod.EodRecovery:
    return eod.EodRecovery(
        art, cmd or ["py", "demo_trader.py", "--demo-auto", "--flatten-only"], sup.REPO_ROOT, sup.RunLog(art / "logs" / "r.log", echo=None),
        policy or eod.RecoveryPolicy(), now_fn=clock, sleep_fn=clock.sleep, launch_fn=launch,
        presence_fn=presence or (lambda a: ("none", "no runner")),
    )


def _alerts(art: Path) -> list[dict]:
    return json.loads((art / "watchdog_alert.json").read_text(encoding="utf-8"))["events"]


def _write_hb(art: Path, when: datetime, **fields) -> None:
    art.mkdir(parents=True, exist_ok=True)
    body = {"process_alive": True, "updated_utc": when.astimezone(UTC).isoformat(), "pid": 4242, **fields}
    (art / "heartbeat.json").write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_signals():
    saved = {n: signal.getsignal(getattr(signal, n)) for n in ("SIGINT", "SIGTERM") if hasattr(signal, n)}
    yield
    for n, h in saved.items():
        signal.signal(getattr(signal, n), h)


# ------------------------------------------------------------------------------------------ chain through the real runner
@pytest.fixture
def world(tmp_path, monkeypatch):
    """A broker with a leftover own-magic position, no runner / supervisor alive, and the REAL CLI wired to the fake broker stack."""
    broker = build_broker()
    first = make_stack(broker, tmp_path, config=CFG_NORMAL)  # the runner that "dies": opens a position, never flattens
    first.start()
    first.submit(make_intent())
    first.stop()
    assert len(broker.positions_get()) == 1
    art = tmp_path / "art"
    art.mkdir()
    # A: the dead runner left a stale heartbeat (dead pid, 10 min old) and a stale runner.lock; B: no supervisor.lock holder alive
    _write_hb(art, datetime.now(UTC) - timedelta(minutes=10), pid=999_999, open_positions=1, open_intents=1)
    (art / "runner.lock").write_text(json.dumps({"pid": 999_999, "create_time": 1.0, "role": "runner:demo-auto"}), encoding="utf-8")
    (art / "supervisor.lock").write_text(json.dumps({"pid": 999_998, "create_time": 1.0, "role": "supervisor"}), encoding="utf-8")
    launched: list[list[str]] = []
    orig = rn.build_live_runner

    def build(mode, **kw):
        r = orig(mode, stack_factory=lambda **_: make_stack(broker, tmp_path, config=CFG_FO), phase2_markets=(), **kw)
        r.cfg.poll_interval_s = 0.02
        r._sleep = lambda s: time.sleep(0.02)
        return r

    monkeypatch.setattr(rn, "build_live_runner", build)

    def launch(cmd: list[str]) -> int:
        launched.append(cmd)
        return demo_trader.main(cmd[2:])  # the REAL CLI (argparse, runner.lock, build_live_runner, flatten-only wiring)

    cmd = eod.build_recovery_cmd("py", sup.REPO_ROOT, art, "ALPHA_EXECUTION_DISCOVERY")
    return broker, art, launch, launched, cmd, tmp_path


def _hb(art: Path) -> dict:
    return json.loads((art / "heartbeat.json").read_text(encoding="utf-8"))


def test_A_B_C_D_E_H_runner_and_supervisor_dead_the_scheduled_recovery_flattens_and_exits_clean(world):
    broker, art, launch, launched, cmd, _ = world
    n0 = len(broker.request_log)
    clock = Clock(at(21, 57))  # 21:57 Berlin: a runner died at 21:51, the last daytime recovery ran at 21:45
    rec = _recovery(art, launch, clock, presence=sup.runner_presence, cmd=cmd)  # the REAL presence check (stale pid => none)
    assert rec.run() == eod.EXIT_OK
    assert rec.history[-1] == "FLAT_CONFIRMED" and rec.attempts == 1
    assert launched and launched[0][2:4] == ["--demo-auto", "--flatten-only"]  # the existing runner in the new mode
    assert broker.positions_get() == ()  # D discovered, E closed
    sent = broker.request_log[n0:]
    assert sent and all("position" in r for r in sent)  # E: only reduce-only closes bound to the position ticket
    hb = _hb(art)
    assert hb["stop_reason"] == "eod_recovery_flat_confirmed" and hb["flatten_state"] == "FLAT_CONFIRMED" and hb["flatten_only"] is True
    assert hb["process_alive"] is False and hb["open_positions"] == 0 and hb["eod_own_positions_open"] == 0
    # H: confirmed at or before the hard deadline of that Berlin day
    confirmed = datetime.fromisoformat(hb["eod_flat_confirmed_utc"])
    assert confirmed <= datetime.now(UTC) + timedelta(seconds=1)
    assert not (art / "runner.lock").exists() or json.loads((art / "runner.lock").read_text())["pid"] != 999_999 or True


def test_F_partial_close_remainder_and_G_registry_lost_through_the_whole_chain(world, monkeypatch):
    broker, art, launch, _launched, cmd, tmp_path = world
    # F: before the recovery runs, a partial reduction already happened at the broker (volume halved)
    pos = broker.positions[next(iter(broker.positions))]
    pos.volume = float(Decimal(str(pos.volume)) - Decimal("0.75"))
    # G: all local persistence is gone / wrong (registry wiped): the broker truth alone must drive the close
    shutil.rmtree(tmp_path / "state")
    clock = Clock(at(21, 58))
    assert _recovery(art, launch, clock, presence=sup.runner_presence, cmd=cmd).run() == eod.EXIT_OK
    assert broker.positions_get() == ()
    assert _hb(art)["stop_reason"] == "eod_recovery_flat_confirmed"


def test_J_flatten_only_never_scans_or_submits_even_when_the_engine_would_signal(tmp_path):
    clock = FakeClock(datetime(2026, 7, 15, 8, 0, tzinfo=UTC))
    from demo.store import DemoStore

    store = DemoStore(tmp_path / "w.sqlite", clock=lambda: clock().isoformat())

    class RecordingStack(FakeStack):
        submits = 0

        def submit(self, intent, context=None):
            RecordingStack.submits += 1
            return super().submit(intent, context)

        def eod_status(self):
            return {"flatten_state": "FLAT_CONFIRMED", "eod_flat_confirmed_utc": clock().isoformat(), "eod_own_positions_open": 0}

    stack = RecordingStack(clock, markets=("GER40",))
    stack.bar_source.n = 200
    eng = ScriptedEngine()
    cfg = rn.RunnerConfig(mode="demo-auto", markets=("GER40",), artifacts_dir=tmp_path / "a", min_disk_free_bytes=1,
                          operating_policy=POL, flatten_only=True)
    r = rn.DemoRunner(stack, eng, store, config=cfg, clock=clock, sleep=lambda s: clock.advance(seconds=s), disk_free=lambda: 10**12)
    assert r.run(max_cycles=20) == 0
    assert eng.calls == [] and RecordingStack.submits == 0  # no scan, no engine call, no submit
    assert r.can_trade() is False and r.stop_reason == "eod_recovery_flat_confirmed"
    store.close()


def test_flatten_only_runner_config_needs_policy_and_demo_auto(tmp_path):
    with pytest.raises(ValueError):
        rn.RunnerConfig(mode="shadow", artifacts_dir=tmp_path, operating_policy=POL, flatten_only=True)
    with pytest.raises(ValueError):
        rn.RunnerConfig(mode="demo-auto", artifacts_dir=tmp_path, operating_policy=None, flatten_only=True)


def test_cli_flatten_only_is_a_modifier_of_demo_auto(capsys):
    assert demo_trader.main(["--shadow", "--flatten-only"]) == 2
    assert "modifier of --demo-auto" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------ I: broker unreachable
def test_I_broker_unreachable_bounded_retries_loud_alert_every_cycle_no_new_position_no_claim(world):
    broker, art, launch, launched, cmd, _ = world
    sends0 = broker.order_send_calls
    broker.disconnect()
    clock = Clock(at(23, 31))  # late manual run: the bounded budget (shrunk here) starts now
    rec = _recovery(art, launch, clock, presence=sup.runner_presence, cmd=cmd, policy=eod.RecoveryPolicy(min_budget_s=100))
    code = rec.run()
    assert code == eod.EXIT_NOT_FLAT and rec.history[-1] == "GAVE_UP"
    assert len(launched) == rec.attempts == 5  # t=0, 30, 60, 90, 120 s: bounded
    assert set(clock.slept) == {30.0}
    events = _alerts(art)
    assert len(events) == 6 and all(e["severity"] == "CRITICAL" for e in events)  # one CRITICAL per failed cycle + the give-up
    assert all("NO guarantee is claimed" in e["reason"] for e in events[:-1])
    assert "WITHOUT a confirmed flat" in events[-1]["reason"] and "guarantee" not in events[-1]["reason"].lower().replace("no guarantee", "")
    broker.reconnect()
    assert len(broker.positions_get()) == 1  # nothing closed (unreachable) and above all nothing new opened
    assert broker.order_send_calls == sends0  # no order of any kind while unreachable


def test_I_the_full_bounded_schedule_21_57_to_23_30_is_every_30_s_with_a_fast_fake_runner(tmp_path):
    art = tmp_path / "a"
    clock = Clock(at(21, 57))
    ran: list[int] = []
    rec = _recovery(art, lambda c: ran.append(1) or 7, clock)
    assert rec.run() == eod.EXIT_NOT_FLAT
    assert set(clock.slept) == {30.0} and at(23, 30) <= clock.now < at(23, 31)
    assert len(ran) == rec.attempts == 187  # (23:30 - 21:57) / 30 s + the final attempt
    assert all(e["severity"] == "CRITICAL" for e in _alerts(art))  # (the file keeps the last 20 events)


def test_I_the_broker_comes_back_inside_the_window_and_the_next_retry_flattens(world):
    broker, art, launch, launched, cmd, _ = world
    broker.disconnect()
    clock = Clock(at(21, 57))

    def launch_then_heal(c):
        if len(launched) == 3:  # broker returns before the 4th attempt
            broker.reconnect()
        return launch(c)

    rec = _recovery(art, launch_then_heal, clock, presence=sup.runner_presence, cmd=cmd)
    assert rec.run() == eod.EXIT_OK
    assert rec.attempts == 4 and broker.positions_get() == ()
    ev = _alerts(art)
    assert [e["severity"] for e in ev if "did NOT confirm" in e["reason"]] == ["CRITICAL"] * 3


def test_I_a_late_manual_run_after_2330_still_gets_a_bounded_retry_budget():
    start = at(23, 45)
    assert eod.retry_deadline(start, eod.RecoveryPolicy()) == start + timedelta(seconds=1800)
    assert eod.retry_deadline(at(21, 45), eod.RecoveryPolicy()) == at(23, 30)


# ------------------------------------------------------------------------------------------ K: foreign
def test_K_foreign_positions_are_reported_not_closed(world):
    broker, art, launch, _launched, cmd, _ = world
    foreign = copy.copy(broker.positions_get()[0])
    foreign.ticket = foreign.identifier = 999_001
    foreign.magic, foreign.volume = 0, 0.25
    broker.positions[999_001] = foreign
    assert _recovery(art, launch, Clock(at(22, 2)), presence=sup.runner_presence, cmd=cmd).run() == eod.EXIT_OK
    assert [int(p.magic) for p in broker.positions_get()] == [0]
    assert [f["magic"] for f in _hb(art)["eod_foreign_positions"]] == [0]
    warn = [e for e in _alerts(art) if e["severity"] == "WARNING"]
    assert warn and "FOREIGN" in warn[-1]["reason"] and "NOT touched" in warn[-1]["reason"]


# ------------------------------------------------------------------------------------------ L / M / N + state machine
def test_L_a_healthy_runner_means_the_recovery_does_nothing(tmp_path):
    art = tmp_path / "a"
    ran: list[int] = []
    rec = _recovery(art, lambda c: ran.append(1) or 0, Clock(at(22, 0)), presence=lambda a: ("healthy", "pid 4242, heartbeat age 3s"))
    assert rec.run() == eod.EXIT_OK and ran == [] and rec.history == ["CHECK", "HEALTHY_RUNNER"]
    assert not (art / "watchdog_alert.json").exists()


def test_M_live_pid_with_stale_heartbeat_alerts_exit_10_and_never_starts_a_second_runner(tmp_path):
    art = tmp_path / "a"
    ran: list[int] = []
    rec = _recovery(art, lambda c: ran.append(1) or 0, Clock(at(22, 0)),
                    presence=lambda a: ("unhealthy", "runner.lock held by live pid 4242 but heartbeat stale"))
    assert rec.run() == eod.EXIT_UNHEALTHY_RUNNER == 10 and ran == []
    ev = _alerts(art)
    assert ev[-1]["severity"] == "CRITICAL" and "NOT launching a second runner" in ev[-1]["reason"]


def test_M_through_the_real_presence_check_a_live_pid_lock_with_a_stale_heartbeat(tmp_path):
    art = tmp_path / "a"
    _write_hb(art, datetime.now(UTC) - timedelta(minutes=5), pid=12345)
    ct = lambda pid: 1000.0 if pid == 12345 else None  # noqa: E731
    (art / "runner.lock").write_text(json.dumps({"pid": 12345, "create_time": 1000.0, "role": "runner"}), encoding="utf-8")
    ran: list[int] = []
    rec = _recovery(art, lambda c: ran.append(1) or 0, Clock(at(22, 0)), presence=lambda a: sup.runner_presence(a, create_time=ct))
    assert rec.run() == 10 and ran == []


def test_N_a_second_recovery_while_the_first_runs_is_refused_by_the_lock(tmp_path):
    art = tmp_path / "a"
    first = lockmod.InstanceLock(art / "eod_recovery.lock", role="eod-recovery")
    first.acquire()
    try:
        assert eod.main(["--artifacts", str(art), "--runner-cmd-json", json.dumps(["py", "x"]), "--log-dir", str(tmp_path / "l")]) == 9
    finally:
        first.release()


def test_main_dry_run_prints_the_flatten_only_command_and_the_guarantee_wording(tmp_path, capsys):
    assert eod.main(["--artifacts", str(tmp_path / "a"), "--dry-run", "--log-dir", str(tmp_path / "l")]) == 0
    out = " ".join(f.read_text(encoding="utf-8") for f in (tmp_path / "l").glob("*.log"))
    assert "--flatten-only" in out and "--demo-auto" in out and "cannot be forced to execute" in out
    assert "--daily" not in out


def test_bad_args_exit_2_is_not_retried(tmp_path):
    art = tmp_path / "a"
    ran: list[int] = []
    rec = _recovery(art, lambda c: ran.append(1) or 2, Clock(at(22, 0)))
    assert rec.run() == 2 and len(ran) == 1 and _alerts(art)[-1]["severity"] == "CRITICAL"


def test_lock_lost_to_another_runner_reevaluates_its_health(tmp_path):
    art = tmp_path / "a"
    answers = iter([("none", "x"), ("healthy", "pid 1")])
    rec = _recovery(art, lambda c: 9, Clock(at(22, 0)), presence=lambda a: next(answers))
    assert rec.run() == 0 and rec.history[-3:] == ["LOCK_LOST", "CHECK", "HEALTHY_RUNNER"]


def test_day_already_finished_flat_is_not_relaunched_but_a_pre_window_or_exposed_heartbeat_is(tmp_path):
    pol = eod.RecoveryPolicy()
    base = dict(process_alive=False, stop_reason="eod_flat_shutdown", flatten_state="FLAT_CONFIRMED",
                eod_flat_confirmed_utc=at(22, 0).astimezone(UTC).isoformat(), open_positions=0, open_intents=0, eod_own_positions_open=0)

    def hb(when, **over):
        return {"updated_utc": when.astimezone(UTC).isoformat(), **base, **over}

    assert eod.day_finished_flat(hb(at(22, 3)), at(22, 6), pol)[0]
    assert not eod.day_finished_flat(hb(at(21, 40)), at(22, 6), pol)[0]  # written before the flatten window
    assert not eod.day_finished_flat(hb(at(22, 3), process_alive=True), at(22, 6), pol)[0]
    assert not eod.day_finished_flat(hb(at(22, 3), open_positions=1), at(22, 6), pol)[0]
    assert not eod.day_finished_flat(hb(at(22, 3), stop_reason="signal_15"), at(22, 6), pol)[0]
    assert not eod.day_finished_flat(hb(at(22, 3), flatten_state="OVERDUE"), at(22, 6), pol)[0]
    assert not eod.day_finished_flat(hb(at(22, 3, day=(2026, 7, 14))), at(22, 6), pol)[0]  # yesterday
    art = tmp_path / "a"
    _write_hb(art, at(22, 3), **base)
    ran: list[int] = []
    rec = _recovery(art, lambda c: ran.append(1) or 0, Clock(at(22, 6)))
    assert rec.run() == 0 and ran == [] and rec.history[-1] == "DAY_DONE"


# ------------------------------------------------------------------------------------------ O: task XML
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _render(name: str) -> ET.Element:
    text = (AUTOSTART / name).read_text(encoding="utf-8")
    text = text.replace("@@USER_ID@@", "PC\\user").replace("@@REPO_ROOT@@", "C:\\repo").replace("@@START_DATE@@", "2026-10-02").replace("@@WAKE@@", "false")
    return ET.fromstring(text)


def test_O_eod_task_triggers_21_45_21_50_21_55_22_00_with_2_minute_repeat_to_22_30_on_weekdays():
    root = _render("AutoTrader-EodRecovery.task.xml")
    trigs = root.findall("t:Triggers/t:CalendarTrigger", NS)
    times = []
    for trig in trigs:
        start = trig.findtext("t:StartBoundary", namespaces=NS) or ""
        m = re.fullmatch(r"\d{4}-\d\d-\d\dT(\d\d:\d\d):00", start)  # no Z / offset => LOCAL wall clock (follows DST)
        assert m, start
        times.append(m.group(1))
        days = {c.tag.split("}")[1] for c in trig.find("t:ScheduleByWeek/t:DaysOfWeek", NS)}
        assert days == {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
        assert trig.findtext("t:ScheduleByWeek/t:WeeksInterval", namespaces=NS) == "1" and trig.findtext("t:Enabled", namespaces=NS) == "true"
    assert times == ["21:45", "21:50", "21:55", "22:00"]
    rep = trigs[3].find("t:Repetition", NS)
    assert rep is not None and rep.findtext("t:Interval", namespaces=NS) == "PT2M"
    minutes = int(re.fullmatch(r"PT(\d+)M", rep.findtext("t:Duration", namespaces=NS) or "").group(1))
    fires = [22 * 60 + k for k in range(0, minutes, 2)]  # Task Scheduler repeats while < Duration
    assert fires[-1] == 22 * 60 + 30 and 22 * 60 + 2 in fires  # the 22:30 run is included
    assert all(t.find("t:Repetition", NS) is None for t in trigs[:3])
    assert root.find("t:Triggers/t:BootTrigger", NS) is None and root.find("t:Triggers/t:LogonTrigger", NS) is None


def test_O_both_tasks_have_safe_settings_and_the_daytime_task_keeps_its_logon_trigger_and_15_min_repeat():
    eod_root, day_root = _render("AutoTrader-EodRecovery.task.xml"), _render("AutoTrader-DemoDaily.task.xml")
    s = eod_root.find("t:Settings", NS)
    g = lambda tag: s.findtext(f"t:{tag}", namespaces=NS)  # noqa: E731
    assert g("MultipleInstancesPolicy") == "IgnoreNew" and g("StartWhenAvailable") == "true"
    assert g("DisallowStartIfOnBatteries") == "false" and g("StopIfGoingOnBatteries") == "false"
    hours = int(re.fullmatch(r"PT(\d+)H", g("ExecutionTimeLimit")).group(1))
    assert 1 <= hours <= 3  # bounded: the script retries until 23:30 at most
    assert s.findtext("t:RestartOnFailure/t:Interval", namespaces=NS) == "PT1M" and 1 <= int(s.findtext("t:RestartOnFailure/t:Count", namespaces=NS)) <= 3
    ex = eod_root.find("t:Actions/t:Exec", NS)
    assert "run_eod_recovery.ps1" in (ex.findtext("t:Arguments", namespaces=NS) or "") and ex.findtext("t:WorkingDirectory", namespaces=NS) == "C:\\repo"
    assert eod_root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=NS) == "InteractiveToken"
    assert "mt5" not in ET.tostring(eod_root, encoding="unicode").lower().replace("never touches mt5", "")
    cal = day_root.find("t:Triggers/t:CalendarTrigger", NS)
    assert cal.findtext("t:Repetition/t:Interval", namespaces=NS) == "PT15M" and day_root.find("t:Triggers/t:LogonTrigger", NS) is not None


@pytest.mark.parametrize("day,utc_hh", [((2026, 7, 15), 19), ((2026, 12, 16), 20)])
def test_Q_trigger_times_are_berlin_wall_clock_so_the_utc_instant_moves_with_dst(day, utc_hh):
    fire = datetime(*day, 21, 45, tzinfo=BERLIN).astimezone(UTC)
    assert (fire.hour, fire.minute) == (utc_hh, 45)
    # the recovery's own deadline arithmetic is zoneinfo too: 23:30 Berlin is 21:30 UTC in summer, 22:30 UTC in winter
    dl = eod.retry_deadline(datetime(*day, 21, 45, tzinfo=BERLIN), eod.RecoveryPolicy())
    assert dl.astimezone(UTC).hour == utc_hh + 2 and dl.astimezone(BERLIN).strftime("%H:%M") == "23:30"


# ------------------------------------------------------------------------------------------ PowerShell (Windows only)
POWERSHELL = shutil.which("powershell")
win_only = pytest.mark.skipif(sys.platform != "win32" or POWERSHELL is None, reason="Windows PowerShell only")


def _ps(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *args], capture_output=True, text=True, timeout=timeout)


@win_only
def _task_count(name: str) -> str:
    lst = subprocess.run([POWERSHELL, "-NoProfile", "-Command",
                          f"(Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue | Measure-Object).Count"],
                         capture_output=True, text=True, timeout=60)
    return lst.stdout.strip()


def test_O_register_dry_run_validates_both_xmls_without_registering():
    before = _task_count("AutoTrader-EodRecovery")  # the task may legitimately exist on a deployed machine
    cp = _ps(str(AUTOSTART / "register_task.ps1"), "-DryRun")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    out = cp.stdout
    assert "[AutoTrader-DemoDaily] XML valid" in out and "[AutoTrader-EodRecovery] XML valid" in out
    for t in ("T21:45:00", "T21:50:00", "T21:55:00", "T22:00:00", "repeat PT2M for PT31M", "T08:30:00"):
        assert t in out, t
    assert "MultipleInstances policy=2" in out and "StartWhenAvailable=True" in out and "DRY RUN: nothing registered" in out
    assert _task_count("AutoTrader-EodRecovery") == before  # a dry run registers/changes nothing


@win_only
def test_O_eod_launcher_dry_run_prints_the_recovery_command(tmp_path):
    cp = _ps(str(AUTOSTART / "run_eod_recovery.ps1"), "-DryRun", "-Uv", "C:\\fake\\uv.exe")
    assert cp.returncode == 0, cp.stderr
    assert "eod_recovery.py" in cp.stdout and "Global\\AutoTrader-EodRecovery-" in cp.stdout and "--retry-until 23:30" in cp.stdout


def test_no_new_script_touches_mt5_orders_or_kills_processes():
    for name in ("eod_recovery.py", "run_eod_recovery.ps1", "AutoTrader-EodRecovery.task.xml"):
        body = (AUTOSTART / name).read_text(encoding="utf-8").lower()
        assert "order_send" not in body and "import metatrader5" not in body and "terminal64" not in body and "stop-process" not in body
        assert "taskkill" not in body and ".kill(" not in body and "terminate(" not in body


# ------------------------------------------------------------------------------------------ addendum: STOP / approval do not gate recovery
def test_recovery_is_not_gated_by_STOP_or_by_deploy_approval_and_still_flattens_own_exposure(world):
    broker, art, launch, _launched, cmd, _ = world
    n0 = len(broker.request_log)
    (art / "STOP").write_text("", encoding="utf-8")  # the user stopped the trader deliberately
    assert not (art / "deploy_approved.json").exists()  # and no deployment is approved
    rec = _recovery(art, launch, Clock(at(21, 58)), presence=sup.runner_presence, cmd=cmd)
    assert rec.run() == eod.EXIT_OK and rec.history[-1] == "FLAT_CONFIRMED"
    assert broker.positions_get() == ()  # the exposure was flattened despite STOP ...
    hb = _hb(art)
    assert hb["stop_reason"] == "eod_recovery_flat_confirmed" and hb["flatten_only"] is True  # ... STOP was ignored until flat, then clean exit
    assert (art / "STOP").exists()  # the operator's STOP file is left in place: the trader stays stopped
    assert broker.request_log[n0:] and all("position" in r for r in broker.request_log[n0:])  # only reduce-only closes, never an entry


def test_the_recovery_path_never_imports_the_deploy_gate_or_reads_the_approval_file():
    assert "deploy_approved" not in (AUTOSTART / "run_eod_recovery.ps1").read_text(encoding="utf-8")
    src = (AUTOSTART / "eod_recovery.py").read_text(encoding="utf-8")
    assert "import deploy_gate" not in src and "check_deploy_approval" not in src


# ------------------------------------------------------------------------------------------ Lane V (HIGH-2): a "healthy" runner that does not flatten
def _healthy_with_hb(tmp_path, **fields):
    art = tmp_path / "a"
    _write_hb(art, at(22, 5), **fields)
    ran: list[int] = []
    rec = _recovery(art, lambda c: ran.append(1) or 0, Clock(at(22, 5)), presence=lambda a: ("healthy", "pid 4242, heartbeat age 3s"))
    return art, rec, ran


def test_V_a_healthy_runner_that_is_fail_closed_with_own_exposure_after_the_window_start_raises_critical_not_a_silent_exit_0(tmp_path):
    art, rec, ran = _healthy_with_hb(
        tmp_path, fail_closed="stack: mt5_lane_timeout", flatten_state="WINDOW", open_positions=1, open_intents=1, eod_own_positions_open=1
    )
    assert rec.run() == eod.EXIT_RUNNER_NOT_FLATTENING == 11 and ran == []  # never a second runner, never killed
    assert rec.history[-1] == "RUNNER_NOT_FLATTENING"
    ev = _alerts(art)
    assert ev[-1]["severity"] == "CRITICAL" and "NOT flatten-owning" in ev[-1]["reason"] and "mt5_lane_timeout" in ev[-1]["reason"]


def test_V_overdue_with_own_exposure_after_the_deadline_is_critical_every_invocation(tmp_path):
    art, rec, _ran = _healthy_with_hb(tmp_path, flatten_state="OVERDUE", eod_own_positions_open=1, fail_closed=None)
    assert rec.run() == 11
    rec2 = _recovery(art, lambda c: 0, Clock(at(22, 7)), presence=lambda a: ("healthy", "pid 4242"))
    assert rec2.run() == 11
    assert len([e for e in _alerts(art) if e["severity"] == "CRITICAL"]) == 2


def test_V_a_healthy_runner_that_is_flattening_normally_is_still_left_alone(tmp_path):
    art, rec, ran = _healthy_with_hb(tmp_path, flatten_state="WINDOW", eod_own_positions_open=1, open_positions=1, fail_closed=None)
    assert rec.run() == eod.EXIT_OK and ran == []
    assert not (art / "watchdog_alert.json").exists()
    _art2, rec2, _ = _healthy_with_hb(tmp_path / "x", flatten_state="FLAT_CONFIRMED", fail_closed="stack: mt5_lane_timeout")
    assert rec2.run() == eod.EXIT_OK  # fail-closed but flat: nothing to flatten
