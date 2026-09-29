"""Defense in depth: reduce-only admission needs a RECONCILED state; HALTED alone
does not prohibit an exit. Plus the OUTBOUND-vs-INBOUND invariant: broker truth
(report_fill) is always ingested, whatever the mode or reconciliation state."""

from decimal import Decimal

import pytest

from execution.events import TradeEvent
from execution.models import OrderSide, OrderType, TimeInForce
from execution.orders import HaltCode, OrderStatus, RejectCode
from execution.paper import EngineMode
from risk.models import ReconciliationSource, ReconciliationState

from .conftest import INSTRUMENT, make_decision, make_quote, make_request


def _open_position(engine, now, qty="2"):
    engine.submit(
        make_request(
            request_id="r-open", risk_decision_id="d-open", client_order_id="c-open", quantity=qty
        ),
        make_decision(decision_id="d-open", quantity=qty),
        make_quote(),
        now,
    )
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal(qty)


def _close(engine, now, request_id="r-close", client_id="c-close"):
    decision = make_decision(
        decision_id=f"d-{request_id}", quantity="1", side="sell", reduce_only=True
    )
    request = make_request(
        request_id=request_id,
        risk_decision_id=f"d-{request_id}",
        client_order_id=client_id,
        side=OrderSide.SELL,
        quantity="1",
        reduce_only=True,
    )
    return engine.submit(request, decision, make_quote(), now)


# -- reduce-only admission matrix -------------------------------------------


def test_ready_and_reconciled_allows_reduce_only(engine, now):
    _open_position(engine, now)
    assert engine.mode is EngineMode.READY
    assert engine.reconciliation_state is ReconciliationState.RECONCILED
    assert _close(engine, now).accepted is True


def test_halted_and_reconciled_allows_reduce_only(engine, now):
    _open_position(engine, now)
    engine._halt(HaltCode.OVERFILL, "halt that leaves reconciled state intact")
    assert (engine.mode, engine.reconciliation_state) == (
        EngineMode.HALTED,
        ReconciliationState.RECONCILED,
    )
    assert _close(engine, now).accepted is True


@pytest.mark.parametrize(
    "state",
    [
        ReconciliationState.NOT_RECONCILED,
        ReconciliationState.RECONCILING,
        ReconciliationState.MISMATCH,
    ],
)
@pytest.mark.parametrize("mode", [EngineMode.READY, EngineMode.HALTED])
def test_any_non_reconciled_state_blocks_reduce_only_in_any_mode(engine, now, state, mode):
    _open_position(engine, now)
    engine.mode = mode
    engine.reconciliation_state = state
    result = _close(engine, now)
    assert result.accepted is False
    assert result.reject_code == RejectCode.NOT_READY


@pytest.mark.parametrize(
    "code",
    [HaltCode.UNKNOWN_ORDER, HaltCode.INTERNAL_ERROR, HaltCode.RECONCILIATION_MISMATCH],
)
def test_state_unreliable_halts_block_reduce_only(engine, now, code):
    _open_position(engine, now)
    engine._halt(code, "corrupt/unknown state")
    assert engine.reconciliation_state is ReconciliationState.MISMATCH
    assert _close(engine, now).reject_code == RejectCode.NOT_READY


def test_mismatch_via_reconcile_blocks_reduce_only_until_a_clean_reconcile(engine, now):
    _open_position(engine, now)
    assert engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, now) is False
    assert _close(engine, now).reject_code == RejectCode.NOT_READY
    venue = {"orders": {}, "positions": {INSTRUMENT: "2"}}
    assert engine.resume_after_reconcile(venue, now) is True
    # a fresh request id: the rejected first attempt is idempotently cached
    assert _close(engine, now, request_id="r-close2", client_id="c-close2").accepted is True


def test_reduce_only_cancel_replace_blocked_when_unreconciled(engine, now):
    _open_position(engine, now)
    decision = make_decision(decision_id="d-c", quantity="1", side="sell", reduce_only=True)
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
        decision,
        make_quote(),
        now,
    )
    engine.reconciliation_state = ReconciliationState.MISMATCH
    result = engine.cancel_replace("c-rest", "c-rest-r1", now, new_price=Decimal("119"))
    assert result.accepted is False and result.reject_code == RejectCode.NOT_READY


# -- inbound broker truth is always ingested --------------------------------


def _rest_buy(engine, now):
    engine.submit(
        make_request(
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            limit_price="99",
            quantity="3",
        ),
        make_decision(quantity="3"),
        make_quote(),
        now,
    )
    assert engine._orders["client-1"].status == OrderStatus.ACCEPTED


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        (EngineMode.RECONCILING, ReconciliationState.NOT_RECONCILED),
        (EngineMode.RECONCILING, ReconciliationState.RECONCILING),
        (EngineMode.HALTED, ReconciliationState.MISMATCH),
        (EngineMode.HALTED, ReconciliationState.RECONCILED),
        (EngineMode.READY, ReconciliationState.MISMATCH),
    ],
)
def test_reported_broker_fill_is_ingested_in_every_mode_and_state(engine, now, mode, state):
    _rest_buy(engine, now)
    engine.mode = mode
    engine.reconciliation_state = state

    engine.report_fill("client-1", "broker-trade-1", Decimal("98"), Decimal("1"), now)

    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("1")
    # duplicate delivery of the same broker fill is still idempotent
    engine.report_fill("client-1", "broker-trade-1", Decimal("98"), Decimal("1"), now)
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("1")


def test_simulated_matching_of_a_resting_order_is_still_deferred_when_unreconciled(engine, now):
    """The paper venue's own matching decision (on_trade) is not broker truth; it
    stays gated (unchanged behavior) while broker-reported fills never are."""
    _rest_buy(engine, now)
    engine.reconciliation_state = ReconciliationState.MISMATCH
    engine.on_trade(
        TradeEvent(
            instrument=INSTRUMENT,
            timestamp=now,
            trade_id="sim-1",
            price=Decimal("98"),
            quantity=Decimal("5"),
        ),
        now,
    )
    assert engine._orders["client-1"].filled_quantity == Decimal("0")


def test_fill_for_unknown_order_is_flagged_not_silently_dropped(engine, now):
    engine.report_fill("nope", "t-1", Decimal("98"), Decimal("1"), now)
    assert engine.mode is EngineMode.HALTED
    assert engine.reconciliation_state is ReconciliationState.MISMATCH


def test_venue_reconciled_requires_a_venue_source(engine, now):
    assert engine.reconciliation_source is ReconciliationSource.VENUE_SNAPSHOT
    assert engine.venue_reconciled is True
    engine.reconcile(
        {"orders": {}, "positions": {}}, now, source=ReconciliationSource.PAPER_SELF_CHECK
    )
    assert engine.reconciliation_state is ReconciliationState.RECONCILED
    assert engine.venue_reconciled is False
