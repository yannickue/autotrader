# ruff: noqa: E501
"""Pure position sizing for the DEMO discovery phase (no I/O, no clock, no broker, no signal).

Pipeline (the ONLY thing that decides quantity):

    structural stop (from the intent; NEVER moved to hit a risk %)
      -> broker min lot / lot step
      -> ACTUAL loss at the stop -> ACTUAL account-currency risk -> ACTUAL equity risk %
      -> leverage -> portfolio / cluster / family risk -> HARD SAFETY CAPS -> TRADE / SKIP

Quantity: the size that ``target_risk_fraction x risk_budget_multiplier`` would give at the
structural stop, fitted DOWN to every hard cap and to the lot step, but never below the broker
minimum lot. The minimum lot with an actual risk of 1.4 % / 2.3 % / 3.1 % is NOT invalid by itself:
a trade is skipped only when the minimum lot would violate a configured hard safety cap
(``size_below_min`` + ``violated_cap`` + the numbers).

Independence. Sizing never looks at confidence, confluence, family score, model probability or any
other signal-quality input: it does not accept them. ``risk_budget_multiplier`` (default 1) is the
single hook for a future, explicit forecast/volatility scaling; nothing scales it automatically.
``atr`` and ``spread`` are accepted for LOGGING ratios only.

Money model. ``contract_size x price-distance x fx`` is account currency (fx = account currency per
unit of the market's profit currency), so XAUUSD (100 oz, USD) and EURUSD (100 000, USD) are exact.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from demo.execution.tranches import UNKNOWN_FAMILY
from risk.models import MAX_SYSTEM_LEVERAGE

ZERO = Decimal(0)
ONE = Decimal(1)
REASON_SIZE_BELOW_MIN = "size_below_min"
REASON_INVALID_STOP = "invalid_stop"
REASON_INVALID_MARKET_FACTS = "invalid_market_facts"
REASON_EQUITY = "equity_non_positive"
REASON_RISK_FRACTION_INVALID = "risk_fraction_invalid"
# Extra clearance (bps) on the liquidation-safety fit so Decimal/step floor edge cases can never land on
# the wrong side of the evaluator's ``distance_bps >= min_required`` boundary (fitted => approved).
LIQUIDATION_FIT_EPSILON_BPS = Decimal("0.1")


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskCaps:
    """The single frozen configuration of the DEMO discovery risk policy.

    Every number here is a named, configurable HARD SAFETY CAP or a default INPUT to the sizer,
    chosen as a discovery-demo starting point. None of them is an alpha rule.
    """

    # default input to the sizer (an intent may request its own); NOT a rule
    target_risk_fraction: Decimal = Decimal("0.01")
    risk_budget_multiplier: Decimal = ONE  # hook for later explicit scaling; never automatic
    # hard caps ------------------------------------------------------------------------------------
    max_position_stop_risk_fraction: Decimal = Decimal("0.05")  # per trade, of equity
    max_aggregate_open_stop_risk_fraction: Decimal = Decimal("0.10")
    max_cluster_stop_risk_fraction: Decimal = Decimal("0.06")  # INDEX / METAL / FX
    max_family_share_of_open_risk: Decimal = Decimal("0.60")  # of the aggregate risk BUDGET
    max_leverage: Decimal = MAX_SYSTEM_LEVERAGE  # 30, HARD ceiling, never a target
    max_margin_fraction_of_free_margin: Decimal = Decimal("0.90")
    max_daily_loss_fraction: Decimal = Decimal("0.06")  # of start-of-day equity (UTC day)
    max_drawdown_fraction: Decimal = Decimal("0.25")  # of peak equity, halts
    max_consecutive_losses: int = 8
    # add-on classification (a definition of "shared stop", not a risk opinion)
    max_shared_stop_tightening_fraction: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        for name in (
            "target_risk_fraction",
            "max_position_stop_risk_fraction",
            "max_aggregate_open_stop_risk_fraction",
            "max_cluster_stop_risk_fraction",
            "max_family_share_of_open_risk",
            "max_margin_fraction_of_free_margin",
            "max_daily_loss_fraction",
            "max_drawdown_fraction",
        ):
            value = getattr(self, name)
            if not value.is_finite() or not ZERO < value <= ONE:
                raise ValueError(f"{name} must be finite and in (0, 1]")
        if not self.risk_budget_multiplier.is_finite() or not ZERO < self.risk_budget_multiplier <= 5:
            raise ValueError("risk_budget_multiplier must be in (0, 5]")
        if not ZERO < self.max_leverage <= MAX_SYSTEM_LEVERAGE:
            raise ValueError(f"max_leverage must be in (0, {MAX_SYSTEM_LEVERAGE}]")
        if self.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses must be >= 1")
        if not ZERO <= self.max_shared_stop_tightening_fraction <= ONE:
            raise ValueError("max_shared_stop_tightening_fraction must be in [0, 1]")

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True, kw_only=True)
class PortfolioRisk:
    """Open stop-risk (account currency) BEFORE the candidate trade."""

    total: Decimal = ZERO
    by_market: Mapping[str, Decimal] = field(default_factory=dict)
    by_cluster: Mapping[str, Decimal] = field(default_factory=dict)
    by_family: Mapping[str, Decimal] = field(default_factory=dict)
    gross_notional: Decimal = ZERO


@dataclass(frozen=True, slots=True, kw_only=True)
class SizingInput:
    market: str
    cluster: str
    family: str
    direction: int
    executable_price: Decimal  # ask (long) / bid (short) at the decision
    structural_stop: Decimal  # from the intent, used AS IS
    contract_size: Decimal
    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal
    fx: Decimal
    equity: Decimal
    target_risk_fraction: Decimal
    risk_budget_multiplier: Decimal = ONE
    caps: RiskCaps = field(default_factory=RiskCaps)
    instrument_max_leverage: Decimal = MAX_SYSTEM_LEVERAGE
    account_leverage: Decimal = MAX_SYSTEM_LEVERAGE
    portfolio: PortfolioRisk = field(default_factory=PortfolioRisk)
    free_margin: Decimal | None = None  # broker free margin (account currency)
    margin_per_lot: Decimal | None = None  # order_calc_margin for 1.0 lot (account currency)
    atr: Decimal | None = None  # logging only
    spread: Decimal | None = None  # logging only
    # Existing liquidation-safety invariant (margin.engine) expressed as a leverage cap so that the
    # quantity is FITTED to it instead of being rejected afterwards: the structural stop must stay
    # ``liquidation_safety_bps`` (estimate buffer + minimum clearance) clear of the estimated
    # liquidation price at the portfolio leverage after the fill.
    maintenance_margin_rate: Decimal | None = None
    liquidation_safety_bps: Decimal = Decimal("150")
    # The price the EVALUATOR measures the stop->liquidation distance from (RiskPolicyEvaluator uses
    # ask x (1 + slippage_bps) for BUY / bid x (1 - slippage_bps) for SELL, never the executable price).
    # The fit MUST use the same reference or fitted quantities get rejected afterwards. ``None`` keeps
    # the executable price (no slippage buffer).
    liquidation_reference_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SizingDecision:
    accepted: bool
    quantity: Decimal  # lots; 0 when rejected
    reason: str | None
    detail: dict[str, Any]


def floor_to_step(value: Decimal, minimum: Decimal, step: Decimal) -> Decimal:
    """Round DOWN onto the broker grid ``minimum + n x step``; 0 if below the minimum."""
    if value < minimum:
        return ZERO
    n = ((value - minimum) / step).to_integral_value(rounding=ROUND_FLOOR)
    return minimum + n * step


def _share(part: Decimal, whole: Decimal) -> Decimal:
    return part / whole if whole > 0 else ZERO


class DemoPositionSizer:
    """Stateless: ``SizingInput -> SizingDecision``."""

    def size(self, inp: SizingInput) -> SizingDecision:
        caps = inp.caps
        detail: dict[str, Any] = {
            "market": inp.market,
            "cluster": inp.cluster,
            "family": inp.family,
            "structural_stop": inp.structural_stop,
            "executable_price": inp.executable_price,
            "broker_min_lot": inp.volume_min,
            "lot_step": inp.volume_step,
            "broker_max_lot": inp.volume_max,
            "contract_size": inp.contract_size,
            "fx": inp.fx,
            "equity": inp.equity,
            "target_risk_fraction": inp.target_risk_fraction,
            "risk_budget_multiplier": inp.risk_budget_multiplier,
            "caps": caps.as_dict(),
            "atr": inp.atr,
            "spread": inp.spread,
            "sizing_independent_of_signal_quality": True,
        }
        if inp.equity <= 0:
            return self._reject(REASON_EQUITY, detail)
        if inp.target_risk_fraction <= 0 or inp.risk_budget_multiplier <= 0:
            return self._reject(REASON_RISK_FRACTION_INVALID, detail)
        if (
            inp.contract_size <= 0
            or inp.fx <= 0
            or inp.volume_min <= 0
            or inp.volume_step <= 0
            or inp.executable_price <= 0
        ):
            return self._reject(REASON_INVALID_MARKET_FACTS, detail)
        distance = (inp.executable_price - inp.structural_stop) * inp.direction
        detail["stop_distance"] = abs(inp.executable_price - inp.structural_stop)
        if distance <= 0:
            return self._reject(REASON_INVALID_STOP, detail)  # stop not on the protective side
        loss_per_lot = inp.contract_size * distance * inp.fx
        notional_per_lot = inp.contract_size * inp.executable_price * inp.fx
        detail["loss_per_lot_at_stop"] = loss_per_lot
        detail["notional_per_lot"] = notional_per_lot
        if inp.atr is not None and inp.atr > 0:
            detail["stop_distance_atr"] = distance / inp.atr
        if inp.spread is not None:
            detail["spread_to_stop"] = inp.spread / distance

        equity, pf = inp.equity, inp.portfolio
        port_lev = min(caps.max_leverage, inp.account_leverage, MAX_SYSTEM_LEVERAGE)
        family_open = pf.by_family.get(inp.family, ZERO)
        cluster_open = pf.by_cluster.get(inp.cluster, ZERO)
        family_budget = equity * caps.max_aggregate_open_stop_risk_fraction * caps.max_family_share_of_open_risk

        # max lots allowed by each hard cap (name -> (lots, limit, observed-at-min-lot builder))
        limits: dict[str, tuple[Decimal, Decimal]] = {
            "max_position_stop_risk_fraction": (
                equity * caps.max_position_stop_risk_fraction / loss_per_lot,
                caps.max_position_stop_risk_fraction,
            ),
            "max_aggregate_open_stop_risk_fraction": (
                (equity * caps.max_aggregate_open_stop_risk_fraction - pf.total) / loss_per_lot,
                caps.max_aggregate_open_stop_risk_fraction,
            ),
            "max_cluster_stop_risk_fraction": (
                (equity * caps.max_cluster_stop_risk_fraction - cluster_open) / loss_per_lot,
                caps.max_cluster_stop_risk_fraction,
            ),
            "max_leverage": (equity * caps.max_leverage / notional_per_lot, caps.max_leverage),
            "max_portfolio_leverage": (
                (port_lev * equity - pf.gross_notional) / notional_per_lot,
                port_lev,
            ),
            "instrument_volume_max": (inp.volume_max, inp.volume_max),
        }
        if inp.family != UNKNOWN_FAMILY:
            # Concentration needs a family: an intent without one cannot be attributed, so it is
            # logged as UNKNOWN and NOT lumped into one artificial family (which would cap the
            # whole book). The runner supplies ``family``.
            limits["max_family_share_of_open_risk"] = (
                (family_budget - family_open) / loss_per_lot,
                caps.max_family_share_of_open_risk,
            )
        if inp.instrument_max_leverage < caps.max_leverage:
            limits["instrument_max_leverage"] = (
                inp.instrument_max_leverage * equity / notional_per_lot,
                inp.instrument_max_leverage,
            )
        if inp.account_leverage < caps.max_leverage:
            limits["max_leverage"] = (
                min(limits["max_leverage"][0], inp.account_leverage * equity / notional_per_lot),
                inp.account_leverage,
            )
        if inp.maintenance_margin_rate is not None:
            # 1/L >= mm + safety + d/entry  <=>  L <= 1 / (mm + safety + d/entry)
            # Same geometry as margin.engine.evaluate_stop_safety: distance measured from the reference
            # price R, in the protective direction (BUY: (R - stop)/R, SELL: (stop - R)/R).
            ref = inp.liquidation_reference_price or inp.executable_price
            ratio = inp.direction * (ref - inp.structural_stop) / ref
            detail["liquidation_reference_price"] = ref
            liq_lev = ONE / (
                inp.maintenance_margin_rate
                + (inp.liquidation_safety_bps + LIQUIDATION_FIT_EPSILON_BPS) / Decimal(10000)
                + ratio
            )
            limits["liquidation_safe_leverage"] = (
                (min(liq_lev, port_lev) * equity - pf.gross_notional) / notional_per_lot,
                liq_lev,
            )
            detail["liquidation_safe_leverage"] = liq_lev
        if inp.free_margin is not None and inp.margin_per_lot is not None and inp.margin_per_lot > 0:
            limits["max_margin_fraction_of_free_margin"] = (
                inp.free_margin * caps.max_margin_fraction_of_free_margin / inp.margin_per_lot,
                caps.max_margin_fraction_of_free_margin,
            )
            detail["free_margin"] = inp.free_margin
            detail["margin_per_lot"] = inp.margin_per_lot
        else:
            detail["margin_check"] = "unavailable"

        tightest = min(limits, key=lambda name: limits[name][0])
        max_lots = limits[tightest][0]
        binding = tightest  # name used by the below-min path (the tightest cap IS the violated one there)
        desired = equity * inp.target_risk_fraction * inp.risk_budget_multiplier / loss_per_lot
        detail["desired_quantity"] = desired
        # ``binding_cap`` is reported only when the cap actually bound the size (target size > cap max);
        # otherwise the target risk bound it and the tightest cap is informational (``tightest_cap``).
        detail["tightest_cap"] = tightest
        detail["binding_cap"] = tightest if desired > max_lots else None
        detail["max_quantity_by_cap"] = {name: lots for name, (lots, _) in limits.items()}

        if max_lots < inp.volume_min:
            return self._below_min(inp, detail, limits, binding, loss_per_lot, notional_per_lot)

        quantity = max(inp.volume_min, floor_to_step(min(desired, max_lots), inp.volume_min, inp.volume_step))
        risk = quantity * loss_per_lot
        notional = quantity * notional_per_lot
        self._fill_numbers(inp, detail, quantity, risk, notional, loss_per_lot)
        detail["min_lot_used"] = quantity == inp.volume_min and desired < inp.volume_min
        detail["quantity_reduced_by_cap"] = desired > max_lots and quantity <= max_lots
        detail["decision"] = "TRADE"
        detail["reject_code"] = None
        return SizingDecision(True, quantity, None, detail)

    # -- rejection: the MINIMUM lot violates a hard cap ------------------------------------------------------

    def _below_min(
        self,
        inp: SizingInput,
        detail: dict[str, Any],
        limits: Mapping[str, tuple[Decimal, Decimal]],
        binding: str,
        loss_per_lot: Decimal,
        notional_per_lot: Decimal,
    ) -> SizingDecision:
        minimum = inp.volume_min
        detail["binding_cap"] = binding  # the minimum lot itself violates it
        risk = minimum * loss_per_lot
        notional = minimum * notional_per_lot
        self._fill_numbers(inp, detail, minimum, risk, notional, loss_per_lot)
        pf, equity = inp.portfolio, inp.equity
        observed: dict[str, Decimal] = {
            "max_position_stop_risk_fraction": risk / equity,
            "max_aggregate_open_stop_risk_fraction": (pf.total + risk) / equity,
            "max_cluster_stop_risk_fraction": (pf.by_cluster.get(inp.cluster, ZERO) + risk) / equity,
            "max_family_share_of_open_risk": (pf.by_family.get(inp.family, ZERO) + risk)
            / (equity * inp.caps.max_aggregate_open_stop_risk_fraction),
            "max_leverage": notional / equity,
            "max_portfolio_leverage": (pf.gross_notional + notional) / equity,
            "instrument_volume_max": minimum,
            "instrument_max_leverage": notional / equity,
            "liquidation_safe_leverage": (pf.gross_notional + notional) / equity,
        }
        if inp.margin_per_lot is not None and inp.free_margin:
            observed["max_margin_fraction_of_free_margin"] = minimum * inp.margin_per_lot / inp.free_margin
        limit = limits[binding][1]
        seen = observed.get(binding, ZERO)
        detail.update(
            {
                "decision": "SKIP",
                "reject_code": REASON_SIZE_BELOW_MIN,
                "violated_cap": binding,
                "cap_limit": limit,
                "observed_at_min_lot": seen,
                "quantity_at_min_lot": minimum,
                "violated_caps": [
                    name for name, (lots, _) in limits.items() if lots < minimum
                ],
                "message": (
                    f"minimum lot {minimum} would violate {binding}: observed {seen:.6f} > limit {limit}"
                ),
            }
        )
        return SizingDecision(False, ZERO, REASON_SIZE_BELOW_MIN, detail)

    def _reject(self, reason: str, detail: dict[str, Any]) -> SizingDecision:
        detail.update({"decision": "SKIP", "reject_code": reason, "message": reason})
        return SizingDecision(False, ZERO, reason, detail)

    # -- numbers logged for every decision ---------------------------------------------------------------------

    @staticmethod
    def _fill_numbers(
        inp: SizingInput,
        detail: dict[str, Any],
        quantity: Decimal,
        risk: Decimal,
        notional: Decimal,
        loss_per_lot: Decimal,
    ) -> None:
        pf, equity = inp.portfolio, inp.equity
        total_after = pf.total + risk
        cluster_before = pf.by_cluster.get(inp.cluster, ZERO)
        family_before = pf.by_family.get(inp.family, ZERO)
        market_before = pf.by_market.get(inp.market, ZERO)
        detail.update(
            {
                "quantity": quantity,
                "stop_risk_eur": risk,
                "equity_risk_fraction": risk / equity,
                "notional_eur": notional,
                "leverage": notional / equity,
                "portfolio_leverage_after": (pf.gross_notional + notional) / equity,
                "portfolio_risk_before": pf.total,
                "portfolio_risk_after": total_after,
                "portfolio_risk_fraction_before": pf.total / equity,
                "portfolio_risk_fraction_after": total_after / equity,
                "cluster_risk_before": cluster_before,
                "cluster_risk_after": cluster_before + risk,
                "cluster_risk_fraction_after": (cluster_before + risk) / equity,
                "family_risk_before": family_before,
                "family_risk_after": family_before + risk,
                "market_risk_before": market_before,
                "market_risk_after": market_before + risk,
                "concentration_after": {
                    "market": _share(market_before + risk, total_after),
                    "cluster": _share(cluster_before + risk, total_after),
                    "family": _share(family_before + risk, total_after),
                },
                "concentration_before": {
                    "market": _share(market_before, pf.total),
                    "cluster": _share(cluster_before, pf.total),
                    "family": _share(family_before, pf.total),
                },
            }
        )
        if inp.margin_per_lot is not None:
            detail["margin_required"] = quantity * inp.margin_per_lot
