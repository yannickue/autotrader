from decimal import Decimal

from execution.events import TradeEvent
from execution.models import OrderSide, OrderType, TimeInForce
from execution.orders import FixedDistanceTrailingStop, OrderStatus
from execution.paper import EngineMode

from .conftest import INSTRUMENT, make_decision, make_quote, make_request


def test_limit_order_does_not_fill_on_quote_touch_only(engine, now):
    decision = make_decision()
    request = make_request(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        side=OrderSide.BUY,
    )
    result = engine.submit(request, decision, make_quote(bid="99", ask="99.05"), now)
    assert result.status == OrderStatus.ACCEPTED
    assert INSTRUMENT not in engine.portfolio.positions


def test_limit_order_fills_from_trade_event_crossing_price(engine, now):
    decision = make_decision()
    request = make_request(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        side=OrderSide.BUY,
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
    assert order.avg_fill_price == Decimal("99")
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("1")


def test_limit_order_partial_fill_then_fill(engine, now):
    decision = make_decision(quantity="3")
    request = make_request(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        side=OrderSide.BUY,
        quantity="3",
    )
    engine.submit(request, decision, make_quote(), now)

    e1 = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("98"),
        quantity=Decimal("1"),
    )
    engine.on_trade(e1, now)
    order = engine._orders["client-1"]
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == Decimal("1")

    e2 = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t2",
        price=Decimal("98"),
        quantity=Decimal("5"),
    )
    engine.on_trade(e2, now)
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == Decimal("3")


def test_duplicate_trade_event_is_noop(engine, now):
    decision = make_decision(quantity="3")
    request = make_request(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        side=OrderSide.BUY,
        quantity="3",
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
    engine.on_trade(event, now)  # replay same trade id
    order = engine._orders["client-1"]
    assert order.filled_quantity == Decimal("1")


def test_stale_resting_order_expires(engine, now):
    from datetime import timedelta

    decision = make_decision()
    request = make_request(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        side=OrderSide.BUY,
    )
    engine.submit(request, decision, make_quote(), now)

    later = now + timedelta(hours=1)
    engine.on_time(later)
    assert engine._orders["client-1"].status == OrderStatus.EXPIRED


def test_protective_stop_and_take_profit_created_after_entry_fill(engine, now):
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1", metadata={"take_profit": Decimal("120")})
    engine.submit(request, decision, make_quote(), now)

    stop = engine._orders["client-1:stop"]
    tp = engine._orders["client-1:take_profit"]
    assert stop.side == OrderSide.SELL
    assert stop.reduce_only is True
    assert stop.quantity == Decimal("1")
    assert tp.quantity == Decimal("1")
    assert stop.oco_sibling_id == tp.client_order_id
    assert tp.oco_sibling_id == stop.client_order_id


def test_stop_fill_cancels_take_profit_sibling(engine, now):
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1", metadata={"take_profit": Decimal("120")})
    engine.submit(request, decision, make_quote(), now)

    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("89"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)

    stop = engine._orders["client-1:stop"]
    tp = engine._orders["client-1:take_profit"]
    assert stop.status == OrderStatus.FILLED
    assert tp.status == OrderStatus.CANCELED


def test_take_profit_fill_cancels_stop_sibling(engine, now):
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1", metadata={"take_profit": Decimal("120")})
    engine.submit(request, decision, make_quote(), now)

    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("121"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)

    stop = engine._orders["client-1:stop"]
    tp = engine._orders["client-1:take_profit"]
    assert tp.status == OrderStatus.FILLED
    assert stop.status == OrderStatus.CANCELED


def test_take_profit_fill_then_later_crossing_stop_trade_does_not_flip_position(engine, now):
    """Once the take-profit fills, OCO cancels the stop. A later trade event
    that would otherwise have crossed the (now-canceled) stop's trigger price
    must not fill it -- the position must stay flat, never flip short."""
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1", metadata={"take_profit": Decimal("120")})
    engine.submit(request, decision, make_quote(), now)

    tp_event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("121"),
        quantity=Decimal("1"),
    )
    engine.on_trade(tp_event, now)

    stop = engine._orders["client-1:stop"]
    tp = engine._orders["client-1:take_profit"]
    assert tp.status == OrderStatus.FILLED
    assert stop.status == OrderStatus.CANCELED
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("0")

    # A later trade crosses the canceled stop's old trigger price (90).
    later_event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t2",
        price=Decimal("85"),
        quantity=Decimal("1"),
    )
    engine.on_trade(later_event, now)

    assert stop.status == OrderStatus.CANCELED  # never resurrected
    assert stop.filled_quantity == Decimal("0")
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("0")  # never flips short


def test_protective_stop_does_not_expire_like_a_normal_resting_order(engine, now):
    """Regression: on_time()'s age-based expiry (max_order_age = 30 min, for
    cleaning up entry limit orders that never filled) applied uniformly to
    ALL non-terminal orders, including protective STOP/TAKE_PROFIT children.
    A stop resting for longer than max_order_age silently went EXPIRED with
    no halt/reduce, leaving an OPEN position unprotected -- violating
    EXECUTION_CONTRACT.md: "Stops must be acknowledged or an equivalent
    deterministic contingency must be active; otherwise the system reduces
    or halts exposure." A protective order's correct lifecycle is to stay
    active for as long as the position it guards is open, not to go stale
    on the same clock as an unfilled entry order."""
    from datetime import timedelta

    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1", metadata={"take_profit": Decimal("120")})
    engine.submit(request, decision, make_quote(), now)

    stop = engine._orders["client-1:stop"]
    tp = engine._orders["client-1:take_profit"]
    assert stop.status == OrderStatus.ACCEPTED
    assert tp.status == OrderStatus.ACCEPTED

    much_later = now + timedelta(hours=6)  # far past max_order_age (30 min)
    engine.on_time(much_later)

    assert stop.status == OrderStatus.ACCEPTED  # still resting, protecting the position
    assert tp.status == OrderStatus.ACCEPTED
    assert engine.mode == EngineMode.READY  # no false halt either

    # The stop must still be live: a later crossing trade fills it normally.
    event = TradeEvent(
        instrument=INSTRUMENT, timestamp=much_later, trade_id="t1",
        price=Decimal("89"), quantity=Decimal("1"),
    )
    engine.on_trade(event, much_later)
    assert stop.status == OrderStatus.FILLED
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("0")


def test_entry_limit_order_still_expires_normally(engine, now):
    """The fix must not blanket-exempt every order from expiry -- only
    protective STOP/TAKE_PROFIT children. An ordinary unfilled entry limit
    order must still expire after max_order_age, same as before."""
    from datetime import timedelta

    decision = make_decision()
    request = make_request(
        order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC,
        limit_price="99", side=OrderSide.BUY,
    )
    engine.submit(request, decision, make_quote(), now)

    later = now + timedelta(hours=1)
    engine.on_time(later)
    assert engine._orders["client-1"].status == OrderStatus.EXPIRED


def test_trailing_stop_only_tightens_for_long_position(engine, now):
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1")
    engine.submit(request, decision, make_quote(), now)
    engine.attach_trailing_stop("client-1", FixedDistanceTrailingStop(distance=Decimal("5")))

    up = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("110"),
        quantity=Decimal("0"),
    )
    engine.on_trade(up, now)
    stop = engine._orders["client-1:stop"]
    assert stop.trigger_price == Decimal("105")  # tightened up from 90

    down = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t2",
        price=Decimal("95"),
        quantity=Decimal("0"),
    )
    engine.on_trade(down, now)
    assert stop.trigger_price == Decimal("105")  # never loosens
