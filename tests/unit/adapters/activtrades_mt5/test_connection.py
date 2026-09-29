from datetime import UTC, datetime, timedelta

from adapters.activtrades_mt5.connection import ConnectionState, MT5Connection
from adapters.activtrades_mt5.testing import FakeMT5Client
from adapters.config import MT5ConnectionConfig
from tests.unit.adapters.activtrades_mt5.conftest import make_account_info, make_tick

CONFIG = MT5ConnectionConfig(login=1, password="secret", server="ActivTrades-Demo")
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


def test_starts_disconnected() -> None:
    conn = MT5Connection(FakeMT5Client())
    assert conn.state is ConnectionState.DISCONNECTED
    assert conn.is_new_exposure_allowed() is False


def test_connect_success_transitions_to_connected() -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    conn = MT5Connection(client)

    result = conn.connect(CONFIG)

    assert result.success is True
    assert result.state is ConnectionState.CONNECTED
    assert conn.state is ConnectionState.CONNECTED
    assert conn.is_new_exposure_allowed() is False  # CONNECTED != READY


def test_connect_failure_transitions_to_error_and_reports_last_error() -> None:
    client = FakeMT5Client()
    client.set_initialize_result(False, error=(-6, "authorization failed"))
    conn = MT5Connection(client)

    result = conn.connect(CONFIG)

    assert result.success is False
    assert result.state is ConnectionState.ERROR
    assert result.error_code == -6
    assert result.error_description == "authorization failed"


def test_connect_exception_never_raises_past_boundary() -> None:
    class RaisingClient(FakeMT5Client):
        def initialize(self, *args: object, **kwargs: object) -> bool:  # type: ignore[override]
            raise RuntimeError("boom")

    conn = MT5Connection(RaisingClient())
    result = conn.connect(CONFIG)

    assert result.success is False
    assert result.state is ConnectionState.ERROR
    assert "boom" in result.reason


def test_connect_never_stores_config_on_self() -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    conn = MT5Connection(client)
    conn.connect(CONFIG)

    for attr_name in vars(conn):
        value = getattr(conn, attr_name)
        assert value is not CONFIG
        assert not isinstance(value, MT5ConnectionConfig)


def test_disconnect_calls_shutdown_and_resets_state() -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    conn = MT5Connection(client)
    conn.connect(CONFIG)

    conn.disconnect()

    assert conn.state is ConnectionState.DISCONNECTED
    assert client.shutdown_calls == 1


def _ready_client() -> FakeMT5Client:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(make_account_info())
    client.set_symbol_select_default(True)
    client.set_tick("GER40.cash", make_tick(time=int(NOW.timestamp())))
    return client


def test_check_ready_from_disconnected_refuses() -> None:
    conn = MT5Connection(FakeMT5Client())
    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )
    assert result.ready is False
    assert result.state is ConnectionState.DISCONNECTED


def test_check_ready_success_transitions_to_ready() -> None:
    client = _ready_client()
    conn = MT5Connection(client)
    conn.connect(CONFIG)

    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )

    assert result.ready is True
    assert result.state is ConnectionState.READY
    assert conn.state is ConnectionState.READY
    assert conn.is_new_exposure_allowed() is True


def test_check_ready_degrades_when_account_unreadable() -> None:
    client = _ready_client()
    client.set_account_info(None)
    conn = MT5Connection(client)
    conn.connect(CONFIG)

    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )

    assert result.ready is False
    assert result.state is ConnectionState.DEGRADED
    assert conn.is_new_exposure_allowed() is False


def test_check_ready_degrades_when_symbol_not_selectable() -> None:
    client = _ready_client()
    client.set_symbol_select_result("GER40.cash", False)
    conn = MT5Connection(client)
    conn.connect(CONFIG)

    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )

    assert result.ready is False
    assert result.state is ConnectionState.DEGRADED
    assert "GER40.cash" in result.missing_symbols


def test_check_ready_degrades_when_quote_stale() -> None:
    client = _ready_client()
    stale_time = NOW - timedelta(minutes=10)
    client.set_tick("GER40.cash", make_tick(time=int(stale_time.timestamp())))
    conn = MT5Connection(client)
    conn.connect(CONFIG)

    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )

    assert result.ready is False
    assert result.state is ConnectionState.DEGRADED
    assert "GER40.cash" in result.stale_symbols


def test_check_ready_degrades_when_tick_missing() -> None:
    client = _ready_client()
    client.set_tick("GER40.cash", None)
    conn = MT5Connection(client)
    conn.connect(CONFIG)

    result = conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )

    assert result.ready is False
    assert "GER40.cash" in result.missing_symbols


def test_is_new_exposure_allowed_only_when_ready() -> None:
    client = _ready_client()
    conn = MT5Connection(client)
    assert conn.is_new_exposure_allowed() is False
    conn.connect(CONFIG)
    assert conn.is_new_exposure_allowed() is False
    conn.check_ready(
        required_symbols=["GER40.cash"], max_quote_age=timedelta(seconds=5), now=NOW
    )
    assert conn.is_new_exposure_allowed() is True
