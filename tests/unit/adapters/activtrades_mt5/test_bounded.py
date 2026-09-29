"""Tests for `adapters.activtrades_mt5.bounded` -- the hard-timeout-bounded
subprocess wrapper every real MT5 call in this codebase's scripts runs
inside, so the main process can never hang on a stuck MT5 IPC call.

Uses small, disposable throwaway worker scripts (written to `tmp_path`)
instead of real MT5 scripts, so these tests are fast and fully deterministic
regardless of whether a real MT5 terminal is present.
"""

from __future__ import annotations

import time
from pathlib import Path

from adapters.activtrades_mt5.bounded import run_worker_bounded
from adapters.config import MT5ConnectionConfig

_CONFIG = MT5ConnectionConfig(login=1, password="pw", server="srv", terminal_path=None)


def _write_worker(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "worker.py"
    script.write_text(
        "import sys\n"
        "if '--worker' in sys.argv:\n"
        f"{body}\n"
        "else:\n"
        "    print('not a worker invocation')\n",
        encoding="utf-8",
    )
    return script


def test_child_returns_normally(tmp_path: Path) -> None:
    script = _write_worker(
        tmp_path,
        "    print('hello from child')\n"
        "    sys.exit(0)\n",
    )

    result = run_worker_bounded(script, _CONFIG, timeout=10)

    assert result.timed_out is False
    assert result.returncode == 0
    assert "hello from child" in result.stdout


def test_child_nonzero_exit_is_reported(tmp_path: Path) -> None:
    script = _write_worker(tmp_path, "    sys.exit(3)\n")

    result = run_worker_bounded(script, _CONFIG, timeout=10)

    assert result.timed_out is False
    assert result.returncode == 3


def test_child_that_hangs_is_killed_after_timeout(tmp_path: Path) -> None:
    script = _write_worker(
        tmp_path,
        "    print('before hang', flush=True)\n"
        "    import time\n"
        "    time.sleep(60)\n"  # far longer than the timeout below
        "    print('should never reach here')\n",
    )

    started = time.monotonic()
    result = run_worker_bounded(script, _CONFIG, timeout=1.5)
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.returncode is None
    # The bound must actually be enforced -- this must not silently wait out
    # the full 60s sleep.
    assert elapsed < 30
    # Whatever the child printed and flushed before being killed must still
    # be captured -- partial progress is never silently lost.
    assert "before hang" in result.stdout


def test_credentials_passed_via_env_not_argv(tmp_path: Path) -> None:
    """Command-line arguments are visible to any process listing on the
    machine (e.g. Task Manager); the password must only ever travel via the
    child's environment."""
    script = _write_worker(
        tmp_path,
        "    import os\n"
        "    assert os.environ.get('MT5_PASSWORD') == 'pw'\n"
        "    assert os.environ.get('MT5_LOGIN') == '1'\n"
        "    assert os.environ.get('MT5_SERVER') == 'srv'\n"
        "    print('env-config-ok')\n",
    )

    result = run_worker_bounded(script, _CONFIG, timeout=10)

    assert result.timed_out is False
    assert result.returncode == 0
    assert "env-config-ok" in result.stdout
    # And 'pw' must never appear in how the child was invoked (argv) --
    # this test only proves it arrived via env, not that it's absent from
    # argv, but combined with `bounded.py`'s own construction of `cmd`
    # (never including config fields) this closes the loop.


def test_no_terminal_path_omits_env_var(tmp_path: Path) -> None:
    script = _write_worker(
        tmp_path,
        "    import os\n"
        "    assert 'MT5_TERMINAL_PATH' not in os.environ or not os.environ['MT5_TERMINAL_PATH']\n"
        "    print('no-terminal-path-ok')\n",
    )
    config = MT5ConnectionConfig(login=1, password="pw", server="srv", terminal_path=None)

    result = run_worker_bounded(script, config, timeout=10)

    assert result.timed_out is False, result.stderr
    assert "no-terminal-path-ok" in result.stdout


def test_extra_args_forwarded_to_child(tmp_path: Path) -> None:
    script = _write_worker(
        tmp_path,
        "    print('argv=' + ','.join(sys.argv[1:]))\n",
    )

    result = run_worker_bounded(script, _CONFIG, timeout=10, extra_args=("--stage=2",))

    assert result.timed_out is False
    assert "--stage=2" in result.stdout


def test_shutdown_always_attempted_even_on_probe_failure() -> None:
    """`run_isolated_probe` (the actual MT5 probe logic, not the bounded
    subprocess wrapper) must always call `client.shutdown()` in a `finally`
    block once `initialize()` has succeeded, regardless of what fails
    afterward -- proven directly against a fake client here since this is a
    property of `probe.py`, not `bounded.py`."""
    from adapters.activtrades_mt5.probe import run_isolated_probe
    from adapters.activtrades_mt5.testing import FakeMT5Client

    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(None)  # forces a failure AFTER a successful initialize()

    report = run_isolated_probe(_CONFIG, client)

    assert report.success is False
    assert client.shutdown_calls == 1
