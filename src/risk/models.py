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

    HALTED = "HALTED"
    RUNTIME_NOT_READY = "RUNTIME_NOT_READY"
    ACCOUNT_UNKNOWN = "ACCOUNT_UNKNOWN"
    ACCOUNT_UNRECONCILED = "ACCOUNT_UNRECONCILED"
    INVALID_INPUT = "INVALID_INPUT"
    EQUITY_NON_POSITIVE = "EQUITY_NON_POSITIVE"
    INSTRUMENT_MISMATCH = "INSTRUMENT_MISMATCH"
    DATA_NOT_LIVE = "DATA_NOT_LIVE"
    DATA_STALE = "DATA_STALE"
    SIGNAL_STALE = "SIGNAL_STALE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    ENTRY_PRICE_DEVIATION = "ENTRY_PRICE_DEVIATION"
    INVALID_STOP = "INVALID_STOP"
    VOLATILITY_TOO_HIGH = "VOLATILITY_TOO_HIGH"
    DATA_MISSING = "DATA_MISSING"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    CONSECUTIVE_LOSS_LIMIT = "CONSECUTIVE_LOSS_LIMIT"
    LIQUIDITY_INSUFFICIENT = "LIQUIDITY_INSUFFICIENT"
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
    SIZE_BELOW_MINIMUM = "SIZE_BELOW_MINIMUM"
    RISK_ERROR = "RISK_ERROR"
    MARGIN_STOP_TOO_CLOSE_TO_LIQUIDATION = "MARGIN_STOP_TOO_CLOSE_TO_LIQUIDATION"
    MARGIN_STOP_BEYOND_LIQUIDATION = "MARGIN_STOP_BEYOND_LIQUIDATION"

    # Reduce-only specific
    NO_POSITION = "NO_POSITION"
    SIDE_MISMATCH = "SIDE_MISMATCH"
    QUANTITY_INVALID = "QUANTITY_INVALID"


def _validate_fraction(value: Decimal, field: str) -> None:
    if not value.is_finite() or not Decimal("0") < value <= Decimal("1"):
        raise ValueError(f"{field} must be finite and in (0, 1]")


def _validate_positive(value: Decimal, field: str) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be finite and positive")


def _validate_leverage_ceiling(value: Decimal, field: str) -> None:
    if value <= 0 or value > MAX_SYSTEM_LEVERAGE:
        raise ValueError(f"{field} must be in (0, {MAX_SYSTEM_LEVERAGE}]")


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
    max_signal_age: timedelta = timedelta(seconds=5)
    max_volatility: Decimal | None = None
    estimated_cost_bps: Decimal = Decimal("10")
    reduce_risk_multiplier: Decimal = Decimal("0.5")
    reduce_risk_drawdown_fraction: Decimal = Decimal("0.5")
    reduce_risk_daily_loss_fraction: Decimal = Decimal("0.5")
    reduce_risk_after_losses: int = 2
    # -- risk_reference_price contract (docs/OPEN_QUESTIONS.md #23) --------
    # Sizing, exposure caps, leverage, margin, and liquidation-distance math must
    # use the executable market reference price (ask for BUY, bid for SELL) plus
    # a slippage buffer, never the strategy-supplied request.entry_price
    # directly. request.entry_price is validated against that reference with a
    # dynamic tolerance (never a single fixed global bps constant) derived from
    # a configured floor, the current spread, and the current volatility.
    reference_price_slippage_bps: Decimal = Decimal("5")
    reference_price_min_tolerance_bps: Decimal = Decimal("20")
    reference_price_spread_tolerance_multiplier: Decimal = Decimal("2")
    reference_price_volatility_tolerance_multiplier: Decimal = Decimal("2")
    # -- margin/liquidation-safety contract (docs/OPEN_QUESTIONS.md #25, Q-M1) ---
    # Both are required, fail-closed configuration (never None-means-disabled):
    # liquidation_uncertainty_buffer_bps pads MarginEngine's simplified liquidation
    # estimate toward entry (conservative direction); min_stop_liquidation_distance_bps
    # is the minimum clearance a stop must keep from that buffered estimate.
    liquidation_uncertainty_buffer_bps: Decimal = Decimal("50")
    min_stop_liquidation_distance_bps: Decimal = Decimal("100")

    def __post_init__(self) -> None:
        _validate_leverage_ceiling(self.max_leverage, "max_leverage")
        for name in (
            "risk_fraction",
            "liquidity_fraction",
            "reduce_risk_multiplier",
            "reduce_risk_drawdown_fraction",
            "reduce_risk_daily_loss_fraction",
        ):
            _validate_fraction(getattr(self, name), name)
        for name in ("max_gross_notional", "max_net_notional", "max_daily_loss", "max_drawdown"):
            _validate_positive(getattr(self, name), name)
        if not self.estimated_cost_bps.is_finite() or self.estimated_cost_bps < 0:
            raise ValueError("estimated_cost_bps must be finite and non-negative")
        if self.max_volatility is not None:
            _validate_positive(self.max_volatility, "max_volatility")
        if self.max_consecutive_losses < 1 or self.reduce_risk_after_losses < 1:
            raise ValueError("loss-count limits must be at least 1")
        if self.max_data_age <= timedelta(0) or self.max_signal_age <= timedelta(0):
            raise ValueError("max_data_age and max_signal_age must be positive")
        for name in (
            "reference_price_slippage_bps",
            "reference_price_min_tolerance_bps",
            "reference_price_spread_tolerance_multiplier",
            "reference_price_volatility_tolerance_multiplier",
            "liquidation_uncertainty_buffer_bps",
            "min_stop_liquidation_distance_bps",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentRiskLimits:
    instrument: str
    max_leverage: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    max_notional: Decimal
    max_spread_bps: Decimal
    # Per-instrument maintenance-margin-rate assumption fed into
    # margin.models.MaintenanceMarginPolicy for the liquidation-safety check
    # (docs/OPEN_QUESTIONS.md #25, Q-M1) -- venue/instrument-specific, so it
    # lives here rather than on the shared RiskPolicy.
    maintenance_margin_rate: Decimal

    def __post_init__(self) -> None:
        _validate_leverage_ceiling(self.max_leverage, "max_leverage")
        if not self.maintenance_margin_rate.is_finite() or not (
            Decimal("0") <= self.maintenance_margin_rate < Decimal("1")
        ):
            raise ValueError("maintenance_margin_rate must be finite and in [0, 1)")


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
        if self.leverage < 0:
            raise ValueError("leverage cannot be negative")
        if self.quantity < 0:
            raise ValueError("quantity cannot be negative")
        if self.notional < 0:
            raise ValueError("notional cannot be negative")
        if self.approved and self.quantity <= 0:
            raise ValueError("approved decisions must have positive quantity")
        if not self.approved and (self.quantity != 0 or self.notional != 0):
            raise ValueError("rejected decisions must have zero quantity and notional")
