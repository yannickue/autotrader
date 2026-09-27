"""Deterministic, fail-closed position sizing and gating for linear USD-M instruments."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from data.models import DataQuality, MarketSnapshot
from risk.models import (
    MAX_SYSTEM_LEVERAGE,
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    RiskDecision,
    RiskPolicy,
    RiskReason,
    RiskSide,
    RuntimeMode,
    RuntimeRiskState,
)

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


class RiskRejection(Exception):
    """Internal control-flow exception carrying a machine-readable reason code."""

    def __init__(self, reason_code: RiskReason | str, reason: str | None = None) -> None:
        self.reason_code = reason_code
        self.reason = reason or str(reason_code)
        super().__init__(self.reason)


def _finite(value: Decimal) -> bool:
    return not (value.is_nan() or value.is_infinite())


def _utc_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


class RiskEngine:
    """Evaluate exposure-changing requests against an immutable policy.

    Fails closed: any unexpected exception during evaluation is converted into a
    rejected decision (reason_code RISK_ERROR) and latches the engine into HALT
    until :meth:`reset_halt` is called explicitly.
    """

    def __init__(self, policy: RiskPolicy, hooks: Sequence[RiskHook] | None = None) -> None:
        self._policy = policy
        self._hooks: tuple[RiskHook, ...] = tuple(hooks) if hooks else ()
        self._halted = False
        self._halt_reason: str | None = None
        self._decisions: dict[str, RiskDecision] = {}
        # decision_id -> (side, abs_notional, signed_notional)
        self._reservations: dict[str, tuple[RiskSide, Decimal, Decimal]] = {}
        # decision_id -> (instrument, quantity); tracks reduce-only requests
        # that have been approved but not yet released (filled/canceled), so
        # concurrent reduce-only requests against the same position can't
        # jointly reduce past zero and flip it (RISK_CONTRACT.md: reduce-only
        # can never increase absolute or directional exposure).
        self._reduce_only_reservations: dict[str, tuple[str, Decimal]] = {}

    # -- halt management -------------------------------------------------

    @property
    def halted(self) -> bool:
        return self._halted

    def halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason

    def reset_halt(self, operator_note: str) -> None:
        if not operator_note:
            raise ValueError("operator_note is required to reset a halt")
        self._halted = False
        self._halt_reason = None

    # -- reservation ledger -----------------------------------------------

    @property
    def _reserved_gross(self) -> Decimal:
        return sum((abs_notional for _, abs_notional, _ in self._reservations.values()), ZERO)

    @property
    def _reserved_net(self) -> Decimal:
        return sum((signed for _, _, signed in self._reservations.values()), ZERO)

    def release(self, decision_id: str) -> None:
        """Idempotent release of a reservation (fill-applied or cancel)."""
        self._reservations.pop(decision_id, None)
        self._reduce_only_reservations.pop(decision_id, None)

    def _reserved_reduce_only_quantity(self, instrument: str) -> Decimal:
        return sum(
            (qty for inst, qty in self._reduce_only_reservations.values() if inst == instrument),
            ZERO,
        )

    # -- public API ---------------------------------------------------------

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
        decision_id = f"risk:{request.signal_id}"
        cached = self._decisions.get(decision_id)
        if cached is not None:
            if cached.approved and (
                self._halted or runtime.kill_switch or runtime.mode == RuntimeMode.HALTED
            ):
                # Fail-closed: the engine has halted since this decision was
                # approved. Do not silently re-hand out a stale approval —
                # the stored decision stays in the audit trail unchanged,
                # but a caller re-querying it now gets a fresh rejection.
                return self._reject(
                    decision_id=decision_id,
                    request=request,
                    reason_code=RiskReason.HALTED,
                    reason="engine halted since this decision was approved; "
                    "cached approval withheld",
                )
            return cached

        try:
            decision = self._evaluate_inner(
                decision_id=decision_id,
                request=request,
                snapshot=snapshot,
                account=account,
                runtime=runtime,
                instrument=instrument,
                now=now,
            )
        except RiskRejection as rejection:
            decision = self._reject(
                decision_id=decision_id,
                request=request,
                reason_code=rejection.reason_code,
                reason=rejection.reason,
            )
        except Exception as exc:
            self.halt(f"RISK_ERROR: {exc}")
            decision = self._reject(
                decision_id=decision_id,
                request=request,
                reason_code=RiskReason.RISK_ERROR,
                reason=f"unexpected error: {exc}",
            )

        self._decisions[decision_id] = decision
        return decision

    def evaluate_reduce_only(
        self,
        *,
        request_id: str,
        instrument: str,
        side: RiskSide,
        quantity: Decimal,
        account: AccountRiskState,
        runtime: RuntimeRiskState,
        now: datetime,
    ) -> RiskDecision:
        del runtime  # reduce-only is allowed even while halted/limited
        decision_id = f"reduce-only:{request_id}"
        cached = self._decisions.get(decision_id)
        if cached is not None:
            return cached

        try:
            decision = self._evaluate_reduce_only_inner(
                decision_id=decision_id,
                request_id=request_id,
                instrument=instrument,
                side=side,
                quantity=quantity,
                account=account,
                now=now,
            )
        except RiskRejection as rejection:
            decision = self._reduce_only_reject(
                decision_id=decision_id,
                request_id=request_id,
                instrument=instrument,
                side=side,
                now=now,
                reason_code=rejection.reason_code,
                reason=rejection.reason,
            )
        except Exception as exc:
            self.halt(f"RISK_ERROR: {exc}")
            decision = self._reduce_only_reject(
                decision_id=decision_id,
                request_id=request_id,
                instrument=instrument,
                side=side,
                now=now,
                reason_code=RiskReason.RISK_ERROR,
                reason=f"unexpected error: {exc}",
            )

        self._decisions[decision_id] = decision
        return decision

    # -- reduce-only ---------------------------------------------------------

    def _evaluate_reduce_only_inner(
        self,
        *,
        decision_id: str,
        request_id: str,
        instrument: str,
        side: RiskSide,
        quantity: Decimal,
        account: AccountRiskState,
        now: datetime,
    ) -> RiskDecision:
        del request_id
        if not account.known:
            raise RiskRejection(RiskReason.ACCOUNT_UNKNOWN)
        if not account.reconciled:
            raise RiskRejection(RiskReason.ACCOUNT_UNRECONCILED)
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

        already_reserved = self._reserved_reduce_only_quantity(instrument)
        if quantity + already_reserved > abs(position):
            raise RiskRejection(RiskReason.QUANTITY_INVALID)

        self._reduce_only_reservations[decision_id] = (instrument, quantity)

        # Reduce-only never adds exposure; the exposure-increasing notional is zero.
        notional = ZERO
        return RiskDecision(
            decision_id=decision_id,
            signal_id=decision_id,
            instrument=instrument,
            timestamp=now,
            approved=True,
            reason="reduce-only approved",
            reason_code=RiskReason.APPROVED,
            quantity=quantity,
            notional=notional,
            leverage=ZERO,
            max_leverage=MAX_SYSTEM_LEVERAGE,
            risk_budget=ZERO,
            stop_price=None,
            metadata={
                "reduce_only": True,
                "side": side.value,
                "account_state_version": account.state_version,
            },
        )

    def _reduce_only_reject(
        self,
        *,
        decision_id: str,
        request_id: str,
        instrument: str,
        side: RiskSide,
        now: datetime,
        reason_code: RiskReason | str,
        reason: str,
    ) -> RiskDecision:
        del request_id
        return RiskDecision(
            decision_id=decision_id,
            signal_id=decision_id,
            instrument=instrument,
            timestamp=now,
            approved=False,
            reason=reason,
            reason_code=reason_code,
            quantity=ZERO,
            notional=ZERO,
            leverage=ZERO,
            max_leverage=MAX_SYSTEM_LEVERAGE,
            risk_budget=ZERO,
            stop_price=None,
            metadata={"reduce_only": True, "side": side.value},
        )

    # -- main evaluation ------------------------------------------------------

    def _reject(
        self,
        *,
        decision_id: str,
        request: PositionSizingRequest,
        reason_code: RiskReason | str,
        reason: str,
    ) -> RiskDecision:
        return RiskDecision(
            decision_id=decision_id,
            signal_id=request.signal_id,
            instrument=request.instrument,
            timestamp=request.timestamp,
            approved=False,
            reason=reason,
            reason_code=reason_code,
            quantity=ZERO,
            notional=ZERO,
            leverage=ZERO,
            max_leverage=MAX_SYSTEM_LEVERAGE,
            risk_budget=ZERO,
            stop_price=None,
            metadata={"side": request.side.value},
        )

    def _evaluate_inner(
        self,
        *,
        decision_id: str,
        request: PositionSizingRequest,
        snapshot: MarketSnapshot,
        account: AccountRiskState,
        runtime: RuntimeRiskState,
        instrument: InstrumentRiskLimits,
        now: datetime,
    ) -> RiskDecision:
        policy = self._policy

        if self._halted or runtime.kill_switch or runtime.mode == RuntimeMode.HALTED:
            raise RiskRejection(RiskReason.HALTED)

        if runtime.mode != RuntimeMode.READY or not runtime.risk_ready:
            raise RiskRejection(RiskReason.RUNTIME_NOT_READY)

        if not account.known:
            raise RiskRejection(RiskReason.ACCOUNT_UNKNOWN)
        if not account.reconciled:
            raise RiskRejection(RiskReason.ACCOUNT_UNRECONCILED)

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

        stop_distance = self._stop_distance(request)

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

        max_leverage = min(
            MAX_SYSTEM_LEVERAGE,
            policy.max_leverage,
            instrument.max_leverage,
            account.leverage_cap,
        )

        reserved_gross = self._reserved_gross
        reserved_net = self._reserved_net

        remaining_gross = policy.max_gross_notional - account.gross_notional - reserved_gross
        remaining_portfolio_leverage = (
            max_leverage * account.equity - account.gross_notional - reserved_gross
        )
        if request.side == RiskSide.BUY:
            remaining_net = policy.max_net_notional - account.net_notional - reserved_net
        else:
            remaining_net = policy.max_net_notional + account.net_notional + reserved_net

        if remaining_gross <= 0 or remaining_net <= 0 or remaining_portfolio_leverage <= 0:
            raise RiskRejection(RiskReason.EXPOSURE_LIMIT)

        drawdown_trigger = policy.reduce_risk_drawdown_fraction * policy.max_drawdown
        daily_loss_trigger = policy.reduce_risk_daily_loss_fraction * policy.max_daily_loss
        reduce_risk = (
            (account.peak_equity - account.equity) >= drawdown_trigger
            or (-(account.realized_pnl_today + account.unrealized_pnl)) >= daily_loss_trigger
            or account.consecutive_losses >= policy.reduce_risk_after_losses
        )
        effective_risk_fraction = policy.risk_fraction * (
            policy.reduce_risk_multiplier if reduce_risk else Decimal("1")
        )
        risk_budget = account.equity * effective_risk_fraction
        round_trip_cost = request.entry_price * policy.estimated_cost_bps * 2 / TEN_THOUSAND
        per_unit_loss = stop_distance + round_trip_cost
        qty_risk = risk_budget / per_unit_loss

        entry = request.entry_price
        candidates: dict[str, Decimal] = {
            "risk_budget": qty_risk,
            "leverage_cap": max_leverage * account.equity / entry,
            "instrument_max_notional": instrument.max_notional / entry,
            "liquidity": policy.liquidity_fraction * request.available_liquidity_notional / entry,
            "gross_capacity": remaining_gross / entry,
            "portfolio_leverage_capacity": remaining_portfolio_leverage / entry,
            "net_capacity": remaining_net / entry,
        }
        binding_constraint = min(candidates, key=lambda key: candidates[key])
        raw_quantity = min(candidates.values())
        raw_quantity = max(raw_quantity, ZERO)

        quantity = (raw_quantity / instrument.quantity_step).to_integral_value(
            rounding=ROUND_DOWN
        ) * instrument.quantity_step
        notional = quantity * entry

        if quantity < instrument.min_quantity or notional < instrument.min_notional:
            raise RiskRejection(RiskReason.SIZE_BELOW_MINIMUM)

        for hook in self._hooks:
            context = RiskHookContext(
                request=request,
                snapshot=snapshot,
                account=account,
                instrument=instrument,
                proposed_quantity=quantity,
                proposed_notional=notional,
            )
            hook_reason = hook(context)
            if hook_reason is not None:
                raise RiskRejection(f"HOOK:{hook_reason}")

        leverage = notional / account.equity
        if leverage > max_leverage:
            raise RuntimeError("computed leverage exceeds max_leverage")
        gross_after_fill = account.gross_notional + reserved_gross + notional
        account_gross_leverage_after = gross_after_fill / account.equity
        if account_gross_leverage_after > max_leverage:
            raise RuntimeError("account gross leverage after fill exceeds max_leverage")

        signed_notional = notional if request.side == RiskSide.BUY else -notional
        self._reservations[decision_id] = (request.side, notional, signed_notional)

        return RiskDecision(
            decision_id=decision_id,
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
            metadata={
                "policy_id": policy.policy_id,
                "side": request.side.value,
                "account_state_version": account.state_version,
                "runtime_state_version": runtime.state_version,
                "effective_risk_fraction": str(effective_risk_fraction),
                "reduce_risk": reduce_risk,
                "binding_constraint": binding_constraint,
                "spread_bps": str(spread_bps),
                "per_unit_loss": str(per_unit_loss),
                "account_gross_leverage_after": str(account_gross_leverage_after),
            },
        )

    def _stop_distance(self, request: PositionSizingRequest) -> Decimal:
        if request.side == RiskSide.BUY:
            if request.stop_price >= request.entry_price:
                raise RiskRejection(RiskReason.INVALID_STOP)
        else:
            if request.stop_price <= request.entry_price:
                raise RiskRejection(RiskReason.INVALID_STOP)
        return abs(request.entry_price - request.stop_price)

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

        for dt in (request.timestamp, snapshot.timestamp, now):
            if not _utc_aware(dt):
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
