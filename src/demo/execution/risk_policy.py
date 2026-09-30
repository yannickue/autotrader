"""``demo-discovery-policy-v1``: the risk policy of the ActivTrades DEMO discovery phase.

Pure and deterministic (no I/O, no clock reads). Built on the repository's ``RiskPolicyEvaluator`` /
``PositionSizer`` / ``InstrumentRiskLimits`` - it adds three things the generic policy lacks and
never removes or relaxes any existing gate:

* percent-of-equity limits (daily loss 6 % of start-of-day equity, drawdown 25 % of peak equity)
  are turned into the absolute ``RiskPolicy`` amounts fresh for every evaluation;
* the *min-lot rule*: if the 1 %-sized quantity is below the broker minimum lot, the minimum lot is
  used ONLY if its actual risk is <= 2 % of equity, otherwise ``size_below_min``;
* a correlated-cluster / total-open-risk gate (INDEX{GER40,NAS100,SPX500}, METAL{XAUUSD},
  FX{EURUSD}: total simultaneously open initial risk <= 4 % of equity, per cluster <= 3 %).

Money model. ``PositionSizer`` assumes "1 unit costs ``price`` account-currency units". CFD lots do
not: XAUUSD is 100 oz per lot in USD, EURUSD 100 000 EUR per lot quoted in USD. The gate therefore
sizes a *virtual instrument*: quantity in contract UNITS (lots x contract size) and every price
multiplied by ``fx`` (account currency per profit-currency unit). Then ``stop distance x quantity``
is exactly the loss in account currency and ``price x quantity`` is exactly the notional, so the
sizer's risk / leverage arithmetic is money-correct for every market without touching ``src/risk``.

Leverage: 30 is a hard ceiling (``MAX_SYSTEM_LEVERAGE``), never a target; the effective cap is
``min(30, policy, instrument, account leverage)``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from data.models import DataQuality, MarketSnapshot
from demo.contracts import TradeIntent
from demo.execution.parity import executable_price
from risk.models import (
    MAX_SYSTEM_LEVERAGE,
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    ReconciliationState,
    RiskPolicy,
    RiskReason,
    RiskSide,
    RuntimeMode,
    RuntimeRiskState,
)
from risk.policy import PolicyRejection, RiskHookContext, RiskPolicyEvaluator
from risk.sizing import ExposureCapacity, PositionSizer, RiskRejection, SizingResult

ZERO = Decimal(0)
TEN_THOUSAND = Decimal(10000)

POLICY_ID = "demo-discovery-policy-v1"
RISK_FRACTION = Decimal("0.01")  # of equity per trade
MIN_LOT_MAX_RISK_FRACTION = Decimal("0.02")  # min-lot override ceiling
BROKER_LEVERAGE_CEILING = MAX_SYSTEM_LEVERAGE  # 30, hard, never a target
DAILY_LOSS_FRACTION = Decimal("0.06")  # of start-of-day equity
MAX_DRAWDOWN_FRACTION = Decimal("0.25")  # of peak equity
MAX_CONSECUTIVE_LOSSES = 8
TOTAL_OPEN_RISK_FRACTION = Decimal("0.04")
CLUSTER_RISK_FRACTION = Decimal("0.03")
CLUSTERS: Mapping[str, str] = {
    "GER40": "INDEX",
    "NAS100": "INDEX",
    "SPX500": "INDEX",
    "XAUUSD": "METAL",
    "EURUSD": "FX",
}
MAX_QUOTE_AGE = timedelta(seconds=30)

# machine reason codes emitted by this module (in addition to lower-cased ``RiskReason`` values)
REASON_SIZE_BELOW_MIN = "size_below_min"
REASON_TOTAL_OPEN_RISK = "total_open_risk_limit"
REASON_CLUSTER_RISK = "cluster_risk_limit"
REASON_UNKNOWN_CLUSTER = "unknown_cluster"
REASON_RISK_FRACTION_ABOVE_CAP = "risk_fraction_above_cap"
REASON_UNPROTECTED_POSITION = "unprotected_position"


def cluster_of(market: str) -> str | None:
    return CLUSTERS.get(market)


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenRisk:
    """Initial risk (entry -> broker stop, account currency) of one currently open position."""

    market: str
    cluster: str
    risk_money: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class GateAccount:
    """Broker-truth account facts for one evaluation (already in account currency)."""

    equity: Decimal
    balance: Decimal
    peak_equity: Decimal
    start_of_day_equity: Decimal
    realized_pnl_today: Decimal
    unrealized_pnl: Decimal
    consecutive_losses: int
    gross_notional: Decimal
    net_notional: Decimal
    notionals: Mapping[str, Decimal]
    signed_units: Mapping[str, Decimal]
    account_leverage: Decimal
    day_start: datetime
    open_risks: tuple[OpenRisk, ...] = ()
    state_version: str = "demo-account"


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketFacts:
    """Per-market facts (broker symbol_info reconciled with configs/markets/*.toml)."""

    market: str
    contract_size: Decimal
    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal
    max_leverage: Decimal
    max_spread: Decimal  # price units
    fx: Decimal  # account currency per profit-currency unit (1 for an EUR-profit market)


@dataclass(frozen=True, slots=True, kw_only=True)
class SizedApproval:
    quantity: Decimal  # LOTS (broker volume units), on the broker step
    equity: Decimal
    risk_fraction: Decimal  # ACTUAL fraction of equity risked (incl. cost allowance)
    risk_budget: Decimal  # account currency; the actual risk when the min-lot rule applied
    leverage: Decimal
    notional: Decimal
    stop_risk_money: Decimal  # quantity x |executable - stop| in account currency
    binding_constraint: str
    min_lot_override: bool
    policy_id: str = POLICY_ID


def build_policy(
    account: GateAccount, *, risk_fraction: Decimal = RISK_FRACTION
) -> RiskPolicy:
    """The named policy with percent limits resolved to absolute amounts for THIS account state."""
    if account.equity <= 0:
        raise ValueError("equity must be positive")
    return RiskPolicy(
        policy_id=POLICY_ID,
        risk_fraction=risk_fraction,
        max_leverage=BROKER_LEVERAGE_CEILING,
        max_gross_notional=account.equity * BROKER_LEVERAGE_CEILING,
        max_net_notional=account.equity * BROKER_LEVERAGE_CEILING,
        max_daily_loss=max(account.start_of_day_equity, Decimal("0.01")) * DAILY_LOSS_FRACTION,
        max_drawdown=max(account.peak_equity, Decimal("0.01")) * MAX_DRAWDOWN_FRACTION,
        max_consecutive_losses=MAX_CONSECUTIVE_LOSSES,
        liquidity_fraction=Decimal("0.5"),
        max_data_age=MAX_QUOTE_AGE,
        max_signal_age=MAX_QUOTE_AGE,
        estimated_cost_bps=Decimal("3"),
    )


class DemoDiscoverySizer(PositionSizer):
    """``PositionSizer`` plus the min-lot rule (min lot only if its risk <= 2 % of equity)."""

    def __init__(
        self, policy: RiskPolicy, *, min_lot_max_risk: Decimal = MIN_LOT_MAX_RISK_FRACTION
    ) -> None:
        super().__init__(policy)
        self._policy_ref = policy
        self._min_lot_max_risk = min_lot_max_risk

    def size(  # type: ignore[override]
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
        try:
            return super().size(
                request=request,
                account=account,
                instrument=instrument,
                reference_price=reference_price,
                stop_distance=stop_distance,
                max_leverage=max_leverage,
                capacity=capacity,
                pending_gross=pending_gross,
            )
        except RiskRejection as rejection:
            if rejection.reason_code != RiskReason.SIZE_BELOW_MINIMUM:
                raise
        return self._min_lot(
            request=request,
            account=account,
            instrument=instrument,
            reference_price=reference_price,
            stop_distance=stop_distance,
            max_leverage=max_leverage,
            capacity=capacity,
            pending_gross=pending_gross,
        )

    def _min_lot(
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
        policy = self._policy_ref
        reduce_risk, _fraction = self.risk_reduction(account)
        minimum = instrument.min_quantity
        entry = reference_price
        round_trip_cost = entry * policy.estimated_cost_bps * 2 / TEN_THOUSAND
        per_unit_loss = stop_distance + round_trip_cost
        risk = minimum * per_unit_loss
        # Every NON-risk cap must still admit the minimum lot (the override only relaxes the
        # 1 % risk budget, never leverage / exposure / liquidity limits).
        caps = {
            "leverage_cap": max_leverage * account.equity / entry,
            "instrument_max_notional": instrument.max_notional / entry,
            "liquidity": policy.liquidity_fraction * request.available_liquidity_notional / entry,
            "gross_capacity": capacity.remaining_gross / entry,
            "portfolio_leverage_capacity": capacity.remaining_portfolio_leverage / entry,
            "net_capacity": capacity.remaining_net / entry,
        }
        # A risk-reduced state (drawdown / loss streak) must not be sidestepped by the min lot:
        # the override ceiling shrinks with the same multiplier.
        ceiling = self._min_lot_max_risk * (
            policy.reduce_risk_multiplier if reduce_risk else Decimal(1)
        )
        if (
            minimum <= 0
            or risk > account.equity * ceiling
            or any(minimum > value for value in caps.values())
            or minimum * entry < instrument.min_notional
        ):
            raise RiskRejection(REASON_SIZE_BELOW_MIN)
        notional = minimum * entry
        leverage = notional / account.equity
        if leverage > max_leverage:
            raise RiskRejection(REASON_SIZE_BELOW_MIN)
        if (account.gross_notional + pending_gross + notional) / account.equity > max_leverage:
            raise RiskRejection(REASON_SIZE_BELOW_MIN)
        return SizingResult(
            quantity=minimum,
            notional=notional,
            reference_price=reference_price,
            binding_constraint="min_lot_override",
            risk_budget=risk,
            per_unit_loss=per_unit_loss,
            effective_risk_fraction=risk / account.equity,
            reduce_risk=reduce_risk,
            max_leverage=max_leverage,
            leverage=leverage,
        )


def machine_reason(rejection: PolicyRejection) -> str:
    code = str(getattr(rejection.reason_code, "value", rejection.reason_code))
    if code.startswith("HOOK:"):
        return code[len("HOOK:") :]
    return code.lower()


@dataclass(slots=True)
class DemoRiskGate:
    """Stateless facade: (intent, quote, broker-truth account) -> SizedApproval | reason code."""

    sizer_min_lot_risk: Decimal = MIN_LOT_MAX_RISK_FRACTION
    _decisions: int = field(default=0, init=False)

    def size(
        self,
        *,
        intent: TradeIntent,
        market: MarketFacts,
        account: GateAccount,
        bid: Decimal,
        ask: Decimal,
        quote_time: datetime,
        now: datetime,
    ) -> SizedApproval | str:
        self._decisions += 1
        rf = Decimal(str(intent.risk_fraction))
        if rf <= 0:
            return "risk_fraction_invalid"
        if rf > RISK_FRACTION:
            return REASON_RISK_FRACTION_ABOVE_CAP
        cluster = cluster_of(market.market)
        if cluster is None:
            return REASON_UNKNOWN_CLUSTER
        if market.fx <= 0 or market.contract_size <= 0:
            return "invalid_market_facts"
        if account.equity <= 0:
            return "equity_non_positive"

        fx, units = market.fx, market.contract_size
        side = RiskSide.BUY if intent.direction == 1 else RiskSide.SELL
        v_bid, v_ask = bid * fx, ask * fx
        v_exec = executable_price(intent.direction, v_bid, v_ask)
        v_stop = Decimal(str(intent.stop)) * fx
        mid = (v_bid + v_ask) / 2
        if mid <= 0:
            return "invalid_quote"
        limits = InstrumentRiskLimits(
            instrument=market.market,
            max_leverage=min(market.max_leverage, BROKER_LEVERAGE_CEILING),
            quantity_step=market.volume_step * units,
            min_quantity=market.volume_min * units,
            min_notional=ZERO,
            max_notional=market.volume_max * units * mid,
            max_spread_bps=(market.max_spread * fx) / mid * TEN_THOUSAND + Decimal("0.0001"),
            maintenance_margin_rate=Decimal(1) / min(market.max_leverage, BROKER_LEVERAGE_CEILING),
        )
        risk_state = AccountRiskState(
            state_version=account.state_version,
            known=True,
            reconciliation=ReconciliationState.RECONCILED,
            equity=account.equity,
            peak_equity=max(account.peak_equity, account.equity),
            realized_pnl_today=account.realized_pnl_today,
            pnl_window_start=account.day_start,
            unrealized_pnl=account.unrealized_pnl,
            gross_notional=account.gross_notional,
            net_notional=account.net_notional,
            instrument_notionals=dict(account.notionals),
            positions=dict(account.signed_units),
            leverage_cap=min(account.account_leverage, BROKER_LEVERAGE_CEILING),
            consecutive_losses=account.consecutive_losses,
        )
        policy = build_policy(account, risk_fraction=rf)
        open_total = sum((r.risk_money for r in account.open_risks), ZERO)
        open_cluster = sum((r.risk_money for r in account.open_risks if r.cluster == cluster), ZERO)
        equity = account.equity

        def hook(context: RiskHookContext) -> str | None:
            request = context.request
            new_risk = context.proposed_quantity * abs(request.entry_price - request.stop_price)
            if open_total + new_risk > equity * TOTAL_OPEN_RISK_FRACTION:
                return REASON_TOTAL_OPEN_RISK
            if open_cluster + new_risk > equity * CLUSTER_RISK_FRACTION:
                return REASON_CLUSTER_RISK
            return None

        evaluator = RiskPolicyEvaluator(
            policy,
            hooks=(hook,),
            sizer=DemoDiscoverySizer(policy, min_lot_max_risk=self.sizer_min_lot_risk),
        )
        snapshot = MarketSnapshot(
            instrument=market.market,
            timestamp=quote_time,
            bid=v_bid,
            ask=v_ask,
            last=mid,
            volume=ZERO,
            volatility=None,
            liquidity=None,
            source="ACTIVTRADES_MT5_CFD",
            quality=DataQuality.LIVE,
            metadata={"fx": str(fx), "contract_size": str(units)},
        )
        request = PositionSizingRequest(
            signal_id=intent.intent_id,
            instrument=market.market,
            timestamp=now,
            side=side,
            entry_price=v_exec,
            stop_price=v_stop,
            confidence=Decimal(1),
            available_liquidity_notional=account.equity * BROKER_LEVERAGE_CEILING * 10,
            metadata={},
        )
        runtime = RuntimeRiskState(
            state_version="demo-stack", mode=RuntimeMode.READY, risk_ready=True, kill_switch=False
        )
        try:
            result = evaluator.evaluate_entry(
                request=request,
                snapshot=snapshot,
                account=risk_state,
                runtime=runtime,
                instrument=limits,
                now=now,
            )
        except (RiskRejection, ArithmeticError) as exc:  # fail closed on any internal error
            return f"risk_error:{type(exc).__name__}"
        if isinstance(result, PolicyRejection):
            return machine_reason(result)
        lots = result.quantity / units
        if lots < market.volume_min or lots > market.volume_max:
            return REASON_SIZE_BELOW_MIN if lots < market.volume_min else "size_above_max"
        stop_risk = result.quantity * abs(v_exec - v_stop)
        per_unit_loss = Decimal(str(result.metadata["per_unit_loss"]))
        actual_fraction = result.quantity * per_unit_loss / account.equity
        override = result.metadata["binding_constraint"] == "min_lot_override"
        return SizedApproval(
            quantity=lots,
            equity=account.equity,
            risk_fraction=actual_fraction,
            risk_budget=result.risk_budget,
            leverage=result.leverage,
            notional=result.notional,
            stop_risk_money=stop_risk,
            binding_constraint=str(result.metadata["binding_constraint"]),
            min_lot_override=override,
        )
