"""Configuration, inputs, and outputs for the deterministic exit engine.

This module is intentionally standalone: it has no dependency on `src/pipeline`
or `src/execution`, and nothing here calls a venue API. It imports only the
`RiskSide` type from `src/risk/models.py` so an `ExitDecision`'s closing side
plugs directly into `RiskEngine.evaluate_reduce_only(side=...)` without the
caller having to translate types at the integration boundary.

Every trigger here reads externally supplied, already-computed market/position
state (price, volatility, a momentum score, a reversal flag, a liquidity
figure). The engine never computes indicators, never calls an LLM or remote
reasoning service, and never invents a hardcoded monetary threshold -- all
thresholds are configured on `ExitPolicy` in relative terms (R-multiples,
basis points, fractions) so behavior is fully deterministic and reviewable.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from risk.models import RiskSide

ZERO = Decimal("0")


class PositionSide(StrEnum):
    """Directional side of the open position the exit engine is managing."""

    LONG = "long"
    SHORT = "short"


class StopStage(StrEnum):
    """Which rule last set the position's active protective stop."""

    ORIGINAL = "original"
    BREAK_EVEN = "break_even"
    TRAILING = "trailing"


class ExitReason(StrEnum):
    """Machine-readable reason code for a terminal exit decision."""

    INVALIDATION_STOP = "INVALIDATION_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    MOMENTUM_DETERIORATION = "MOMENTUM_DETERIORATION"
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"
    TIME_STOP = "TIME_STOP"
    LIQUIDITY_DETERIORATION = "LIQUIDITY_DETERIORATION"
    EMERGENCY_RISK_EXIT = "EMERGENCY_RISK_EXIT"


class ExitOutcome(StrEnum):
    """Terminal execution outcome of a submitted reduce-only exit request."""

    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


def _require_finite(name: str, value: Decimal) -> None:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


