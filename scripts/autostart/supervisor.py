# ruff: noqa: E501
"""Bounded watchdog / supervisor for the DEMO trader (native Task Scheduler companion).

Launched by ``run_trader_day.ps1`` (Task ``AutoTrader-DemoDaily``).  It

* refuses to run outside the operating day (Mon-Fri, before the end-of-day cut-off, Berlin time),
* refuses to run while a STOP file exists,
* takes a ``supervisor.lock`` (one supervisor per artifacts dir),
* does NOT launch a second runner when a healthy one already runs (fresh heartbeat + live pid),
* starts ``scripts/demo_trader.py --demo-auto ...`` (the runner itself does DEMO verification,
  broker connectivity, persisted-state recovery, broker/local reconciliation, READY only if safe and
  takes its own ``runner.lock``),
* restarts it on unexpected death with BOUNDED retries and exponential backoff,
* writes ``<artifacts>/watchdog_alert.json`` + a loud log line when failures repeat.

It never starts, stops, kills or touches MetaTrader 5 and never kills the runner (open positions are
protected by broker-side stops; an operator stop is a STOP file or Ctrl+C -> orderly runner shutdown).

Runner exit-code contract consumed here (src/demo/runner.py, scripts/demo_trader.py):
0 done (``eod_flat_shutdown`` with --daily; before that contract lands: day finished, never restart),
2 bad args / DEMO auth refused (no restart), 7 fail-closed orderly stop (bounded restart, surfaced),
8 MT5 stack unavailable (bounded restart), 9 second runner refused (no restart), other / signal =
crash (bounded restart).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, TextIO

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
for _p in (HERE, REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import instance_lock  # noqa: E402

CONFIRMATION = "I-AUTHORIZE-ACTIVTRADES-DEMO-TRADING-ONLY"
TZ_NAME = "Europe/Berlin"

# supervisor process exit codes
SUP_OK = 0
SUP_UNHEALTHY_RUNNER_PRESENT = 10  # a runner process holds the lock but its heartbeat is not fresh
SUP_GAVE_UP = 20  # restart budget exhausted (alert written)
# runner exit codes (mirrors src/demo/runner.py + scripts/demo_trader.py)
RUN_OK, RUN_BAD_ARGS, RUN_NO_HEARTBEAT, RUN_FAIL_CLOSED, RUN_UNAVAILABLE = 0, 2, 3, 7, 8
RUN_ALREADY_RUNNING = instance_lock.EXIT_ALREADY_RUNNING

DEFAULT_BACKOFF_S = (30, 60, 120, 300, 600)


# ----------------------------------------------------------------------------- policy (pure)
@dataclass(frozen=True)
class Policy:
    backoff_s: tuple[float, ...] = DEFAULT_BACKOFF_S
    max_restarts_per_day: int = 8
    max_fail_closed_restarts_per_day: int = 3
    end_of_day: dtime = dtime(22, 15)
    stable_reset_s: float = 1800.0  # a run this long resets the consecutive-failure streak
    alert_after_consecutive: int = 2


@dataclass
class WatchdogState:
    date: str = ""
    restarts_today: int = 0
    fail_closed_restarts_today: int = 0
    consecutive_failures: int = 0

    @classmethod
    def load(cls, path: Path, today: str) -> WatchdogState:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            st = cls(**{k: raw[k] for k in asdict(cls()) if k in raw})
        except (OSError, ValueError, TypeError):
            st = cls()
        if st.date != today:  # new day: fresh budget
            st = cls(date=today)
        return st

    def save(self, path: Path) -> None:
        _atomic_write(path, json.dumps(asdict(self), indent=1))


@dataclass(frozen=True)
class Decision:
    action: str  # "done" | "restart" | "no_restart" | "give_up"
    reason: str
    delay_s: float = 0.0
    alert: bool = False
    consecutive: int = 0
    exit_code: int = SUP_OK


def parse_hhmm(text: str) -> dtime:
    hh, mm = text.split(":")
    return dtime(int(hh), int(mm))


def berlin_now() -> datetime:
    """Aware 'now' in Europe/Berlin (zoneinfo; falls back to the OS zone, which is Berlin on this PC)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:  # pragma: no cover - tzdata missing
        return datetime.now().astimezone()


