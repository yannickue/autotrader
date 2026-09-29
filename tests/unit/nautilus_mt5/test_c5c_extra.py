"""C5C extras: bracket children, out-of-order deals, adapter wiring, zero-real-MT5 guarantee."""

import sys
from decimal import Decimal

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus

from adapters.config import MT5ConnectionConfig
from nautilus_mt5.factory import build_mt5_adapter
from nautilus_mt5.instruments import InstrumentAssumptions
from nautilus_mt5.session import AttachOnlyViolation, SessionState
from risk.models import ReconciliationState
from tests.unit.nautilus_mt5.harness import ExecHarness, Harness

BUY, SELL = OrderSide.BUY, OrderSide.SELL


@pytest.fixture
def h(broker, tmp_path):
    harness = ExecHarness(broker, tmp_path)
    yield harness
    harness.shutdown()


def test_denied_bracket_denies_its_children_too(h, broker):
    entry, stop = h.submit_bracket(BUY, "0.30", 24_900.0)  # invalid volume
    assert h.order(entry).status is OrderStatus.DENIED
    assert h.order(stop).status is OrderStatus.DENIED  # never left dangling as INITIALIZED
    assert broker.order_send_calls == 0
    assert all(r.status == "REJECTED" for r in h.store.children_of(str(entry.client_order_id)))


def test_in_doubt_bracket_children_are_promoted_when_the_parent_is_adopted(h, broker):
    broker.lose_response_after_execute = 1
    entry, stop = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert h.order(stop).status is OrderStatus.INITIALIZED  # parent unknown => child not live
    assert h.session.reconnect().success
    h.client.reconcile()
    assert h.order(entry).status is OrderStatus.FILLED
    assert h.order(stop).status is OrderStatus.ACCEPTED  # now mirrors the broker-side stop
    assert h.client.recon.state is ReconciliationState.RECONCILED


def test_deals_arriving_out_of_order_still_converge(h, broker):
    broker.progressive = True
    broker.fill_plan.append([(0.25, 25_001.5), (0.25, 25_001.75), (0.5, 25_002.0)])
    entry, _ = h.submit_bracket(BUY, "1.00", 24_900.0)
    a, b, c = sorted(d.ticket for d in broker.deals)
    broker.release_deals(c)  # the LAST fill is reported first
    h.client.sync_once()
    broker.release_deals(a, b)
    h.client.sync_once()
    h.client.sync_once()
    order = h.order(entry)
    assert order.status is OrderStatus.FILLED and order.filled_qty == 1.0
    assert h.net_qty() == Decimal("1.00")


def test_commission_reported_by_the_broker_is_booked_not_assumed(broker, tmp_path):
    broker.cfg.commission_per_lot = 2.0
    harness = ExecHarness(broker, tmp_path)
    try:
        entry, _ = harness.submit_bracket(BUY, "0.50", 24_900.0)
        fill = harness.order(entry).events[-1]
        assert float(fill.commission.as_decimal()) == pytest.approx(1.0)  # 2.0/lot * 0.5, cost>0
        assert harness.order(entry).status is OrderStatus.FILLED
    finally:
        harness.shutdown()


# -- wiring
# ----------------------------------------------------------------------


def test_build_adapter_is_io_free_and_shares_one_attach_only_session(broker, tmp_path):
    harness = Harness()
    try:
        adapter = build_mt5_adapter(
            client=broker,
            connection=MT5ConnectionConfig(
                login=broker.cfg.login, password="x", server="s", terminal_path="t"
            ),
            assumptions=InstrumentAssumptions(
                margin_init=Decimal("0.05"), margin_maint=Decimal("0.05")
            ),
            loop=harness.loop,
            msgbus=harness.msgbus,
            cache=harness.cache,
            clock=harness.clock,
            state_path=tmp_path / "s.db",
            lock_path=tmp_path / "l",
        )
        assert broker.calls == [] and broker.initialize_calls == []  # constructing touches nothing
        assert adapter.data_client._session is adapter.exec_client._session
        assert adapter.session.state is SessionState.DISCONNECTED
        adapter.store.close()
        adapter.lane.shutdown()
    finally:
        harness.close()


def test_build_adapter_refuses_a_login_opt_in(broker, tmp_path):
    harness = Harness()
    try:
        with pytest.raises(AttachOnlyViolation):
            build_mt5_adapter(
                client=broker,
                connection=MT5ConnectionConfig(
                    login=1, password="x", server="s", terminal_path="t", allow_account_login=True
                ),
                assumptions=InstrumentAssumptions(
                    margin_init=Decimal("0.05"), margin_maint=Decimal("0.05")
                ),
                loop=harness.loop,
                msgbus=harness.msgbus,
                cache=harness.cache,
                clock=harness.clock,
                state_path=tmp_path / "s.db",
            )
    finally:
        harness.close()


def test_both_clients_share_the_session_refcount_and_disconnect_cleanly(broker, tmp_path):
    harness = Harness()
    try:
        adapter = build_mt5_adapter(
            client=broker,
            connection=MT5ConnectionConfig(
                login=broker.cfg.login, password="x", server="s", terminal_path="t"
            ),
            assumptions=InstrumentAssumptions(
                margin_init=Decimal("0.05"), margin_maint=Decimal("0.05")
            ),
            loop=harness.loop,
            msgbus=harness.msgbus,
            cache=harness.cache,
            clock=harness.clock,
            state_path=tmp_path / "s.db",
            lock_path=tmp_path / "l",
        )
        harness.run(adapter.data_client._connect())
        harness.run(adapter.exec_client._connect())
        assert adapter.session.is_connected
        harness.run(adapter.data_client._disconnect())
        assert adapter.session.is_connected  # exec client still needs it
        harness.run(adapter.exec_client._disconnect())
        assert adapter.session.state is SessionState.DISCONNECTED
        adapter.store.close()
        adapter.lane.shutdown()
    finally:
        harness.close()


# -- zero real MT5
# ----------------------------------------------------------------------


def test_metatrader5_is_never_imported_and_real_client_is_unreachable(broker, tmp_path):
    from adapters.activtrades_mt5 import real_client

    assert "MetaTrader5" not in sys.modules
    harness = ExecHarness(broker, tmp_path)
    try:
        harness.submit_bracket(BUY, "0.25", 24_900.0)
        harness.client.reconcile()
    finally:
        harness.shutdown()
    assert "MetaTrader5" not in sys.modules
    with pytest.raises(AssertionError, match="real MT5 client requested"):
        real_client.get_real_client()  # the autouse guard makes it unreachable in unit tests
