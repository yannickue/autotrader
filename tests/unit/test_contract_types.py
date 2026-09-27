from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from data.models import DataQuality, MarketSnapshot
from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from risk.models import RiskDecision
from signals.models import Direction, Signal

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_market_snapshot_rejects_crossed_quote() -> None:
    with pytest.raises(ValueError, match="bid cannot exceed ask"):
        MarketSnapshot(
            instrument="BTCUSDT-PERP",
            timestamp=NOW,
            bid=Decimal("101"),
            ask=Decimal("100"),
            last=Decimal("100.5"),
            volume=Decimal("12"),
            volatility=Decimal("0.02"),
            liquidity=Decimal("1000000"),
            source="venue-a",
            quality=DataQuality.LIVE,
        )


def test_signal_rejects_confidence_outside_unit_interval() -> None:
    with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
        Signal(
            signal_id="signal-1",
            instrument="BTCUSDT-PERP",
            direction=Direction.LONG,
            timestamp=NOW,
            strategy_id="momentum-v1",
            entry_zone=(Decimal("99"), Decimal("100")),
            invalidation_level=Decimal("97"),
            expected_move=Decimal("0.03"),
            expected_horizon=timedelta(minutes=15),
            confidence=Decimal("1.01"),
            metadata={},
        )


def test_risk_decision_rejects_leverage_above_configured_limit() -> None:
    with pytest.raises(ValueError, match="leverage cannot exceed max_leverage"):
        RiskDecision(
            decision_id="risk-1",
            signal_id="signal-1",
            instrument="BTCUSDT-PERP",
            timestamp=NOW,
            approved=True,
            reason="within risk budget",
            quantity=Decimal("0.02"),
            notional=Decimal("2000"),
            leverage=Decimal("6"),
            max_leverage=Decimal("5"),
            risk_budget=Decimal("10"),
            stop_price=Decimal("98000"),
            metadata={},
        )


def test_risk_decision_rejects_configured_limit_above_twenty() -> None:
    with pytest.raises(ValueError, match="max_leverage cannot exceed system maximum"):
        RiskDecision(
            decision_id="risk-1",
            signal_id="signal-1",
            instrument="BTCUSDT-PERP",
            timestamp=NOW,
            approved=True,
            reason="within risk budget",
            quantity=Decimal("0.02"),
            notional=Decimal("2000"),
            leverage=Decimal("20"),
            max_leverage=Decimal("21"),
            risk_budget=Decimal("10"),
            stop_price=Decimal("98000"),
            metadata={},
        )


def test_execution_request_requires_limit_price_for_limit_order() -> None:
    with pytest.raises(ValueError, match="limit_price is required for limit orders"):
        ExecutionRequest(
            request_id="request-1",
            risk_decision_id="risk-1",
            instrument="BTCUSDT-PERP",
            timestamp=NOW,
            side=OrderSide.BUY,
            quantity=Decimal("0.02"),
            order_type=OrderType.LIMIT,
            limit_price=None,
            reduce_only=False,
            client_order_id="client-1",
            time_in_force=TimeInForce.GTC,
            metadata={},
        )
