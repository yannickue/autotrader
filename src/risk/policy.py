"""PURE risk policy: admission gates + sizing -> a `ProposedOrderIntent`.

    Market / Account / Strategy inputs
                 |
          RiskPolicyEvaluator      (gates, this module; pure)
                 |
            PositionSizer          (risk.sizing; pure)
                 |
        ProposedOrderIntent        (a proposal, never an order)
                 |
    [later] Nautilus RiskEngine performs final runtime admission

PURE means: deterministic; no mutable reservation ledger; no authoritative
portfolio/order/position state; no broker or MT5 calls; no clock reads; no
globals; nothing here owns or creates an order. State that used to live in the
legacy `RiskEngine` is now an explicit INPUT:

- `latched_halt`: the caller's halt latch (a boolean, not owned here);
- `pending`: exposure already reserved by not-yet-filled approvals
  (`PendingExposure`), supplied by whoever owns the open-order ledger (the
  legacy `RiskEngine` today, Nautilus' portfolio/cache later);
- `reserved_reduce_only_quantity` for reduce-only checks.

Validated trading semantics are unchanged: the gate ORDER, reason codes and
arithmetic are identical to the pre-split `RiskEngine._evaluate_inner`, so the
legacy engine remains a shadow oracle (`tests/unit/risk/test_policy_parity.py`).
The two intentional behavior corrections in this slice are documented in
docs/RISK_CONTRACT.md ("Daily PnL window", "Reconciliation state").
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from data.models import DataQuality, MarketSnapshot
from margin.engine import MarginEngine
from margin.models import (
    MaintenanceMarginPolicy,
    MarginPositionSide,
    MarginSafetyReason,
    VenueUncertaintyBuffer,
)
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
from risk.sizing import ExposureCapacity, PositionSizer, RiskRejection
from risk.trading_day import MAX_TRADING_DAY

ZERO = Decimal("0")
TEN_THOUSAND = Decimal("10000")


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskHookContext:
    """Read-only context handed to guard hooks after sizing but before approval."""

    request: PositionSizingRequest
    snapshot: MarketSnapshot
    account: AccountRiskState
    instrument: InstrumentRiskLimits
    proposed_quantity: Decimal
    proposed_notional: Decimal


RiskHook = Callable[[RiskHookContext], str | None]


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingExposure:
    """Notional already committed by approved-but-unfilled requests."""

    gross: Decimal = ZERO
    net: Decimal = ZERO


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposedOrderIntent:
    """An admitted proposal. Not an order: it owns no lifecycle or state."""

    instrument: str
    side: RiskSide
    quantity: Decimal
    notional: Decimal
    leverage: Decimal
    max_leverage: Decimal
    risk_budget: Decimal
    stop_price: Decimal
    reference_price: Decimal
    reduce_only: bool
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyRejection:
    reason_code: RiskReason | str
    reason: str


def _finite(value: Decimal) -> bool:
    return not (value.is_nan() or value.is_infinite())


def _utc_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def readiness_rejection(runtime: RuntimeRiskState, *, latched_halt: bool) -> RiskReason | None:
    """Halt/runtime-state reason a new-exposure evaluation must reject on, if any."""
    if latched_halt or runtime.kill_switch or runtime.mode == RuntimeMode.HALTED:
        return RiskReason.HALTED
    if runtime.mode != RuntimeMode.READY or not runtime.risk_ready:
        return RiskReason.RUNTIME_NOT_READY
    return None


def require_account_known_and_reconciled(account: AccountRiskState) -> None:
    """Shared by new-exposure and reduce-only paths: unknown account state, or
    any reconciliation state other than RECONCILED, blocks the request."""
    if not account.known:
        raise RiskRejection(RiskReason.ACCOUNT_UNKNOWN)
    if account.reconciliation is not ReconciliationState.RECONCILED:
        # One reason code for every non-RECONCILED state (unchanged public
        # contract); the text says which state, so audit logs can tell a
        # never-compared account from a mismatched one.
        raise RiskRejection(
            RiskReason.ACCOUNT_UNRECONCILED,
            f"account not reconciled (state={account.reconciliation.value})",
        )


class RiskPolicyEvaluator:
    """Stateless evaluator: (policy, inputs) -> ProposedOrderIntent | PolicyRejection."""

    def __init__(
        self,
        policy: RiskPolicy,
        *,
        hooks: Sequence[RiskHook] = (),
        sizer: PositionSizer | None = None,
    ) -> None:
        self._policy = policy
        self._hooks: tuple[RiskHook, ...] = tuple(hooks)
        self._sizer = sizer if sizer is not None else PositionSizer(policy)
        self._margin = MarginEngine()  # stateless (see margin.engine docstring)

    @property
    def policy(self) -> RiskPolicy:
        return self._policy

    # -- new exposure -----------------------------------------------------

    def evaluate_entry(
        self,
        *,
        request: PositionSizingRequest,
        snapshot: MarketSnapshot,
        account: AccountRiskState,
        runtime: RuntimeRiskState,
        instrument: InstrumentRiskLimits,
        now: datetime,
        pending: PendingExposure | None = None,
        latched_halt: bool = False,
    ) -> ProposedOrderIntent | PolicyRejection:
        """Unexpected internal errors propagate (the caller decides how to
        fail closed); expected rejections are returned as `PolicyRejection`."""
        try:
            return self._evaluate_entry(
                request=request,
                snapshot=snapshot,
                account=account,
                runtime=runtime,
                instrument=instrument,
                now=now,
                pending=pending if pending is not None else PendingExposure(),
                latched_halt=latched_halt,
            )
        except RiskRejection as rejection:
            return PolicyRejection(reason_code=rejection.reason_code, reason=rejection.reason)

    def _evaluate_entry(
        self,
        *,
        request: PositionSizingRequest,
        snapshot: MarketSnapshot,
        account: AccountRiskState,
        runtime: RuntimeRiskState,
        instrument: InstrumentRiskLimits,
        now: datetime,
        pending: PendingExposure,
        latched_halt: bool,
    ) -> ProposedOrderIntent:
        policy = self._policy
        sizer = self._sizer

        not_ready = readiness_rejection(runtime, latched_halt=latched_halt)
        if not_ready is not None:
            raise RiskRejection(not_ready)

        require_account_known_and_reconciled(account)

        self._check_invalid_input(
            request=request, account=account, instrument=instrument, snapshot=snapshot, now=now
        )

        if account.equity <= 0 or account.peak_equity <= 0:
            raise RiskRejection(RiskReason.EQUITY_NON_POSITIVE)

        if request.instrument != snapshot.instrument or request.instrument != instrument.instrument:
            raise RiskRejection(RiskReason.INSTRUMENT_MISMATCH)

        if snapshot.quality != DataQuality.LIVE:
            raise RiskRejection(RiskReason.DATA_NOT_LIVE)

        if snapshot.timestamp > now or (now - snapshot.timestamp) > policy.max_data_age:
            raise RiskRejection(RiskReason.DATA_STALE)

        if request.timestamp > now or (now - request.timestamp) > policy.max_signal_age:
            raise RiskRejection(RiskReason.SIGNAL_STALE)

        if snapshot.bid <= 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)
        mid = (snapshot.bid + snapshot.ask) / 2
        spread_bps = (snapshot.ask - snapshot.bid) / mid * TEN_THOUSAND
        if spread_bps > instrument.max_spread_bps:
            raise RiskRejection(RiskReason.SPREAD_TOO_WIDE)

        reference_price = sizer.reference_price(side=request.side, snapshot=snapshot)
        if reference_price <= 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)
        volatility_bps = (snapshot.volatility or ZERO) * TEN_THOUSAND
        deviation_tolerance_bps = max(
            policy.reference_price_min_tolerance_bps,
            policy.reference_price_spread_tolerance_multiplier * spread_bps,
            policy.reference_price_volatility_tolerance_multiplier * volatility_bps,
        )
        deviation_bps = (
            abs(request.entry_price - reference_price) / reference_price * TEN_THOUSAND
        )
        if deviation_bps > deviation_tolerance_bps:
            raise RiskRejection(RiskReason.ENTRY_PRICE_DEVIATION)

        stop_distance = sizer.stop_distance(request, reference_price)

        if policy.max_volatility is not None:
            if snapshot.volatility is None:
                raise RiskRejection(RiskReason.DATA_MISSING)
            if snapshot.volatility > policy.max_volatility:
                raise RiskRejection(RiskReason.VOLATILITY_TOO_HIGH)

        if account.realized_pnl_today + account.unrealized_pnl <= -policy.max_daily_loss:
            raise RiskRejection(RiskReason.DAILY_LOSS_LIMIT)

        if account.peak_equity - account.equity >= policy.max_drawdown:
            raise RiskRejection(RiskReason.DRAWDOWN_LIMIT)

        if account.consecutive_losses >= policy.max_consecutive_losses:
            raise RiskRejection(RiskReason.CONSECUTIVE_LOSS_LIMIT)

        if request.available_liquidity_notional <= 0:
            raise RiskRejection(RiskReason.LIQUIDITY_INSUFFICIENT)

        max_leverage = sizer.max_leverage(instrument=instrument, account=account)

        remaining_gross = policy.max_gross_notional - account.gross_notional - pending.gross
        remaining_portfolio_leverage = (
            max_leverage * account.equity - account.gross_notional - pending.gross
        )
        if request.side == RiskSide.BUY:
            remaining_net = policy.max_net_notional - account.net_notional - pending.net
        else:
            remaining_net = policy.max_net_notional + account.net_notional + pending.net

        if remaining_gross <= 0 or remaining_net <= 0 or remaining_portfolio_leverage <= 0:
            raise RiskRejection(RiskReason.EXPOSURE_LIMIT)

        sizing = sizer.size(
            request=request,
            account=account,
            instrument=instrument,
            reference_price=reference_price,
            stop_distance=stop_distance,
            max_leverage=max_leverage,
            capacity=ExposureCapacity(
                remaining_gross=remaining_gross,
                remaining_net=remaining_net,
                remaining_portfolio_leverage=remaining_portfolio_leverage,
            ),
            pending_gross=pending.gross,
        )
        quantity, notional = sizing.quantity, sizing.notional
        account_gross_leverage_after = (
            account.gross_notional + pending.gross + notional
        ) / account.equity

        # Margin/liquidation-safety check (docs/OPEN_QUESTIONS.md #25, Q-M1):
        # account-wide cross-margin worst case, reference price (never
        # request.entry_price).
        margin_side = (
            MarginPositionSide.LONG if request.side == RiskSide.BUY else MarginPositionSide.SHORT
        )
        margin_estimate = self._margin.evaluate_stop_safety(
            side=margin_side,
            entry_price=reference_price,
            stop_price=request.stop_price,
            leverage=account_gross_leverage_after,
            maintenance_policy=MaintenanceMarginPolicy(
                maintenance_margin_rate=instrument.maintenance_margin_rate
            ),
            uncertainty_buffer=VenueUncertaintyBuffer(
                buffer_bps=policy.liquidation_uncertainty_buffer_bps
            ),
            min_required_distance_bps=policy.min_stop_liquidation_distance_bps,
        )
        if not margin_estimate.safe:
            raise RiskRejection(
                RiskReason.MARGIN_STOP_BEYOND_LIQUIDATION
                if margin_estimate.reason_code == MarginSafetyReason.STOP_BEYOND_LIQUIDATION
                else RiskReason.MARGIN_STOP_TOO_CLOSE_TO_LIQUIDATION
            )

        for hook in self._hooks:
            hook_reason = hook(
                RiskHookContext(
                    request=request,
                    snapshot=snapshot,
                    account=account,
                    instrument=instrument,
                    proposed_quantity=quantity,
                    proposed_notional=notional,
                )
            )
            if hook_reason is not None:
                raise RiskRejection(f"HOOK:{hook_reason}")

        return ProposedOrderIntent(
            instrument=request.instrument,
            side=request.side,
            quantity=quantity,
            notional=notional,
            leverage=sizing.leverage,
            max_leverage=max_leverage,
            risk_budget=sizing.risk_budget,
            stop_price=request.stop_price,
            reference_price=reference_price,
            reduce_only=False,
            metadata={
                "policy_id": policy.policy_id,
                "side": request.side.value,
                "account_state_version": account.state_version,
                "runtime_state_version": runtime.state_version,
                "effective_risk_fraction": str(sizing.effective_risk_fraction),
                "reduce_risk": sizing.reduce_risk,
                "binding_constraint": sizing.binding_constraint,
                "spread_bps": str(spread_bps),
                "per_unit_loss": str(sizing.per_unit_loss),
                "account_gross_leverage_after": str(account_gross_leverage_after),
                "risk_reference_price": str(reference_price),
                "entry_price_deviation_bps": str(deviation_bps),
                "entry_price_deviation_tolerance_bps": str(deviation_tolerance_bps),
                "liquidation_estimate_buffered": str(
                    margin_estimate.liquidation_estimate.buffered_liquidation_price
                ),
                "stop_liquidation_distance_bps": str(margin_estimate.distance_bps),
                "liquidation_is_estimate": True,
            },
        )

    # -- reduce-only ------------------------------------------------------

    def evaluate_reduce_only(
        self,
        *,
        instrument: str,
        side: RiskSide,
        quantity: Decimal,
        account: AccountRiskState,
        now: datetime,
        reserved_reduce_only_quantity: Decimal = ZERO,
    ) -> ProposedOrderIntent | PolicyRejection:
        """A reduce-only proposal can never add exposure or flip a position.

        Ignores halt/kill-switch on purpose (reduce-only stays available while
        halted / limited) but still requires known account state and
        RECONCILED reconciliation.
        """
        try:
            require_account_known_and_reconciled(account)
            if not _finite(quantity) or quantity <= 0:
                raise RiskRejection(RiskReason.QUANTITY_INVALID)
            if not _utc_aware(now):
                raise RiskRejection(RiskReason.INVALID_INPUT)

            position = account.positions.get(instrument, ZERO)
            if not _finite(position):
                raise RiskRejection(RiskReason.INVALID_INPUT)
            if position == 0:
                raise RiskRejection(RiskReason.NO_POSITION)

            position_side = RiskSide.BUY if position > 0 else RiskSide.SELL
            if side == position_side:
                raise RiskRejection(RiskReason.SIDE_MISMATCH)

            if quantity + reserved_reduce_only_quantity > abs(position):
                raise RiskRejection(RiskReason.QUANTITY_INVALID)
        except RiskRejection as rejection:
            return PolicyRejection(reason_code=rejection.reason_code, reason=rejection.reason)

        return ProposedOrderIntent(
            instrument=instrument,
            side=side,
            quantity=quantity,
            notional=ZERO,  # reduce-only never adds exposure
            leverage=ZERO,
            max_leverage=MAX_SYSTEM_LEVERAGE,
            risk_budget=ZERO,
            stop_price=ZERO,
            reference_price=ZERO,
            reduce_only=True,
            metadata={
                "reduce_only": True,
                "side": side.value,
                "account_state_version": account.state_version,
            },
        )

    # -- input validation -------------------------------------------------

    def _check_invalid_input(
        self,
        *,
        request: PositionSizingRequest,
        account: AccountRiskState,
        instrument: InstrumentRiskLimits,
        snapshot: MarketSnapshot,
        now: datetime,
    ) -> None:
        decimals: list[Decimal] = [
            request.entry_price,
            request.stop_price,
            request.confidence,
            request.available_liquidity_notional,
            account.equity,
            account.peak_equity,
            account.realized_pnl_today,
            account.unrealized_pnl,
            account.gross_notional,
            account.net_notional,
            account.leverage_cap,
            instrument.max_leverage,
            instrument.quantity_step,
            instrument.min_quantity,
            instrument.min_notional,
            instrument.max_notional,
            instrument.max_spread_bps,
            snapshot.bid,
            snapshot.ask,
            snapshot.last,
            snapshot.volume,
        ]
        if snapshot.volatility is not None:
            decimals.append(snapshot.volatility)
        if snapshot.liquidity is not None:
            decimals.append(snapshot.liquidity)
        decimals.extend(account.instrument_notionals.values())
        decimals.extend(account.positions.values())

        for value in decimals:
            if not _finite(value):
                raise RiskRejection(RiskReason.INVALID_INPUT)

        for dt in (request.timestamp, snapshot.timestamp, now, account.pnl_window_start):
            if not _utc_aware(dt):
                raise RiskRejection(RiskReason.INVALID_INPUT)

        # `realized_pnl_today` must be a single-trading-day figure: its window
        # cannot start in the future or more than one (DST-extended) day ago.
        # This is what stops an all-time realized PnL from being passed under
        # that name.
        if account.pnl_window_start > now or (now - account.pnl_window_start) > MAX_TRADING_DAY:
            raise RiskRejection(RiskReason.INVALID_INPUT)

        if request.entry_price <= 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)
        if request.stop_price <= 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)

        if instrument.quantity_step <= 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)
        if instrument.min_quantity < 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)
        if instrument.min_notional < 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)
        if instrument.max_notional <= 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)
        if instrument.max_spread_bps < 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)

        if account.gross_notional < 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)
        if account.leverage_cap <= 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)
        if account.consecutive_losses < 0:
            raise RiskRejection(RiskReason.INVALID_INPUT)
