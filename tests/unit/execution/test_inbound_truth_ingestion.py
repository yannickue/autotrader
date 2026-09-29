"""Inbound broker truth is ingested faithfully; nothing is marked seen unless it was
actually booked; protective-order creation (outbound) waits for RECONCILED."""

from decimal import Decimal

from execution.models import OrderSide, OrderType, TimeInForce
from execution.orders import HaltCode, OrderStatus
from execution.paper import EngineMode
from portfolio.models import Fill
from risk.models import ReconciliationState

from .conftest import INSTRUMENT, make_decision, make_quote, make_request


def _rest_entry(engine, now, quantity="3", **meta):
    engine.submit(
        make_request(
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            limit_price="99",
            quantity=quantity,
            metadata=meta or None,
        ),
        make_decision(quantity=quantity, stop_price="90"),
        make_quote(),
        now,
    )
    assert engine._orders["client-1"].status == OrderStatus.ACCEPTED


# -- Critical: seen-key only after the fill is actually booked --------------


def test_unbookable_inbound_fill_is_not_marked_seen_and_flags_mismatch(engine, now):
    _rest_entry(engine, now, quantity="3")
    engine.report_fill("client-1", "big", Decimal("99"), Decimal("5"), now)  # > order quantity

    assert engine.mode is EngineMode.HALTED
    assert engine.reconciliation_state is ReconciliationState.MISMATCH  # NOT silently RECONCILED
    assert ("client-1", "big") not in engine._seen_fill_keys
    assert engine.portfolio.positions.get(INSTRUMENT) is None
    assert any(e["event"] == "inbound_fill_not_booked" for e in engine.audit_log)


def test_exception_while_booking_does_not_consume_the_fill_key(engine, now, monkeypatch):
    _rest_entry(engine, now, quantity="3")
    real = engine.portfolio.apply_fill
    calls = {"n": 0}

    def flaky(fill):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real(fill)

    monkeypatch.setattr(engine.portfolio, "apply_fill", flaky)
    engine.report_fill("client-1", "t-1", Decimal("99"), Decimal("1"), now)
    assert engine.mode is EngineMode.HALTED
    assert ("client-1", "t-1") not in engine._seen_fill_keys

    engine.report_fill("client-1", "t-1", Decimal("99"), Decimal("1"), now)  # venue re-delivers
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("1")
    assert ("client-1", "t-1") in engine._seen_fill_keys


# -- High: inbound reduce-only fills are booked as reported -----------------


def _open_and_rest_reduce_only(engine, now):
    engine.submit(
        make_request(
            request_id="r-open", risk_decision_id="d-open", client_order_id="c-open", quantity="2"
        ),
        make_decision(decision_id="d-open", quantity="2"),
        make_quote(),
        now,
    )
    engine.submit(
        make_request(
            request_id="r-c",
            risk_decision_id="d-c",
            client_order_id="c-rest",
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            limit_price="120",
            side=OrderSide.SELL,
            quantity="1",
            reduce_only=True,
        ),
        make_decision(decision_id="d-c", quantity="1", side="sell", reduce_only=True),
        make_quote(),
        now,
    )
    assert engine._orders["c-rest"].status == OrderStatus.ACCEPTED


def test_inbound_reduce_only_fill_within_position_is_booked_without_halt(engine, now):
    _open_and_rest_reduce_only(engine, now)
    engine.report_fill("c-rest", "b-1", Decimal("120"), Decimal("1"), now)
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("1")
    assert engine.mode is EngineMode.READY


def test_inbound_reduce_only_fill_exceeding_local_position_is_booked_in_full_then_flagged(
    engine, now
):
    _open_and_rest_reduce_only(engine, now)
    # The broker says the reduce-only order filled while our local position is already flat.
    engine.portfolio.apply_fill(  # local position closed by other means -> flat
        Fill(
            fill_id="manual-close",
            instrument=INSTRUMENT,
            side=OrderSide.SELL,
            quantity=Decimal("2"),
            price=Decimal("100"),
        )
    )
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("0")

    engine.report_fill("c-rest", "b-2", Decimal("120"), Decimal("1"), now)

    # Truth booked as reported (position is now short 1, as the broker says) ...
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("-1")
    # ... and the disagreement is flagged, not silently absorbed or clamped away.
    assert engine.mode is EngineMode.HALTED
    assert engine.reconciliation_state is ReconciliationState.MISMATCH
    assert HaltCode.RECONCILIATION_MISMATCH.value in (engine.halt_reason or "")


