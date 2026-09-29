"""Tests that each MT5 script can be invoked and reports a clear message
instead of crashing, whether real MT5 env vars are absent (config missing)
or present.

ACCOUNT PROTECTION INVARIANT (see `adapters/activtrades_mt5/connection.py`,
`lock.py`, `docs/ARCHITECTURE.md`): ordinary `pytest` must NEVER perform any
real MT5 IPC call -- not a login, not even a read-only attach -- because a
manually logged-in ActivTrades terminal must never be touched just by
running the test suite. Every test below that exercises the "config
present" path therefore replaces `adapters.activtrades_mt5.bounded.
run_worker_bounded` (the ONE function that ever spawns a real-MT5-capable
child process) with a fake before calling `module.main()`, so real MT5 is
categorically unreachable here regardless of what `main()` does internally
-- this is a stronger guarantee than "uses fake credentials", which
previously still let a real subprocess attempt a real IPC call.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from adapters.activtrades_mt5.bounded import BoundedRunResult

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SRC_ROOT = REPO_ROOT / "src"

for path in (str(REPO_ROOT), str(SRC_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

_MT5_ENV_VARS = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_TERMINAL_PATH")

_SCRIPT_NAMES = (
    "mt5_connection_check",
    "mt5_account_snapshot",
    "mt5_symbol_discovery",
    "mt5_preflight",
)


def _load_script(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clear_mt5_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in _MT5_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # This checkout may have a real, git-ignored `.env` with real credentials
    # (see PENDING_USER_INPUT.md) -- without this, these tests would silently
    # pick it up via `load_mt5_connection_config()`'s real-repo-root
    # auto-detection and attempt a real connection instead of exercising the
    # "missing config" / "synthetic fake config" paths they're testing.
    import adapters.config as config_module

    monkeypatch.setattr(config_module, "_find_repo_root", lambda start: tmp_path)
    # Every mt5_*.py script now runs its real MT5 work inside a bounded
    # child process (adapters.activtrades_mt5.bounded) -- these tests
    # genuinely exercise that subprocess + timeout path (there is no live
    # MT5 terminal in this test environment), so keep the bound very short
    # to keep the test suite fast without weakening the production default.
    monkeypatch.setenv("MT5_PROBE_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("MT5_STAGED_TIMEOUT_SECONDS", "3")


@pytest.mark.parametrize("script_name", _SCRIPT_NAMES)
def test_script_reports_missing_config_without_crashing(
    script_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script(script_name)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "MT5_LOGIN" in captured.err or "MT5_LOGIN" in captured.out


@pytest.mark.parametrize("script_name", _SCRIPT_NAMES)
def test_script_never_touches_real_mt5_during_normal_pytest(
    script_name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With config present, every script must reach exactly one call to
    `run_worker_bounded` (the sole function that can spawn a real-MT5-
    capable child) with the genuinely-loaded config, and terminate cleanly
    on the canned result -- never crash, never leak the password, and (the
    account protection invariant) never call real MT5 in any way, since
    `run_worker_bounded` itself is replaced with a fake below.

    Also asserts `config.allow_account_login is False`: with
    `MT5_ALLOW_ACCOUNT_LOGIN` unset (the normal case), the config this
    script would hand to a real connection attempt must carry the safe,
    attach-only default -- proving the explicit-auth gate defaults OFF.
    """
    monkeypatch.setenv("MT5_LOGIN", "12345")
    monkeypatch.setenv("MT5_PASSWORD", "test-password-not-real")
    monkeypatch.setenv("MT5_SERVER", "ActivTrades-Demo")
    monkeypatch.delenv("MT5_ALLOW_ACCOUNT_LOGIN", raising=False)

    module = _load_script(script_name)

    calls: list[dict[str, object]] = []

    def _fake_run_worker_bounded(script_path, config, *, timeout, extra_args=()):
        del script_path, timeout, extra_args
        calls.append({"login": config.login, "allow_account_login": config.allow_account_login})
        return BoundedRunResult(
            timed_out=False,
            returncode=0,
            stdout="Config loaded: server='ActivTrades-Demo', login=12345.\n",
            stderr="",
        )

    monkeypatch.setattr(module, "run_worker_bounded", _fake_run_worker_bounded)

    exit_code = module.main()

    assert len(calls) == 1, (
        "main() must reach exactly one run_worker_bounded call and never call real MT5 directly"
    )
    assert calls[0]["login"] == 12345
    assert calls[0]["allow_account_login"] is False
    assert exit_code == 0

    captured = capsys.readouterr()
    combined = (captured.out + captured.err).lower()
    assert "config loaded" in combined
    # The password must never be printed anywhere in the script's output.
    assert "test-password-not-real" not in captured.out
    assert "test-password-not-real" not in captured.err


@pytest.mark.parametrize("script_name", _SCRIPT_NAMES)
def test_script_allow_account_login_env_var_reaches_config(
    script_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MT5_ALLOW_ACCOUNT_LOGIN=1` must be an explicit, visible opt-in that
    reaches the config handed to `run_worker_bounded` -- proving the gate is
    wired, without ever actually calling real MT5 (still faked)."""
    monkeypatch.setenv("MT5_LOGIN", "12345")
    monkeypatch.setenv("MT5_PASSWORD", "test-password-not-real")
    monkeypatch.setenv("MT5_SERVER", "ActivTrades-Demo")
    monkeypatch.setenv("MT5_ALLOW_ACCOUNT_LOGIN", "1")

    module = _load_script(script_name)

    calls: list[dict[str, object]] = []

    def _fake_run_worker_bounded(script_path, config, *, timeout, extra_args=()):
        del script_path, timeout, extra_args
        calls.append({"allow_account_login": config.allow_account_login})
        return BoundedRunResult(
            timed_out=False, returncode=0, stdout="Config loaded: ok\n", stderr=""
        )

    monkeypatch.setattr(module, "run_worker_bounded", _fake_run_worker_bounded)

    module.main()

    assert len(calls) == 1
    assert calls[0]["allow_account_login"] is True
