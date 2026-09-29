"""PURE position sizing: deterministic arithmetic over explicit inputs.

No mutable state, no reservation ledger, no portfolio/order ownership, no
broker or MT5 I/O, no clock, no globals. Every input (account snapshot,
remaining exposure capacity, instrument limits) is passed in by value, so the
same function can be driven by the legacy `RiskEngine`, a backtest, or (later)
a Nautilus strategy adapter that supplies capacity from Nautilus' own
portfolio/cache.

The formulas below are moved verbatim from the legacy `RiskEngine`
(docs/OPEN_QUESTIONS.md #23 reference-price contract, docs/RISK_CONTRACT.md);
this slice changes where they live, not what they compute.
"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from data.models import MarketSnapshot
from risk.models import (
    MAX_SYSTEM_LEVERAGE,
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    RiskPolicy,
    RiskReason,
    RiskSide,
)

ZERO = Decimal("0")
TEN_THOUSAND = Decimal("10000")


class RiskRejection(Exception):
    """Control-flow exception carrying a machine-readable reason code."""

    def __init__(self, reason_code: RiskReason | str, reason: str | None = None) -> None:
        self.reason_code = reason_code
        self.reason = reason or str(reason_code)
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExposureCapacity:
    """Remaining notional capacity (already net of existing AND pending
    exposure), supplied by the caller."""

    remaining_gross: Decimal
    remaining_net: Decimal
    remaining_portfolio_leverage: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class SizingResult:
    quantity: Decimal
    notional: Decimal
    reference_price: Decimal
    binding_constraint: str
    risk_budget: Decimal
    per_unit_loss: Decimal
    effective_risk_fraction: Decimal
    reduce_risk: bool
    max_leverage: Decimal
    leverage: Decimal


class PositionSizer:
    """Stop-distance / risk-budget sizing with cost allowance and hard caps."""

    def __init__(self, policy: RiskPolicy) -> None:
        self._policy = policy

    def reference_price(self, *, side: RiskSide, snapshot: MarketSnapshot) -> Decimal:
        """Executable-market reference price for sizing/exposure math.

        Never `request.entry_price` (docs/OPEN_QUESTIONS.md #23): ask + slippage
        buffer for BUY, bid - slippage buffer for SELL.
        """
        policy = self._policy
        if side == RiskSide.BUY:
            return snapshot.ask + snapshot.ask * policy.reference_price_slippage_bps / TEN_THOUSAND
        return snapshot.bid - snapshot.bid * policy.reference_price_slippage_bps / TEN_THOUSAND

    def stop_distance(self, request: PositionSizingRequest, reference_price: Decimal) -> Decimal:
        """Stop must sit on the correct side of BOTH the strategy entry_price
        AND the executable reference price (docs/RISK_CONTRACT.md)."""
        if request.side == RiskSide.BUY:
            if request.stop_price >= request.entry_price or request.stop_price >= reference_price:
                raise RiskRejection(RiskReason.INVALID_STOP)
        else:
            if request.stop_price <= request.entry_price or request.stop_price <= reference_price:
                raise RiskRejection(RiskReason.INVALID_STOP)
        return abs(request.entry_price - request.stop_price)

    def max_leverage(
        self, *, instrument: InstrumentRiskLimits, account: AccountRiskState
    ) -> Decimal:
        """Hard system ceiling (20x) is always the outermost cap."""
        return min(
            MAX_SYSTEM_LEVERAGE,
            self._policy.max_leverage,
            instrument.max_leverage,
            account.leverage_cap,
        )

    def risk_reduction(self, account: AccountRiskState) -> tuple[bool, Decimal]:
        """Drawdown / daily-loss / loss-streak based risk-fraction reduction."""
        policy = self._policy
        drawdown_trigger = policy.reduce_risk_drawdown_fraction * policy.max_drawdown
        daily_loss_trigger = policy.reduce_risk_daily_loss_fraction * policy.max_daily_loss
        reduce_risk = (
            (account.peak_equity - account.equity) >= drawdown_trigger
            or (-(account.realized_pnl_today + account.unrealized_pnl)) >= daily_loss_trigger
            or account.consecutive_losses >= policy.reduce_risk_after_losses
        )
        fraction = policy.risk_fraction * (
            policy.reduce_risk_multiplier if reduce_risk else Decimal("1")
        )
        return reduce_risk, fraction

    def size(
        self,
        *,
        request: PositionSizingRequest,
        account: AccountRiskState,
        instrument: InstrumentRiskLimits,
        reference_price: Decimal,
        stop_distance: Decimal,
        max_leverage: Decimal,
        capacity: ExposureCapacity,
        pending_gross: Decimal,
    ) -> SizingResult:
        """Return the largest quantity satisfying every constraint, rounded
        DOWN to the instrument step. Raises RiskRejection(SIZE_BELOW_MINIMUM)
        when the result is below the instrument minimums."""
        policy = self._policy
        reduce_risk, effective_risk_fraction = self.risk_reduction(account)
        risk_budget = account.equity * effective_risk_fraction
        round_trip_cost = reference_price * policy.estimated_cost_bps * 2 / TEN_THOUSAND
        per_unit_loss = stop_distance + round_trip_cost
        qty_risk = risk_budget / per_unit_loss

        entry = reference_price
        candidates: dict[str, Decimal] = {
            "risk_budget": qty_risk,
            "leverage_cap": max_leverage * account.equity / entry,
            "instrument_max_notional": instrument.max_notional / entry,
            "liquidity": policy.liquidity_fraction * request.available_liquidity_notional / entry,
            "gross_capacity": capacity.remaining_gross / entry,
            "portfolio_leverage_capacity": capacity.remaining_portfolio_leverage / entry,
            "net_capacity": capacity.remaining_net / entry,
        }
        binding_constraint = min(candidates, key=lambda key: candidates[key])
        raw_quantity = max(min(candidates.values()), ZERO)

        quantity = (raw_quantity / instrument.quantity_step).to_integral_value(
            rounding=ROUND_DOWN
        ) * instrument.quantity_step
        notional = quantity * entry

        if quantity < instrument.min_quantity or notional < instrument.min_notional:
            raise RiskRejection(RiskReason.SIZE_BELOW_MINIMUM)

        leverage = notional / account.equity
        if leverage > max_leverage:
            raise RuntimeError("computed leverage exceeds max_leverage")
        if (account.gross_notional + pending_gross + notional) / account.equity > max_leverage:
            raise RuntimeError("account gross leverage after fill exceeds max_leverage")

        return SizingResult(
            quantity=quantity,
            notional=notional,
            reference_price=reference_price,
            binding_constraint=binding_constraint,
            risk_budget=risk_budget,
            per_unit_loss=per_unit_loss,
            effective_risk_fraction=effective_risk_fraction,
            reduce_risk=reduce_risk,
            max_leverage=max_leverage,
            leverage=leverage,
        )
