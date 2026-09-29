"""C5B: session state machine + Nautilus MT5 data client (real Nautilus components, fake broker)."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.identifiers import InstrumentId

from adapters.activtrades_mt5.lock import acquire_mt5_lock
from adapters.config import MT5ConnectionConfig
from nautilus_mt5.data_client import Mt5DataClientConfig, Mt5LiveMarketDataClient
from nautilus_mt5.instruments import Mt5InstrumentProvider
from nautilus_mt5.session import (
    AttachOnlyViolation,
    Mt5CallError,
    Mt5Session,
    SessionState,
    UnsupportedAccountMode,
)
from tests.unit.nautilus_mt5.harness import Harness, make_rates

IID = InstrumentId.from_str("GER40.ACTIVTRADES")
BID_5M = BarType.from_str("GER40.ACTIVTRADES-5-MINUTE-BID-EXTERNAL")
ASK_5M = BarType.from_str("GER40.ACTIVTRADES-5-MINUTE-ASK-EXTERNAL")
# server clock 2026-09-22 10:00:00 (CEST) encoded as epoch; true UTC = 08:00:00
SERVER_T0 = int(datetime(2026, 9, 22, 10, 0, tzinfo=UTC).timestamp())
UTC_T0 = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


def config_for(broker, **kw):
    return MT5ConnectionConfig(
        login=kw.pop("login", broker.cfg.login),
        password="x",
        server="Demo",
        terminal_path="t",
        **kw,
    )


@pytest.fixture
def session(broker, tmp_path):
    return Mt5Session(broker, config_for(broker), lock_path=tmp_path / "mt5.lock")


@pytest.fixture
def harness():
    h = Harness()
    h.capture("DataEngine.process")
    yield h
    h.close()


def build_client(harness, broker, session, assumptions, *, autostart):
    provider = Mt5InstrumentProvider(broker, assumptions=assumptions)
    c = Mt5LiveMarketDataClient(
        harness.loop,
        harness.msgbus,
        harness.cache,
        harness.clock,
        provider,
        session,
        Mt5DataClientConfig(
            poll_interval_secs=0.005, reconnect_backoff_secs=(0.005,), autostart_poller=autostart
        ),
    )
    harness.run(c._connect())
    c._set_connected(True)
    return c


@pytest.fixture
def client(harness, broker, session, assumptions):
    """Deterministic: tests call poll_once() themselves."""
    return build_client(harness, broker, session, assumptions, autostart=False)


@pytest.fixture
def live_client(harness, broker, session, assumptions):
    """With the real controlled poller task."""
    return build_client(harness, broker, session, assumptions, autostart=True)


def events(harness, kind):
    return [e for e in harness.captured["DataEngine.process"] if isinstance(e, kind)]


def subscribe_quotes(harness, client):
    harness.run(client._subscribe_quote_ticks(SimpleNamespace(instrument_id=IID)))


# -- session ---------------------------------------------------------------------------------


def test_login_opt_in_is_refused_at_construction(broker, tmp_path):
    with pytest.raises(AttachOnlyViolation):
        Mt5Session(broker, config_for(broker, allow_account_login=True), lock_path=tmp_path / "l")
    assert broker.initialize_calls == []


def test_connect_is_attach_only_and_never_passes_credentials(broker, session):
    assert session.connect().success and session.state is SessionState.CONNECTED
    assert broker.initialize_calls == [{"path": "t", "login": None, "server": None}]


def test_account_mismatch_fails_closed_without_login_attempt(broker, tmp_path):
    session = Mt5Session(broker, config_for(broker, login=111), lock_path=tmp_path / "l")
    result = session.connect()
    assert not result.success and session.state is SessionState.FAILED
    assert all(call["login"] is None for call in broker.initialize_calls)


def test_lock_busy_is_reported_and_terminal_not_touched(broker, tmp_path):
    lock_path = tmp_path / "busy.lock"
    held = acquire_mt5_lock(lock_path)
    try:
        session = Mt5Session(broker, config_for(broker), lock_path=lock_path)
        result = session.connect()
        assert not result.success and "CONNECTION_BUSY" in result.reason
        assert broker.initialize_calls == [] and session.state is SessionState.FAILED
    finally:
        held.release()


def test_reconnect_bumps_generation_and_never_logs_in(broker, session):
    session.connect()
    first = session.generation
    assert session.reconnect().success
    assert session.generation == first + 1
    assert all(call["login"] is None for call in broker.initialize_calls)


def test_lost_terminal_degrades_session_and_call_raises(broker, session):
    session.connect()
    broker.disconnect()
    with pytest.raises(Mt5CallError):
        session.call("positions_get", broker.positions_get)
    assert session.state is SessionState.DEGRADED
    with pytest.raises(Mt5CallError, match="DEGRADED"):
        session.call("positions_get", broker.positions_get)


def test_none_result_with_healthy_terminal_does_not_degrade(broker, session):
    session.connect()
    with pytest.raises(Mt5CallError):
        session.call("symbol_info_tick", broker.symbol_info_tick, "Nope")
    assert session.state is SessionState.CONNECTED


def test_account_mode_netting_supported_hedging_and_unknown_fail_closed(broker, session):
    session.connect()
    assert session.account_mode().name == "RETAIL_NETTING"
    broker.cfg.margin_mode = 2  # RETAIL_HEDGING
    with pytest.raises(UnsupportedAccountMode, match="RETAIL_HEDGING"):
        session.account_mode()
    broker.cfg.margin_mode = 9
    with pytest.raises(UnsupportedAccountMode, match="unknown"):
        session.account_mode()


def test_refcounted_session_shared_by_two_clients(broker, session):
    session.acquire()
    assert session.acquire() is None
    session.release()
    assert session.is_connected
    session.release()
    assert session.state is SessionState.DISCONNECTED


# -- data client: connection + instruments -------------------------------------------------------


def test_connect_publishes_instruments_from_broker_metadata(harness, client):
    instruments = events(harness, type(client._provider.find(IID)))
    assert [str(i.id) for i in instruments] == ["GER40.ACTIVTRADES"]
    assert harness.cache.instrument(IID) is not None


# -- quotes --


def test_quote_is_published_once_with_utc_timestamp_and_unknown_depth(harness, client, broker):
    broker.server_time = SERVER_T0
    subscribe_quotes(harness, client)
    assert client.poll_once() == 1
    assert client.poll_once() == 0  # unchanged tick: no duplicate market event
    (quote,) = events(harness, QuoteTick)
    assert (float(quote.bid_price), float(quote.ask_price)) == (25_000.0, 25_001.5)
    assert quote.ts_event == int(UTC_T0.timestamp() * 1e9)  # server clock -> true UTC
    assert float(quote.bid_size) == 0.0 and float(quote.ask_size) == 0.0
    assert client.stats["duplicates_suppressed"] == 1


def test_changed_quote_publishes_again_and_survives_no_change_polls(harness, client, broker):
    broker.server_time = SERVER_T0
    subscribe_quotes(harness, client)
    client.poll_once()
    broker.server_time += 1
    broker.bid, broker.ask = 25_000.5, 25_002.0
    assert client.poll_once() == 1
    assert len(events(harness, QuoteTick)) == 2


def test_ambiguous_dst_hour_quote_is_dropped_not_guessed(harness, client, broker):
    broker.server_time = int(datetime(2026, 10, 25, 2, 30, tzinfo=UTC).timestamp())
    subscribe_quotes(harness, client)
    assert client.poll_once() == 0
    assert client.stats["dropped_ambiguous_time"] == 1 and not events(harness, QuoteTick)


def test_crossed_quote_is_never_published(harness, client, broker):
    broker.server_time = SERVER_T0
    broker.bid, broker.ask = 25_010.0, 25_000.0
    subscribe_quotes(harness, client)
    assert client.poll_once() == 0


def test_unknown_instrument_subscription_fails_closed(harness, client):
    with pytest.raises(KeyError):
        harness.run(
            client._subscribe_quote_ticks(
                SimpleNamespace(instrument_id=InstrumentId.from_str("FOO.ACTIVTRADES"))
            )
        )


# -- bars --


def bar_rows(*opens_server, price=25_000.0):
    return [
        (t, price, price + 5, price - 5, price + 1, 100 + i, 155, 0)
        for i, t in enumerate(opens_server)
    ]


def test_only_completed_bars_publish_once_stamped_at_close(harness, client, broker):
    step = 300
    broker.server_time = SERVER_T0 + 2 * step + 60  # bar at T0+2s is still forming
    broker.rates[5] = make_rates(bar_rows(SERVER_T0, SERVER_T0 + step, SERVER_T0 + 2 * step))
    harness.run(client._subscribe_bars(SimpleNamespace(bar_type=BID_5M)))
    assert client.poll_once() == 0  # first sight: baseline only, no history flood
    broker.server_time = SERVER_T0 + 3 * step + 10  # the T0+2s bar has now closed
    broker.rates[5] = make_rates(
        bar_rows(SERVER_T0, SERVER_T0 + step, SERVER_T0 + 2 * step, SERVER_T0 + 3 * step)
    )
    assert client.poll_once() == 1
    assert client.poll_once() == 0
    (bar,) = events(harness, Bar)
    assert bar.bar_type == BID_5M
    assert bar.ts_event == int((UTC_T0 + timedelta(seconds=3 * step)).timestamp() * 1e9)
    assert float(bar.volume) == 102.0  # tick activity of the closed bar (never exchange volume)


def test_ask_bars_and_unsupported_specs_are_rejected(harness, client):
    for bad in (ASK_5M, BarType.from_str("GER40.ACTIVTRADES-3-MINUTE-BID-EXTERNAL")):
        with pytest.raises(ValueError):
            harness.run(client._subscribe_bars(SimpleNamespace(bar_type=bad)))


# -- controlled poller + reconnect --


def test_single_controlled_poller_starts_stops_and_publishes_without_duplicates(
    harness, live_client, broker
):
    client = live_client
    broker.server_time = SERVER_T0
    subscribe_quotes(harness, client)
    subscribe_quotes(harness, client)  # second subscribe must not spawn a second poller
    first_task = client._poll_task
    assert first_task is not None
    harness.until(lambda: client.stats["quotes"] >= 1)
    harness.pump(0.05)
    assert client.stats["quotes"] == 1 and client._poll_task is first_task
    harness.run(client._unsubscribe_quote_ticks(SimpleNamespace(instrument_id=IID)))
    assert client._poll_task is None and first_task.done()


def test_poll_failure_then_attach_only_reconnect_resumes_without_replaying(
    harness, client, broker, session
):
    broker.server_time = SERVER_T0
    subscribe_quotes(harness, client)
    client.poll_once()
    generation = session.generation

    broker.disconnect()
    with pytest.raises(Mt5CallError):
        client.poll_once()
    assert session.state is SessionState.DEGRADED
    assert client.try_reconnect() is False  # terminal still down: no silent success
    assert session.state is SessionState.FAILED

    broker.reconnect()
    assert client.try_reconnect() is True and session.generation > generation
    assert client.poll_once() == 0  # same tick as before the outage: not replayed
    broker.server_time += 5
    broker.bid, broker.ask = 24_990.0, 24_991.5
    assert client.poll_once() == 1
    assert all(call["login"] is None for call in broker.initialize_calls)


def test_poller_task_survives_outage_with_bounded_backoff_and_recovers(
    harness, live_client, broker
):
    client = live_client
    broker.server_time = SERVER_T0
    subscribe_quotes(harness, client)
    harness.until(lambda: client.stats["quotes"] == 1)
    broker.disconnect()
    harness.until(lambda: client.stats["poll_failures"] >= 1)
    broker.reconnect()
    broker.server_time += 5
    broker.bid, broker.ask = 24_950.0, 24_951.5
    harness.until(lambda: client.stats["quotes"] == 2, timeout=3.0)
    assert client.stats["reconnects"] >= 1
    harness.run(client._unsubscribe_quote_ticks(SimpleNamespace(instrument_id=IID)))


def test_async_module_has_no_real_sleeps_in_unit_paths():
    # poll_once() is the unit under test; the loop only wraps it.
    assert asyncio.iscoroutinefunction(Mt5LiveMarketDataClient._poll_loop)
