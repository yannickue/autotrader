# ruff: noqa: E501
"""Lane E: after a PARTIAL reduce-only exit the protective SL/TP child orders follow the remainder.

Invariant under test (never weakened): ``broker_open_quantity == local_remaining_quantity ==
stop-protected quantity`` after every partial -- no over-protection (a stop larger than the position
could flip through flat) and no under-protection (a live position whose broker stop the local
book no longer tracks). MT5 keeps ONE position-wide SL/TP, so the broker level survives a partial
close; the local Nautilus protective ORDERS must be resized to the remainder.
"""

import inspect
from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.objects import Price

from tests.unit.nautilus_mt5.harness import IID, ExecHarness

BUY, SELL = OrderSide.BUY, OrderSide.SELL


@pytest.fixture
def h(broker, tmp_path):
    harness = ExecHarness(broker, tmp_path)
    yield harness
    harness.shutdown()


def open_long(h, sl=24_900.0):
    entry, stop = h.submit_bracket(BUY, "1.00", sl)
    assert h.order(entry).status is OrderStatus.FILLED
    return entry, stop


def triple(h, broker, stop):
    """(broker volume, nautilus net qty, stop-protected quantity)."""
    broker_volume = Decimal(str(broker.positions_get()[0].volume))
    local = abs(h.net_qty())
    protected = Decimal(str(h.order(stop).quantity))
    return broker_volume, local, protected


def modify(h, order, trigger):
    return SimpleNamespace(
        client_order_id=order.client_order_id,
        strategy_id=order.strategy_id,
        instrument_id=IID,
        venue_order_id=h.order(order).venue_order_id or VenueOrderId("SL:0"),
        trigger_price=Price(trigger, 2),
        price=None,
        quantity=None,
    )


def test_partial_exit_resizes_the_stop_to_the_remaining_quantity(h, broker):
    _, stop = open_long(h)
    assert triple(h, broker, stop) == (Decimal("1.00"),) * 3
    broker.set_quote(25_010.0, 25_011.5)
    part = h.market(SELL, "0.25", reduce_only=True)
    h.submit(part)
    assert h.order(part).status is OrderStatus.FILLED
    assert triple(h, broker, stop) == (Decimal("0.75"),) * 3
    assert broker.positions_get()[0].sl == 24_900.0  # broker stop untouched by the resize
    assert h.order(stop).status is OrderStatus.ACCEPTED  # still a working protective order
    row = h.store.by_client_order_id(str(stop.client_order_id))
    assert row.status == "ACCEPTED"


def test_second_partial_keeps_the_invariant_and_final_close_retires_protection(h, broker):
    _, stop = open_long(h)
    broker.set_quote(25_010.0, 25_011.5)
    for qty, expected in (("0.25", "0.75"), ("0.25", "0.50")):
        order = h.market(SELL, qty, reduce_only=True)
        h.submit(order)
        assert h.order(order).status is OrderStatus.FILLED
        assert triple(h, broker, stop) == (Decimal(expected),) * 3
    last = h.market(SELL, "0.50", reduce_only=True)
    h.submit(last)
    assert broker.positions_get() == ()
    assert h.order(stop).status is OrderStatus.CANCELED


def test_partial_exit_resizes_the_take_profit_child_too(h, broker):
    from nautilus_trader.model.enums import TimeInForce
    from nautilus_trader.model.objects import Quantity

    entry = h.factory.market(IID, BUY, Quantity.from_str("1.00"), time_in_force=TimeInForce.IOC)
    stop = h.factory.stop_market(
        IID, SELL, Quantity.from_str("1.00"), Price(24_900.0, 2), reduce_only=True
    )
    take = h.factory.limit(
        IID, SELL, Quantity.from_str("1.00"), Price(25_300.0, 2),
        time_in_force=TimeInForce.GTC, reduce_only=True,
    )
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.execution.messages import SubmitOrderList
    from nautilus_trader.model.orders import OrderList

    from tests.unit.nautilus_mt5.harness import TRADER

    for order in (entry, stop, take):
        h._add(order)
    h.engine.execute(
        SubmitOrderList(
            TRADER, stop.strategy_id, OrderList(h.factory.generate_order_list_id(), [entry, stop, take]),
            UUID4(), h.clock.timestamp_ns(), client_id=h.client.id,
        )
    )
    h.pump(0.02)
    assert h.order(take).status is OrderStatus.ACCEPTED
    broker.set_quote(25_010.0, 25_011.5)
    part = h.market(SELL, "0.25", reduce_only=True)
    h.submit(part)
    assert Decimal(str(h.order(stop).quantity)) == Decimal("0.75")
    assert Decimal(str(h.order(take).quantity)) == Decimal("0.75")
    assert broker.positions_get()[0].tp == 25_300.0


