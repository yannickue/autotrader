"""Risk decisions that gate every exposure-increasing execution request."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

MAX_SYSTEM_LEVERAGE = Decimal("20")


class RiskSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class RuntimeMode(StrEnum):
    STARTING = "starting"
    RECONCILING = "reconciling"
    READY = "ready"
    DEGRADED = "degraded"
    HALTED = "halted"


class RiskReason(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskPolicy:
    policy_id: str
    risk_fraction: Decimal
    max_leverage: Decimal
    max_gross_notional: Decimal
    max_net_notional: Decimal
    max_daily_loss: Decimal
    max_drawdown: Decimal
    max_consecutive_losses: int
    liquidity_fraction: Decimal
    max_data_age: timedelta
    max_volatility: Decimal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentRiskLimits:
    instrument: str
    max_leverage: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    max_notional: Decimal
    max_spread_bps: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountRiskState:
    state_version: str
    known: bool
    reconciled: bool
    equity: Decimal
    peak_equity: Decimal
    realized_pnl_today: Decimal
    unrealized_pnl: Decimal
    gross_notional: Decimal
    net_notional: Decimal
    instrument_notionals: Mapping[str, Decimal]
    positions: Mapping[str, Decimal]
    leverage_cap: Decimal
    consecutive_losses: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeRiskState:
    state_version: str
    mode: RuntimeMode
    risk_ready: bool
    kill_switch: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionSizingRequest:
    signal_id: str
    instrument: str
    timestamp: datetime
    side: RiskSide
    entry_price: Decimal
    stop_price: Decimal
    confidence: Decimal
    available_liquidity_notional: Decimal
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskDecision:
    """Immutable approval or rejection emitted by the risk engine."""

    decision_id: str
    signal_id: str
    instrument: str
    timestamp: datetime
    approved: bool
    reason: str
    quantity: Decimal
    notional: Decimal
    leverage: Decimal
    max_leverage: Decimal
    risk_budget: Decimal
    stop_price: Decimal | None
    metadata: Mapping[str, Any]
    reason_code: RiskReason | str | None = None

    def __post_init__(self) -> None:
        if self.max_leverage > MAX_SYSTEM_LEVERAGE:
            raise ValueError("max_leverage cannot exceed system maximum of 20")
        if self.leverage > self.max_leverage:
            raise ValueError("leverage cannot exceed max_leverage")