# -- High: protective order creation is an outbound action ------------------


def test_protective_orders_wait_for_reconciliation_then_are_created(engine, now):
    _rest_entry(engine, now, quantity="1", take_profit=Decimal("120"))
    engine.mode = EngineMode.RECONCILING
    engine.reconciliation_state = ReconciliationState.NOT_RECONCILED

    engine.report_fill("client-1", "t-1", Decimal("99"), Decimal("1"), now)  # inbound: booked
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("1")
    assert "client-1:stop" not in engine._orders  # no outbound order while unreconciled
    assert "client-1" in engine._protective_sync_pending
    assert "client-1" in engine.export_checkpoint()["protective_sync_pending"]

    assert engine.reconcile({"orders": {}, "positions": {INSTRUMENT: "1"}}, now) is True
    assert engine._orders["client-1:stop"].status == OrderStatus.ACCEPTED
    assert "client-1:take_profit" in engine._orders
    assert engine._protective_sync_pending == set()


def test_deferred_protective_sync_is_skipped_if_the_position_is_gone(engine, now):
    _rest_entry(engine, now, quantity="1")
    engine.mode = EngineMode.RECONCILING
    engine.reconciliation_state = ReconciliationState.NOT_RECONCILED
    engine.report_fill("client-1", "t-1", Decimal("99"), Decimal("1"), now)
    assert "client-1" in engine._protective_sync_pending

    engine.portfolio.apply_fill(
        Fill(
            fill_id="closed-elsewhere",
            instrument=INSTRUMENT,
            side=OrderSide.SELL,
            quantity=Decimal("1"),
            price=Decimal("100"),
        )
    )
    assert engine.reconcile({"orders": {}, "positions": {}}, now) is True
    assert "client-1:stop" not in engine._orders  # nothing left to protect


# -- simulated reduce-only/protective fills: deliberate, pinned behavior -----


def test_resting_protective_stop_still_fills_when_state_is_unreconciled(engine, now):
    """A resting protective stop is the paper stand-in for a broker-side stop: its
    fill only shrinks exposure and mirrors what a real venue would do regardless
    of our reconciliation state, so it is deliberately never deferred."""
    from execution.events import TradeEvent

    engine.submit(
        make_request(quantity="1"), make_decision(quantity="1", stop_price="90"), make_quote(), now
    )
    assert "client-1:stop" in engine._orders
    engine.reconciliation_state = ReconciliationState.MISMATCH
    engine.on_trade(
        TradeEvent(
            instrument=INSTRUMENT,
            timestamp=now,
            trade_id="stop-hit",
            price=Decimal("89"),
            quantity=Decimal("1"),
        ),
        now,
    )
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("0")


# -- partial-mutation exceptions cannot be reconciled away ------------------


def test_exception_after_booking_latches_and_reconcile_cannot_restore_ready(engine, now):
    _rest_entry(engine, now, quantity="3")

    def boom(_event):
        raise RuntimeError("listener failed after the portfolio booked the fill")

    engine.fill_listener = boom
    engine.report_fill("client-1", "t-1", Decimal("99"), Decimal("1"), now)

    assert engine.mode is EngineMode.HALTED
    assert engine.reconciliation_state is ReconciliationState.MISMATCH
    # Positions and open-order ids now "match" the broker, but internal order
    # accounting may be stale, so a position/order-id comparison must not clear it.
    venue = {"orders": {"client-1": True}, "positions": {INSTRUMENT: "1"}}
    assert engine.reconcile(venue, now) is False
    assert engine.reconciliation_state is ReconciliationState.MISMATCH
    assert engine.resume_after_reconcile(venue, now) is False

    # Recovery from durable state (a checkpoint import) is the only way out.
    checkpoint = engine.export_checkpoint()
    engine.import_checkpoint(checkpoint)
    assert engine.reconcile(venue, now) is True
