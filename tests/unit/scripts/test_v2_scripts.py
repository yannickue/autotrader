"""V2 market scripts: guards and argument parsing only (never reaches a real MT5 terminal)."""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_tfspec_and_estimate():
    m = _load("v2_download_market")
    tfs = m.parse_tfspec("M5,M1@2026-01,H4,D1", "2025-02")
    assert tfs == {"M5": "2025-02", "M1": "2026-01", "H4": "2025-02", "D1": "2025-02"}
    with pytest.raises(SystemExit):
        m.parse_tfspec("M7", "2025-02")
    est = m.estimate_bytes({"M5": "2025-02", "M1": "2026-01"}, "2026-09", 5)
    assert 0 < est < m.MAX_TOTAL_BYTES


def test_guard_refuses_low_disk_and_oversized_estimate(tmp_path, monkeypatch):
    m = _load("v2_download_market")
    small = type("U", (), {"free": 1 * 1024**3, "total": 0, "used": 0})
    monkeypatch.setattr(m.shutil, "disk_usage", lambda _p: small)
    with pytest.raises(SystemExit, match="free disk"):
        m.guard(tmp_path, {"M5": "2025-02"}, "2026-09", 1)
    big = type("U", (), {"free": 50 * 1024**3, "total": 0, "used": 0})
    monkeypatch.setattr(m.shutil, "disk_usage", lambda _p: big)
    with pytest.raises(SystemExit, match="150 MB"):
        m.guard(tmp_path, {"M1": "2015-01"}, "2026-09", 5)
    m.guard(tmp_path, {"M5": "2025-02"}, "2026-09", 1)  # passes


def test_unresolved_market_is_never_guessed(tmp_path, monkeypatch):
    m = _load("v2_download_market")
    snap = tmp_path / "snap.json"
    snap.write_text('{"markets": {"NAS100": {"resolved_broker_symbol": null}}}', encoding="utf-8")
    monkeypatch.setattr(m, "SNAPSHOT", snap)
    with pytest.raises(SystemExit, match="not guessing"):
        m._snapshot_symbol("NAS100")


@pytest.mark.parametrize("name", ["v2_discover_symbols", "v2_download_market"])
def test_scripts_refuse_login_opt_in_before_any_mt5_access(name, monkeypatch, tmp_path, capsys):
    import adapters.activtrades_mt5.bounded as bounded

    for var, value in (
        ("MT5_LOGIN", "1"),
        ("MT5_PASSWORD", "x"),
        ("MT5_SERVER", "s"),
        ("MT5_ALLOW_ACCOUNT_LOGIN", "1"),
    ):
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("MT5_ENV_FILE", str(tmp_path / "missing.env"))

    def boom(*_a, **_k):
        raise AssertionError("bounded worker (real MT5) must be unreachable")

    monkeypatch.setattr(bounded, "run_worker_bounded", boom)
    module = _load(name)
    monkeypatch.setattr(module, "run_worker_bounded", boom, raising=False)
    argv = [name, "GER40", "2026-01", "2026-02", "M5", str(tmp_path / "out")]
    monkeypatch.setattr(sys, "argv", argv if name == "v2_download_market" else [name])
    assert module.main() == 2
    assert "attach-only" in capsys.readouterr().err