def _require_finite_positive(name: str, value: Decimal) -> None:
    _require_finite(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_finite_non_negative(name: str, value: Decimal) -> None:
    _require_finite(name, value)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _require_fraction(name: str, value: Decimal) -> None:
    _require_finite(name, value)
    if not (ZERO < value <= Decimal("1")):
        raise ValueError(f"{name} must be finite and in (0, 1]")


def _require_utc_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitPolicy:
    """Immutable, configured thresholds for every exit rule.

    Every threshold is relative (R-multiples of the initial stop distance,
    basis points, or fractions of the current position) rather than a fixed
    monetary amount, so a trade is never closed just because price crossed
    some arbitrary absolute number -- the design principle behind supporting
    "runners" (see `docs/OPEN_QUESTIONS.md` and `ExitEngine` docstring).
    """

    policy_id: str

    # -- break-even transition --------------------------------------------
    breakeven_trigger_r_multiple: Decimal
    breakeven_buffer_bps: Decimal = Decimal("0")

    # -- trailing stop ------------------------------------------------------
    trailing_activation_r_multiple: Decimal
    trailing_distance_volatility_multiplier: Decimal

    # -- target / partial profit taking -------------------------------------
    # If a position carries no `fixed_target_price`, a volatility-adjusted
    # target is derived as `entry +/- target_r_multiple * initial_risk` when
    # `target_r_multiple` is configured. Either way, reaching the target
    # takes a PARTIAL profit (`partial_take_profit_fraction` of the open
    # quantity) rather than closing the whole trade, so a strongly
    # continuing move is never cut short by a single fixed level.
    target_r_multiple: Decimal | None = None
    partial_take_profit_fraction: Decimal = Decimal("0.5")
    min_remaining_quantity: Decimal = ZERO
    quantity_step: Decimal = Decimal("0.00000001")

    # -- momentum / signal / liquidity ---------------------------------------
    momentum_deterioration_threshold: Decimal | None = None
    max_spread_bps: Decimal | None = None
    min_liquidity_notional: Decimal | None = None

    # -- time stop ------------------------------------------------------------
    max_holding_duration: timedelta | None = None

    # -- fail-closed market-data staleness -----------------------------------
    max_market_data_age: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        if not self.policy_id:
            raise ValueError("policy_id must be non-empty")

        _require_finite_non_negative(
            "breakeven_trigger_r_multiple", self.breakeven_trigger_r_multiple
        )
        _require_finite_non_negative("breakeven_buffer_bps", self.breakeven_buffer_bps)
        _require_finite_non_negative(
            "trailing_activation_r_multiple", self.trailing_activation_r_multiple
        )
        _require_finite_positive(
            "trailing_distance_volatility_multiplier",
            self.trailing_distance_volatility_multiplier,
        )
        if self.target_r_multiple is not None:
            _require_finite_positive("target_r_multiple", self.target_r_multiple)
        _require_fraction("partial_take_profit_fraction", self.partial_take_profit_fraction)
        _require_finite_non_negative("min_remaining_quantity", self.min_remaining_quantity)
        _require_finite_positive("quantity_step", self.quantity_step)
        if self.momentum_deterioration_threshold is not None:
            _require_finite(
                "momentum_deterioration_threshold", self.momentum_deterioration_threshold
            )
        if self.max_spread_bps is not None:
            _require_finite_non_negative("max_spread_bps", self.max_spread_bps)
        if self.min_liquidity_notional is not None:
            _require_finite_non_negative("min_liquidity_notional", self.min_liquidity_notional)
        if self.max_holding_duration is not None and self.max_holding_duration <= timedelta(0):
            raise ValueError("max_holding_duration must be positive when set")
        if self.max_market_data_age <= timedelta(0):
            raise ValueError("max_market_data_age must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitPosition:
    """Snapshot of an open position handed to the engine on every tick.

    Immutable by convention: `ExitEngine.evaluate()` never mutates this
    object. The caller builds the next tick's `ExitPosition` from the
    `updated_*` fields on the returned `ExitEvaluation`.
    """

    position_id: str
    instrument: str
    side: PositionSide
    entry_price: Decimal
    quantity: Decimal
    initial_stop_price: Decimal
    current_stop_price: Decimal
    stop_stage: StopStage = StopStage.ORIGINAL
    high_water_mark: Decimal | None = None
    fixed_target_price: Decimal | None = None
    opened_at: datetime
    realized_partial_quantity: Decimal = ZERO
    # Set by the caller once a close request for this position has been
    # submitted to risk/execution and is awaiting a terminal outcome, so the
    # engine never emits a second, overlapping close for the same position
    # (docs/OPEN_QUESTIONS.md #24 -- reduce-only reservations are per
    # decision_id and must not be double-submitted).
    pending_close_request_id: str | None = None

    def __post_init__(self) -> None:
        if not self.position_id:
            raise ValueError("position_id must be non-empty")
        if not self.instrument:
            raise ValueError("instrument must be non-empty")
        _require_finite_positive("entry_price", self.entry_price)
        _require_finite_non_negative("quantity", self.quantity)
        _require_finite_positive("initial_stop_price", self.initial_stop_price)
        _require_finite_positive("current_stop_price", self.current_stop_price)
        _require_finite_non_negative("realized_partial_quantity", self.realized_partial_quantity)
        if self.fixed_target_price is not None:
            _require_finite_positive("fixed_target_price", self.fixed_target_price)
        if self.high_water_mark is not None:
            _require_finite_positive("high_water_mark", self.high_water_mark)
        _require_utc_aware("opened_at", self.opened_at)
        if self.side == PositionSide.LONG:
            if self.initial_stop_price >= self.entry_price:
                raise ValueError("a long position's initial_stop_price must be below entry_price")
        elif self.initial_stop_price <= self.entry_price:
            raise ValueError("a short position's initial_stop_price must be above entry_price")

    @property
    def effective_high_water_mark(self) -> Decimal:
        """`high_water_mark`, defaulting to `entry_price` for a fresh position."""
        return self.high_water_mark if self.high_water_mark is not None else self.entry_price

    @property
    def initial_risk(self) -> Decimal:
        """Absolute per-unit distance between entry and the original stop (`1R`)."""
        return abs(self.entry_price - self.initial_stop_price)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitMarketState:
    """Externally supplied, already-computed market/microstructure state.

    `momentum_score` and `signal_reversal` are deterministic outputs of the
    strategy/feature layer, not something the exit engine derives itself --
    it only thresholds them, keeping the live hot path free of any LLM or
    remote reasoning dependency (repository `CLAUDE.md`).
    """

    instrument: str
    timestamp: datetime
    price: Decimal
    bid: Decimal
    ask: Decimal
    volatility: Decimal | None = None
    momentum_score: Decimal | None = None
    signal_reversal: bool = False
    risk_halt: bool = False
    available_liquidity_notional: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.instrument:
            raise ValueError("instrument must be non-empty")
        _require_finite_positive("price", self.price)
        _require_finite_positive("bid", self.bid)
        _require_finite_positive("ask", self.ask)
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        if self.volatility is not None:
            _require_finite_non_negative("volatility", self.volatility)
        if self.momentum_score is not None:
            _require_finite("momentum_score", self.momentum_score)
        if self.available_liquidity_notional is not None:
            _require_finite_non_negative(
                "available_liquidity_notional", self.available_liquidity_notional
            )
        _require_utc_aware("timestamp", self.timestamp)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitDecision:
    """A request to reduce (partially or fully close) a position.

    Mirrors the strategy/`Signal` boundary: this is intent only. It never
    touches a venue API. The caller is expected to route `close_side` and
    `quantity` into `RiskEngine.evaluate_reduce_only(request_id=request_id,
    ...)`, then submit the approved `RiskDecision` to execution, then call
    `ExitEngine.notify_terminal()` on the fill/cancel/reject outcome.
    """

    request_id: str
    position_id: str
    instrument: str
    close_side: RiskSide
    quantity: Decimal
    is_partial: bool
    reason: ExitReason
    reason_detail: str
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id must be non-empty")
        _require_finite_positive("quantity", self.quantity)
        _require_utc_aware("timestamp", self.timestamp)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitEvaluation:
    """Result of one `ExitEngine.evaluate()` tick.

    `decision` is `None` when no exit should happen this tick. The
    `updated_*` fields always reflect the (possibly ratcheted) stop/high
    -water-mark state regardless of whether a decision was emitted, and the
    caller folds them into the `ExitPosition` it passes on the next tick.
    """

    decision: ExitDecision | None
    updated_stop_price: Decimal
    updated_stop_stage: StopStage
    updated_high_water_mark: Decimal
    break_even_activated: bool
