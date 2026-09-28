"""Tests that each MT5 script skeleton can be invoked and reports a clear
message instead of crashing, whether real MT5 env vars are absent (config
missing) or present but the adapter doesn't exist yet (not yet implemented).

No real MT5 credentials are used anywhere here -- `monkeypatch` clears any
real env vars for the "missing config" case and sets synthetic test values
for the "adapter pending" case.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

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
def _clear_mt5_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _MT5_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


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
def test_script_reports_adapter_pending_when_config_present(
    script_name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MT5_LOGIN", "12345")
    monkeypatch.setenv("MT5_PASSWORD", "test-password-not-real")
    monkeypatch.setenv("MT5_SERVER", "ActivTrades-Demo")

    module = _load_script(script_name)

    exit_code = module.main()

    captured = capsys.readouterr()
    assert exit_code in (0, 1)
    assert "not yet implemented" in (captured.out + captured.err).lower()
    # The password must never be printed anywhere in the script's output.
    assert "test-password-not-real" not in captured.out
    assert "test-password-not-real" not in captured.err
