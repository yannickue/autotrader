"""Data contracts for margin requirement and liquidation-safety estimation.

PRECISION HONESTY: every price this module derives (liquidation price,
buffered liquidation price) is an ESTIMATE from a simplified isolated-margin
formula, never an exact venue computation. `LiquidationEstimate.is_estimate`
and `MarginSafetyResult.is_estimate` are always True and must stay that way --
callers must never present these numbers as exact venue truth. See
`margin.engine` module docstring for the formula and its known omissions
(funding, fees, mark-price basis, tiered maintenance-margin schedules,
insurance-fund mechanics).
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from risk.models import MAX_SYSTEM_LEVERAGE

ZERO = Decimal("0")
ONE = Decimal("1")


class MarginPositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class MarginSafetyReason(StrEnum):
    SAFE = "SAFE"
    STOP_TOO_CLOSE_TO_LIQUIDATION = "STOP_TOO_CLOSE_TO_LIQUIDATION"
    STOP_BEYOND_LIQUIDATION = "STOP_BEYOND_LIQUIDATION"


def _validate_positive(value: Decimal, field: str) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be finite and positive")


def _validate_non_negative(value: Decimal, field: str) -> None:
    if not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be finite and non-negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class MaintenanceMarginPolicy:
    """Configured maintenance-margin-rate assumption used for liquidation
    estimation.

    `maintenance_margin_rate` is a fraction of notional (e.g. Decimal("0.005")
    == 0.5%) that this module ASSUMES applies at the position's leverage.
    Real venues use tiered maintenance-margin schedules that increase with
    notional and vary per instrument; if the caller has the venue's exact
    tier table, it should resolve the correct rate before constructing this
    policy. This module does not fetch or embed any venue-specific schedule.
    """

    maintenance_margin_rate: Decimal

    def __post_init__(self) -> None:
        if not self.maintenance_margin_rate.is_finite() or not (
            ZERO <= self.maintenance_margin_rate < ONE
        ):
            raise ValueError("maintenance_margin_rate must be finite and in [0, 1)")


@dataclass(frozen=True, slots=True, kw_only=True)
class VenueUncertaintyBuffer:
    """Explicit safety buffer added because the exact venue liquidation
    formula (mark-price basis, funding accrual, insurance-fund haircuts,
    tiered maintenance margin, cross- vs isolated-margin mechanics) is not
    precisely known and may differ from this module's simplified estimate.

    `buffer_bps` widens the estimated liquidation zone toward the entry price
    (i.e. makes liquidation look CLOSER than the raw formula computes), which
    is the conservative direction for a safety check. A larger buffer must
    never make an unsafe stop look safer -- see `margin.engine.MarginEngine`.
    """

    buffer_bps: Decimal

    def __post_init__(self) -> None:
        _validate_non_negative(self.buffer_bps, "buffer_bps")


@dataclass(frozen=True, slots=True, kw_only=True)
class MarginRequirement:
    """Initial margin needed to open `notional` at `leverage`."""

    notional: Decimal
    leverage: Decimal
    initial_margin: Decimal

    def __post_init__(self) -> None:
        _validate_positive(self.notional, "notional")
        _validate_positive(self.leverage, "leverage")
        _validate_positive(self.initial_margin, "initial_margin")


@dataclass(frozen=True, slots=True, kw_only=True)
class MaintenanceMarginRequirement:
    """Minimum margin required to keep a `notional` position open."""

    notional: Decimal
    maintenance_margin: Decimal

    def __post_init__(self) -> None:
        _validate_positive(self.notional, "notional")
        _validate_non_negative(self.maintenance_margin, "maintenance_margin")


@dataclass(frozen=True, slots=True, kw_only=True)
class LiquidationEstimate:
    """Estimated (never exact) liquidation price for a leveraged position.

    Computed from a simplified isolated-margin formula --
        LONG:  liquidation_price = entry_price * (1 - 1/leverage + maintenance_margin_rate)
        SHORT: liquidation_price = entry_price * (1 + 1/leverage - maintenance_margin_rate)
    -- ignoring fees, funding accrual, mark-vs-last price basis, tiered
    maintenance-margin schedules, and insurance-fund mechanics.
    `buffered_liquidation_price` additionally applies the configured
    `VenueUncertaintyBuffer` in the conservative direction (toward entry).
    `is_estimate` is always True; this type must never be constructed to
    represent an exact venue liquidation price.
    """

    side: MarginPositionSide
    entry_price: Decimal
    leverage: Decimal
    maintenance_margin_rate: Decimal
    raw_liquidation_price: Decimal
    buffered_liquidation_price: Decimal
    is_estimate: bool = True

    def __post_init__(self) -> None:
        _validate_positive(self.entry_price, "entry_price")
        _validate_positive(self.leverage, "leverage")
        _validate_non_negative(self.maintenance_margin_rate, "maintenance_margin_rate")
        _validate_non_negative(self.raw_liquidation_price, "raw_liquidation_price")
        _validate_non_negative(self.buffered_liquidation_price, "buffered_liquidation_price")
        if not self.is_estimate:
            raise ValueError(
                "LiquidationEstimate.is_estimate must be True: this module never "
                "computes an exact venue liquidation price"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class MarginSafetyResult:
    """Whether a strategy's stop/invalidation level is safely clear of the
    estimated, buffered liquidation price.

    This is the explicit boolean/reason-code contract a later integration
    step in `RiskEngine._evaluate_inner` must check before approving a
    decision: `safe=False` means the decision MUST NOT be treated as safe by
    any caller. `is_estimate` is always True (see `LiquidationEstimate`).
    """

    safe: bool
    reason_code: MarginSafetyReason
    liquidation_estimate: LiquidationEstimate
    stop_price: Decimal
    distance_bps: Decimal
    distance_ratio: Decimal
    min_required_distance_bps: Decimal
    is_estimate: bool = True

    def __post_init__(self) -> None:
        _validate_positive(self.stop_price, "stop_price")
        if not self.distance_bps.is_finite():
            raise ValueError("distance_bps must be finite")
        if not self.distance_ratio.is_finite():
            raise ValueError("distance_ratio must be finite")
        _validate_non_negative(self.min_required_distance_bps, "min_required_distance_bps")
        if not self.is_estimate:
            raise ValueError("MarginSafetyResult.is_estimate must be True")
        if self.safe and self.reason_code != MarginSafetyReason.SAFE:
            raise ValueError("safe=True requires reason_code SAFE")
        if not self.safe and self.reason_code == MarginSafetyReason.SAFE:
            raise ValueError("safe=False requires a non-SAFE reason_code")


@dataclass(frozen=True, slots=True, kw_only=True)
class VolatilityLeverageCapPolicy:
    """Suggested dynamic leverage cap that shrinks as volatility rises.

    This is layered ON TOP OF -- and can only be more restrictive than -- the
    hard `risk.models.MAX_SYSTEM_LEVERAGE` ceiling (30x) and any stricter cap
    a caller configures elsewhere (e.g. an instrument/account/policy leverage
    limit already enforced by `risk.engine`). It never raises the effective
    cap above either; see `MarginEngine.volatility_adjusted_leverage_cap`.
    """

    base_cap: Decimal
    volatility_sensitivity: Decimal
    min_cap: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        _validate_positive(self.base_cap, "base_cap")
        if self.base_cap > MAX_SYSTEM_LEVERAGE:
            raise ValueError("base_cap cannot exceed MAX_SYSTEM_LEVERAGE")
        _validate_non_negative(self.volatility_sensitivity, "volatility_sensitivity")
        _validate_positive(self.min_cap, "min_cap")
        if self.min_cap > self.base_cap:
            raise ValueError("min_cap cannot exceed base_cap")
