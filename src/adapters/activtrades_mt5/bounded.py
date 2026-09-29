"""Run an MT5 script's real work in a short-lived, hard-timeout-bounded
child process.

CRITICAL SAFETY PROPERTY (per repository policy): the main/parent process
must NEVER hang indefinitely on an MT5 IPC call. `MetaTrader5.initialize()`
and related calls are blocking C-extension calls that Python cannot
interrupt from another thread within the SAME process once they are stuck --
a `threading.Timer` cannot forcibly abort a blocked C call. A separate OS
process CAN be killed unconditionally (`Popen.kill()` -> `TerminateProcess`
on Windows), which is why every real MT5 call in this codebase's scripts
happens inside a child process that this module's `run_worker_bounded`
spawns and bounds with `subprocess.run(..., timeout=...)`.

Credentials are passed to the child via its environment (`env=`), never via
command-line arguments (which would be visible to any process listing on
the machine, e.g. Task Manager's command-line column or `wmic process`).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from adapters.config import MT5ConnectionConfig


def _default_timeout(env_var: str, fallback: float) -> float:
    """Read a default timeout from the environment at CALL time (not import
    time), so a test suite's `monkeypatch.setenv(...)` -- applied after this
    module is first imported -- reliably takes effect. Test suites set these
    very low (e.g. "2") to keep a genuine-timeout test case fast without
    weakening the production default."""
    import os

    raw = os.environ.get(env_var)
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def default_probe_timeout_seconds() -> float:
    return _default_timeout("MT5_PROBE_TIMEOUT_SECONDS", 20.0)


def default_staged_timeout_seconds() -> float:
    return _default_timeout("MT5_STAGED_TIMEOUT_SECONDS", 45.0)


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundedRunResult:
    """Outcome of a bounded child-process run. Never raises past the
    boundary this module exists to enforce."""

    timed_out: bool
    returncode: int | None
    stdout: str
    stderr: str


def _child_env(config: MT5ConnectionConfig) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["MT5_LOGIN"] = str(config.login)
    env["MT5_PASSWORD"] = config.password
    env["MT5_SERVER"] = config.server
    if config.terminal_path:
        env["MT5_TERMINAL_PATH"] = config.terminal_path
    else:
        env.pop("MT5_TERMINAL_PATH", None)
    # Forward the explicit real-authentication gate unchanged -- the child
    # re-loads config from its own environment, so without this the parent's
    # `config.allow_account_login` would silently NOT reach the worker that
    # actually calls `initialize()`.
    if config.allow_account_login:
        env["MT5_ALLOW_ACCOUNT_LOGIN"] = "1"
    else:
        env.pop("MT5_ALLOW_ACCOUNT_LOGIN", None)
    return env


def run_worker_bounded(
    script_path: Path,
    config: MT5ConnectionConfig,
    *,
    timeout: float,
    extra_args: tuple[str, ...] = (),
) -> BoundedRunResult:
    """Run `script_path --worker <extra_args>` as a child process, killing
    it if it exceeds `timeout` seconds.

    A small, bounded retry policy (never a retry storm, never infinite):
    exactly one attempt, no automatic retry. A caller wanting a second
    attempt must call this again explicitly and account for that in its own
    reported diagnostics -- this function never silently retries.
    """
    # `-u`: unbuffered stdout/stderr for the whole child process. Without
    # this, a captured pipe (never a TTY) is block-buffered by default, so
    # output the child printed just before being killed on timeout could sit
    # in an unflushed buffer and never reach the parent's captured
    # `exc.stdout` -- silently losing partial progress on exactly the
    # timeout path this module exists to handle cleanly.
    cmd = [sys.executable, "-u", str(script_path), "--worker", *extra_args]
    try:
        proc = subprocess.run(
            cmd,
            env=_child_env(config),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run's own TimeoutExpired handling already kills the
        # child (Popen.kill()) and waits for it before re-raising -- no
        # orphaned process is left behind by this path.
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(
            "utf-8", errors="replace"
        )
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(
            "utf-8", errors="replace"
        )
        return BoundedRunResult(timed_out=True, returncode=None, stdout=stdout, stderr=stderr)

    return BoundedRunResult(
        timed_out=False, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )
