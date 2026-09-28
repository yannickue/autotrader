"""Slice 2: per-fill fee debiting and the `fill_listener` observability hook."""

from decimal import Decimal

from costs.engine import calculate_fill_fee
from costs.models import LiquidityRole
from execution.events import TradeEvent
from execution.models import OrderSide, OrderType, TimeInForce
from execution.orders import OrderStatus

from .conftest import INSTRUMENT, make_decision, make_quote, make_request


def test_market_order_fill_is_debited_at_taker_rate(engine, portfolio, cost_schedule, now):
    decision = make_decision()
    request = make_request()

    result = engine.submit(request, decision, make_quote(), now)

    assert result.status == OrderStatus.FILLED
    order = engine._orders["client-1"]
    expected_fee = calculate_fill_fee(
        notional=order.avg_fill_price * order.filled_quantity,
        liquidity_role=LiquidityRole.TAKER,
        schedule=cost_schedule,
    )
    assert expected_fee > Decimal("0")
    assert portfolio.fees == expected_fee


def test_crossed_resting_limit_is_debited_at_maker_rate(engine, portfolio, cost_schedule, now):
    decision = make_decision(quantity="1")
    request = make_request(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        side=OrderSide.BUY,
        quantity="1",
    )
    engine.submit(request, decision, make_quote(), now)

    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("98"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)

    order = engine._orders["client-1"]
    assert order.status == OrderStatus.FILLED
    expected_fee = calculate_fill_fee(
        notional=order.avg_fill_price * order.filled_quantity,
        liquidity_role=LiquidityRole.MAKER,
        schedule=cost_schedule,
    )
    assert expected_fee > Decimal("0")
    assert expected_fee != calculate_fill_fee(
        notional=order.avg_fill_price * order.filled_quantity,
        liquidity_role=LiquidityRole.TAKER,
        schedule=cost_schedule,
    )
    assert portfolio.fees == expected_fee


def test_duplicate_fill_id_does_not_double_charge_fee(engine, portfolio, now):
    decision = make_decision()
    request = make_request()

    engine.submit(request, decision, make_quote(), now)
    fees_after_first = portfolio.fees
    assert fees_after_first > Decimal("0")

    # Resubmitting the exact same request_id is idempotent at submit() and
    # must not touch the ledger a second time.
    engine.submit(request, decision, make_quote(), now)
    assert portfolio.fees == fees_after_first

    # report_fill() with a trade_id already seen for this order is also a
    # no-op (order is already terminal/FILLED, and the fill_id would dedupe
    # in the portfolio regardless).
    order = engine._orders["client-1"]
    engine.report_fill("client-1", "entry", order.avg_fill_price, Decimal("1"), now)
    assert portfolio.fees == fees_after_first


def test_fill_listener_fires_exactly_once_with_correct_fill_event(engine, portfolio, now):
    events = []
    engine.fill_listener = events.append

    decision = make_decision()
    request = make_request()
    engine.submit(request, decision, make_quote(), now)

    assert len(events) == 1
    event = events[0]
    order = engine._orders["client-1"]

    assert event.fill_id == "client-1:entry"
    assert event.client_order_id == "client-1"
    assert event.decision_id == decision.decision_id
    assert event.instrument == INSTRUMENT
    assert event.side == OrderSide.BUY
    assert event.quantity == order.filled_quantity
    assert event.price == order.avg_fill_price
    assert event.fee == portfolio.fees
    assert event.fee > Decimal("0")
    assert event.liquidity_role == LiquidityRole.TAKER
    assert event.reduce_only is False
    assert event.timestamp == now


def test_fill_listener_not_called_for_duplicate_or_no_op_fill(engine, now):
    events = []
    engine.fill_listener = events.append

    decision = make_decision()
    request = make_request()
    engine.submit(request, decision, make_quote(), now)
    assert len(events) == 1

    # Resubmitting the identical request is a pure idempotent no-op at
    # submit(): no new fill, so the listener must not fire again.
    engine.submit(request, decision, make_quote(), now)
    assert len(events) == 1


def test_fill_listener_none_by_default_does_not_raise(engine, now):
    assert engine.fill_listener is None
    decision = make_decision()
    request = make_request()
    result = engine.submit(request, decision, make_quote(), now)
    assert result.status == OrderStatus.FILLED
