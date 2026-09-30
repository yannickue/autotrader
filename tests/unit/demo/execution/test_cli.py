# ruff: noqa: E501
import json
from datetime import UTC, datetime

from scripts.demo_trader import CONFIRMATION, main


def test_status_reads_heartbeat_without_touching_mt5(tmp_path, capsys):
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps({"mode": "DEMO MODE", "process_alive": True, "updated_utc": datetime.now(UTC).isoformat()}))
    assert main(["--status", "--heartbeat", str(heartbeat)]) == 0
    assert json.loads(capsys.readouterr().out)["process_alive"] is True


def test_demo_auto_requires_exact_confirmation(capsys):
    assert main(["--demo-auto"]) == 2
    assert CONFIRMATION in capsys.readouterr().err
