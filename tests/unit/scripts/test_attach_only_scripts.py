"""C3/C4 read-only MT5 scripts: refuse login opt-in, and never reach real MT5 from tests."""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCRIPTS = (
    "mt5_download_sample",
    "mt5_diag_c3_observe",
    "mt5_diag_c3_tzcheck",
    "mt5_diag_c4_account_facts",
    "mt5_diag_inspect_rates_ticks",
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_refuses_login_opt_in_before_any_mt5_access(name, monkeypatch, tmp_path, capsys):
    import adapters.activtrades_mt5.bounded as bounded
    import adapters.config as config_module

    monkeypatch.setattr(config_module, "_find_repo_root", lambda start: tmp_path)
    for var, value in (
        ("MT5_LOGIN", "1"),
        ("MT5_PASSWORD", "x"),
        ("MT5_SERVER", "s"),
        ("MT5_TERMINAL_PATH", "t"),
        ("MT5_ALLOW_ACCOUNT_LOGIN", "1"),
    ):
        monkeypatch.setenv(var, value)

    def boom(*_a, **_k):
        raise AssertionError("bounded worker (real MT5) must be unreachable")

    monkeypatch.setattr(bounded, "run_worker_bounded", boom)
    module = _load(name)
    monkeypatch.setattr(module, "run_worker_bounded", boom, raising=False)
    monkeypatch.setattr(sys, "argv", [name])
    assert module.main() == 2
    assert "attach-only" in capsys.readouterr().err
