import json

from demo.execution.status import write_status_atomic


def test_status_write_is_complete_json_and_marks_demo(tmp_path):
    path = tmp_path / "heartbeat.json"
    write_status_atomic(path, {"equity": 1000, "mt5_connected": True})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["mode"] == "DEMO MODE"
    assert payload["equity"] == 1000
    assert payload["process_alive"] is True
    assert not list(tmp_path.glob("*.tmp"))
