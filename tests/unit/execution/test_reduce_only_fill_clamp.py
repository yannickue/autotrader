"""Fill-time reduce-only safety net: a reduce-only order (manual or a
protective STOP/TAKE_PROFIT child) can never push the position past flat,
even when another reduce-only order on the same instrument reduced or
flattened the position after submit-time validation already approved it."""

from decimal import Decimal

from execution.events import TradeEvent
from execution.models import OrderSide, OrderType, TimeInForce
from execution.orders import OrderStatus

from .conftest import INSTRUMENT, make_decision, make_quote, make_request


def test_reduce_only_fill_clips_to_current_position_and_cancels_remainder(engine, now):
    entry_decision = make_decision(quantity="3", stop_price="90")
    entry_request = make_request(quantity="3")
    engine.submit(entry_request, entry_decision, make_quote(), now)
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("3")

    stop = engine._orders["client-1:stop"]
    assert stop.quantity == Decimal("3")

    manual_decision = make_decision(
        decision_id="d-manual", quantity="1", side="sell", reduce_only=True
    )
    manual_request = make_request(
        request_id="r-manual",
        risk_decision_id="d-manual",
        client_order_id="manual-close",
        side=OrderSide.SELL,
        quantity="1",
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        reduce_only=True,
    )
    engine.submit(manual_request, manual_decision, make_quote(), now)

    # First trade only crosses the manual close order (price >= 99); the stop
    # (trigger 90, crosses when price <= 90) is untouched.
    event1 = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("99"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event1, now)
    manual = engine._orders["manual-close"]
    assert manual.status == OrderStatus.FILLED
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("2")

    # Second trade crosses the stop. Its own remaining_quantity is still 3,
    # but only 2 units of position remain -- the fill must clip to 2, not 3,
    # and never drive the position negative.
    event2 = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t2",
        price=Decimal("85"),
        quantity=Decimal("3"),
    )
    engine.on_trade(event2, now)

    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("0")
    assert stop.filled_quantity == Decimal("2")
    assert stop.status == OrderStatus.CANCELED  # remainder (1) canceled, position flat


def test_manual_reduce_only_and_protective_stop_crossed_by_same_trade_never_go_negative(
    engine, now
):
    entry_decision = make_decision(quantity="2", stop_price="90")
    entry_request = make_request(quantity="2")
    engine.submit(entry_request, entry_decision, make_quote(), now)
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("2")

    manual_decision = make_decision(
        decision_id="d-manual", quantity="2", side="sell", reduce_only=True
    )
    manual_request = make_request(
        request_id="r-manual",
        risk_decision_id="d-manual",
        client_order_id="manual-close",
        side=OrderSide.SELL,
        quantity="2",
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="89",
        reduce_only=True,
    )
    engine.submit(manual_request, manual_decision, make_quote(), now)

    stop = engine._orders["client-1:stop"]
    manual = engine._orders["manual-close"]

    # A single trade crosses both: stop (<=90) and manual limit (>=89).
    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("89.5"),
        quantity=Decimal("5"),
    )
    engine.on_trade(event, now)

    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("0")
    statuses = {stop.status, manual.status}
    assert statuses == {OrderStatus.FILLED, OrderStatus.CANCELED}
    filled = stop if stop.status is OrderStatus.FILLED else manual
    assert filled.filled_quantity == Decimal("2")


def test_reduce_only_order_with_no_remaining_position_is_canceled_not_filled(engine, now):
    entry_decision = make_decision(quantity="1", stop_price="90")
    entry_request = make_request(quantity="1")
    engine.submit(entry_request, entry_decision, make_quote(), now)

    manual_decision = make_decision(
        decision_id="d-manual", quantity="1", side="sell", reduce_only=True
    )
    manual_request = make_request(
        request_id="r-manual",
        risk_decision_id="d-manual",
        client_order_id="manual-close",
        side=OrderSide.SELL,
        quantity="1",
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        reduce_only=True,
    )
    engine.submit(manual_request, manual_decision, make_quote(), now)

    # Fully close the position via the stop first.
    event1 = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("85"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event1, now)
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("0")
    manual = engine._orders["manual-close"]
    assert manual.status == OrderStatus.CANCELED

    # A later crossing trade must not resurrect the manual order or fill it.
    event2 = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t2",
        price=Decimal("100"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event2, now)
    assert manual.status == OrderStatus.CANCELED
    assert manual.filled_quantity == Decimal("0")
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("0")
