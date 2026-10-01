# ruff: noqa: E501
"""Lane Z (H1 / M2 / M5): supervisor exposure continuation, exit-0 contract, STOP-with-exposure alert, start-failure schedule,
explicit --daily, task XML repetition + logon trigger.  Pure functions / patched process launch: no MT5, no scheduler."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.autostart import supervisor as sup

BERLIN = ZoneInfo("Europe/Berlin")
AUTOSTART = Path(sup.HERE)
POLICY = sup.Policy()
MON = (2026, 10, 26)


def at(hh: int, mm: int = 0, day=MON) -> datetime:
    return datetime(*day, hh, mm, tzinfo=BERLIN)


def _hb(art: Path, when: datetime, **fields) -> None:
    art.mkdir(parents=True, exist_ok=True)
    body = {"process_alive": True, "updated_utc": when.astimezone(UTC).isoformat(), "pid": 4242, **fields}
    (art / "heartbeat.json").write_text(json.dumps(body), encoding="utf-8")


def _d(code, *, now=None, state=None, ran=60.0, **kw):
    now = now or at(10)
    return sup.decide(code, stop_file_exists=kw.pop("stop", False), now=now, state=state or sup.WatchdogState(date="x"),
                      policy=POLICY, ran_s=ran, **kw)


# ------------------------------------------------------------------------------------------ H1: exposure continuation
def test_H1_exposure_at_2216_keeps_the_supervisor_going_until_2330(tmp_path: Path) -> None:
    art = tmp_path / "a"
    _hb(art, at(22, 10), open_positions=1, open_intents=1, flatten_state="OVERDUE")
    ok, why = sup.operating_day_or_exposure(at(22, 16), POLICY, art)
    assert ok and "exposure pending" in why
    assert sup.operating_day_or_exposure(at(23, 29), POLICY, art)[0]
    assert not sup.operating_day_or_exposure(at(23, 31), POLICY, art)[0]  # bounded
    assert not sup.operating_day_or_exposure(at(22, 16, day=(2026, 10, 24)), POLICY, art)[0]  # Saturday


def test_H1_no_exposure_or_old_heartbeat_keeps_the_old_cutoff(tmp_path: Path) -> None:
    art = tmp_path / "a"
    _hb(art, at(22, 10), open_positions=0, open_intents=0, flatten_state="FLAT_CONFIRMED")
    assert not sup.operating_day_or_exposure(at(22, 16), POLICY, art)[0]
    _hb(art, at(21, 50, day=(2026, 10, 25)), open_positions=1)  # yesterday's heartbeat proves nothing today
    assert not sup.operating_day_or_exposure(at(22, 16), POLICY, art)[0]
    assert not sup.operating_day_or_exposure(at(22, 16), POLICY, tmp_path / "missing")[0]


def test_H1_decide_restarts_past_2215_and_ignores_the_budgets_while_exposed() -> None:
    late = at(22, 20)
    assert _d(7, now=late).action == "no_restart"
    d = _d(7, now=late, exposure=True)
    assert d.action == "restart" and d.alert and d.delay_s <= POLICY.exposure_backoff_cap_s
    spent = sup.WatchdogState(date="x", restarts_today=8, fail_closed_restarts_today=3, consecutive_failures=9)
    assert _d(7, state=spent).action == "give_up"
    assert _d(7, state=spent, now=late, exposure=True).action == "restart"  # budgets suspended while exposed


def test_H1_exit0_is_done_only_with_eod_flat_shutdown() -> None:
    assert _d(0, stop_reason="eod_flat_shutdown").action == "done"
    for reason in (None, "signal_15", "stop_file", "requested"):
        d = _d(0, stop_reason=reason)
        assert d.action == "restart" and d.alert and "without eod_flat_shutdown" in d.reason
    assert _d(0, stop_reason="signal_15", now=at(22, 30)).action == "no_restart"  # past the cut-off, no exposure
    assert _d(0, stop_reason="signal_15", now=at(22, 30), exposure=True).action == "restart"


def _sup(tmp_path: Path, now: datetime, runs: list[tuple[int, dict]]) -> tuple[sup.Supervisor, list[int]]:
    art = tmp_path / "art"
    log = sup.RunLog(tmp_path / "l.log", echo=None)
    s = sup.Supervisor(art, ["x"], sup.REPO_ROOT, log, sup.Policy(backoff_s=(0,)), poll_s=0.01, now_fn=lambda: now)
    launched: list[int] = []

    def fake_run_once():
        code, hb = runs[len(launched)]
        launched.append(code)
        s._launched_utc = now.astimezone(UTC) - timedelta(seconds=1)
        s.last_ready = bool(hb.pop("_ready", True))
        _hb(art, now, process_alive=False, **hb)
        return code, 5.0

    s._run_once = fake_run_once  # type: ignore[method-assign]
    s._sleep = lambda _s: None  # type: ignore[method-assign]
    return s, launched


def test_H1_supervisor_restarts_an_exit0_without_eod_flat_shutdown_then_accepts_the_real_one(tmp_path: Path) -> None:
    s, launched = _sup(tmp_path, at(14), [(0, {"stop_reason": "signal_15"}), (0, {"stop_reason": "eod_flat_shutdown"})])
    assert s.run() == 0 and launched == [0, 0]
    state = json.loads((tmp_path / "art" / "watchdog_state.json").read_text())
    assert state["restarts_today"] == 1


def test_H1_supervisor_after_2215_relaunches_while_the_heartbeat_shows_exposure(tmp_path: Path) -> None:
    s, launched = _sup(tmp_path, at(22, 20), [
        (7, {"open_positions": 1, "open_intents": 1, "flatten_state": "OVERDUE", "_ready": True}),
        (0, {"stop_reason": "eod_flat_shutdown", "flatten_state": "FLAT_CONFIRMED"}),
    ])
    _hb(tmp_path / "art", at(22, 19), open_positions=1, open_intents=1, flatten_state="OVERDUE")
    assert s.run() == 0 and launched == [7, 0]  # relaunched past 22:15, then done after flat


def test_H1_stop_file_in_the_flatten_window_with_exposure_is_a_loud_alert(tmp_path: Path) -> None:
    s, launched = _sup(tmp_path, at(21, 58), [])
    art = tmp_path / "art"
    _hb(art, at(21, 57), open_positions=1, open_intents=1, flatten_state="WINDOW")
    (art / "STOP").write_text("stop", encoding="utf-8")
    assert s.run() == 0 and launched == []  # never (re)starts while STOP exists ...
    alert = json.loads((art / "watchdog_alert.json").read_text())["latest"]
    assert alert["severity"] == "CRITICAL" and "STOP file present in the flatten window WITH OWN EXPOSURE" in alert["reason"]


def test_H1_stop_file_without_exposure_or_outside_the_window_raises_no_alert(tmp_path: Path) -> None:
    for now, fields in ((at(21, 58), {"open_positions": 0, "flatten_state": "FLAT_CONFIRMED"}), (at(14), {"open_positions": 1})):
        root = tmp_path / now.strftime("%H%M")
        s, _ = _sup(root, now, [])
        _hb(root / "art", now, **fields)
        (root / "art" / "STOP").write_text("stop", encoding="utf-8")
        assert s.run() == 0
        assert not (root / "art" / "watchdog_alert.json").exists()


# ------------------------------------------------------------------------------------------ M5: start failures
def test_M5_mt5_not_running_uses_the_long_start_schedule_not_the_crash_budget() -> None:
    first = _d(7, now=at(8, 31), ready=False)
    assert first.action == "restart" and first.start_failure and first.delay_s == 60 and first.alert
    assert _d(8, now=at(8, 31), ready=False).delay_s == 60  # exit 8 (stack unavailable) is the same class
    # every budget is already spent: a start failure still retries (it does not consume / depend on them)
    spent = sup.WatchdogState(date="x", restarts_today=8, fail_closed_restarts_today=3, consecutive_failures=9,
                              start_failure_since=at(8, 31).isoformat())
    assert _d(7, now=at(8, 40), ready=False, state=spent).action == "restart"
    # after 45 minutes of failures: every 5 minutes
    slow = sup.WatchdogState(date="x", start_failure_since=at(8, 31).isoformat())
    assert _d(7, now=at(9, 20), ready=False, state=slow).delay_s == 300
    # ... until 12:00 Berlin, then a loud give-up
    g = _d(7, now=at(12, 1), ready=False, state=slow)
    assert g.action == "give_up" and g.alert and g.exit_code == sup.SUP_GAVE_UP
    assert _d(7, now=at(12, 1), ready=False, state=slow, exposure=True).action == "restart"


def test_M5_a_crash_after_ready_keeps_the_existing_budget() -> None:
    crash = _d(7, ready=True)
    assert crash.action == "restart" and not crash.start_failure and crash.delay_s == 30
    st = sup.WatchdogState(date="x", restarts_today=3, fail_closed_restarts_today=3)
    assert _d(7, ready=True, state=st).action == "give_up"


def test_M5_the_supervisor_loop_counts_start_failures_separately(tmp_path: Path) -> None:
    s, launched = _sup(tmp_path, at(8, 31), [(7, {"_ready": False}) for _ in range(4)] + [(0, {"stop_reason": "eod_flat_shutdown"})])
    assert s.run() == 0 and launched == [7, 7, 7, 7, 0]
    state = json.loads((tmp_path / "art" / "watchdog_state.json").read_text())
    assert state["restarts_today"] == 0 and state["start_failures_today"] == 4  # the crash budget is untouched
    alert = json.loads((tmp_path / "art" / "watchdog_alert.json").read_text())
    assert alert["latest"]["start_failures_today"] >= 1


# ------------------------------------------------------------------------------------------ M2: explicit --daily
def test_M2_the_production_launch_command_contains_daily_and_the_approved_exit_policy(tmp_path: Path) -> None:
    assert sup._parser().parse_args([]).daily == "on"  # default ON, not a --help probe
    log = tmp_path / "logs"
    rc = sup.main(["--dry-run", "--ignore-operating-day", "--artifacts", str(tmp_path / "a"), "--log-dir", str(log)])
    assert rc == 0
    text = next(log.glob("trader_*.log")).read_text(encoding="utf-8")
    plan = json.loads(text.split("DRY RUN: ", 1)[1].splitlines()[0])
    cmd = plan["runner_cmd"]
    assert "--daily" in cmd
    # Deployment decision 2026-10-01 (user approval, variant A): chart-based exit profiles after the real-broker canary PASS. The legacy
    # plain `staged` policy and fixed_1_5r-by-omission are NOT the production setting.
    assert cmd[cmd.index("--exit-policy") + 1] == "staged_profiles" and "staged" not in cmd
    assert "--shadow-exit-lab" in cmd and cmd[cmd.index("--shadow-universe") + 1] == "all-ready"


def test_M2_the_launcher_and_the_task_pass_daily_on_explicitly() -> None:
    ps1 = (AUTOSTART / "run_trader_day.ps1").read_text(encoding="utf-8")
    assert re.search(r"\$Daily\s*=\s*'on'", ps1) and "'--daily', $Daily" in ps1
    assert "'auto'" not in ps1.split("$Daily", 2)[1].splitlines()[0]  # the default is not auto


# ------------------------------------------------------------------------------------------ H1(c): task XML
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _render() -> ET.Element:
    text = (AUTOSTART / "AutoTrader-DemoDaily.task.xml").read_text(encoding="utf-8")
    text = text.replace("@@USER_ID@@", "PC\\user").replace("@@REPO_ROOT@@", "C:\\repo").replace("@@START_DATE@@", "2026-10-02").replace("@@WAKE@@", "false")
    return ET.fromstring(text)


def test_H1_task_repeats_every_15_minutes_until_2330_and_re_enters_after_logon() -> None:
    root = _render()
    cal = root.find("t:Triggers/t:CalendarTrigger", NS)
    assert cal is not None
    assert cal.findtext("t:Repetition/t:Interval", namespaces=NS) == "PT15M"
    assert cal.findtext("t:Repetition/t:Duration", namespaces=NS) == "PT15H"  # 08:30 + 15 h = 23:30
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT08:30:00", cal.findtext("t:StartBoundary", namespaces=NS) or "")
    logon = root.find("t:Triggers/t:LogonTrigger", NS)
    assert logon is not None and logon.findtext("t:Enabled", namespaces=NS) == "true"
    assert root.find("t:Triggers/t:BootTrigger", NS) is None  # needs admin rights to register
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"  # no duplicate instances


@pytest.mark.parametrize("hh,mm", [(8, 30), (12, 0)])
def test_operating_day_unchanged_without_exposure(hh: int, mm: int) -> None:
    assert sup.operating_day(at(hh, mm), POLICY.end_of_day)[0]


# ------------------------------------------------------------------------------------------ Lane V (HIGH-2): exit 7 after a latched mt5_lane_timeout
@pytest.mark.parametrize("hh,mm", [(21, 58), (22, 5), (22, 20)])
def test_V_exit_7_from_a_lane_timeout_with_exposure_restarts_within_the_exposure_cap(hh: int, mm: int, tmp_path: Path) -> None:
    now = at(hh, mm)
    state = sup.WatchdogState(date="x", restarts_today=POLICY.max_restarts_per_day, fail_closed_restarts_today=POLICY.max_fail_closed_restarts_per_day)
    d = _d(7, now=now, state=state, ready=True, exposure=True, stop_reason=None)
    assert d.action == "restart" and d.delay_s <= POLICY.exposure_backoff_cap_s  # budgets suspended: a fresh process gets a working lane
    assert d.alert  # and it is loud


def test_V_supervisor_loop_relaunches_after_the_lane_timeout_exit_with_exposure_in_the_flatten_window(tmp_path: Path) -> None:
    s, launched = _sup(tmp_path, at(21, 58), [
        (7, {"open_positions": 1, "open_intents": 1, "flatten_state": "WINDOW", "fail_closed": "stack: mt5_lane_timeout", "_ready": True}),
        (0, {"stop_reason": "eod_flat_shutdown", "flatten_state": "FLAT_CONFIRMED"}),
    ])
    _hb(tmp_path / "art", at(21, 57), open_positions=1, open_intents=1, flatten_state="WINDOW")
    assert s.run() == 0 and launched == [7, 0]


# ------------------------------------------------------------------------------------------ Lane V (LOW): -Disabled renders the task disabled
@pytest.mark.parametrize("name", ["AutoTrader-DemoDaily", "AutoTrader-EodRecovery"])
def test_V_the_task_templates_carry_a_task_level_enabled_placeholder_and_render_disabled(name: str) -> None:
    text = (AUTOSTART / f"{name}.task.xml").read_text(encoding="utf-8")
    for enabled in ("true", "false"):
        body = (text.replace("@@USER_ID@@", "PC-user").replace("@@REPO_ROOT@@", "C:/repo").replace("@@START_DATE@@", "2026-10-02")
                .replace("@@WAKE@@", "false").replace("@@ENABLED@@", enabled))
        root = ET.fromstring(body)  # well-formed XML
        assert root.findtext("t:Settings/t:Enabled", namespaces=NS) == enabled
        assert all(t.findtext("t:Enabled", namespaces=NS) == "true" for t in root.find("t:Triggers", NS))  # triggers untouched


def test_V_register_task_renders_enabled_false_when_disabled() -> None:
    ps = (AUTOSTART / "register_task.ps1").read_text(encoding="utf-8")
    assert "@@ENABLED@@" in ps and "$Disabled.IsPresent" in ps  # the XML is rendered disabled: never registered enabled first


def test_V_logon_caveat_is_documented_prominently_and_in_the_status_helper() -> None:
    doc = (AUTOSTART.parent.parent / "docs" / "AUTOSTART.md").read_text(encoding="utf-8")
    assert "no logon = no task" in doc.lower() and "InteractiveToken" in doc and "EOD recovery" in doc
    assert doc.index("no logon = no task") < doc.index("## Operating model")  # prominent: before the operating model
    status = (AUTOSTART / "status_trader.ps1").read_text(encoding="utf-8")
    assert "InteractiveToken" in status and "NO task runs" in status
