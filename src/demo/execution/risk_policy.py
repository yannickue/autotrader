# ruff: noqa: E501
"""``demo-discovery-policy-v1``: the risk policy of the ActivTrades DEMO discovery phase.

Pure and deterministic (no I/O, no clock reads). Built on the repository's ``RiskPolicyEvaluator``
(all of its safety gates stay: data freshness, reference-price deviation, spread, margin /
liquidation-distance check, daily-loss / drawdown / consecutive-loss halts, reconciliation) with the
sizing step replaced by ``DemoPositionSizer`` (``demo.execution.sizing``):

    structural stop (never moved) -> broker min lot / step -> ACTUAL loss at the stop -> ACTUAL EUR
    risk -> ACTUAL equity risk % -> leverage -> portfolio / cluster / family risk -> HARD CAPS ->
    TRADE / SKIP

There is NO fixed "1 % risk" or "2 % min-lot" rule: ``target_risk_fraction`` is only the default
input of the sizer, and the minimum lot is accepted whatever its actual risk is, unless it would
violate a configured hard cap (``RiskCaps``, one frozen dataclass). Sizing is independent of
signal quality; there is no automatic risk escalation and the automatic risk reduction of the
generic policy sizer is NOT applied.

Money model. The evaluator assumes "1 unit costs ``price`` account-currency units". CFD lots do
not, so the gate hands it a *virtual instrument*: quantity in contract UNITS (lots x contract size)
and every price multiplied by ``fx`` (account currency per profit-currency unit). Then
``stop distance x quantity`` is exactly the loss in account currency and ``price x quantity`` the
notional, for every market (XAUUSD 100 oz USD, EURUSD 100 000 quoted in USD), without touching
``src/risk``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from data.models import DataQuality, MarketSnapshot
from demo.contracts import TradeIntent
from demo.execution import gates as G
from demo.execution.parity import executable_price
from demo.execution.sizing import (
    DemoPositionSizer,
    PortfolioRisk,
    RiskCaps,
    SizingDecision,
    SizingInput,
)
from demo.execution.tranches import UNKNOWN_FAMILY
from risk.models import (
    MAX_SYSTEM_LEVERAGE,
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    ReconciliationState,
    RiskPolicy,
    RiskSide,
    RuntimeMode,
    RuntimeRiskState,
)
from risk.policy import PolicyRejection, RiskPolicyEvaluator
from risk.sizing import ExposureCapacity, PositionSizer, RiskRejection, SizingResult

ZERO = Decimal(0)
TEN_THOUSAND = Decimal(10000)

BINDING_TARGET_RISK = "target_risk"  # the requested risk (not a hard cap) bound the size
POLICY_ID = "demo-discovery-policy-v1"
POLICY_REVISION = "2-tunable-hard-caps"
BROKER_LEVERAGE_CEILING = MAX_SYSTEM_LEVERAGE  # 30, hard, never a target
CLUSTERS: Mapping[str, str] = {
    "GER40": "INDEX",
    "NAS100": "INDEX",
    "SPX500": "INDEX",
    "XAUUSD": "METAL",
    "EURUSD": "FX",
}
MAX_QUOTE_AGE = timedelta(seconds=30)

REASON_SIZE_BELOW_MIN = G.R_SIZE_BELOW_MIN
REASON_UNKNOWN_CLUSTER = G.R_UNKNOWN_CLUSTER
# The evaluator's MARGIN check rejected a quantity the sizer had FITTED to the same invariant: the two
# disagree. Must never happen (tests prove fitted => approved); reported distinctly so it is never silent.
REASON_SIZER_EVALUATOR_MISMATCH = G.R_SIZER_EVALUATOR_MISMATCH
_EVALUATOR_MARGIN_REASONS = frozenset({G.R_MARGIN_LIQUIDATION, G.R_MARGIN_BEYOND})


def cluster_of(market: str) -> str | None:
    return CLUSTERS.get(market)


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenRisk:
    """Initial stop-risk (entry -> broker stop, account currency) of one open tranche."""

    market: str
    cluster: str
    risk_money: Decimal
    family: str = UNKNOWN_FAMILY
    intent_id: str | None = None


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
    free_margin: Decimal | None = None
    state_version: str = "demo-account"


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketFacts:
    """Per-market facts (broker symbol_info reconciled with configs/markets/*.toml)."""

    market: str
    contract_size: Decimal
    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal
    max_leverage: Decimal  # observed broker margin leverage of the instrument (<= 30)
    max_spread: Decimal  # price units
    fx: Decimal  # account currency per profit-currency unit (1 for an EUR-profit market)
    margin_per_lot: Decimal | None = None  # order_calc_margin(1.0 lot), account currency


@dataclass(frozen=True, slots=True, kw_only=True)
class SizedApproval:
    quantity: Decimal  # LOTS (broker volume units), on the broker step
    equity: Decimal
    risk_fraction: Decimal  # ACTUAL fraction of equity at risk at the structural stop
    risk_budget: Decimal  # target budget (target_risk_fraction x multiplier x equity), EUR
    leverage: Decimal
    notional: Decimal
    stop_risk_money: Decimal  # quantity x contract x |executable - stop| x fx (EUR)
    binding_constraint: str
    min_lot_used: bool
    detail: dict[str, Any]
    policy_id: str = POLICY_ID


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskOutcome:
    """Approval or rejection - both carry the complete risk detail for logging."""

    approval: SizedApproval | None
    reason: str | None
    detail: dict[str, Any]


def build_policy(
    account: GateAccount, caps: RiskCaps, *, risk_fraction: Decimal | None = None
) -> RiskPolicy:
    """The named policy with percent limits resolved to absolute amounts for THIS account state."""
    if account.equity <= 0:
        raise ValueError("equity must be positive")
    return RiskPolicy(
        policy_id=POLICY_ID,
        risk_fraction=risk_fraction or caps.target_risk_fraction,
        max_leverage=caps.max_leverage,
        max_gross_notional=account.equity * caps.max_leverage,
        max_net_notional=account.equity * caps.max_leverage,
        max_daily_loss=max(account.start_of_day_equity, Decimal("0.01")) * caps.max_daily_loss_fraction,
        max_drawdown=max(account.peak_equity, Decimal("0.01")) * caps.max_drawdown_fraction,
        max_consecutive_losses=caps.max_consecutive_losses,
        liquidity_fraction=Decimal("0.5"),
        max_data_age=MAX_QUOTE_AGE,
        max_signal_age=MAX_QUOTE_AGE,
        estimated_cost_bps=Decimal("3"),
    )


def machine_reason(rejection: PolicyRejection) -> str:
    code = str(getattr(rejection.reason_code, "value", rejection.reason_code))
    if code.startswith("HOOK:"):
        return code[len("HOOK:") :]
    return code.lower()


class _PolicySizer(PositionSizer):
    """Plugs ``DemoPositionSizer`` into the evaluator. The generic sizer's 1 %-budget arithmetic and
    its automatic risk reduction are replaced; ``reference_price`` / ``stop_distance`` /
    ``max_leverage`` (structural-stop side check, hard leverage ceiling) are inherited."""

    def __init__(self, policy: RiskPolicy, build_input: Any, holder: dict[str, Any]) -> None:
        super().__init__(policy)
        self._build_input = build_input
        self._holder = holder

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
        decision: SizingDecision = DemoPositionSizer().size(self._build_input())
        self._holder["decision"] = decision
        if not decision.accepted:
            raise RiskRejection(decision.reason or REASON_SIZE_BELOW_MIN, decision.detail.get("message"))
        detail = decision.detail
        units = decision.quantity * detail["contract_size"]
        return SizingResult(
            quantity=units,
            notional=detail["notional_eur"],
            reference_price=reference_price,
            binding_constraint=str(detail["binding_cap"] or BINDING_TARGET_RISK),
            risk_budget=detail["equity"] * detail["target_risk_fraction"] * detail["risk_budget_multiplier"],
            per_unit_loss=detail["loss_per_lot_at_stop"] / detail["contract_size"],
            effective_risk_fraction=detail["equity_risk_fraction"],
            reduce_risk=False,
            max_leverage=max_leverage,
            leverage=detail["leverage"],
        )


@dataclass(slots=True)
class DemoRiskGate:
    """Facade: (intent, quote, broker-truth account) -> ``RiskOutcome`` with full risk detail."""

    caps: RiskCaps = field(default_factory=RiskCaps)
    # Maintenance (stop-out) rate as a fraction of the instrument's initial margin (1/leverage). 1.0 =
    # the conservative default (stop-out at full initial margin). The fit AND the evaluator use the same
    # value, so changing it can never create a sizer/evaluator mismatch.
    stopout_fraction_of_initial_margin: Decimal = Decimal(1)
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
        family: str = UNKNOWN_FAMILY,
        atr: Decimal | None = None,
        risk_budget_multiplier: Decimal | None = None,
        stop_price: Decimal | None = None,
    ) -> RiskOutcome:
        """``stop_price`` defaults to the intent's STRUCTURAL stop (the only production use); a
        different value is only used to evaluate a hypothetical shared-stop add-on."""
        self._decisions += 1
        caps = self.caps
        structural = stop_price if stop_price is not None else Decimal(str(intent.stop))
        target = Decimal(str(intent.risk_fraction))
        multiplier = risk_budget_multiplier or caps.risk_budget_multiplier
        cluster = cluster_of(market.market)
        executable = executable_price(intent.direction, bid, ask)
        base = self._pre_sizing_detail(
            intent, market, account, cluster, family, structural, executable, bid, ask, target, multiplier, atr
        )
        if target <= 0:
            return self._skip(G.R_RISK_FRACTION_INVALID, base)
        if cluster is None:
            return self._skip(REASON_UNKNOWN_CLUSTER, base)
        if market.fx <= 0 or market.contract_size <= 0:
            return self._skip(G.R_INVALID_MARKET_FACTS, base)
        if account.equity <= 0:
            return self._skip(G.R_EQUITY, base)

        fx, units = market.fx, market.contract_size
        side = RiskSide.BUY if intent.direction == 1 else RiskSide.SELL
        v_bid, v_ask = bid * fx, ask * fx
        v_exec = executable * fx
        v_stop = structural * fx
        mid = (v_bid + v_ask) / 2
        if mid <= 0:
            return self._skip(G.R_INVALID_QUOTE, base)
        instrument_leverage = min(market.max_leverage, BROKER_LEVERAGE_CEILING)
        limits = InstrumentRiskLimits(
            instrument=market.market,
            max_leverage=instrument_leverage,
            quantity_step=market.volume_step * units,
            min_quantity=market.volume_min * units,
            min_notional=ZERO,
            max_notional=market.volume_max * units * mid * 1000,
            # The evaluator's absolute spread bound is only the SAFETY cap (4x the per-market p99 bound);
            # the PRIMARY cost gate (spread <= 20% of 1R) runs earlier in demo.execution.parity.
            max_spread_bps=(market.max_spread * G.SPREAD_EXTREME_MULTIPLE * fx) / mid * TEN_THOUSAND
            + Decimal("0.0001"),
            maintenance_margin_rate=self.stopout_fraction_of_initial_margin / instrument_leverage,
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
        portfolio = self._portfolio(account)
        holder: dict[str, Any] = {}

        def build_input() -> SizingInput:
            return SizingInput(
                market=market.market,
                cluster=cluster,
                family=family,
                direction=intent.direction,
                executable_price=executable,
                structural_stop=structural,
                contract_size=market.contract_size,
                volume_min=market.volume_min,
                volume_step=market.volume_step,
                volume_max=market.volume_max,
                fx=market.fx,
                equity=account.equity,
                target_risk_fraction=target,
                risk_budget_multiplier=multiplier,
                caps=caps,
                instrument_max_leverage=instrument_leverage,
                account_leverage=min(account.account_leverage, BROKER_LEVERAGE_CEILING),
                portfolio=portfolio,
                free_margin=account.free_margin,
                margin_per_lot=market.margin_per_lot,
                atr=atr,
                spread=ask - bid,
                maintenance_margin_rate=limits.maintenance_margin_rate,
                liquidation_safety_bps=(
                    policy.liquidation_uncertainty_buffer_bps + policy.min_stop_liquidation_distance_bps
                ),
                # the evaluator measures the liquidation distance from THIS price, not the executable one
                liquidation_reference_price=executable
                * (Decimal(1) + intent.direction * policy.reference_price_slippage_bps / TEN_THOUSAND),
            )

        policy = build_policy(account, caps, risk_fraction=min(target, Decimal(1)))
        evaluator = RiskPolicyEvaluator(policy, sizer=_PolicySizer(policy, build_input, holder))
        snapshot = MarketSnapshot(
            instrument=market.market,
            timestamp=min(quote_time, now),
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
            confidence=Decimal(1),  # constant: sizing never sees signal confidence
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
            return self._skip(f"{G.R_RISK_ERROR}:{type(exc).__name__}", {**base, **self._sized(holder)})
        if isinstance(result, PolicyRejection):
            reason = machine_reason(result)
            sized = self._sized(holder)
            decision_made: SizingDecision | None = holder.get("decision")
            if reason in _EVALUATOR_MARGIN_REASONS and decision_made is not None and decision_made.accepted:
                # the sizer claimed a liquidation-safe fit and the evaluator disagrees: never silent
                sized["evaluator_reject_reason"] = reason
                sized["sizer_liquidation_safe_leverage"] = sized.get("liquidation_safe_leverage")
                reason = REASON_SIZER_EVALUATOR_MISMATCH
            return self._skip(reason, {**base, **sized})
        decision: SizingDecision = holder["decision"]
        detail = {**base, **decision.detail}
        detail.update(
            {"decision": "TRADE", "reject_code": None, "gate_reject_class": None,
             "policy_id": POLICY_ID, "policy_revision": POLICY_REVISION}
        )
        approval = SizedApproval(
            quantity=decision.quantity,
            equity=account.equity,
            risk_fraction=detail["equity_risk_fraction"],
            risk_budget=account.equity * target * multiplier,
            leverage=detail["leverage"],
            notional=detail["notional_eur"],
            stop_risk_money=detail["stop_risk_eur"],
            binding_constraint=str(detail["binding_cap"] or BINDING_TARGET_RISK),
            min_lot_used=bool(detail["min_lot_used"]),
            detail=detail,
        )
        return RiskOutcome(approval=approval, reason=None, detail=detail)

    # -- helpers -----------------------------------------------------------------------------------------

    @staticmethod
    def _sized(holder: dict[str, Any]) -> dict[str, Any]:
        decision = holder.get("decision")
        return {} if decision is None else dict(decision.detail)

    @staticmethod
    def _portfolio(account: GateAccount) -> PortfolioRisk:
        by_market: dict[str, Decimal] = {}
        by_cluster: dict[str, Decimal] = {}
        by_family: dict[str, Decimal] = {}
        for r in account.open_risks:
            by_market[r.market] = by_market.get(r.market, ZERO) + r.risk_money
            by_cluster[r.cluster] = by_cluster.get(r.cluster, ZERO) + r.risk_money
            by_family[r.family] = by_family.get(r.family, ZERO) + r.risk_money
        return PortfolioRisk(
            total=sum((r.risk_money for r in account.open_risks), ZERO),
            by_market=by_market,
            by_cluster=by_cluster,
            by_family=by_family,
            gross_notional=account.gross_notional,
        )

    def _skip(self, reason: str, detail: dict[str, Any]) -> RiskOutcome:
        gate = G.gate_for(reason)
        merged = dict(detail)
        merged.update(
            {
                "decision": "SKIP",
                "reject_code": reason,
                "gate_reject_class": gate.gate_class.value if gate else None,
                "policy_id": POLICY_ID,
                "policy_revision": POLICY_REVISION,
            }
        )
        return RiskOutcome(approval=None, reason=reason, detail=merged)

    def _pre_sizing_detail(
        self,
        intent: TradeIntent,
        market: MarketFacts,
        account: GateAccount,
        cluster: str | None,
        family: str,
        structural: Decimal,
        executable: Decimal,
        bid: Decimal,
        ask: Decimal,
        target: Decimal,
        multiplier: Decimal,
        atr: Decimal | None,
    ) -> dict[str, Any]:
        """Everything worth logging even if the trade is skipped before sizing."""
        portfolio = self._portfolio(account)
        equity = account.equity if account.equity > 0 else Decimal(1)
        detail: dict[str, Any] = {
            "market": market.market,
            "cluster": cluster,
            "family": family,
            "direction": intent.direction,
            "structural_stop": structural,
            "stop_distance": abs(executable - structural),
            "executable_price": executable,
            "entry_ref": Decimal(str(intent.entry_ref)),
            "target": None if intent.target is None else Decimal(str(intent.target)),
            "broker_min_lot": market.volume_min,
            "lot_step": market.volume_step,
            "contract_size": market.contract_size,
            "fx": market.fx,
            "equity": account.equity,
            "target_risk_fraction": target,
            "risk_budget_multiplier": multiplier,
            "spread": ask - bid,
            "atr": atr,
            "free_margin": account.free_margin,
            "portfolio_risk_before": portfolio.total,
            "portfolio_risk_fraction_before": portfolio.total / equity,
            "cluster_risk_before": portfolio.by_cluster.get(cluster or "", ZERO),
            "family_risk_before": portfolio.by_family.get(family, ZERO),
            "gross_leverage_before": account.gross_notional / equity,
            "realized_pnl_today": account.realized_pnl_today,
            "start_of_day_equity": account.start_of_day_equity,
            "peak_equity": account.peak_equity,
            "consecutive_losses": account.consecutive_losses,
            "caps": self.caps.as_dict(),
        }
        if atr is not None and atr > 0:
            detail["stop_distance_atr"] = detail["stop_distance"] / atr
        distance = abs(executable - structural)
        if intent.target is not None and distance > 0:
            detail["planned_r_to_target"] = abs(Decimal(str(intent.target)) - executable) / distance
        return detail
