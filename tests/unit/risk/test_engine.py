from datetime import UTC, datetime, timedelta
from decimal import Decimal

from data.models import DataQuality, MarketSnapshot
from risk.engine import RiskEngine
from risk.models import (
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    RiskPolicy,
    RiskReason,
    RiskSide,
    RuntimeMode,
    RuntimeRiskState,
)


NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _policy(**changes: object) -> RiskPolicy:
    values: dict[str, object] = {
        "policy_id": "risk-v1",
        "risk_fraction": Decimal("0.01"),
        "max_leverage": Decimal("10"),
        "max_gross_notional": Decimal("10000"),
        "max_net_notional": Decimal("5000"),
        "max_daily_loss": Decimal("100"),
        "max_drawdown": Decimal("200"),
        "max_consecutive_losses": 3,
        "liquidity_fraction": Decimal("0.10"),
        "max_data_age": timedelta(seconds=5),
    }
    values.update(changes)
    return RiskPolicy(**values)


def _limits(**changes: object) -> InstrumentRiskLimits:
    values: dict[str, object] = {
        "instrument": "BTCUSDT-PERP",
        "max_leverage": Decimal("8"),
        "quantity_step": Decimal("0.1"),
        "min_quantity": Decimal("0.1"),
        "min_notional": Decimal("10"),
        "max_notional": Decimal("4000"),
        "max_spread_bps": Decimal("250"),
    }
    values.update(changes)
    return InstrumentRiskLimits(**values)


def _account(**changes: object) -> AccountRiskState:
    values: dict[str, object] = {
        "state_version": "account-1",
        "known": True,
        "reconciled": True,
        "equity": Decimal("1000"),
        "peak_equity": Decimal("1000"),
        "realized_pnl_today": Decimal("0"),
        "unrealized_pnl": Decimal("0"),
        "gross_notional": Decimal("0"),
        "net_notional": Decimal("0"),
        "instrument_notionals": {},
        "positions": {},
        "leverage_cap": Decimal("5"),
        "consecutive_losses": 0,
    }
    values.update(changes)
    return AccountRiskState(**values)


def _runtime(**changes: object) -> RuntimeRiskState:
    values: dict[str, object] = {
        "state_version": "runtime-1",
        "mode": RuntimeMode.READY,
        "risk_ready": True,
        "kill_switch": False,
    }
    values.update(changes)
    return RuntimeRiskState(**values)


def _snapshot(**changes: object) -> MarketSnapshot:
    values: dict[str, object] = {
        "instrument": "BTCUSDT-PERP",
        "timestamp": NOW,
        "bid": Decimal("99"),
        "ask": Decimal("101"),
        "last": Decimal("100"),
        "volume": Decimal("1000"),
        "volatility": Decimal("0.02"),
        "liquidity": Decimal("1"),
        "source": "venue-a",
        "quality": DataQuality.LIVE,
    }
    values.update(changes)
    return MarketSnapshot(**values)


def _request(**changes: object) -> PositionSizingRequest:
    values: dict[str, object] = {
        "signal_id": "signal-1",
        "instrument": "BTCUSDT-PERP",
        "timestamp": NOW,
        "side": RiskSide.BUY,
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "confidence": Decimal("0.2"),
        "available_liquidity_notional": Decimal("10000"),
        "metadata": {"signal_version": "v1"},
    }
    values.update(changes)
    return PositionSizingRequest(**values)


def test_sizes_linear_usdm_by_risk_and_rounds_down_to_step() -> None:
    decision = RiskEngine(_policy()).evaluate(
        request=_request(),
        snapshot=_snapshot(),
        account=_account(),
        runtime=_runtime(),
        instrument=_limits(),
        now=NOW,
    )

    assert decision.approved is True
    assert decision.reason_code is RiskReason.APPROVED
    assert decision.quantity == Decimal("2.0")
    assert decision.notional == Decimal("200.0")
    assert decision.risk_budget == Decimal("10.00")
    assert decision.max_leverage == Decimal("5")
    assert decision.leverage == Decimal("0.2")
    assert decision.stop_price == Decimal("95")

