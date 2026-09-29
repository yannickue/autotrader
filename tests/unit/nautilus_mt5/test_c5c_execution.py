"""C5C: Nautilus MT5 execution client against the stateful fake broker, wired to a REAL
Nautilus ExecutionEngine + Cache + Portfolio. Nautilus stays authoritative: every assertion on
orders/positions/PnL reads the Nautilus cache, never adapter state."""

from decimal import Decimal

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus, PositionSide

from nautilus_mt5.constants import Retcode
from nautilus_mt5.session import UnsupportedAccountMode
from risk.models import ReconciliationState, RuntimeMode
from tests.unit.nautilus_mt5.harness import IID, ExecHarness

BUY, SELL = OrderSide.BUY, OrderSide.SELL


@pytest.fixture
def h(broker, tmp_path):
    harness = ExecHarness(broker, tmp_path)
    yield harness
    harness.shutdown()


def status_of(h, order):
    return h.order(order).status


# -- connect / account
# ----------------------------------------------------------------------


def test_connect_registers_broker_account_and_reconciles_against_real_snapshot(h, broker):
    assert h.client.recon.state is ReconciliationState.RECONCILED
    assert h.client.recon.runtime is RuntimeMode.READY
    assert h.client.recon.source.value == "venue_snapshot"
    account = h.cache.account(h.client.account_id)
    assert account is not None and str(account.base_currency) == "EUR"
    assert account.balance_total().as_decimal() == Decimal("10000.00")  # broker balance, not C4's
    assert all(call["login"] is None for call in broker.initialize_calls)  # attach only


def test_hedging_account_fails_closed_at_connect(broker, tmp_path):
    broker.cfg.margin_mode = 2
    with pytest.raises(UnsupportedAccountMode):
        ExecHarness(broker, tmp_path)


# -- entries
# ----------------------------------------------------------------------


def test_bare_market_entry_is_denied_without_an_attached_stop(h, broker):
    order = h.market(BUY, "0.25")
    h.submit(order)
    assert status_of(h, order) is OrderStatus.DENIED
    assert "ENTRY_WITHOUT_ATTACHED_STOP_LOSS" in h.denial(order)
    assert broker.order_send_calls == 0 and broker.check_log == []


def test_bracket_entry_fills_in_nautilus_with_atomic_broker_side_stop(h, broker):
    entry, stop = h.submit_bracket(BUY, "0.25", 24_900.0)

    # order_check ran first (success == retcode 0) and carried the stop in the SAME request
    assert len(broker.check_log) == 1 and broker.request_log[0]["sl"] == 24_900.0
    assert broker.request_log[0]["type_filling"] == 1 and broker.request_log[0]["deviation"] == 20
    assert broker.request_log[0]["comment"].startswith("NT")

    filled = h.order(entry)
    assert filled.status is OrderStatus.FILLED and filled.filled_qty == 0.25
    assert float(filled.avg_px) == 25_001.5  # ask: BUY fills at the executable side
    (position,) = h.cache.positions_open()
    assert position.side is PositionSide.LONG and float(position.quantity) == 0.25

    (bpos,) = broker.positions_get()
    assert bpos.sl == 24_900.0  # protection existed from the first instant at the broker
    assert h.order(stop).status is OrderStatus.ACCEPTED
    assert str(h.order(stop).venue_order_id) == f"SL:{bpos.ticket}"

    # identity: ClientOrderId <-> order ticket, fill identity == broker deal ticket
    row = h.store.by_client_order_id(str(entry.client_order_id))
    assert row.status == "ACCEPTED" and row.order_ticket == broker.history_orders[0].ticket
    assert row.position_ticket == broker.positions_get()[0].ticket
    deal = broker.history_deals_get()[0]
    assert h.store.is_ingested(deal.ticket)
    fill_event = filled.events[-1]
    assert str(fill_event.trade_id) == str(deal.ticket)
    assert h.client.recon.state is ReconciliationState.RECONCILED


def test_entry_is_denied_when_not_venue_reconciled_then_allowed_after_reconcile(h):
    h.client.recon.invalidate("test")
    order_list = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert status_of(h, order_list[0]) is OrderStatus.DENIED
    assert "NOT_VENUE_RECONCILED" in h.denial(order_list[0])
    assert h.client.reconcile().state is ReconciliationState.RECONCILED
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert status_of(h, entry) is OrderStatus.FILLED


def test_v1_one_position_no_pyramiding_no_flip(h):
    h.submit_bracket(BUY, "0.25", 24_900.0)
    again = h.submit_bracket(BUY, "0.25", 24_900.0)[0]
    flip = h.submit_bracket(SELL, "0.25", 25_100.0)[0]
    for order in (again, flip):
        assert status_of(h, order) is OrderStatus.DENIED
        assert "POSITION_EXISTS" in h.denial(order)
    assert h.net_qty() == Decimal("0.25")


def test_order_check_failure_rejects_and_nothing_is_sent(h, broker):
    broker.market_open = False
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert status_of(h, entry) is OrderStatus.REJECTED
    assert f"ORDER_CHECK_{int(Retcode.MARKET_CLOSED)}" in h.denial(entry)
    assert broker.order_send_calls == 0 and broker.positions_get() == ()
    assert h.store.by_client_order_id(str(entry.client_order_id)).status == "REJECTED"


def test_order_send_rejection_is_a_definite_reject(h, broker):
    broker.send_retcode_override.append(int(Retcode.REJECT))
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert status_of(h, entry) is OrderStatus.REJECTED and broker.positions_get() == ()


def test_invalid_volume_and_wrong_side_stop_are_denied_locally(h, broker):
    bad_volume = h.submit_bracket(BUY, "0.30", 24_900.0)[0]
    bad_stop = h.submit_bracket(BUY, "0.25", 25_100.0)[0]  # stop above a BUY
    assert "INVALID_VOLUME" in h.denial(bad_volume) and "INVALID_STOPS" in h.denial(bad_stop)
    assert broker.order_send_calls == 0


def test_unsupported_order_types_are_denied(h):
    from nautilus_trader.model.objects import Price, Quantity

    order = h._add(h.factory.limit(IID, BUY, Quantity.from_str("0.25"), Price(24_000.0, 2)))
    h.submit(order)
    assert status_of(h, order) is OrderStatus.DENIED and "UNSUPPORTED_ORDER_TYPE" in h.denial(order)
