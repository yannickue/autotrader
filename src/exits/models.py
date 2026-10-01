# ruff: noqa: E501
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

# Documented marker for a stage list that deliberately has NO second target (TP1 + runner): a second
# target is only ever created from a structurally justified level (Lane E2). Absent such a
# level the remainder runs as a runner behind the tighten-only stop -- a TP2 is never invented.
SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED = "SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED"

STAGE_SOURCE_R = "R"
STAGE_SOURCE_STRUCTURE_PREFIX = "STRUCTURE:"


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
    # Lane E2 (chart-based management; every one is a STRATEGY exit, only EMERGENCY_RISK_EXIT is not):
    STRUCTURE_FAILURE = "STRUCTURE_FAILURE"  # a closed bar broke the latest confirmed post-entry swing
    MFE_GIVEBACK = "MFE_GIVEBACK"  # too much of a meaningful favourable excursion was given back
    LATE_SESSION_DETERIORATION = "LATE_SESSION_DETERIORATION"  # late-session loser with adverse momentum


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
class TakeProfitStage:
    """One ordered stage of a multi-stage take-profit ladder.

    The trigger is EXACTLY ONE of
      * `r_multiple` -- "offset from entry = initial_risk * r_multiple" (the legacy convention of
        `ExitPolicy.target_r_multiple`; `source` must be "R"), or
      * `target_price` -- an absolute chart price, e.g. a structural level
        (`source` must be "STRUCTURE:<id>"). R is computed from chart prices afterwards.

    `close_fraction` is a fraction of the position's ORIGINAL quantity (the quantity when the
    position first opened, i.e. `ExitPosition.quantity + realized_partial_quantity` at any
    later tick) to close when this stage triggers -- not a fraction of whatever remains open. It is
    family-configurable and never hardcoded. `stage_id` is an optional stable label for audit.
    """

    close_fraction: Decimal
    r_multiple: Decimal | None = None
    target_price: Decimal | None = None
    stage_id: str = ""
    source: str = STAGE_SOURCE_R

    def __post_init__(self) -> None:
        if (self.r_multiple is None) == (self.target_price is None):
            raise ValueError("a stage needs exactly one of r_multiple or target_price")
        if self.r_multiple is not None:
            _require_finite_positive("r_multiple", self.r_multiple)
            if self.source != STAGE_SOURCE_R:
                raise ValueError('an r_multiple stage must have source "R"')
        if self.target_price is not None:
            _require_finite_positive("target_price", self.target_price)
            tag = self.source[len(STAGE_SOURCE_STRUCTURE_PREFIX):]
            if not self.source.startswith(STAGE_SOURCE_STRUCTURE_PREFIX) or not tag:
                raise ValueError('a target_price stage needs source "STRUCTURE:<id>"')
        _require_fraction("close_fraction", self.close_fraction)


def stage_target_price(
    stage: TakeProfitStage, *, side: PositionSide, entry_price: Decimal, initial_risk: Decimal
) -> Decimal:
    """Absolute chart price at which `stage` triggers (R stages are converted from entry/risk)."""
    if stage.target_price is not None:
        return stage.target_price
    assert stage.r_multiple is not None
    offset = initial_risk * stage.r_multiple
    return entry_price + offset if side == PositionSide.LONG else entry_price - offset


