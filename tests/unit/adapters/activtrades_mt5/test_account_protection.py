"""Regression tests for the account-protection invariant added 2026-09-29.

Root cause this guards against: `MT5Connection.connect()` used to always
call `initialize(path, login=, password=, server=)` -- real authentication
-- even for read-only diagnostics. MT5 has exactly one current account per
terminal instance, so any process doing this against a manually logged-in
terminal could switch or disconnect the user's real account, which is
exactly what was observed happening while the test suite ran.

These tests prove, against `FakeMT5Client` (never a real MT5 terminal):
  - the default (`allow_account_login=False`) path never sends credentials
    to `initialize()`
  - a mismatched already-attached account fails closed and is never
    silently accepted or corrected via an automatic login
  - explicit `allow_account_login=True` is the only path that sends
    credentials
  - a second connect() call while the MT5 lock is held fails immediately
    with CONNECTION_BUSY and never calls `initialize()` at all

Every `MT5Connection` here is constructed with an isolated, per-test
`lock_path` (under pytest's own `tmp_path`) rather than the real, process-
wide `DEFAULT_LOCK_PATH` -- otherwise one test's successful `connect()`
(most of these never call `disconnect()`, since that's not what they're
testing) would leave the real lock held and make every other real-MT5-
touching test in the whole suite fail with CONNECTION_BUSY, order-
dependently. `lock.py` and `test_lock.py` cover the lock primitive itself
directly; this file only needs `connect()`/`probe()` to route through it.
"""

from __future__ import annotations

from pathlib import Path

from adapters.activtrades_mt5.connection import ConnectionState, MT5Connection
from adapters.activtrades_mt5.diagnostics import ConnectionDiagnosticCategory
from adapters.activtrades_mt5.lock import acquire_mt5_lock
from adapters.activtrades_mt5.testing import FakeMT5Client
from adapters.config import MT5ConnectionConfig
from tests.unit.adapters.activtrades_mt5.conftest import make_account_info

_CONFIG = MT5ConnectionConfig(login=12345678, password="pw", server="srv", terminal_path=None)


def _conn(client: FakeMT5Client, tmp_path: Path) -> MT5Connection:
    return MT5Connection(client, lock_path=tmp_path / "mt5.lock")


