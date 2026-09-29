from datetime import timedelta
from decimal import Decimal

from execution.models import OrderSide, OrderType, TimeInForce
from execution.orders import OrderStatus, RejectCode

from .conftest import INSTRUMENT, make_decision, make_quote, make_request


def test_market_order_fills_immediately(engine, now):
    decision = make_decision()
    request = make_request()
    result = engine.submit(request, decision, make_quote(), now)
    assert result.accepted is True
    assert result.status == OrderStatus.FILLED


def test_resubmitting_same_request_id_is_idempotent(engine, now):
    decision = make_decision()
    request = make_request()
    first = engine.submit(request, decision, make_quote(), now)
    second = engine.submit(request, decision, make_quote(), now)
    assert first == second
    assert len(engine._orders) == 1


def test_same_client_order_id_different_payload_is_duplicate_conflict(engine, now):
    decision = make_decision()
    request = make_request()
    engine.submit(request, decision, make_quote(), now)
    conflicting = make_request(request_id="request-2", quantity="2")
    result = engine.submit(conflicting, decision, make_quote(), now)
    assert result.accepted is False
    assert result.reject_code == RejectCode.DUPLICATE_CONFLICT
    assert engine.mode.value == "ready"


def test_missing_decision_is_risk_not_approved(engine, now):
    request = make_request()
    result = engine.submit(request, None, make_quote(), now)
    assert result.reject_code == RejectCode.RISK_NOT_APPROVED


def test_unapproved_decision_is_risk_not_approved(engine, now):
    decision = make_decision(approved=False)
    request = make_request()
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.RISK_NOT_APPROVED


def test_decision_instrument_mismatch(engine, now):
    decision = make_decision(instrument="ETHUSDT-PERP")
    request = make_request()
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.RISK_MISMATCH


def test_decision_id_mismatch(engine, now):
    decision = make_decision(decision_id="other-decision")
    request = make_request()
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.RISK_MISMATCH


def test_decision_already_consumed_by_another_request(engine, now):
    decision = make_decision()
    request1 = make_request(request_id="r1", client_order_id="c1")
    engine.submit(request1, decision, make_quote(), now)

    request2 = make_request(request_id="r2", client_order_id="c2")
    result = engine.submit(request2, decision, make_quote(), now)
    assert result.reject_code == RejectCode.RISK_ALREADY_CONSUMED


def test_quantity_exceeds_approved_size(engine, now):
    decision = make_decision(quantity="1")
    request = make_request(quantity="2")
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.EXCEEDS_APPROVED_SIZE


def test_stale_request_rejected(engine, now):
    decision = make_decision()
    request = make_request(timestamp=now - timedelta(seconds=30))
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.STALE_REQUEST


def test_stale_decision_rejected(engine, now):
    decision = make_decision(timestamp=now - timedelta(seconds=30))
    request = make_request()
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.STALE_REQUEST


def test_future_timestamp_rejected(engine, now):
    decision = make_decision()
    request = make_request(timestamp=now + timedelta(seconds=30))
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.STALE_REQUEST


def test_naive_timestamp_is_invalid_request(engine, now):
    from datetime import datetime as dt

    decision = make_decision()
    request = make_request(timestamp=dt(2026, 1, 1, 12, 0, 0))
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.INVALID_REQUEST


def test_non_positive_quantity_is_invalid_request(engine, now):
    decision = make_decision(quantity="1")
    request = make_request(quantity="1")
    object.__setattr__(request, "quantity", Decimal("0"))
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.INVALID_REQUEST


def test_market_order_with_gtc_is_invalid_tif(engine, now):
    decision = make_decision()
    request = make_request(order_type=OrderType.MARKET, time_in_force=TimeInForce.GTC)
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.INVALID_TIF


def test_limit_order_gtc_rests(engine, now):
    decision = make_decision()
    request = make_request(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        side=OrderSide.BUY,
    )
    result = engine.submit(request, decision, make_quote(), now)
    assert result.accepted is True
    assert result.status == OrderStatus.ACCEPTED


def test_limit_order_ioc_cancels_without_trade_event(engine, now):
    decision = make_decision()
    request = make_request(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        limit_price="99",
        side=OrderSide.BUY,
    )
    result = engine.submit(request, decision, make_quote(), now)
    assert result.status == OrderStatus.CANCELED


def test_quote_instrument_mismatch_is_stale_market_data(engine, now):
    decision = make_decision()
    request = make_request()
    result = engine.submit(request, decision, make_quote(instrument="ETHUSDT-PERP"), now)
    assert result.reject_code == RejectCode.STALE_MARKET_DATA


def test_non_live_quote_is_stale_market_data(engine, now):
    from data.models import DataQuality

    decision = make_decision()
    request = make_request()
    result = engine.submit(request, decision, make_quote(quality=DataQuality.DELAYED), now)
    assert result.reject_code == RejectCode.STALE_MARKET_DATA


def test_stale_quote_timestamp_is_stale_market_data(engine, now):
    decision = make_decision()
    request = make_request()
    stale_quote = make_quote(timestamp=now - timedelta(seconds=30))
    result = engine.submit(request, decision, stale_quote, now)
    assert result.reject_code == RejectCode.STALE_MARKET_DATA