def test_resize_survives_a_stop_move_after_the_partial(h, broker):
    _, stop = open_long(h)
    broker.set_quote(25_010.0, 25_011.5)
    part = h.market(SELL, "0.25", reduce_only=True)
    h.submit(part)
    h.run(h.client._modify_order(modify(h, stop, 24_950.0)))  # tighten after the partial
    assert broker.positions_get()[0].sl == 24_950.0
    assert triple(h, broker, stop) == (Decimal("0.75"),) * 3
    assert Decimal(str(h.order(stop).trigger_price)) == Decimal("24950.00")


def test_modify_rejected_keeps_quantity_and_broker_stop(h, broker):
    _, stop = open_long(h)
    broker.set_quote(25_010.0, 25_011.5)
    h.submit(h.market(SELL, "0.25", reduce_only=True))
    h.client.recon.invalidate("test")  # loosening while unreconciled is refused
    h.run(h.client._modify_order(modify(h, stop, 24_800.0)))
    assert broker.positions_get()[0].sl == 24_900.0
    assert triple(h, broker, stop) == (Decimal("0.75"),) * 3


def test_late_partial_deal_is_resized_when_it_is_finally_ingested(h, broker):
    _, stop = open_long(h)
    broker.set_quote(25_010.0, 25_011.5)
    part = h.market(SELL, "0.25", reduce_only=True)
    h.submit(part)
    h.client.sync_once()  # idempotent: a repeated sync must not resize again or flip anything
    h.client.sync_once()
    assert triple(h, broker, stop) == (Decimal("0.75"),) * 3


def test_failed_resize_is_retried_from_broker_truth_on_the_next_sync(h, broker, monkeypatch):
    _, stop = open_long(h)
    broker.set_quote(25_010.0, 25_011.5)
    real = h.client.generate_order_updated
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(h.client, "generate_order_updated", flaky)
    h.submit(h.market(SELL, "0.25", reduce_only=True))
    assert Decimal(str(h.order(stop).quantity)) == Decimal("1.00")  # first attempt failed
    assert h.client._protective_resize_pending is True
    h.client.sync_once()  # retry: broker truth says 0.75
    assert triple(h, broker, stop) == (Decimal("0.75"),) * 3
    assert h.client._protective_resize_pending is False


def test_reconnect_then_reconcile_after_partial_is_clean(h, broker):
    _, stop = open_long(h)
    broker.set_quote(25_010.0, 25_011.5)
    h.submit(h.market(SELL, "0.25", reduce_only=True))
    h.client.recon.invalidate("reconnect")
    h.client.reconcile()
    assert h.client.recon.state.name == "RECONCILED", h.client.recon.discrepancies
    assert triple(h, broker, stop) == (Decimal("0.75"),) * 3


def test_restart_after_partial_and_stop_move_reconciles_against_broker(broker, tmp_path):
    first = ExecHarness(broker, tmp_path)
    _, stop = open_long(first)
    broker.set_quote(25_010.0, 25_011.5)
    first.submit(first.market(SELL, "0.25", reduce_only=True))
    first.run(first.client._modify_order(modify(first, stop, 24_950.0)))
    assert broker.positions_get()[0].sl == 24_950.0
    first.session.disconnect()
    first.shutdown()
    second = ExecHarness(broker, tmp_path, live_engine=True)
    try:
        assert broker.positions_get()[0].volume == 0.75
        assert broker.positions_get()[0].sl == 24_950.0  # the stop move survived the restart
        assert second.client.recon.state.name == "MISMATCH"  # connect is not reconciliation
        mass = second.run(second.client.generate_mass_status())
        result = second.engine.reconcile_execution_mass_status(mass)
        if inspect.isawaitable(result):
            second.run(result)
        second.pump(0.05)
        assert second.net_qty() == Decimal("0.75")  # remaining quantity, not the original 1.00
        assert second.client.reconcile().state.name == "RECONCILED"
    finally:
        second.shutdown()
