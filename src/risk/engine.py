"""Deterministic position sizing for linear USD-M instruments."""

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from data.models import MarketSnapshot
from risk.models import (
    MAX_SYSTEM_LEVERAGE,
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    RiskDecision,
    RiskPolicy,
    RiskReason,
    RuntimeRiskState,
)


class RiskEngine:
    """Evaluate exposure-changing requests against an immutable policy."""

    def __init__(self, policy: RiskPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        *,
        request: PositionSizingRequest,
        snapshot: MarketSnapshot,
        account: AccountRiskState,
        runtime: RuntimeRiskState,
        instrument: InstrumentRiskLimits,
        now: datetime,
    ) -> RiskDecision:
        del snapshot, runtime, now
        max_leverage = min(
            MAX_SYSTEM_LEVERAGE,
            self._policy.max_leverage,
            instrument.max_leverage,
            account.leverage_cap,
        )
        risk_budget = account.equity * self._policy.risk_fraction
        stop_distance = abs(request.entry_price - request.stop_price)
        raw_quantity = risk_budget / stop_distance
        quantity = (
            raw_quantity / instrument.quantity_step
        ).to_integral_value(rounding=ROUND_DOWN) * instrument.quantity_step
        notional = quantity * request.entry_price
        leverage = notional / account.equity
        return RiskDecision(
            decision_id=f"risk:{request.signal_id}",
            signal_id=request.signal_id,
            instrument=request.instrument,
            timestamp=request.timestamp,
            approved=True,
            reason="approved",
            reason_code=RiskReason.APPROVED,
            quantity=quantity,
            notional=notional,
            leverage=leverage,
            max_leverage=max_leverage,
            risk_budget=risk_budget,
            stop_price=request.stop_price,
            metadata={},
        )
