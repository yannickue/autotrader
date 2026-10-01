# ruff: noqa: E501
"""Independent END-OF-DAY RECOVERY (flatten-only) entry point (Lane R, scheduled task ``AutoTrader-EodRecovery``).

WHY: the daytime task (``AutoTrader-DemoDaily``, every 15 min from 08:30) must not be the last line of defence for the hard rule
"22:00 Europe/Berlin = ZERO strategy exposure".  If the supervisor AND the runner both die at 21:51 and the last daytime
recovery ran at 21:45, the next one (22:00+) is too late.  This separate task fires at 21:45 / 21:50 / 21:55 / 22:00 and then every
2 minutes until 22:30 (Mon-Fri, local Berlin time) and calls this script, which

* takes ``<artifacts>/eod_recovery.lock`` (a second invocation while one runs is refused: exit 9),
* looks at the artifacts dir exactly like the supervisor does (``supervisor.runner_presence``):
    - healthy runner (runner.lock + fresh heartbeat + live pid)  -> does NOTHING, exit 0 ("runner healthy, its own sweep handles flat"),
    - live pid but stale heartbeat (starting / hung)             -> CRITICAL alert, does NOT start a second one, never kills it, exit 10,
    - nothing alive                                              -> starts the EXISTING runner in FLATTEN-ONLY mode,
* flatten-only runner = ``scripts/demo_trader.py --demo-auto --flatten-only``: DEMO + account binding, broker connect, persisted-state
  recovery, broker reconciliation, then the existing end-of-day sweep (reduce-only, bounded backoff, broker-ticket fallback for positions
  the registry lost / mis-adopted) until the broker is flat for our magic and reconciled -> exit 0, ``stop_reason=eod_recovery_flat_confirmed``.
  It never scans, never submits an entry (the stack's entry gate is permanently closed), never learns.  Foreign / manual positions
  (other magic) are NOT closed: they are reported in the heartbeat (``eod_foreign_positions``) and as a WARNING alert,
* if the broker is unreachable / the flatten is not confirmed: bounded retries (default every 30 s until 23:30 Berlin, or at least
  30 min for a manual late invocation), a loud CRITICAL ``watchdog_alert.json`` event EVERY cycle, no new position, the protective
  broker stop untouched, and NEVER a guarantee claim.

GUARANTEE (exact wording): every controllable execution path actively enforces flat-before-22:00; an unavailable broker cannot be
forced to execute.

Exit codes: 0 flat confirmed (or healthy runner owns the day / day already finished flat); 2 bad arguments / DEMO authorisation refused
(no retry can help); 9 another recovery holds the lock; 10 live runner process with a stale heartbeat (not touched); 20 retry window
ended WITHOUT a confirmed flat (CRITICAL alert).  It never starts, stops or touches MetaTrader 5 and never kills a process.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
for _p in (HERE, REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import instance_lock  # noqa: E402
import supervisor as sup  # noqa: E402  (pure helpers: presence, alerts, RunLog, heartbeat)

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_LOCKED = instance_lock.EXIT_ALREADY_RUNNING  # 9
EXIT_UNHEALTHY_RUNNER = sup.SUP_UNHEALTHY_RUNNER_PRESENT  # 10
EXIT_NOT_FLAT = 20  # retry window ended without a confirmed flat

RECOVERY_STOP_REASON = "eod_recovery_flat_confirmed"
DAY_DONE_STOP_REASONS = (sup.EOD_STOP_REASON, RECOVERY_STOP_REASON)
GUARANTEE = ("every controllable execution path actively enforces flat-before-22:00; "
             "an unavailable broker cannot be forced to execute")


@dataclass(frozen=True)
class RecoveryPolicy:
    retry_s: float = 30.0  # pause between failed recovery attempts (broker unreachable / flat not confirmed)
    retry_until: dtime = dtime(23, 30)  # Berlin
    min_budget_s: float = 1800.0  # a late / manual invocation still gets this much retry time
    pending_alert_s: float = 60.0  # CRITICAL while a flatten-only runner is still working on an open exposure
    flatten_start: dtime = dtime(21, 55)  # only used to recognise "the day already finished flat" heartbeats


def retry_deadline(start: datetime, policy: RecoveryPolicy) -> datetime:
    """Berlin instant after which no further attempt is made: ``retry_until`` of the start day, but never less than
    ``min_budget_s`` after the start (a manual invocation after 23:30 still gets a bounded budget)."""
    base = start.replace(hour=policy.retry_until.hour, minute=policy.retry_until.minute, second=0, microsecond=0)
    return max(base, start + timedelta(seconds=policy.min_budget_s))


def build_recovery_cmd(python: str, repo_root: Path, artifacts: Path, account_phase: str) -> list[str]:
    return [python, str(repo_root / "scripts" / "demo_trader.py"), "--demo-auto", "--flatten-only",
            f"--confirm-demo-auto={sup.CONFIRMATION}", "--artifacts", str(artifacts), "--account-phase", account_phase]


def day_finished_flat(st: dict[str, Any] | None, now: datetime, policy: RecoveryPolicy) -> tuple[bool, str]:
    """Pure: does the LAST heartbeat prove that today's EOD sweep already finished flat?  Only a heartbeat of a runner that
    EXITED (``process_alive`` false) with stop_reason eod_flat_shutdown / eod_recovery_flat_confirmed, FLAT_CONFIRMED, no open
    position / intent / own position, written inside today's flatten window (never a pre-window heartbeat).  Anything else -> launch."""
    if not st:
        return False, "no heartbeat"
    updated = sup._parse_iso(str(st.get("updated_utc", "")))
    if updated is None:
        return False, "heartbeat unreadable"
    upd = updated.astimezone(now.tzinfo)
    if upd.date() != now.date() or upd.time() < policy.flatten_start:
        return False, "heartbeat not from today's flatten window"
    if st.get("process_alive") or st.get("stop_reason") not in DAY_DONE_STOP_REASONS:
        return False, "last runner did not end with an EOD stop reason"
    exposed, detail = sup.status_shows_exposure(st, now)
    if exposed or st.get("flatten_state") != "FLAT_CONFIRMED" or not st.get("eod_flat_confirmed_utc"):
        return False, f"flat not confirmed ({detail})"
    return True, f"day already finished flat (stop_reason={st.get('stop_reason')}, confirmed {st.get('eod_flat_confirmed_utc')})"