def test_wide_spread_is_spread_guard(engine, now):
    decision = make_decision()
    request = make_request()
    wide_quote = make_quote(bid="90", ask="110")
    result = engine.submit(request, decision, wide_quote, now)
    assert result.reject_code == RejectCode.SPREAD_GUARD


def test_reduce_only_with_no_position_is_violation(engine, now):
    decision = make_decision(quantity="1", side="sell", reduce_only=True)
    request = make_request(reduce_only=True, side=OrderSide.SELL)
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.REDUCE_ONLY_VIOLATION


def test_reduce_only_reducing_long_position_accepted(engine, now):
    open_decision = make_decision(decision_id="d-open", quantity="2")
    open_request = make_request(
        request_id="r-open", risk_decision_id="d-open", client_order_id="c-open", quantity="2"
    )
    engine.submit(open_request, open_decision, make_quote(), now)

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
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("1")


def test_reduce_only_same_direction_is_violation(engine, now):
    open_decision = make_decision(decision_id="d-open", quantity="2")
    open_request = make_request(
        request_id="r-open", risk_decision_id="d-open", client_order_id="c-open", quantity="2"
    )
    engine.submit(open_request, open_decision, make_quote(), now)

    bad_decision = make_decision(decision_id="d-bad", quantity="1", side="buy", reduce_only=True)
    bad_request = make_request(
        request_id="r-bad",
        risk_decision_id="d-bad",
        client_order_id="c-bad",
        side=OrderSide.BUY,
        quantity="1",
        reduce_only=True,
    )
    result = engine.submit(bad_request, bad_decision, make_quote(), now)
    assert result.reject_code == RejectCode.REDUCE_ONLY_VIOLATION


def test_engine_not_ready_rejects_non_reduce_only(config, portfolio, cost_schedule, now):
    from execution.paper import PaperExecutionEngine

    eng = PaperExecutionEngine(config, portfolio, cost_schedule)  # still RECONCILING
    decision = make_decision()
    request = make_request()
    result = eng.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.NOT_READY


def test_decision_side_mismatch_is_risk_mismatch(engine, now):
    decision = make_decision(side="buy")
    request = make_request(side=OrderSide.SELL)
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.RISK_MISMATCH
    assert "client-1" not in engine._orders


def test_decision_reduce_only_mismatch_rejects_non_reduce_request(engine, now):
    decision = make_decision(side="buy", reduce_only=True)
    request = make_request(side=OrderSide.BUY, reduce_only=False)
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.RISK_MISMATCH
    assert "client-1" not in engine._orders


def test_request_reduce_only_mismatch_rejects_non_reduce_decision(engine, now):
    decision = make_decision(side="sell", reduce_only=False)
    request = make_request(side=OrderSide.SELL, reduce_only=True)
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.RISK_MISMATCH
    assert "client-1" not in engine._orders


def test_decision_missing_side_is_risk_mismatch(engine, now):
    decision = make_decision(metadata={"side": None})
    request = make_request()
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.RISK_MISMATCH
    assert "client-1" not in engine._orders


def test_slippage_guard_rejects_market_order(engine, now):
    decision = make_decision(metadata={"reference_price": "100"})
    request = make_request()
    # Narrow spread (passes the spread guard) but far from the reference price,
    # so only the slippage guard should trip.
    wide_quote = make_quote(bid="104.9", ask="105")
    result = engine.submit(request, decision, wide_quote, now)
    assert result.status == OrderStatus.REJECTED
    assert INSTRUMENT not in engine.portfolio.positions


def test_ready_mode_without_reconciliation_does_not_admit_new_exposure(
    config, portfolio, cost_schedule, now
):
    """READY is a permission mode, not proof of reconciliation: a READY engine
    that never reconciled (or whose reconciliation state is not RECONCILED)
    must not admit ordinary exposure, even with a valid decision."""
    from execution.paper import EngineMode, PaperExecutionEngine
    from risk.models import ReconciliationState

    eng = PaperExecutionEngine(config, portfolio, cost_schedule)
    eng.mode = EngineMode.READY  # forced without any comparison
    assert eng.reconciliation_state is ReconciliationState.NOT_RECONCILED
    result = eng.submit(make_request(), make_decision(), make_quote(), now)
    assert result.reject_code == RejectCode.NOT_READY
    assert eng.reconciliation_state is ReconciliationState.NOT_RECONCILED

    eng.reconciliation_state = ReconciliationState.MISMATCH
    result2 = eng.submit(
        make_request(request_id="request-2"), make_decision(), make_quote(), now
    )
    assert result2.reject_code == RejectCode.NOT_READY


def test_resting_order_fill_is_deferred_when_ready_but_not_reconciled(engine, now):
    from execution.events import TradeEvent
    from execution.models import TimeInForce
    from risk.models import ReconciliationState

    request = make_request(
        order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, limit_price="99", quantity="3"
    )
    result = engine.submit(request, make_decision(quantity="3"), make_quote(), now)
    assert result.status == OrderStatus.ACCEPTED
    engine.reconciliation_state = ReconciliationState.MISMATCH  # READY mode, unreliable state

    engine.on_trade(
        TradeEvent(
            instrument=INSTRUMENT,
            timestamp=now,
            trade_id="t1",
            price=Decimal("98"),
            quantity=Decimal("5"),
        ),
        now,
    )

    assert engine._orders["client-1"].filled_quantity == Decimal("0")  # deferred, still resting
    assert engine.mode.value == "ready"