def operating_day(now: datetime, end_of_day: dtime, *, ignore: bool = False) -> tuple[bool, str]:
    if ignore:
        return True, "operating-day guard ignored (--ignore-operating-day)"
    if now.weekday() >= 5:
        return False, f"weekend ({now:%A}): not an operating day"
    if now.time() >= end_of_day:
        return False, f"after operating-day end {end_of_day:%H:%M} (now {now:%H:%M})"
    return True, "operating day"


def backoff_for(consecutive_failures: int, schedule: Sequence[float]) -> float:
    """Delay before restart number ``consecutive_failures`` (1-based); saturates at the last entry."""
    if not schedule:
        return 0.0
    return float(schedule[min(max(consecutive_failures, 1) - 1, len(schedule) - 1)])


def decide(exit_code: int, *, stop_file_exists: bool, now: datetime, state: WatchdogState,
           policy: Policy, ran_s: float, ignore_operating_day: bool = False) -> Decision:
    """Exit-code policy table (pure).  ``state`` is the budget BEFORE this exit."""
    consecutive = 1 if ran_s >= policy.stable_reset_s else state.consecutive_failures + 1
    if stop_file_exists:
        return Decision("done", "STOP file present: never restart", consecutive=consecutive)
    if exit_code == RUN_OK:
        return Decision("done", "runner exited 0 (day finished / eod_flat_shutdown): do not restart")
    if exit_code == RUN_ALREADY_RUNNING:
        return Decision("done", "runner refused: another runner holds runner.lock", consecutive=consecutive)
    if exit_code == RUN_BAD_ARGS:
        return Decision("no_restart", "exit 2 (bad arguments / DEMO authorisation refused): "
                        "restart cannot help", alert=True, consecutive=consecutive, exit_code=RUN_BAD_ARGS)
    ok, why = operating_day(now, policy.end_of_day, ignore=ignore_operating_day)
    if not ok:
        return Decision("no_restart", f"exit {exit_code} but {why}: no restart", alert=exit_code != 0,
                        consecutive=consecutive, exit_code=SUP_OK)
    if exit_code == RUN_FAIL_CLOSED and state.fail_closed_restarts_today >= policy.max_fail_closed_restarts_per_day:
        return Decision("give_up", f"fail-closed stop (7) repeated: {state.fail_closed_restarts_today} restarts "
                        "today already", alert=True, consecutive=consecutive, exit_code=SUP_GAVE_UP)
    if state.restarts_today >= policy.max_restarts_per_day:
        return Decision("give_up", f"restart budget exhausted ({state.restarts_today}/"
                        f"{policy.max_restarts_per_day} today)", alert=True, consecutive=consecutive,
                        exit_code=SUP_GAVE_UP)
    label = {RUN_FAIL_CLOSED: "fail-closed orderly stop (7)", RUN_UNAVAILABLE: "MT5 stack unavailable (8)"}.get(
        exit_code, f"unexpected exit {exit_code}")
    return Decision("restart", f"{label}: bounded restart", delay_s=backoff_for(consecutive, policy.backoff_s),
                    alert=exit_code in (RUN_FAIL_CLOSED, RUN_UNAVAILABLE) or consecutive >= policy.alert_after_consecutive,
                    consecutive=consecutive)


# ----------------------------------------------------------------------------- files / health
def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_alert(artifacts: Path, severity: str, reason: str, **detail: Any) -> Path:
    path = artifacts / "watchdog_alert.json"
    events: list[Any] = []
    with contextlib.suppress(OSError, ValueError):
        events = list(json.loads(path.read_text(encoding="utf-8")).get("events", []))
    event = {"ts_utc": datetime.now(UTC).isoformat(timespec="seconds"), "severity": severity,
             "reason": reason, **detail}
    events = [*events, event][-20:]
    _atomic_write(path, json.dumps({"latest": event, "events": events}, indent=1, default=str))
    return path


def _heartbeat(artifacts: Path, now_utc: datetime | None = None) -> dict[str, Any]:
    from demo.monitor import heartbeat_verdict  # lazy: pure file read, no MT5

    return heartbeat_verdict(artifacts / "heartbeat.json", now_utc or datetime.now(UTC))