LaunchFn = Callable[[list[str]], int]


@dataclass
class EodRecovery:
    artifacts: Path
    runner_cmd: list[str]
    repo_root: Path
    log: sup.RunLog
    policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    now_fn: Callable[[], datetime] = sup.berlin_now
    sleep_fn: Callable[[float], None] = time.sleep
    launch_fn: LaunchFn | None = None  # tests inject a fake runner; default = a real subprocess of the EXISTING runner
    presence_fn: Callable[[Path], tuple[str, str]] = sup.runner_presence
    state: str = "INIT"  # state machine, for diagnostics / tests
    attempts: int = 0
    history: list[str] = field(default_factory=list)

    # -- helpers ---------------------------------------------------------------------------------------------------
    def _to(self, state: str) -> None:
        self.state = state
        self.history.append(state)

    def _alert(self, severity: str, reason: str, **detail: Any) -> None:
        self.log.alert(f"{severity}: {reason}")
        with contextlib.suppress(OSError):
            sup.write_alert(self.artifacts, severity, reason, log=str(self.log.path), source="eod_recovery", **detail)

    def _status(self) -> dict[str, Any] | None:
        try:
            return sup._heartbeat(self.artifacts, datetime.now(UTC)).get("status")
        except Exception:
            return None

    def _launch(self, cmd: list[str]) -> int:
        if self.launch_fn is not None:
            return self.launch_fn(cmd)
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.log.info("launching FLATTEN-ONLY runner: " + " ".join(cmd))
        proc = subprocess.Popen(cmd, cwd=self.repo_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", **kwargs)

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log.write("RUN", line)

        threading.Thread(target=pump, daemon=True).start()
        last_alert = time.monotonic()
        while proc.poll() is None:
            time.sleep(2.0)
            if time.monotonic() - last_alert >= self.policy.pending_alert_s:
                last_alert = time.monotonic()
                st = self._status() or {}
                self._alert("CRITICAL", "EOD recovery runner still working: broker flat NOT yet confirmed "
                            f"(flatten_state={st.get('flatten_state')}, own positions={st.get('eod_own_positions_open')}, "
                            f"detail={st.get('eod_detail')}); no guarantee is claimed", pid=proc.pid)
        code = proc.wait()
        time.sleep(0.2)
        return code

    # -- state machine ----------------------------------------------------------------------------------------------
    def run(self) -> int:
        """CHECK -> (HEALTHY | UNHEALTHY | DAY_DONE | LAUNCH) ; LAUNCH -> (FLAT | LOCK_LOST | BAD_ARGS | RETRY) ; RETRY -> CHECK | GAVE_UP."""
        start = self.now_fn()
        deadline = retry_deadline(start, self.policy)
        self.log.info(f"EOD recovery start now={start:%Y-%m-%d %H:%M:%S %Z} retry window until {deadline:%H:%M} Berlin; "
                      f"STOP file ignored (flattening only reduces exposure); {GUARANTEE}")
        while True:
            self._to("CHECK")
            presence, detail = self.presence_fn(self.artifacts)
            if presence == "healthy":
                self._to("HEALTHY_RUNNER")
                self.log.info(f"runner healthy ({detail}); its own sweep handles flat. Nothing to do. Exit 0")
                return EXIT_OK
            if presence == "unhealthy":
                self._to("UNHEALTHY_RUNNER")
                self._alert("CRITICAL", f"runner process present but heartbeat not fresh ({detail}): NOT launching a second runner, "
                            "NOT killing it; own exposure may be unmanaged - operator action required", presence=detail)
                return EXIT_UNHEALTHY_RUNNER
            done, why = day_finished_flat(self._status(), self.now_fn(), self.policy)
            if done:
                self._to("DAY_DONE")
                self.log.info(f"{why}; nothing to recover. Exit 0")
                return EXIT_OK
            self._to("LAUNCH")
            self.attempts += 1
            code = self._launch(self.runner_cmd)
            st = self._status() or {}
            stop_reason = st.get("stop_reason")
            foreign = st.get("eod_foreign_positions") or []
            if code == 0 and stop_reason == RECOVERY_STOP_REASON:
                self._to("FLAT_CONFIRMED")
                self.log.info(f"broker flat confirmed + reconciled (stop_reason={stop_reason}, attempt {self.attempts}). Exit 0")
                if foreign:
                    self._alert("WARNING", f"{len(foreign)} FOREIGN / manual position(s) on the account were NOT touched "
                                "(other magic): operator decision required", foreign_positions=foreign)
                return EXIT_OK
            if code == sup.RUN_ALREADY_RUNNING:
                self._to("LOCK_LOST")
                self.log.info("runner.lock was taken by another process meanwhile: re-evaluating its health")
                continue
            if code == sup.RUN_BAD_ARGS:
                self._to("BAD_ARGS")
                self._alert("CRITICAL", "flatten-only runner exit 2 (bad arguments / DEMO authorisation refused / MT5_ALLOW_ACCOUNT_LOGIN): "
                            "retry cannot help; own exposure may remain - operator action required")
                return EXIT_BAD_ARGS
            now = self.now_fn()
            self._to("RETRY")
            self._alert("CRITICAL", f"EOD recovery attempt {self.attempts} did NOT confirm flat (runner exit {code}, stop_reason="
                        f"{stop_reason!r}, flatten_state={st.get('flatten_state')}, own positions={st.get('eod_own_positions_open')}, "
                        f"mt5_connected={st.get('mt5_connected')}): broker unreachable or close not executed; the protective broker stop "
                        "is untouched, no new position can be opened, NO guarantee is claimed",
                        exit_code=code, attempt=self.attempts, foreign_positions=foreign or None)
            if now >= deadline:
                self._to("GAVE_UP")
                self._alert("CRITICAL", f"EOD recovery retry window ended at {deadline:%H:%M} Berlin WITHOUT a confirmed flat: "
                            "operator action required (positions are only protected by their broker-side stops)",
                            attempts=self.attempts)
                return EXIT_NOT_FLAT
            self.log.info(f"retry in {self.policy.retry_s:.0f}s (until {deadline:%H:%M} Berlin)")
            self.sleep_fn(self.policy.retry_s)


# ----------------------------------------------------------------------------- CLI
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Independent EOD recovery (flatten-only) for the DEMO trader")
    p.add_argument("--artifacts", type=Path, default=REPO_ROOT / "artifacts" / "demo_100k")
    p.add_argument("--account-phase", default="ALPHA_EXECUTION_DISCOVERY")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--runner-cmd-json", default=None, help="TEST ONLY: full runner command as a JSON list")
    p.add_argument("--retry-s", type=float, default=RecoveryPolicy.retry_s)
    p.add_argument("--retry-until", default="23:30", help="HH:MM Berlin: last retry (a late manual run still gets 30 min)")
    p.add_argument("--log-dir", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true", help="print the plan and the exact flatten-only command; launch nothing")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    artifacts: Path = a.artifacts
    policy = RecoveryPolicy(retry_s=a.retry_s, retry_until=sup.parse_hhmm(a.retry_until))
    stamp = datetime.now()
    log_file = (a.log_dir or artifacts / "logs") / f"eod_recovery_{stamp:%Y%m%d_%H%M%S}.log"
    log = sup.RunLog(log_file)
    try:
        cmd = list(json.loads(a.runner_cmd_json)) if a.runner_cmd_json else build_recovery_cmd(
            a.python, REPO_ROOT, artifacts, a.account_phase)
        if a.dry_run:
            log.info("DRY RUN: " + json.dumps({"runner_cmd": cmd, "retry_s": policy.retry_s,
                                                "retry_until": a.retry_until, "guarantee": GUARANTEE}))
            return EXIT_OK
        lock = instance_lock.InstanceLock(artifacts / "eod_recovery.lock", role="eod-recovery")
        try:
            lock.acquire()
        except instance_lock.LockHeld as exc:
            log.info(f"another EOD recovery is active ({exc}); refused. Exit {EXIT_LOCKED}")
            return EXIT_LOCKED
        try:
            return EodRecovery(artifacts, cmd, REPO_ROOT, log, policy).run()
        finally:
            lock.release()
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
