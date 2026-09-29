"""MT5 contracts 21-22, asserted through the public connection API only.

Never touches a real terminal: every client is `FakeMT5Client`, every lock lives
under pytest's `tmp_path` (never the process-wide default lock path).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.activtrades_mt5.connection import ConnectionState, MT5Connection
from adapters.activtrades_mt5.diagnostics import ConnectionDiagnosticCategory
from adapters.activtrades_mt5.lock import acquire_mt5_lock
from adapters.activtrades_mt5.testing import FakeMT5Client
from adapters.config import MT5ConnectionConfig, load_mt5_connection_config
from tests.unit.adapters.activtrades_mt5.conftest import make_account_info

_LOGIN = 12345678
_CONFIG = MT5ConnectionConfig(login=_LOGIN, password="pw", server="srv", terminal_path=None)
_BASE_ENV = {"MT5_LOGIN": str(_LOGIN), "MT5_PASSWORD": "pw", "MT5_SERVER": "srv"}


def _client(*, attached_login: int | None = _LOGIN) -> FakeMT5Client:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(
        make_account_info(login=attached_login) if attached_login is not None else None
    )
    return client


def _connection(client: FakeMT5Client, tmp_path: Path) -> MT5Connection:
    return MT5Connection(client, lock_path=tmp_path / "mt5.lock")


# -- 21. broker account mismatch fails closed ---------------------------------------


def test_c21_attached_account_mismatch_fails_closed(tmp_path: Path) -> None:
    client = _client(attached_login=99999999)
    connection = _connection(client, tmp_path)

    result = connection.connect(_CONFIG)

    assert result.success is False
    assert result.category is ConnectionDiagnosticCategory.ACCOUNT_MISMATCH
    assert "MT5_ACCOUNT_MISMATCH" in result.reason
    assert connection.state is ConnectionState.ERROR


def test_c21_mismatch_never_triggers_a_corrective_login(tmp_path: Path) -> None:
    client = _client(attached_login=99999999)
    _connection(client, tmp_path).connect(_CONFIG)

    # Exactly one read-only attach; no second initialize()/login() to "fix" it.
    assert len(client.initialize_calls) == 1
    assert client.initialize_calls[0]["login"] is None
    assert client.initialize_calls[0]["server"] is None


def test_c21_unreadable_account_identity_fails_closed(tmp_path: Path) -> None:
    connection = _connection(_client(attached_login=None), tmp_path)

    result = connection.connect(_CONFIG)

    assert result.success is False
    assert result.category is ConnectionDiagnosticCategory.ACCOUNT_INFO_FAILED
    assert connection.state is ConnectionState.ERROR


def test_c21_matching_account_connects(tmp_path: Path) -> None:
    connection = _connection(_client(), tmp_path)

    result = connection.connect(_CONFIG)

    assert result.success is True
    assert connection.state is ConnectionState.CONNECTED
    connection.disconnect()


def test_c21_failed_connect_releases_the_lock(tmp_path: Path) -> None:
    """A fail-closed mismatch must not wedge later, correct connections."""
    assert _connection(_client(attached_login=99999999), tmp_path).connect(_CONFIG).success is False

    later = _connection(_client(), tmp_path)
    assert later.connect(_CONFIG).success is True
    later.disconnect()


# -- 22. attach-only default + single-owner lock ------------------------------------


def test_c22_default_connect_sends_no_credentials(tmp_path: Path) -> None:
    client = _client()
    connection = _connection(client, tmp_path)

    result = connection.connect(_CONFIG)

    assert result.success is True
    assert len(client.initialize_calls) == 1
    call = client.initialize_calls[0]
    assert call["login"] is None
    assert call["server"] is None
    assert call["path"] == _CONFIG.terminal_path
    connection.disconnect()


def test_c22_config_defaults_to_attach_only_when_env_is_unset(tmp_path: Path) -> None:
    config = load_mt5_connection_config(env=dict(_BASE_ENV), env_file=tmp_path / "absent.env")
    assert config.allow_account_login is False


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "2", " 1", "1 "])
def test_c22_only_the_exact_string_one_enables_login(tmp_path: Path, value: str) -> None:
    env = {**_BASE_ENV, "MT5_ALLOW_ACCOUNT_LOGIN": value}
    config = load_mt5_connection_config(env=env, env_file=tmp_path / "absent.env")
    assert config.allow_account_login is False


def test_c22_process_env_one_enables_login(tmp_path: Path) -> None:
    env = {**_BASE_ENV, "MT5_ALLOW_ACCOUNT_LOGIN": "1"}
    config = load_mt5_connection_config(env=env, env_file=tmp_path / "absent.env")
    assert config.allow_account_login is True


def test_c22_the_login_flag_is_never_read_from_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MT5_LOGIN=1\nMT5_PASSWORD=p\nMT5_SERVER=s\nMT5_ALLOW_ACCOUNT_LOGIN=1\n",
        encoding="utf-8",
    )
    config = load_mt5_connection_config(env={}, env_file=env_file)
    assert config.allow_account_login is False


def test_c22_explicit_login_is_the_only_path_that_sends_credentials(tmp_path: Path) -> None:
    client = _client()
    config = MT5ConnectionConfig(
        login=_LOGIN, password="pw", server="srv", terminal_path=None, allow_account_login=True
    )
    connection = _connection(client, tmp_path)

    assert connection.connect(config).success is True
    call = client.initialize_calls[0]
    assert call["login"] == _LOGIN
    assert call["server"] == "srv"
    connection.disconnect()


def test_c22_credentials_never_appear_in_the_config_repr() -> None:
    assert "pw" not in repr(_CONFIG)
    assert "<redacted>" in repr(_CONFIG)


def test_c22_second_concurrent_owner_gets_connection_busy_and_never_touches_the_client(
    tmp_path: Path,
) -> None:
    held = acquire_mt5_lock(tmp_path / "mt5.lock")
    assert held is not None
    try:
        client = _client()
        result = _connection(client, tmp_path).connect(_CONFIG)

        assert result.success is False
        assert result.category is ConnectionDiagnosticCategory.CONNECTION_BUSY
        assert client.initialize_calls == []
    finally:
        held.release()


def test_c22_lock_is_single_owner_and_released_on_disconnect(tmp_path: Path) -> None:
    first = _connection(_client(), tmp_path)
    assert first.connect(_CONFIG).success is True

    busy = _connection(_client(), tmp_path).connect(_CONFIG)
    assert busy.category is ConnectionDiagnosticCategory.CONNECTION_BUSY

    first.disconnect()
    second = _connection(_client(), tmp_path)
    assert second.connect(_CONFIG).success is True
    second.disconnect()
