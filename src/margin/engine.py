"""Standalone margin requirement and liquidation-safety estimation engine.

LEGACY_RUNTIME / SHADOW_ORACLE (Nautilus convergence, see
docs/ARCHITECTURE_AUDIT_2026-09-29.md): retained as the paper/backtest
conservative gate and a parity oracle; do not add features except confirmed safety fixes.

This module is intentionally decoupled from `risk.engine` business logic --
it does not call into `RiskEngine` and `RiskEngine` does not yet call into
it -- so it can be built and tested in isolation. A later integration step
wires `MarginEngine.evaluate_stop_safety` into `RiskEngine._evaluate_inner`
as an additional pre-approval check, using the same `risk_reference_price`
(ask+slippage for BUY / bid-slippage for SELL, see `docs/RISK_CONTRACT.md`
"Risk reference price") as the `entry_price` passed into this module, so
margin/liquidation math stays consistent with how risk sizing already prices
a position. The only dependency this module takes on `risk` is the shared,
read-only `risk.models.MAX_SYSTEM_LEVERAGE` constant, so the two modules can
never silently diverge on what "the hard 20x ceiling" means.

PRECISION HONESTY (explicit repo policy -- do not violate it): every
liquidation price this module produces is an ESTIMATE derived from a
simplified isolated-margin formula, never an exact venue computation
(`LiquidationEstimate.is_estimate` / `MarginSafetyResult.is_estimate` are
always True). The formula used is:

    LONG:  liquidation_price = entry_price * (1 - 1/leverage + maintenance_margin_rate)
    SHORT: liquidation_price = entry_price * (1 + 1/leverage - maintenance_margin_rate)

This is the standard "wipe out (initial margin - maintenance margin)"
approximation for an isolated-margin linear position. It deliberately
ignores: trading fees, funding-rate accrual, mark-price-vs-last-price basis,
tiered/notional-scaled maintenance-margin schedules, and insurance-fund /
auto-deleveraging mechanics -- all of which shift a real venue's actual
liquidation price. Callers must never present these numbers as exact venue
truth, and must treat the configured `VenueUncertaintyBuffer` as mandatory
padding for that unknown-formula risk, not optional decoration.
"""

from decimal import Decimal

from margin.models import (
    LiquidationEstimate,
    MaintenanceMarginPolicy,
    MaintenanceMarginRequirement,
    MarginPositionSide,
    MarginRequirement,
    MarginSafetyReason,
    MarginSafetyResult,
    VenueUncertaintyBuffer,
    VolatilityLeverageCapPolicy,
)
from risk.models import MAX_SYSTEM_LEVERAGE

TEN_THOUSAND = Decimal("10000")
ZERO = Decimal("0")
ONE = Decimal("1")


def _finite(value: Decimal) -> bool:
    return not (value.is_nan() or value.is_infinite())