def runner_presence(artifacts: Path, *, create_time: instance_lock.CreateTimeFn = instance_lock.process_create_time,
                    heartbeat: Callable[[Path], dict[str, Any]] = _heartbeat) -> tuple[str, str]:
    """('healthy'|'unhealthy'|'none', detail) for a runner on THIS artifacts dir.

    healthy   = heartbeat fresh (<= 90 s, process_alive) and its pid is a live process (also covers a
                runner started before runner.lock existed);
    unhealthy = runner.lock holder alive but heartbeat not fresh (starting up, hung) - never launch a 2nd;
    none      = nothing alive."""
    hb = heartbeat(artifacts)
    st = hb.get("status") or {}
    pid = st.get("pid")
    if hb.get("verdict") == "RUNNING" and isinstance(pid, int) and create_time(pid) is not None:
        return "healthy", f"pid {pid}, heartbeat age {hb.get('age_s'):.0f}s"
    lock = instance_lock.lock_status(artifacts / "runner.lock", create_time)
    if lock["alive"]:
        return "unhealthy", f"runner.lock held by live pid {lock['holder']['pid']} but {hb.get('reason')}"
    return "none", str(hb.get("reason"))


def runner_supports_daily(base_cmd: Sequence[str], cwd: Path) -> bool:
    try:
        out = subprocess.run([*base_cmd, "--help"], cwd=cwd, capture_output=True, text=True, timeout=60,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--daily" in (out.stdout + out.stderr)


def build_runner_cmd(python: str, repo_root: Path, artifacts: Path, account_phase: str, daily: bool) -> list[str]:
    cmd = [python, str(repo_root / "scripts" / "demo_trader.py"), "--demo-auto",
           f"--confirm-demo-auto={CONFIRMATION}", "--artifacts", str(artifacts),
           "--account-phase", account_phase]
    if daily:
        cmd.append("--daily")
    return cmd


# ----------------------------------------------------------------------------- logging
class RunLog:
    def __init__(self, path: Path, echo: TextIO | None = sys.stdout) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._echo = echo

    def write(self, tag: str, msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} [{tag}] {msg.rstrip()}"
        with self._lock:
            self._fh.write(line + "\n")
            if self._echo is not None:
                with contextlib.suppress(Exception):
                    print(line, file=self._echo, flush=True)

    def info(self, msg: str) -> None:
        self.write("SUP", msg)

    def alert(self, msg: str) -> None:
        self.write("SUP", f"!!! WATCHDOG ALERT !!! {msg}")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._fh.close()


def log_path_for(artifacts: Path, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    return artifacts / "logs" / f"trader_{now:%Y%m%d_%H%M%S}.log"


# ----------------------------------------------------------------------------- supervisor
@dataclass
class Supervisor:
    artifacts: Path
    runner_cmd: list[str]
    repo_root: Path
    log: RunLog
    policy: Policy = field(default_factory=Policy)
    ignore_operating_day: bool = False
    poll_s: float = 5.0
    ready_timeout_s: float = 300.0
    stop_grace_s: float = 120.0
    now_fn: Callable[[], datetime] = berlin_now
    stop_requested: bool = False

    @property
    def stop_file(self) -> Path:
        return self.artifacts / "STOP"

    def request_stop(self, *_: object) -> None:
        self.stop_requested = True

    def install_signals(self) -> None:
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, self.request_stop)

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stop_requested and time.monotonic() < end:
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def _alert(self, severity: str, reason: str, **detail: Any) -> None:
        self.log.alert(f"{severity}: {reason}")
        with contextlib.suppress(OSError):
            write_alert(self.artifacts, severity, reason, log=str(self.log.path), **detail)

    def _launch(self) -> subprocess.Popen[str]:
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":  # own process group: Ctrl+C on the supervisor must not hit the runner
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.log.info("launching runner: " + " ".join(self.runner_cmd))
        proc = subprocess.Popen(self.runner_cmd, cwd=self.repo_root, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                **kwargs)

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log.write("RUN", line)

        threading.Thread(target=pump, daemon=True).start()
        return proc

    def _check_ready(self, proc_pid: int, launched_utc: datetime) -> dict[str, Any] | None:
        hb = _heartbeat(self.artifacts)
        st = hb.get("status") or {}
        if hb.get("verdict") != "RUNNING" or st.get("pid") != proc_pid:
            return None
        try:
            updated = datetime.fromisoformat(str(st.get("updated_utc", "")).replace("Z", "+00:00"))
        except ValueError:
            return None
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        return st if updated >= launched_utc else None

    def _forward_stop(self, proc: subprocess.Popen[str]) -> None:
        self.log.info("stop requested: asking the runner for an orderly shutdown (runner is never killed)")
        with contextlib.suppress(Exception):
            proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
        deadline = time.monotonic() + self.stop_grace_s
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.5)
        if proc.poll() is None:
            self.log.info(f"runner pid {proc.pid} still shutting down after {self.stop_grace_s:.0f}s; "
                          "supervisor exits, runner left alone")

    def _run_once(self) -> tuple[int | None, float]:
        """Launch + watch one runner process.  (None, ran_s) = supervisor stop requested."""
        launched_utc = datetime.now(UTC)
        t0 = time.monotonic()
        proc = self._launch()
        ready = warned = False
        while proc.poll() is None:
            if self.stop_requested:
                self._forward_stop(proc)
                return None, time.monotonic() - t0
            time.sleep(self.poll_s)
            if ready:
                continue
            st = self._check_ready(proc.pid, launched_utc)
            if st is not None and st.get("reconciliation") == "RECONCILED" and not st.get("fail_closed") \
                    and not st.get("halted"):
                ready = True
                self.log.info(f"READY: pid {proc.pid} heartbeat fresh, reconciliation=RECONCILED, open_positions="
                              f"{st.get('open_positions')}, open_orders={st.get('open_orders')}, "
                              f"mt5_connected={st.get('mt5_connected')}, account_phase={st.get('account_phase')}, "
                              f"git={st.get('git_commit')}")
            elif not warned and time.monotonic() - t0 > self.ready_timeout_s:
                warned = True
                self.log.info(f"WARN: runner pid {proc.pid} not READY/RECONCILED after {self.ready_timeout_s:.0f}s "
                              f"(heartbeat: {None if st is None else st.get('reconciliation')}); runner decides "
                              "(transient grace then exit 7)")
        code = proc.wait()
        time.sleep(0.2)  # let the pump drain
        return code, time.monotonic() - t0

    def run(self) -> int:
        state_path = self.artifacts / "watchdog_state.json"
        self.install_signals()
        while True:
            now = self.now_fn()
            state = WatchdogState.load(state_path, f"{now:%Y-%m-%d}")
            if self.stop_file.exists():
                self.log.info(f"STOP file present ({self.stop_file}): not starting / restarting; done")
                return SUP_OK
            ok, why = operating_day(now, self.policy.end_of_day, ignore=self.ignore_operating_day)
            if not ok:
                self.log.info(f"not launching: {why}")
                return SUP_OK
            code, ran_s = self._run_once()
            if code is None:
                self.log.info("stopped by operator/signal; not restarting")
                return SUP_OK
            now = self.now_fn()
            state = WatchdogState.load(state_path, f"{now:%Y-%m-%d}")
            d = decide(code, stop_file_exists=self.stop_file.exists(), now=now, state=state, policy=self.policy,
                       ran_s=ran_s, ignore_operating_day=self.ignore_operating_day)
            self.log.info(f"runner exit code {code} after {ran_s:.0f}s -> {d.action}: {d.reason}")
            state.consecutive_failures = d.consecutive
            if d.action == "restart":
                state.restarts_today += 1
                if code == RUN_FAIL_CLOSED:
                    state.fail_closed_restarts_today += 1
            with contextlib.suppress(OSError):
                state.save(state_path)
            if d.alert:
                self._alert("CRITICAL" if d.action in ("give_up", "no_restart") else "WARNING", d.reason,
                            exit_code=code, restarts_today=state.restarts_today,
                            consecutive_failures=state.consecutive_failures)
            if d.action != "restart":
                return d.exit_code
            self.log.info(f"restart {state.restarts_today}/{self.policy.max_restarts_per_day} today in "
                          f"{d.delay_s:.0f}s (runner reconciles before READY)")
            self._sleep(d.delay_s)
            if self.stop_requested:
                self.log.info("stop requested during backoff; not restarting")
                return SUP_OK


