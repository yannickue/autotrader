from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from data.models import DataQuality, MarketSnapshot
from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from execution.paper import ExecutionConfig, PaperExecutionEngine
from portfolio.ledger import Portfolio
from risk.models import RiskDecision

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
INSTRUMENT = "BTCUSDT-PERP"


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def config() -> ExecutionConfig:
    return ExecutionConfig(
        max_request_age=timedelta(seconds=5),
        max_decision_age=timedelta(seconds=5),
        max_quote_age=timedelta(seconds=5),
        max_order_age=timedelta(minutes=30),
        max_spread_bps=Decimal("50"),
        max_slippage_bps=Decimal("50"),
        slippage_bps=Decimal("2"),
    )


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio(starting_balance=Decimal("100000"))


@pytest.fixture
def engine(config, portfolio, now) -> PaperExecutionEngine:
    eng = PaperExecutionEngine(config, portfolio)
    eng.reconcile({"orders": {}, "positions": {}}, now)
    return eng


def make_quote(
    *,
    instrument=INSTRUMENT,
    bid="100",
    ask="100.10",
    timestamp=NOW,
    quality=DataQuality.LIVE,
) -> MarketSnapshot:
    return MarketSnapshot(
        instrument=instrument,
        timestamp=timestamp,
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal(bid),
        volume=Decimal("1000"),
        volatility=None,
        liquidity=None,
        source="test",
        quality=quality,
    )


def make_decision(
    *,
    decision_id="decision-1",
    instrument=INSTRUMENT,
    timestamp=NOW,
    approved=True,
    quantity="1",
    stop_price=None,
    side="buy",
    reduce_only=False,
    metadata=None,
) -> RiskDecision:
    meta: dict = {"side": side, "reduce_only": reduce_only}
    if metadata:
        meta.update(metadata)
    return RiskDecision(
        decision_id=decision_id,
        signal_id="signal-1",
        instrument=instrument,
        timestamp=timestamp,
        approved=approved,
        reason="ok",
        quantity=Decimal(quantity),
        notional=Decimal(quantity) * Decimal("100"),
        leverage=Decimal("1"),
        max_leverage=Decimal("5"),
        risk_budget=Decimal("1000"),
        stop_price=Decimal(stop_price) if stop_price is not None else None,
        metadata=meta,
    )


def make_request(
    *,
    request_id="request-1",
    risk_decision_id="decision-1",
    instrument=INSTRUMENT,
    timestamp=NOW,
    side=OrderSide.BUY,
    quantity="1",
    order_type=OrderType.MARKET,
    limit_price=None,
    reduce_only=False,
    client_order_id="client-1",
    time_in_force=TimeInForce.IOC,
    metadata=None,
) -> ExecutionRequest:
    return ExecutionRequest(
        request_id=request_id,
        risk_decision_id=risk_decision_id,
        instrument=instrument,
        timestamp=timestamp,
        side=side,
        quantity=Decimal(quantity),
        order_type=order_type,
        limit_price=Decimal(limit_price) if limit_price is not None else None,
        reduce_only=reduce_only,
        client_order_id=client_order_id,
        time_in_force=time_in_force,
        metadata=metadata or {},
    )
