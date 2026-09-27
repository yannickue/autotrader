from decimal import Decimal

from execution.events import TradeEvent
from execution.models import OrderSide, OrderType, TimeInForce
from execution.orders import OrderStatus, RejectCode
from execution.paper import EngineMode, PaperExecutionEngine

from .conftest import INSTRUMENT, make_decision, make_quote, make_request


def _rest_limit(engine, now, quantity="3"):
    decision = make_decision(quantity=quantity)
    request = make_request(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        side=OrderSide.BUY,
        quantity=quantity,
    )
    engine.submit(request, decision, make_quote(), now)
    return request


def test_cancel_resting_order(engine, now):
    _rest_limit(engine, now)
    result = engine.cancel("client-1", now)
    assert result.accepted is True
    assert result.status == OrderStatus.CANCELED


def test_cancel_is_idempotent_noop_on_terminal_order(engine, now):
    _rest_limit(engine, now)
    engine.cancel("client-1", now)
    second = engine.cancel("client-1", now)
    assert second.accepted is True
    assert second.status == OrderStatus.CANCELED


def test_canceled_limit_order_does_not_fill_on_later_trade_event(engine, now):
    """A CANCELED order must never match a later TradeEvent. In this paper
    simulator, a late fill can only be booked through the explicit
    venue-reported `report_fill` hook (see chaos tests), never through the
    ordinary trade-event crossing path."""
    _rest_limit(engine, now, quantity="1")
    engine.cancel("client-1", now)

    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("98"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)

    order = engine._orders["client-1"]
    assert order.status == OrderStatus.CANCELED
    assert order.filled_quantity == Decimal("0")
    assert INSTRUMENT not in engine.portfolio.positions
    assert engine.mode == EngineMode.READY


def test_expired_order_does_not_fill_on_later_trade_event(engine, now):
    from datetime import timedelta

    _rest_limit(engine, now, quantity="1")
    engine.on_time(now + timedelta(hours=1))
    assert engine._orders["client-1"].status == OrderStatus.EXPIRED

    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("98"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now + timedelta(hours=1))

    order = engine._orders["client-1"]
    assert order.status == OrderStatus.EXPIRED
    assert order.filled_quantity == Decimal("0")
    assert INSTRUMENT not in engine.portfolio.positions


def test_cancel_replace_links_new_client_id_and_cancels_original(engine, now):
    _rest_limit(engine, now, quantity="3")
    result = engine.cancel_replace("client-1", "client-1-r1", now, new_price=Decimal("98"))
    assert result.accepted is True
    original = engine._orders["client-1"]
    replacement = engine._orders["client-1-r1"]
    assert original.status == OrderStatus.CANCELED
    assert original.replaced_by_client_order_id == "client-1-r1"
    assert replacement.replaces_client_order_id == "client-1"
    assert replacement.limit_price == Decimal("98")
    assert replacement.status == OrderStatus.ACCEPTED


def test_cancel_replace_cannot_increase_quantity_beyond_remaining(engine, now):
    _rest_limit(engine, now, quantity="3")
    result = engine.cancel_replace("client-1", "client-1-r1", now, new_quantity=Decimal("5"))
    assert result.accepted is False
    assert result.reject_code == RejectCode.CANCEL_REPLACE_EXCEEDS_REMAINING
    assert "client-1-r1" not in engine._orders
    assert engine._orders["client-1"].status == OrderStatus.ACCEPTED


def test_cancel_replace_after_partial_fill_caps_remaining(engine, now):
    _rest_limit(engine, now, quantity="3")
    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("98"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)  # 1 filled, 2 remaining

    ok = engine.cancel_replace("client-1", "client-1-r1", now, new_quantity=Decimal("2"))
    assert ok.accepted is True

    too_much = engine.cancel_replace("client-1-r1", "client-1-r2", now, new_quantity=Decimal("3"))
    assert too_much.accepted is False


def test_reconcile_halts_on_unknown_external_order(engine, now):
    engine.reconcile({"orders": {"ghost-order": {}}, "positions": {}}, now)
    assert engine.mode == EngineMode.HALTED


def test_reconcile_halts_on_position_mismatch(config, portfolio, now):
    engine = PaperExecutionEngine(config, portfolio)
    engine.reconcile({"orders": {}, "positions": {INSTRUMENT: "5"}}, now)
    assert engine.mode == EngineMode.HALTED


def test_resume_after_reconcile_returns_to_ready(engine, now):
    engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, now)
    assert engine.mode == EngineMode.HALTED

    ok = engine.resume_after_reconcile({"orders": {}, "positions": {}}, now)
    assert ok is True
    assert engine.mode == EngineMode.READY


def test_reduce_only_submit_allowed_while_halted_if_it_truly_reduces(engine, now):
    open_decision = make_decision(decision_id="d-open", quantity="2")
    open_request = make_request(
        request_id="r-open", risk_decision_id="d-open", client_order_id="c-open", quantity="2"
    )
    engine.submit(open_request, open_decision, make_quote(), now)

    # force a halt
    engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, now)
    assert engine.mode == EngineMode.HALTED

    close_decision = make_decision(
        decision_id="d-close", quantity="1", side="sell", reduce_only=True
    )
    close_request = make_request(
        request_id="r-close",
        risk_decision_id="d-close",
        client_order_id="c-close",
        side=OrderSide.SELL,
        quantity="1",
        reduce_only=True,
    )
    result = engine.submit(close_request, close_decision, make_quote(), now)
    assert result.accepted is True


def test_non_reduce_only_submit_rejected_while_halted(engine, now):
    engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, now)
    decision = make_decision()
    request = make_request()
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.NOT_READY


def test_unknown_order_fill_report_halts(engine, now):
    engine.report_fill("no-such-order", "t1", Decimal("100"), Decimal("1"), now)
    assert engine.mode == EngineMode.HALTED


def test_overfill_halts(engine, now):
    _rest_limit(engine, now, quantity="1")
    engine.report_fill("client-1", "t1", Decimal("99"), Decimal("5"), now)
    assert engine.mode == EngineMode.HALTED


def test_export_import_checkpoint_roundtrip(engine, now):
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1", metadata={"take_profit": Decimal("120")})
    engine.submit(request, decision, make_quote(), now)

    checkpoint = engine.export_checkpoint()

    restored = PaperExecutionEngine(engine.config, engine.portfolio)
    restored.import_checkpoint(checkpoint)

    assert restored.export_checkpoint() == checkpoint
    assert "client-1:stop" in restored._orders
    assert "client-1:take_profit" in restored._orders

    # restart flow: RECONCILING then reconcile against matching venue snapshot
    restored.mode = EngineMode.RECONCILING
    venue_orders = {cid: {} for cid, o in restored._orders.items() if not o.is_terminal()}
    venue_positions = {
        instrument: str(view.quantity) for instrument, view in restored.portfolio.positions.items()
    }
    ok = restored.reconcile({"orders": venue_orders, "positions": venue_positions}, now)
    assert ok is True
    assert restored.mode == EngineMode.READY


def test_checkpoint_export_is_deterministic_for_identical_state(engine, now):
    decision = make_decision()
    request = make_request()
    engine.submit(request, decision, make_quote(), now)
    a = engine.export_checkpoint()
    b = engine.export_checkpoint()
    assert a == b