class MarginEngine:
    """Pure, stateless margin/liquidation-safety calculations.

    No I/O, no venue calls, no mutable state: every method is a deterministic
    function of its Decimal inputs, consistent with this repo's requirement
    that the live hot path be deterministic (see root `CLAUDE.md`).
    """

    # -- margin requirement abstractions -------------------------------------

    def initial_margin_requirement(
        self, *, notional: Decimal, leverage: Decimal
    ) -> MarginRequirement:
        """Initial margin needed to open `notional` at `leverage`."""
        if not _finite(notional) or notional <= 0:
            raise ValueError("notional must be finite and positive")
        if not _finite(leverage) or leverage <= 0:
            raise ValueError("leverage must be finite and positive")
        initial_margin = notional / leverage
        return MarginRequirement(
            notional=notional, leverage=leverage, initial_margin=initial_margin
        )

    def maintenance_margin_requirement(
        self, *, notional: Decimal, policy: MaintenanceMarginPolicy
    ) -> MaintenanceMarginRequirement:
        """Minimum margin required to keep a `notional` position open."""
        if not _finite(notional) or notional <= 0:
            raise ValueError("notional must be finite and positive")
        maintenance_margin = notional * policy.maintenance_margin_rate
        return MaintenanceMarginRequirement(
            notional=notional, maintenance_margin=maintenance_margin
        )

    # -- liquidation price estimate ------------------------------------------

    def estimate_liquidation_price(
        self,
        *,
        side: MarginPositionSide,
        entry_price: Decimal,
        leverage: Decimal,
        maintenance_policy: MaintenanceMarginPolicy,
        uncertainty_buffer: VenueUncertaintyBuffer,
    ) -> LiquidationEstimate:
        """Estimate (never exact, see module docstring) the price at which
        this position would be force-closed, given `entry_price`, `leverage`,
        and `maintenance_policy`. Applies `uncertainty_buffer` in the
        conservative direction (toward `entry_price`) to produce
        `buffered_liquidation_price`.
        """
        if not _finite(entry_price) or entry_price <= 0:
            raise ValueError("entry_price must be finite and positive")
        if not _finite(leverage) or leverage <= 0:
            raise ValueError("leverage must be finite and positive")

        maintenance_rate = maintenance_policy.maintenance_margin_rate
        # Fraction of entry price the position can lose before maintenance
        # margin is breached. Clamp at zero: if the configured maintenance
        # rate is at or above the initial-margin fraction (1/leverage), the
        # simplified formula would place "liquidation" on the wrong side of
        # entry -- treat that degenerate configuration as immediate/zero
        # loss tolerance rather than producing a nonsensical price.
        loss_fraction = max(ONE / leverage - maintenance_rate, ZERO)
        buffer_fraction = uncertainty_buffer.buffer_bps / TEN_THOUSAND

        if side == MarginPositionSide.LONG:
            raw_liquidation_price = entry_price * (ONE - loss_fraction)
            buffered_liquidation_price = raw_liquidation_price + entry_price * buffer_fraction
        else:
            raw_liquidation_price = entry_price * (ONE + loss_fraction)
            buffered_liquidation_price = raw_liquidation_price - entry_price * buffer_fraction

        raw_liquidation_price = max(raw_liquidation_price, ZERO)
        buffered_liquidation_price = max(buffered_liquidation_price, ZERO)

        return LiquidationEstimate(
            side=side,
            entry_price=entry_price,
            leverage=leverage,
            maintenance_margin_rate=maintenance_rate,
            raw_liquidation_price=raw_liquidation_price,
            buffered_liquidation_price=buffered_liquidation_price,
        )

    # -- stop-to-liquidation distance / safety -------------------------------

    def stop_to_liquidation_distance(
        self,
        *,
        side: MarginPositionSide,
        stop_price: Decimal,
        liquidation_price: Decimal,
        entry_price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Return `(distance_bps, distance_ratio)` from `stop_price` to
        `liquidation_price`, both expressed relative to `entry_price`.

        Positive means the stop is on the safe side of liquidation (above it
        for LONG, below it for SHORT); zero or negative means the stop would
        trigger at or after liquidation has already occurred.
        """
        if not _finite(entry_price) or entry_price <= 0:
            raise ValueError("entry_price must be finite and positive")
        if not _finite(stop_price) or stop_price <= 0:
            raise ValueError("stop_price must be finite and positive")
        if not _finite(liquidation_price) or liquidation_price < 0:
            raise ValueError("liquidation_price must be finite and non-negative")

        if side == MarginPositionSide.LONG:
            distance = stop_price - liquidation_price
        else:
            distance = liquidation_price - stop_price
        distance_bps = distance / entry_price * TEN_THOUSAND
        distance_ratio = distance / entry_price
        return distance_bps, distance_ratio

    def evaluate_stop_safety(
        self,
        *,
        side: MarginPositionSide,
        entry_price: Decimal,
        stop_price: Decimal,
        leverage: Decimal,
        maintenance_policy: MaintenanceMarginPolicy,
        uncertainty_buffer: VenueUncertaintyBuffer,
        min_required_distance_bps: Decimal = ZERO,
    ) -> MarginSafetyResult:
        """The single pre-approval safety check: is `stop_price` far enough
        from the estimated, buffered liquidation price?

        Returns a `MarginSafetyResult` with an explicit `safe: bool` and
        `reason_code`. `safe=False` whenever the stop is not clear of the
        buffered liquidation price by at least `min_required_distance_bps`.
        A stop that is too close to (or past) the estimated liquidation
        price is NEVER treated as safe by this method -- this is the
        contract a later integration step in `RiskEngine._evaluate_inner`
        checks before approving a decision.
        """
        if not _finite(min_required_distance_bps) or min_required_distance_bps < 0:
            raise ValueError("min_required_distance_bps must be finite and non-negative")

        estimate = self.estimate_liquidation_price(
            side=side,
            entry_price=entry_price,
            leverage=leverage,
            maintenance_policy=maintenance_policy,
            uncertainty_buffer=uncertainty_buffer,
        )
        distance_bps, distance_ratio = self.stop_to_liquidation_distance(
            side=side,
            stop_price=stop_price,
            liquidation_price=estimate.buffered_liquidation_price,
            entry_price=entry_price,
        )

        if distance_bps <= 0:
            reason_code = MarginSafetyReason.STOP_BEYOND_LIQUIDATION
            safe = False
        elif distance_bps < min_required_distance_bps:
            reason_code = MarginSafetyReason.STOP_TOO_CLOSE_TO_LIQUIDATION
            safe = False
        else:
            reason_code = MarginSafetyReason.SAFE
            safe = True

        return MarginSafetyResult(
            safe=safe,
            reason_code=reason_code,
            liquidation_estimate=estimate,
            stop_price=stop_price,
            distance_bps=distance_bps,
            distance_ratio=distance_ratio,
            min_required_distance_bps=min_required_distance_bps,
        )

    # -- volatility-adjusted leverage cap ------------------------------------

    def volatility_adjusted_leverage_cap(
        self,
        *,
        policy: VolatilityLeverageCapPolicy,
        volatility: Decimal,
        configured_cap: Decimal | None = None,
    ) -> Decimal:
        """Suggested max leverage that shrinks as `volatility` rises.

        Layered on top of -- never replacing -- `MAX_SYSTEM_LEVERAGE` (20x)
        and any stricter `configured_cap` supplied by the caller (e.g. an
        instrument/account/policy leverage limit already enforced by
        `risk.engine`). The result is always
        `<= min(MAX_SYSTEM_LEVERAGE, configured_cap, policy.base_cap)` and
        monotonically non-increasing in `volatility`.
        """
        if not _finite(volatility) or volatility < 0:
            raise ValueError("volatility must be finite and non-negative")
        if configured_cap is not None and (not _finite(configured_cap) or configured_cap <= 0):
            raise ValueError("configured_cap must be finite and positive")

        denominator = ONE + policy.volatility_sensitivity * volatility
        cap = policy.base_cap / denominator
        cap = max(cap, policy.min_cap)
        cap = min(cap, MAX_SYSTEM_LEVERAGE)
        if configured_cap is not None:
            cap = min(cap, configured_cap)
        return cap