# ----------------------------------------------------------------------------- CLI
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bounded supervisor for the DEMO trader")
    p.add_argument("--artifacts", type=Path, default=REPO_ROOT / "artifacts" / "demo_100k")
    p.add_argument("--account-phase", default="ALPHA_EXECUTION_DISCOVERY")
    p.add_argument("--daily", choices=("auto", "on", "off"), default="auto",
                   help="pass --daily to the runner: auto = only if demo_trader.py --help lists it")
    p.add_argument("--python", default=sys.executable, help="interpreter for the runner (default: this one)")
    p.add_argument("--runner-cmd-json", default=None, help="TEST ONLY: full runner command as a JSON list")
    p.add_argument("--backoff", default=",".join(str(int(x)) for x in DEFAULT_BACKOFF_S),
                   help="comma separated restart backoff seconds (saturates at the last value)")
    p.add_argument("--max-restarts-per-day", type=int, default=Policy.max_restarts_per_day)
    p.add_argument("--max-fail-closed-restarts-per-day", type=int, default=Policy.max_fail_closed_restarts_per_day)
    p.add_argument("--end-of-day", default="22:15", help="HH:MM Berlin; no (re)start from then on")
    p.add_argument("--ignore-operating-day", action="store_true", help="skip the weekday/end-of-day guard (manual)")
    p.add_argument("--log-dir", type=Path, default=None)
    p.add_argument("--poll-s", type=float, default=5.0)
    p.add_argument("--dry-run", action="store_true", help="print the plan and the exact runner command; launch nothing")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    artifacts: Path = a.artifacts
    policy = Policy(backoff_s=tuple(float(x) for x in a.backoff.split(",") if x.strip() != ""),
                    max_restarts_per_day=a.max_restarts_per_day,
                    max_fail_closed_restarts_per_day=a.max_fail_closed_restarts_per_day,
                    end_of_day=parse_hhmm(a.end_of_day))
    log = RunLog(log_path_for(artifacts) if a.log_dir is None else
                 a.log_dir / log_path_for(artifacts).name)
    try:
        if a.runner_cmd_json:
            cmd = list(json.loads(a.runner_cmd_json))
        else:
            base = [a.python, str(REPO_ROOT / "scripts" / "demo_trader.py")]
            daily = a.daily == "on" or (a.daily == "auto" and runner_supports_daily(base, REPO_ROOT))
            cmd = build_runner_cmd(a.python, REPO_ROOT, artifacts, a.account_phase, daily)
            log.info(f"--daily {'ON' if daily else 'OFF'} (mode={a.daily})")
        sup = Supervisor(artifacts, cmd, REPO_ROOT, log, policy, a.ignore_operating_day, poll_s=a.poll_s)
        now = sup.now_fn()
        log.info(f"supervisor start pid={os.getpid()} now={now:%Y-%m-%d %H:%M:%S %Z} artifacts={artifacts} "
                 f"log={log.path}")
        if a.dry_run:
            log.info("DRY RUN: " + json.dumps({"runner_cmd": cmd, "policy": asdict(policy) | {
                "end_of_day": a.end_of_day}, "operating_day": operating_day(now, policy.end_of_day,
                                                                             ignore=a.ignore_operating_day)},
                                              default=str))
            return SUP_OK
        ok, why = operating_day(now, policy.end_of_day, ignore=a.ignore_operating_day)
        if not ok:
            log.info(f"not launching: {why}")
            return SUP_OK
        if sup.stop_file.exists():
            log.info(f"STOP file present ({sup.stop_file}); delete it to allow the trader to start. Exit 0")
            return SUP_OK
        with contextlib.ExitStack() as stack:
            sup_lock = instance_lock.InstanceLock(artifacts / "supervisor.lock", role="supervisor")
            try:
                sup_lock.acquire()
            except instance_lock.LockHeld as exc:
                log.info(f"another supervisor is active ({exc}); exit 0")
                return SUP_OK
            stack.callback(sup_lock.release)
            presence, detail = runner_presence(artifacts)
            if presence == "healthy":
                log.info(f"healthy runner already running ({detail}); not launching another. Exit 0")
                return SUP_OK
            if presence == "unhealthy":
                msg = f"runner process present but not healthy ({detail}); NOT launching a second, NOT killing it"
                log.alert(msg)
                with contextlib.suppress(OSError):
                    write_alert(artifacts, "CRITICAL", msg, log=str(log.path))
                return SUP_UNHEALTHY_RUNNER_PRESENT
            return sup.run()
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
