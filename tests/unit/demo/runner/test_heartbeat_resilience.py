import pytest

from demo import monitor
from demo.execution import status


def test_heartbeat_write_retries_a_transient_windows_sharing_violation(tmp_path, monkeypatch):
    real = status.write_status_atomic
    calls = {"n": 0}

    def flaky(path, values):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(5, "Zugriff verweigert")
        real(path, values)

    monkeypatch.setattr(status, "write_status_atomic", flaky)
    target = tmp_path / "heartbeat.json"
    monitor.write_heartbeat(target, {"updated_utc": "2026-09-30T00:00:00+00:00"})
    assert calls["n"] == 4
    assert monitor.read_heartbeat(target) == {"updated_utc": "2026-09-30T00:00:00+00:00"}


def test_heartbeat_write_gives_up_after_bounded_retries(tmp_path, monkeypatch):
    def broken(path, values):
        raise PermissionError(5, "Zugriff verweigert")

    monkeypatch.setattr(status, "write_status_atomic", broken)
    with pytest.raises(PermissionError):
        monitor.write_heartbeat(tmp_path / "heartbeat.json", {})