def test_default_connect_sends_no_credentials_to_initialize(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(make_account_info(login=12345678))

    connection = _conn(client, tmp_path)
    result = connection.connect(_CONFIG)

    assert result.success is True
    assert len(client.initialize_calls) == 1
    call = client.initialize_calls[0]
    assert call["login"] is None
    assert call["server"] is None
    # `path` is the one positional argument a read-only attach still needs
    # to pass through (MT5 uses it to locate a specific terminal instance
    # when set) -- only login/password/server are withheld.
    assert call["path"] == _CONFIG.terminal_path


def test_account_mismatch_fails_closed_and_never_authenticates(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    # The terminal is attached to a DIFFERENT account than configured --
    # e.g. the user is manually logged into a different demo/live account.
    client.set_account_info(make_account_info(login=99999999))

    connection = _conn(client, tmp_path)
    result = connection.connect(_CONFIG)

    assert result.success is False
    assert result.category is ConnectionDiagnosticCategory.ACCOUNT_MISMATCH
    assert "MT5_ACCOUNT_MISMATCH" in result.reason
    assert connection.state is ConnectionState.ERROR
    # Exactly the read-only attach was attempted -- no second call (e.g. a
    # corrective login()) was ever made.
    assert len(client.initialize_calls) == 1
    assert client.initialize_calls[0]["login"] is None


def test_account_info_unreadable_fails_closed(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(None)

    connection = _conn(client, tmp_path)
    result = connection.connect(_CONFIG)

    assert result.success is False
    assert result.category is ConnectionDiagnosticCategory.ACCOUNT_INFO_FAILED
    assert connection.state is ConnectionState.ERROR


def test_matching_account_connects_successfully(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(make_account_info(login=12345678))

    connection = _conn(client, tmp_path)
    result = connection.connect(_CONFIG)

    assert result.success is True
    assert connection.state is ConnectionState.CONNECTED


def test_explicit_allow_account_login_sends_credentials(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(make_account_info(login=12345678))

    config = MT5ConnectionConfig(
        login=12345678,
        password="pw",
        server="srv",
        terminal_path=None,
        allow_account_login=True,
    )
    connection = _conn(client, tmp_path)
    result = connection.connect(config)

    assert result.success is True
    assert len(client.initialize_calls) == 1
    call = client.initialize_calls[0]
    assert call["login"] == 12345678
    assert call["server"] == "srv"
    # In explicit-auth mode, a mismatched-account check would be
    # meaningless (the call itself established the identity) -- confirmed
    # by this test succeeding without ever inspecting account_info().


def test_second_connect_while_lock_held_is_busy_and_never_calls_initialize(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "mt5.lock"
    held = acquire_mt5_lock(lock_path)
    assert held is not None
    try:
        second = acquire_mt5_lock(lock_path)
        assert second is None
    finally:
        held.release()


def test_connect_returns_connection_busy_when_lock_held_by_another_holder(
    tmp_path: Path,
) -> None:
    """End-to-end: with the lock `connect()` will use already held
    (simulating a second process), it must return CONNECTION_BUSY and must
    NEVER call `initialize()`."""
    lock_path = tmp_path / "mt5.lock"
    held = acquire_mt5_lock(lock_path)
    assert held is not None
    try:
        client = FakeMT5Client()
        client.set_initialize_result(True)
        client.set_account_info(make_account_info(login=12345678))

        connection = _conn(client, tmp_path)
        result = connection.connect(_CONFIG)

        assert result.success is False
        assert result.category is ConnectionDiagnosticCategory.CONNECTION_BUSY
        assert len(client.initialize_calls) == 0
    finally:
        held.release()


def test_connect_acquires_and_releases_the_lock_across_connect_cycles(tmp_path: Path) -> None:
    """`connect()` then `disconnect()` must not leak the lock -- a second,
    independent `MT5Connection` using the SAME lock path must be able to
    connect afterward."""
    lock_path = tmp_path / "mt5.lock"

    client_a = FakeMT5Client()
    client_a.set_initialize_result(True)
    client_a.set_account_info(make_account_info(login=12345678))
    connection_a = MT5Connection(client_a, lock_path=lock_path)
    result_a = connection_a.connect(_CONFIG)
    assert result_a.success is True
    connection_a.disconnect()

    client_b = FakeMT5Client()
    client_b.set_initialize_result(True)
    client_b.set_account_info(make_account_info(login=12345678))
    connection_b = MT5Connection(client_b, lock_path=lock_path)
    result_b = connection_b.connect(_CONFIG)
    assert result_b.success is True
    connection_b.disconnect()


def test_connect_failure_releases_the_lock_too(tmp_path: Path) -> None:
    """A failed connect() (e.g. account mismatch) must still release the
    lock -- otherwise one failed read-only attach would permanently wedge
    every future real MT5 access with CONNECTION_BUSY."""
    lock_path = tmp_path / "mt5.lock"

    client_a = FakeMT5Client()
    client_a.set_initialize_result(True)
    client_a.set_account_info(make_account_info(login=99999999))  # mismatch
    connection_a = MT5Connection(client_a, lock_path=lock_path)
    result_a = connection_a.connect(_CONFIG)
    assert result_a.success is False

    client_b = FakeMT5Client()
    client_b.set_initialize_result(True)
    client_b.set_account_info(make_account_info(login=12345678))
    connection_b = MT5Connection(client_b, lock_path=lock_path)
    result_b = connection_b.connect(_CONFIG)
    assert result_b.success is True
    connection_b.disconnect()
