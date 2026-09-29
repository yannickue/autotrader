from datetime import UTC, datetime, timedelta
from pathlib import Path

from adapters.activtrades_mt5.connection import ConnectionState, MT5Connection
from adapters.activtrades_mt5.testing import FakeMT5Client
from adapters.config import MT5ConnectionConfig
from tests.unit.adapters.activtrades_mt5.conftest import make_account_info, make_tick

# login matches `make_account_info()`'s default `login` (12345678) -- with
# the account-protection invariant (`connection.py`), `connect()` now
# verifies the already-attached account's login against this value by
# default, so every fixture below must agree on one login across the board.
CONFIG = MT5ConnectionConfig(login=12345678, password="secret", server="ActivTrades-Demo")
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


def _conn(client: FakeMT5Client, tmp_path: Path) -> MT5Connection:
    """Every test gets its own isolated lock file (under pytest's per-test
    `tmp_path`) instead of the real, process-wide `DEFAULT_LOCK_PATH` --
    otherwise one test's successful (and never explicitly disconnected)
    `connect()` would leave the real lock held and make every other test in
    the suite fail with CONNECTION_BUSY, entirely order-dependently."""
    return MT5Connection(client, lock_path=tmp_path / "mt5.lock")


def test_starts_disconnected(tmp_path: Path) -> None:
    conn = _conn(FakeMT5Client(), tmp_path)
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn.is_new_exposure_allowed() is False


def test_connect_success_transitions_to_connected(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(make_account_info(login=CONFIG.login))
    conn = _conn(client, tmp_path)

    result = conn.connect(CONFIG)

    assert result.success is True
    assert result.state is ConnectionState.CONNECTED
    assert conn.state is ConnectionState.CONNECTED
    assert conn.is_new_exposure_allowed() is False  # CONNECTED != READY


def test_connect_failure_transitions_to_error_and_reports_last_error(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(False, error=(-6, "authorization failed"))
    conn = _conn(client, tmp_path)

    result = conn.connect(CONFIG)

    assert result.success is False
    assert result.state is ConnectionState.ERROR
    assert result.error_code == -6
    assert result.error_description == "authorization failed"


def test_connect_exception_never_raises_past_boundary(tmp_path: Path) -> None:
    class RaisingClient(FakeMT5Client):
        def initialize(self, *args: object, **kwargs: object) -> bool:  # type: ignore[override]
            raise RuntimeError("boom")

    conn = _conn(RaisingClient(), tmp_path)
    result = conn.connect(CONFIG)

    assert result.success is False
    assert result.state is ConnectionState.ERROR
    assert "boom" in result.reason


def test_connect_never_stores_config_on_self(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(make_account_info(login=CONFIG.login))
    conn = _conn(client, tmp_path)
    conn.connect(CONFIG)

    for attr_name in vars(conn):
        value = getattr(conn, attr_name)
        assert value is not CONFIG
        assert not isinstance(value, MT5ConnectionConfig)


def test_disconnect_calls_shutdown_and_resets_state(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(make_account_info(login=CONFIG.login))
    conn = _conn(client, tmp_path)
    conn.connect(CONFIG)

    conn.disconnect()

    assert conn.state is ConnectionState.DISCONNECTED
    assert client.shutdown_calls == 1


def _ready_client() -> FakeMT5Client:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(make_account_info(login=CONFIG.login))
    client.set_symbol_select_default(True)
    client.set_tick("GER40.cash", make_tick(time=int(NOW.timestamp())))
    return client


def test_check_ready_from_disconnected_refuses(tmp_path: Path) -> None:
    conn = _conn(FakeMT5Client(), tmp_path)
    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )
    assert result.ready is False
    assert result.state is ConnectionState.DISCONNECTED


def test_check_ready_success_transitions_to_ready(tmp_path: Path) -> None:
    client = _ready_client()
    conn = _conn(client, tmp_path)
    conn.connect(CONFIG)

    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )

    assert result.ready is True
    assert result.state is ConnectionState.READY
    assert conn.state is ConnectionState.READY
    assert conn.is_new_exposure_allowed() is True


def test_check_ready_degrades_when_account_unreadable(tmp_path: Path) -> None:
    client = _ready_client()
    conn = _conn(client, tmp_path)
    conn.connect(CONFIG)
    # Account becomes unreadable only AFTER a successful connect() -- this
    # test targets check_ready()'s own degrade path, not connect()'s
    # account-identity check (see test_account_protection.py for that).
    client.set_account_info(None)

    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )

    assert result.ready is False
    assert result.state is ConnectionState.DEGRADED
    assert conn.is_new_exposure_allowed() is False


def test_check_ready_degrades_when_symbol_not_selectable(tmp_path: Path) -> None:
    client = _ready_client()
    client.set_symbol_select_result("GER40.cash", False)
    conn = _conn(client, tmp_path)
    conn.connect(CONFIG)

    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )

    assert result.ready is False
    assert result.state is ConnectionState.DEGRADED
    assert "GER40.cash" in result.missing_symbols


def test_check_ready_degrades_when_quote_stale(tmp_path: Path) -> None:
    client = _ready_client()
    stale_time = NOW - timedelta(minutes=10)
    client.set_tick("GER40.cash", make_tick(time=int(stale_time.timestamp())))
    conn = _conn(client, tmp_path)
    conn.connect(CONFIG)

    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )

    assert result.ready is False
    assert result.state is ConnectionState.DEGRADED
    assert "GER40.cash" in result.stale_symbols


def test_check_ready_degrades_when_tick_missing(tmp_path: Path) -> None:
    client = _ready_client()
    client.set_tick("GER40.cash", None)
    conn = _conn(client, tmp_path)
    conn.connect(CONFIG)

    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )

    assert result.ready is False
    assert "GER40.cash" in result.missing_symbols


def test_is_new_exposure_allowed_only_when_ready(tmp_path: Path) -> None:
    client = _ready_client()
    conn = _conn(client, tmp_path)
    assert conn.is_new_exposure_allowed() is False
    conn.connect(CONFIG)
    assert conn.is_new_exposure_allowed() is False
    conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )
    assert conn.is_new_exposure_allowed() is True
