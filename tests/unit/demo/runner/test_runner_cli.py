# ruff: noqa: E501
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from demo import runner as rn
from scripts.demo_trader import CONFIRMATION, main


def test_wrong_confirm_string_exit_2(capsys):
    assert main(["--demo-auto", "--confirm-demo-auto=nope"]) == 2
    assert main(["--demo-auto"]) == 2
    assert CONFIRMATION in capsys.readouterr().err


def test_login_env_refused(monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_ACCOUNT_LOGIN", "1")
    assert main(["--demo-auto", f"--confirm-demo-auto={CONFIRMATION}"]) == 2
    assert main(["--shadow"]) == 2


def test_missing_live_stack_exits_8(monkeypatch):
    monkeypatch.delenv("MT5_ALLOW_ACCOUNT_LOGIN", raising=False)

    def unavailable(*a, **k):
        raise rn.LiveStackUnavailable("no live stack")

    monkeypatch.setattr(rn, "build_live_runner", unavailable)
    assert main(["--demo-auto", f"--confirm-demo-auto={CONFIRMATION}"]) == 8
    assert main(["--shadow"]) == 8


def test_status_verdict_stale_and_fresh(tmp_path, capsys):
    hb = tmp_path / "hb.json"
    fresh = datetime.now(UTC)
    hb.write_text(json.dumps({"process_alive": True, "updated_utc": fresh.isoformat()}))
    assert main(["--status", "--heartbeat", str(hb)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "RUNNING"
    hb.write_text(json.dumps({"process_alive": True, "updated_utc": (fresh - timedelta(seconds=120)).isoformat()}))
    assert main(["--status", "--heartbeat", str(hb)]) == 3
    assert json.loads(capsys.readouterr().out)["verdict"] == "NOT RUNNING"
    assert main(["--status", "--heartbeat", str(tmp_path / "missing.json")]) == 3


def test_analyze_cli(tmp_path, capsys):
    from demo.store import DemoStore

    DemoStore(tmp_path / "demo.sqlite").close()
    assert main(["--analyze", "--artifacts", str(tmp_path), "--phase", "FROZEN"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["phase"] == "FROZEN" and out["n_trades"] == 0
    assert main(["--analyze", "--artifacts", str(tmp_path / "empty")]) == 3