def stop_is_unchanged_or_tighter(side: PositionSide, *, old: Decimal, new: Decimal) -> bool:
    """After entry a stop may only stay or move TOWARDS/past entry, never further away from it."""
    return new >= old if side == PositionSide.LONG else new <= old


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
    #
    # PRECEDENCE: when `take_profit_stages` is non-empty, it is used
    # EXCLUSIVELY for target/partial-profit decisions and
    # `target_r_multiple`/`fixed_target_price`/`partial_take_profit_fraction`
    # below are ignored for that purpose (they remain valid, independently
    # validated fields for backward compatibility with existing callers/
    # tests). When `take_profit_stages` is empty, `target_r_multiple`/
    # `fixed_target_price`/`partial_take_profit_fraction` are treated as an
    # implicit single-stage configuration, exactly matching this engine's
    # pre-multi-stage behavior.
    target_r_multiple: Decimal | None = None
    partial_take_profit_fraction: Decimal = Decimal("0.5")
    min_remaining_quantity: Decimal = ZERO
    quantity_step: Decimal = Decimal("0.00000001")
    take_profit_stages: tuple[TakeProfitStage, ...] = ()

    # -- momentum / signal / liquidity ---------------------------------------
    momentum_deterioration_threshold: Decimal | None = None
    max_spread_bps: Decimal | None = None
    min_liquidity_notional: Decimal | None = None

    # -- time stop ------------------------------------------------------------
    max_holding_duration: timedelta | None = None
    # Lane E2 time-ALPHA decay: when set, the time stop only fires if the trade never showed at least this
    # much favourable excursion (a trade that worked is not cut merely for its age; None = age only).
    time_stop_min_mfe_r: Decimal | None = None

    # -- Lane E2 chart-based management (all off by default: the legacy behaviour is unchanged) --------
    # cost-adjusted break-even also triggers once the FIRST target stage has been taken
    breakeven_after_first_stage: bool = False
    # ratchet the stop behind the newest confirmed post-entry swing (ExitMarketState.structure_trail_price)
    structure_trailing: bool = False
    # exit the remainder when a closed bar broke the latest confirmed post-entry swing against the trade
    structure_failure_exit: bool = False
    # Lane Y: when the structure trail moves the stop beyond entry, floor it at the cost-adjusted break-even (entry +/-
    # expected exit cost). NOT an independent break-even trigger: it only applies together with a valid new structure.
    structure_cost_floor: bool = False
    # give back at least this fraction of a favourable excursion of >= giveback_min_mfe_r -> exit
    max_giveback_fraction: Decimal | None = None
    giveback_min_mfe_r: Decimal = Decimal("1")
    # late session (``ExitMarketState.time_to_forced_flat <= late_window``): profitable trades lock the
    # cost-adjusted break-even; a LOSING trade whose momentum is <= the threshold exits early instead of
    # being held to the mandatory flat (the forced flat itself always wins and is not this engine's job)
    late_window: timedelta | None = None
    late_loser_momentum_threshold: Decimal | None = None

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
        if self.time_stop_min_mfe_r is not None:
            _require_finite_non_negative("time_stop_min_mfe_r", self.time_stop_min_mfe_r)
        if self.max_giveback_fraction is not None:
            _require_fraction("max_giveback_fraction", self.max_giveback_fraction)
        _require_finite_non_negative("giveback_min_mfe_r", self.giveback_min_mfe_r)
        if self.late_window is not None and self.late_window <= timedelta(0):
            raise ValueError("late_window must be positive when set")
        if self.late_loser_momentum_threshold is not None:
            _require_finite("late_loser_momentum_threshold", self.late_loser_momentum_threshold)

        if self.take_profit_stages:
            previous_r_multiple = ZERO
            cumulative_close_fraction = ZERO
            for stage in self.take_profit_stages:
                if not isinstance(stage, TakeProfitStage):
                    raise ValueError(
                        "take_profit_stages must contain only TakeProfitStage instances"
                    )
                if stage.r_multiple is None:
                    raise ValueError(
                        "ExitPolicy.take_profit_stages must use r_multiple stages; absolute "
                        "target_price stages are per position (ExitPosition.target_stages)"
                    )
                if stage.r_multiple <= previous_r_multiple:
                    raise ValueError(
                        "take_profit_stages must be given in strictly increasing "
                        "r_multiple order"
                    )
                previous_r_multiple = stage.r_multiple
                cumulative_close_fraction += stage.close_fraction
                if cumulative_close_fraction > Decimal("1"):
                    raise ValueError(
                        "take_profit_stages close_fraction values must not sum to more "
                        "than 1 (fractions are of the ORIGINAL position quantity)"
                    )


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
    # How many entries of `ExitPolicy.take_profit_stages` have already fired
    # (0 = none yet). Only advanced by the caller once a real fill for that
    # stage's reduce-only close has landed -- mirroring how `quantity`/
    # `realized_partial_quantity` are only ever updated from a real fill,
    # never from `ExitEvaluation.updated_*` (see `apply_evaluation`'s
    # docstring). Unused (stays 0) when `ExitPolicy.take_profit_stages` is
    # empty and the single-stage `target_r_multiple`/`fixed_target_price`
    # path is used instead.
    stages_completed: int = 0
    # Set by the caller once a close request for this position has been
    # submitted to risk/execution and is awaiting a terminal outcome, so the
    # engine never emits a second, overlapping close for the same position
    # (docs/OPEN_QUESTIONS.md #24 -- reduce-only reservations are per
    # decision_id and must not be double-submitted).
    pending_close_request_id: str | None = None
    # Per-position target ladder (Lane E). When non-empty it is used INSTEAD of
    # `ExitPolicy.take_profit_stages` for this position; stages may be R- or price-based and are
    # validated to be strictly ordered in the favourable direction beyond entry.
    target_stages: tuple[TakeProfitStage, ...] = ()

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
        if self.stages_completed < 0:
            raise ValueError("stages_completed must be non-negative")
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
        if not stop_is_unchanged_or_tighter(
            self.side, old=self.initial_stop_price, new=self.current_stop_price
        ):
            raise ValueError(
                "current_stop_price must be unchanged or tighter than initial_stop_price"
            )
        self._validate_target_stages()

    def _validate_target_stages(self) -> None:
        if not self.target_stages:
            return
        is_long = self.side == PositionSide.LONG
        previous: Decimal | None = None
        cumulative = ZERO
        for stage in self.target_stages:
            if not isinstance(stage, TakeProfitStage):
                raise ValueError("target_stages must contain only TakeProfitStage instances")
            price = stage_target_price(
                stage, side=self.side, entry_price=self.entry_price, initial_risk=self.initial_risk
            )
            if price <= 0 or (price <= self.entry_price if is_long else price >= self.entry_price):
                raise ValueError(
                    f"target stage {stage.stage_id or price} must lie in the favourable "
                    "direction beyond entry"
                )
            if previous is not None and (price <= previous if is_long else price >= previous):
                raise ValueError(
                    "target_stages must be strictly ordered in the favourable direction"
                )
            previous = price
            cumulative += stage.close_fraction
            if cumulative > Decimal("1"):
                raise ValueError(
                    "target_stages close_fraction values must not sum to more than 1 "
                    "(fractions of the ORIGINAL quantity)"
                )

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
    # Cheap per-position context the caller may supply (informational for decisions/logging; no rule
    # thresholds them yet): best favourable excursion so far in R, how much of it was given back in
    # R, and how long the position has been held.
    mfe_r: Decimal | None = None
    giveback_r: Decimal | None = None
    holding_seconds: Decimal | None = None
    # Lane E2 chart-based inputs (all optional; derived from closed bars by the caller, never by the engine):
    # expected cost of closing now in PRICE units (spread + fees) -> cost-adjusted break-even;
    expected_exit_cost: Decimal | None = None
    # tighter stop candidate behind the newest confirmed post-entry swing (long: below price; short: above);
    structure_trail_price: Decimal | None = None
    # a closed bar broke the latest confirmed post-entry swing against the trade;
    structure_failure: bool = False
    # time left until the mandatory forced flat (Lane P's deadline); None = unknown / no deadline
    time_to_forced_flat: timedelta | None = None

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
        if self.expected_exit_cost is not None:
            _require_finite_non_negative("expected_exit_cost", self.expected_exit_cost)
        if self.structure_trail_price is not None:
            _require_finite_positive("structure_trail_price", self.structure_trail_price)


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
