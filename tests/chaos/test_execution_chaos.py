"""Chaos/failure tests for the paper execution engine: duplicates, reordering,
restart mid-fill, unknown-order fills, and reconciliation mismatches."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from data.models import DataQuality, MarketSnapshot
from execution.events import TradeEvent
from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from execution.orders import OrderStatus
from execution.paper import EngineMode, ExecutionConfig, PaperExecutionEngine
from portfolio.ledger import Portfolio
from risk.models import RiskDecision

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
INSTRUMENT = "BTCUSDT-PERP"


def make_config() -> ExecutionConfig:
    return ExecutionConfig(
        max_request_age=timedelta(seconds=5),
        max_decision_age=timedelta(seconds=5),
        max_quote_age=timedelta(seconds=5),
        max_order_age=timedelta(minutes=30),
        max_spread_bps=Decimal("50"),
        max_slippage_bps=Decimal("50"),
        slippage_bps=Decimal("2"),
    )


def make_quote() -> MarketSnapshot:
    return MarketSnapshot(
        instrument=INSTRUMENT,
        timestamp=NOW,
        bid=Decimal("100"),
        ask=Decimal("100.10"),
        last=Decimal("100"),
        volume=Decimal("1000"),
        volatility=None,
        liquidity=None,
        source="test",
        quality=DataQuality.LIVE,
    )


def make_decision(**overrides) -> RiskDecision:
    fields = {
        "decision_id": "decision-1",
        "signal_id": "signal-1",
        "instrument": INSTRUMENT,
        "timestamp": NOW,
        "approved": True,
        "reason": "ok",
        "quantity": Decimal("3"),
        "notional": Decimal("300"),
        "leverage": Decimal("1"),
        "max_leverage": Decimal("5"),
        "risk_budget": Decimal("1000"),
        "stop_price": None,
        "metadata": {"side": "buy", "reduce_only": False},
    }
    fields.update(overrides)
    return RiskDecision(**fields)


def make_request(**overrides) -> ExecutionRequest:
    fields = {
        "request_id": "request-1",
        "risk_decision_id": "decision-1",
        "instrument": INSTRUMENT,
        "timestamp": NOW,
        "side": OrderSide.BUY,
        "quantity": Decimal("3"),
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("99"),
        "reduce_only": False,
        "client_order_id": "client-1",
        "time_in_force": TimeInForce.GTC,
        "metadata": {},
    }
    fields.update(overrides)
    return ExecutionRequest(**fields)


def new_ready_engine() -> tuple[PaperExecutionEngine, Portfolio]:
    portfolio = Portfolio(starting_balance=Decimal("100000"))
    engine = PaperExecutionEngine(make_config(), portfolio)
    engine.reconcile({"orders": {}, "positions": {}}, NOW)
    return engine, portfolio


def test_duplicated_fill_events_never_double_count():
    engine, portfolio = new_ready_engine()
    engine.submit(make_request(), make_decision(), make_quote(), NOW)

    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=NOW,
        trade_id="dup-1",
        price=Decimal("98"),
        quantity=Decimal("2"),
    )
    engine.on_trade(event, NOW)
    engine.on_trade(event, NOW)
    engine.on_trade(event, NOW)

    order = engine._orders["client-1"]
    assert order.filled_quantity == Decimal("2")
    assert portfolio.positions[INSTRUMENT].quantity == Decimal("2")
    assert engine.mode == EngineMode.READY


def test_reordered_late_fill_after_cancel_is_booked_once():
    engine, portfolio = new_ready_engine()
    engine.submit(
        make_request(quantity=Decimal("1"), limit_price=Decimal("99")),
        make_decision(quantity=Decimal("1")),
        make_quote(),
        NOW,
    )
    engine.cancel("client-1", NOW)

    # The venue's fill report arrives *after* our cancel was processed locally
    # (a classic race/reordering scenario).
    engine.report_fill("client-1", "late-1", Decimal("99"), Decimal("1"), NOW)
    # And it is (incorrectly) redelivered by the transport a second time.
    engine.report_fill("client-1", "late-1", Decimal("99"), Decimal("1"), NOW)

    order = engine._orders["client-1"]
    assert order.status == OrderStatus.CANCELED  # status never regresses
    assert order.filled_quantity == Decimal("1")  # booked exactly once
    assert portfolio.positions[INSTRUMENT].quantity == Decimal("1")
    assert engine.mode == EngineMode.READY


def test_restart_mid_partial_fill_preserves_open_order_and_position():
    engine, portfolio = new_ready_engine()
    engine.submit(
        make_request(quantity=Decimal("3"), limit_price=Decimal("99")),
        make_decision(quantity=Decimal("3")),
        make_quote(),
        NOW,
    )
    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=NOW,
        trade_id="t1",
        price=Decimal("98"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, NOW)
    assert engine._orders["client-1"].status == OrderStatus.PARTIALLY_FILLED

    checkpoint = engine.export_checkpoint()

    # Simulate a process restart: brand-new engine sharing the same durable
    # portfolio state, importing the checkpoint, then re-reconciling.
    restarted = PaperExecutionEngine(make_config(), portfolio)
    restarted.import_checkpoint(checkpoint)
    restarted.mode = EngineMode.RECONCILING

    venue_orders = {cid: {} for cid, o in restarted._orders.items() if not o.is_terminal()}
    venue_positions = {i: str(v.quantity) for i, v in restarted.portfolio.positions.items()}
    ok = restarted.reconcile({"orders": venue_orders, "positions": venue_positions}, NOW)

    assert ok is True
    assert restarted.mode == EngineMode.READY
    restored_order = restarted._orders["client-1"]
    assert restored_order.status == OrderStatus.PARTIALLY_FILLED
    assert restored_order.filled_quantity == Decimal("1")
    assert restarted.portfolio.positions[INSTRUMENT].quantity == Decimal("1")

    # The restarted engine can keep filling the remainder of the same order.
    event2 = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=NOW,
        trade_id="t2",
        price=Decimal("98"),
        quantity=Decimal("5"),
    )
    restarted.on_trade(event2, NOW)
    assert restored_order.status == OrderStatus.FILLED
    assert restored_order.filled_quantity == Decimal("3")


def test_unknown_order_fill_halts_new_exposure():
    engine, _portfolio = new_ready_engine()
    engine.report_fill("ghost-order", "t1", Decimal("100"), Decimal("1"), NOW)
    assert engine.mode == EngineMode.HALTED

    # New (non-reduce-only) exposure is now refused.
    result = engine.submit(
        make_request(request_id="r2", client_order_id="c2"), make_decision(), make_quote(), NOW
    )
    assert result.accepted is False


def test_reconciliation_mismatch_halts_new_exposure():
    engine, _portfolio = new_ready_engine()
    engine.submit(make_request(), make_decision(), make_quote(), NOW)

    # Venue reports a position the local portfolio does not have.
    ok = engine.reconcile({"orders": {"client-1": {}}, "positions": {INSTRUMENT: "999"}}, NOW)
    assert ok is False
    assert engine.mode == EngineMode.HALTED

    result = engine.submit(
        make_request(request_id="r2", client_order_id="c2"), make_decision(), make_quote(), NOW
    )
    assert result.accepted is False


@pytest.mark.parametrize("order_of_events", [("cancel", "fill"), ("fill", "cancel")])
def test_overfill_via_reordered_events_halts(order_of_events):
    engine, _portfolio = new_ready_engine()
    engine.submit(
        make_request(quantity=Decimal("1"), limit_price=Decimal("99")),
        make_decision(quantity=Decimal("1")),
        make_quote(),
        NOW,
    )

    for action in order_of_events:
        if action == "cancel" and engine._orders["client-1"].status != OrderStatus.CANCELED:
            engine.cancel("client-1", NOW)
        elif action == "fill":
            engine.report_fill("client-1", "t-overfill", Decimal("99"), Decimal("5"), NOW)

    assert engine.mode == EngineMode.HALTED
