"""Deterministic paper execution engine.

PAPER ONLY. No venue/network I/O occurs anywhere in this module; all state
transitions are driven by explicitly supplied requests, quotes, and trade events
with an injected clock (`now`), never by wall-clock reads.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from costs.engine import calculate_fill_fee
from costs.models import LiquidityRole, VenueCostSchedule
from data.models import DataQuality, MarketSnapshot
from execution.events import TradeEvent
from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from execution.orders import (
    CancelReplaceResult,
    CancelResult,
    ChildRole,
    HaltCode,
    Order,
    OrderStatus,
    RejectCode,
    SubmitResult,
    is_legal_transition,
)
from portfolio.ledger import Portfolio
from portfolio.models import Fill
from risk.models import RiskDecision

ZERO = Decimal("0")
BPS = Decimal("10000")


class EngineMode(StrEnum):
    STARTING = "starting"
    RECONCILING = "reconciling"
    READY = "ready"
    HALTED = "halted"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionConfig:
    max_request_age: timedelta
    max_decision_age: timedelta
    max_quote_age: timedelta
    max_order_age: timedelta
    max_spread_bps: Decimal
    max_slippage_bps: Decimal
    slippage_bps: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class FillEvent:
    """Observable record of one real (non-duplicate) fill applied to the portfolio.

    Fired synchronously from `_apply_fill`, after `Portfolio.apply_fill`
    returns `True`, to whatever `PaperExecutionEngine.fill_listener` is set
    to. This is how a caller (e.g. `PaperTradingPipeline`, or a later exit
    engine) observes the ACTUAL fill price/fee/liquidity-role instead of
    inferring them from the quote-side reference price used to build the
    request.
    """

    fill_id: str
    client_order_id: str
    decision_id: str
    instrument: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    liquidity_role: LiquidityRole
    reduce_only: bool
    timestamp: datetime


def _is_utc(ts: datetime) -> bool:
    return ts.tzinfo is not None and ts.utcoffset() == timedelta(0)


def _finite_positive(value: Decimal | None) -> bool:
    if value is None:
        return True
    return value.is_finite() and value > ZERO


class PaperExecutionEngine:
    def __init__(
        self,
        config: ExecutionConfig,
        portfolio: Portfolio,
        cost_schedule: VenueCostSchedule,
        *,
        fill_listener: Callable[[FillEvent], None] | None = None,
    ) -> None:
        self.config = config
        self.portfolio = portfolio
        self._cost_schedule = cost_schedule
        self.fill_listener = fill_listener
        self.mode: EngineMode = EngineMode.RECONCILING
        self.halt_reason: str | None = None
        self._orders: dict[str, Order] = {}
        self._requests: dict[str, SubmitResult] = {}
        self._client_id_owner: dict[str, str] = {}
        self._decision_usage: dict[str, str] = {}
        # decision_id -> risk-approved quantity, recorded once at the first
        # submit() that consumes it. decision_id -> cumulative filled
        # quantity across EVERY order in that decision's replacement chain
        # (cancel_replace() reuses the original's decision_id for the
        # replacement, and the original order's own `.quantity` is never
        # reduced, so a per-order fill cap alone cannot catch a late fill on
        # a replaced order plus a fill on its replacement together
        # exceeding what risk approved).
        self._decision_approved_quantity: dict[str, Decimal] = {}
        self._decision_filled_quantity: dict[str, Decimal] = {}
        self._seen_fill_keys: set[tuple[str, str]] = set()
        self.audit_log: list[dict[str, Any]] = []

    # -- lifecycle -----------------------------------------------------

    def _log(self, event: str, **fields: Any) -> None:
        self.audit_log.append({"event": event, **fields})

    def _halt(self, code: HaltCode, reason: str) -> None:
        self.mode = EngineMode.HALTED
        self.halt_reason = f"{code}: {reason}"
        self._log("halt", code=str(code), reason=reason)

    def reconcile(self, venue_state: Mapping[str, Any], now: datetime) -> bool:
        """Compare local open orders/positions against a supplied venue snapshot."""
        mismatch = self._find_reconciliation_mismatch(venue_state)
        if mismatch is not None:
            self._halt(HaltCode.RECONCILIATION_MISMATCH, mismatch)
            return False
        self.mode = EngineMode.READY
        self.halt_reason = None
        self._log("reconciled", at=now.isoformat())
        return True

    def resume_after_reconcile(self, venue_state: Mapping[str, Any], now: datetime) -> bool:
        if self.mode is not EngineMode.HALTED:
            return self.mode is EngineMode.READY
        self.mode = EngineMode.RECONCILING
        return self.reconcile(venue_state, now)

    def _find_reconciliation_mismatch(self, venue_state: Mapping[str, Any]) -> str | None:
        venue_orders: Mapping[str, Any] = venue_state.get("orders", {})
        venue_positions: Mapping[str, Any] = venue_state.get("positions", {})

        local_open = {
            client_id: order for client_id, order in self._orders.items() if not order.is_terminal()
        }
        for client_id in venue_orders:
            if client_id not in local_open:
                return f"unknown external order {client_id}"
        for client_id in local_open:
            if client_id not in venue_orders:
                return f"local open order missing at venue: {client_id}"

        local_positions = self.portfolio.positions
        instruments = set(venue_positions) | set(local_positions)
        for instrument in instruments:
            venue_qty = Decimal(str(venue_positions.get(instrument, "0")))
            local_qty = (
                local_positions[instrument].quantity if instrument in local_positions else ZERO
            )
            if venue_qty != local_qty:
                return f"position mismatch for {instrument}: venue={venue_qty} local={local_qty}"
        return None

    # -- submission ------------------------------------------------------

    def submit(
        self,
        request: ExecutionRequest,
        decision: RiskDecision | None,
        quote: MarketSnapshot,
        now: datetime,
    ) -> SubmitResult:
        if request.request_id in self._requests:
            return self._requests[request.request_id]

        existing_owner = self._client_id_owner.get(request.client_order_id)
        if existing_owner is not None and existing_owner != request.request_id:
            return self._reject_transient(
                request,
                RejectCode.DUPLICATE_CONFLICT,
                "client_order_id reused with a different request",
            )

        result = self._validate_and_execute(request, decision, quote, now)
        self._requests[request.request_id] = result
        return result

    def _reject_transient(
        self, request: ExecutionRequest, code: RejectCode, reason: str
    ) -> SubmitResult:
        # Rejections that must not halt the engine and are not remembered as an
        # order (e.g. DUPLICATE_CONFLICT): still recorded in the audit log.
        self._log("submit_rejected", request_id=request.request_id, code=str(code), reason=reason)
        return SubmitResult(
            accepted=False,
            client_order_id=request.client_order_id,
            status=OrderStatus.REJECTED,
            reject_code=code,
            reason=reason,
        )

    def _validate_and_execute(
        self,
        request: ExecutionRequest,
        decision: RiskDecision | None,
        quote: MarketSnapshot,
        now: datetime,
    ) -> SubmitResult:
        reject = self._validate_submit(request, decision, quote, now)
        if reject is not None:
            code, reason = reject
            return self._reject_transient(request, code, reason)

        assert decision is not None
        order = Order(
            client_order_id=request.client_order_id,
            request_id=request.request_id,
            decision_id=decision.decision_id,
            instrument=request.instrument,
            side=request.side,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            quantity=request.quantity,
            limit_price=request.limit_price,
            reduce_only=request.reduce_only,
            created_at=now,
            updated_at=now,
            trigger_price=decision.stop_price,
            take_profit_price=request.metadata.get("take_profit"),
            metadata=request.metadata,
        )
        self._orders[order.client_order_id] = order
        self._client_id_owner[order.client_order_id] = request.request_id
        self._decision_usage[decision.decision_id] = order.client_order_id
        self._decision_approved_quantity.setdefault(decision.decision_id, decision.quantity)
        self._transition(order, OrderStatus.ACCEPTED, now)
        self._log("submit_accepted", client_order_id=order.client_order_id)

        if order.order_type is OrderType.MARKET:
            self._execute_market_order(order, request, decision, quote, now)
        elif order.time_in_force is not TimeInForce.GTC:
            # LIMIT + IOC/FOK: paper model has no trade event at submit time to
            # cross against (quote touches never fill, per OPEN_QUESTIONS 20), so
            # an immediately-executable limit order that cannot be filled now is
            # canceled rather than left resting. Documented engineering decision.
            self._transition(order, OrderStatus.CANCELED, now)

        return SubmitResult(
            accepted=order.status not in (OrderStatus.REJECTED,),
            client_order_id=order.client_order_id,
            status=order.status,
        )

    def _validate_submit(
        self,
        request: ExecutionRequest,
        decision: RiskDecision | None,
        quote: MarketSnapshot,
        now: datetime,
    ) -> tuple[RejectCode, str] | None:
        if self.mode is not EngineMode.READY:
            if self.mode is EngineMode.HALTED and request.reduce_only:
                pass  # reduce-only may proceed while HALTED, subject to the check below
            else:
                return RejectCode.NOT_READY, f"engine mode is {self.mode}"

        if (
            not _is_utc(request.timestamp)
            or not _is_utc(decision.timestamp if decision else request.timestamp)
            or not _finite_positive(request.quantity)
            or (request.limit_price is not None and not _finite_positive(request.limit_price))
        ):
            return (
                RejectCode.INVALID_REQUEST,
                "timestamps must be UTC; qty/price must be finite and positive",
            )

        if decision is None or not decision.approved:
            return RejectCode.RISK_NOT_APPROVED, "risk decision missing or not approved"

        if (
            decision.decision_id != request.risk_decision_id
            or decision.instrument != request.instrument
        ):
            return RejectCode.RISK_MISMATCH, "risk decision id/instrument does not match request"

        decision_side = decision.metadata.get("side") if decision.metadata else None
        if decision_side is None or decision_side != request.side.value:
            return (
                RejectCode.RISK_MISMATCH,
                "risk decision side is missing or does not match request side",
            )

        decision_reduce_only = (
            bool(decision.metadata.get("reduce_only")) if decision.metadata else False
        )
        if decision_reduce_only != request.reduce_only:
            return (
                RejectCode.RISK_MISMATCH,
                "risk decision reduce-only flag does not match request",
            )

        consumed_by = self._decision_usage.get(decision.decision_id)
        if consumed_by is not None and consumed_by != request.client_order_id:
            return (
                RejectCode.RISK_ALREADY_CONSUMED,
                "risk decision already consumed by another request",
            )

        if request.quantity > decision.quantity:
            return RejectCode.EXCEEDS_APPROVED_SIZE, "request quantity exceeds risk-approved size"

        if (
            request.timestamp > now
            or decision.timestamp > now
            or now - request.timestamp > self.config.max_request_age
            or now - decision.timestamp > self.config.max_decision_age
        ):
            return (
                RejectCode.STALE_REQUEST,
                "request or decision is stale or timestamped in the future",
            )

        if request.order_type is OrderType.MARKET and request.time_in_force is TimeInForce.GTC:
            return RejectCode.INVALID_TIF, "market orders require IOC or FOK"

        if (
            quote.instrument != request.instrument
            or quote.quality is not DataQuality.LIVE
            or now - quote.timestamp > self.config.max_quote_age
        ):
            return (
                RejectCode.STALE_MARKET_DATA,
                "quote is stale, not LIVE, or for a different instrument",
            )

        mid = (quote.bid + quote.ask) / 2
        spread_bps = (quote.ask - quote.bid) / mid * BPS if mid > ZERO else Decimal("Infinity")
        if spread_bps > self.config.max_spread_bps:
            return RejectCode.SPREAD_GUARD, "quote spread exceeds configured maximum"

        if request.reduce_only:
            position = self.portfolio.positions.get(request.instrument)
            current_qty = position.quantity if position else ZERO
            opposes = (current_qty > ZERO and request.side is OrderSide.SELL) or (
                current_qty < ZERO and request.side is OrderSide.BUY
            )
            if current_qty == ZERO or not opposes or request.quantity > abs(current_qty):
                return (
                    RejectCode.REDUCE_ONLY_VIOLATION,
                    "reduce-only request does not reduce the current position",
                )

        return None

    # -- market order execution ------------------------------------------

    def _reference_price(
        self, request: ExecutionRequest, decision: RiskDecision, quote: MarketSnapshot
    ) -> Decimal:
        ref = decision.metadata.get("reference_price") if decision.metadata else None
        if ref is None:
            ref = request.metadata.get("reference_price") if request.metadata else None
        if ref is not None:
            return Decimal(ref)
        return (quote.bid + quote.ask) / 2

    def _execute_market_order(
        self,
        order: Order,
        request: ExecutionRequest,
        decision: RiskDecision,
        quote: MarketSnapshot,
        now: datetime,
    ) -> None:
        slip = self.config.slippage_bps / BPS
        if order.side is OrderSide.BUY:
            fill_price = quote.ask * (1 + slip)
        else:
            fill_price = quote.bid * (1 - slip)

        reference = self._reference_price(request, decision, quote)
        deviation_bps = (
            abs(fill_price - reference) / reference * BPS
            if reference > ZERO
            else Decimal("Infinity")
        )
        if deviation_bps > self.config.max_slippage_bps:
            self._transition(order, OrderStatus.REJECTED, now)
            self._log("market_order_slippage_guard", client_order_id=order.client_order_id)
            return

        self._apply_fill(
            order,
            fill_id=f"{order.client_order_id}:entry",
            price=fill_price,
            quantity=order.quantity,
            now=now,
            # MARKET orders always remove liquidity: they execute immediately
            # against whatever is resting on the book.
            liquidity_role=LiquidityRole.TAKER,
        )

    # -- resting order / trade event processing --------------------------

    def on_time(self, now: datetime) -> None:
        for order in list(self._orders.values()):
            if order.is_terminal():
                continue
            if order.role in (ChildRole.STOP, ChildRole.TAKE_PROFIT):
                # Regression: max_order_age exists to clean up entry limit
                # orders that never filled (a stale signal). Applying the
                # same clock to protective STOP/TAKE_PROFIT children let a
                # stop silently EXPIRE with no halt/reduce after 30 minutes,
                # leaving an OPEN position unprotected --
                # EXECUTION_CONTRACT.md requires "an equivalent
                # deterministic contingency must be active" for as long as
                # the position it guards is open, not just for
                # max_order_age. Removal already happens through explicit
                # paths (OCO-sibling cancel on fill, position-flat cancel
                # in _apply_fill's reduce-only branch), so exempting them
                # here does not leave true orphans.
                continue
            if now - order.created_at > self.config.max_order_age:
                self._transition(order, OrderStatus.EXPIRED, now)
                self._log("order_expired", client_order_id=order.client_order_id)

    def on_trade(self, event: TradeEvent, now: datetime) -> None:
        try:
            self._process_trade(event, now)
        except Exception as exc:
            self._halt(
                HaltCode.INTERNAL_ERROR, f"exception processing trade {event.trade_id}: {exc}"
            )

    def _process_trade(self, event: TradeEvent, now: datetime) -> None:
        if ("trade", event.trade_id) in self._seen_fill_keys:
            return
        self._seen_fill_keys.add(("trade", event.trade_id))

        for order in list(self._orders.values()):
            # Terminal orders (CANCELED/EXPIRED/REJECTED/FILLED) never match a
            # new trade event in this paper simulator: a real venue would not
            # resurrect a dead order either. A legitimate late/in-flight fill
            # can still be booked, but only through the explicit
            # venue-reported `report_fill` hook, never through ordinary
            # trade-event crossing. `order.is_terminal()` is re-evaluated on
            # every iteration (not from a stale snapshot), so an order that
            # was just canceled earlier in *this same* loop (e.g. an OCO
            # sibling, or another reduce-only order flattened by this event)
            # is correctly skipped too.
            if order.is_terminal() or order.instrument != event.instrument:
                continue
            if order.order_type is OrderType.LIMIT and order.role is ChildRole.ENTRY:
                self._try_cross_limit(order, event, now)
            elif order.role is ChildRole.STOP:
                self._try_cross_protective(order, event, now, is_stop=True)
            elif order.role is ChildRole.TAKE_PROFIT:
                self._try_cross_protective(order, event, now, is_stop=False)

        self._update_trailing_stops(event, now)

    def _try_cross_limit(self, order: Order, event: TradeEvent, now: datetime) -> None:
        assert order.limit_price is not None
        crosses = (order.side is OrderSide.BUY and event.price <= order.limit_price) or (
            order.side is OrderSide.SELL and event.price >= order.limit_price
        )
        if not crosses:
            return
        fill_qty = min(order.remaining_quantity, event.quantity)
        if fill_qty <= ZERO:
            return
        self._apply_fill(
            order,
            fill_id=f"{order.client_order_id}:{event.trade_id}",
            price=order.limit_price,
            quantity=fill_qty,
            now=now,
            # A resting entry LIMIT crossed by an incoming trade print was
            # providing liquidity (it sat on the book waiting), so it earns
            # the maker rate.
            liquidity_role=LiquidityRole.MAKER,
        )

    def _try_cross_protective(
        self, order: Order, event: TradeEvent, now: datetime, *, is_stop: bool
    ) -> None:
        assert order.trigger_price is not None
        if is_stop:
            crosses = (order.side is OrderSide.SELL and event.price <= order.trigger_price) or (
                order.side is OrderSide.BUY and event.price >= order.trigger_price
            )
        else:
            crosses = (order.side is OrderSide.SELL and event.price >= order.trigger_price) or (
                order.side is OrderSide.BUY and event.price <= order.trigger_price
            )
        if not crosses:
            return
        fill_qty = min(order.remaining_quantity, event.quantity)
        if fill_qty <= ZERO:
            return
        # Liquidity-role mapping for protective children (both are modeled
        # internally as LIMIT orders, but their real-market semantics
        # differ): a STOP triggers stop-market semantics once the trigger
        # price trades through -- it removes liquidity like a market order,
        # so it is TAKER. A TAKE_PROFIT is a resting limit order sitting on
        # the book ahead of price reaching it, so it is MAKER, exactly like
        # any other crossed resting limit.
        liquidity_role = LiquidityRole.TAKER if is_stop else LiquidityRole.MAKER
        self._apply_fill(
            order,
            fill_id=f"{order.client_order_id}:{event.trade_id}",
            price=order.trigger_price,
            quantity=fill_qty,
            now=now,
            liquidity_role=liquidity_role,
        )
        if order.status is OrderStatus.FILLED and order.oco_sibling_id:
            sibling = self._orders.get(order.oco_sibling_id)
            if sibling is not None and not sibling.is_terminal():
                self._transition(sibling, OrderStatus.CANCELED, now)
                self._log("oco_cancel", client_order_id=sibling.client_order_id)

    def _update_trailing_stops(self, event: TradeEvent, now: datetime) -> None:
        for order in self._orders.values():
            if order.role is not ChildRole.STOP or order.is_terminal():
                continue
            if order.trailing_policy is None or order.instrument != event.instrument:
                continue
            assert order.trigger_price is not None
            position_side = OrderSide.BUY if order.side is OrderSide.SELL else OrderSide.SELL
            new_stop = order.trailing_policy.update(order.trigger_price, position_side, event.price)
            order.trigger_price = new_stop

    # -- fills / halts -----------------------------------------------------

    def _reduce_only_allowed_quantity(self, instrument: str, side: OrderSide) -> Decimal:
        """How much of `side` can still reduce the current position (venue-like cap)."""
        position = self.portfolio.positions.get(instrument)
        current_qty = position.quantity if position else ZERO
        opposes = (current_qty > ZERO and side is OrderSide.SELL) or (
            current_qty < ZERO and side is OrderSide.BUY
        )
        return abs(current_qty) if opposes else ZERO

    def _cancel_other_reduce_only_orders(
        self, instrument: str, now: datetime, *, keep: str
    ) -> None:
        for other in list(self._orders.values()):
            if (
                other.client_order_id == keep
                or other.instrument != instrument
                or not other.reduce_only
                or other.is_terminal()
            ):
                continue
            self._transition(other, OrderStatus.CANCELED, now)
            self._log(
                "reduce_only_canceled_position_flat",
                client_order_id=other.client_order_id,
            )

    def _apply_fill(
        self,
        order: Order,
        *,
        fill_id: str,
        price: Decimal,
        quantity: Decimal,
        now: datetime,
        liquidity_role: LiquidityRole,
    ) -> None:
        # Regression: a duplicate fill_id (e.g. the same underlying trade
        # reported once via on_trade()'s crossing path and again via
        # report_fill(), which use different outer dedup key shapes) used to
        # reach the per-order/decision cap checks below BEFORE
        # portfolio.apply_fill's own fill_id dedup ran, so its quantity
        # could be counted a second time against those caps and trigger a
        # false OVERFILL halt -- even though the portfolio itself would
        # have correctly treated it as a no-op. Check first.
        if self.portfolio.has_fill(fill_id):
            return

        # Regression: submit() and cancel_replace() both check self.mode
        # before letting a NEW order/replacement through, but nothing
        # anywhere checked it before APPLYING a fill from an ordinary
        # crossing trade event or report_fill() -- so an already-resting
        # non-reduce-only order could still open/increase exposure while
        # HALTED (or RECONCILING/DEGRADED). Reduce-only fills (manual
        # closes, or protective STOP/TAKE_PROFIT children) keep the same
        # exception submit()/cancel_replace() already grant them while
        # HALTED specifically, since they can only shrink exposure. The
        # fill is simply not applied yet (order stays resting) rather than
        # triggering another halt -- the engine is already not READY.
        if not order.reduce_only and self.mode is not EngineMode.READY:
            self._log(
                "fill_deferred_not_ready",
                client_order_id=order.client_order_id,
                fill_id=fill_id,
                mode=str(self.mode),
            )
            return

        if order.reduce_only:
            # Fail-closed, venue-like cap: a reduce-only order (manual or a
            # protective STOP/TAKE_PROFIT child) can never sell/buy more than
            # the position it is reducing actually has open *at fill time* --
            # not merely at submit time, since other reduce-only orders may
            # have already reduced or flattened the position since then.
            allowed = self._reduce_only_allowed_quantity(order.instrument, order.side)
            if allowed <= ZERO:
                if not order.is_terminal():
                    self._transition(order, OrderStatus.CANCELED, now)
                    self._log(
                        "reduce_only_canceled_no_position",
                        client_order_id=order.client_order_id,
                    )
                return
            if quantity > allowed:
                quantity = allowed

        if order.filled_quantity + quantity > order.quantity:
            self._halt(HaltCode.OVERFILL, f"order {order.client_order_id} would overfill")
            return

        # Decision-level cumulative cap, in addition to the per-order cap
        # above. cancel_replace() gives a replacement the same decision_id
        # as the order it replaces, but never reduces the original's own
        # `.quantity` -- so a late fill on a replaced (CANCELED) original
        # plus a fill on its replacement can each individually pass the
        # per-order check above while together exceeding what risk
        # approved for that decision. Only non-reduce-only fills consume
        # this cap: reduce-only fills (manual, or a protective STOP/
        # TAKE_PROFIT child sharing the entry's decision_id) reduce
        # exposure rather than using the entry's approval, and are already
        # separately capped by `_reduce_only_allowed_quantity` above.
        if not order.reduce_only:
            approved = self._decision_approved_quantity.get(order.decision_id, order.quantity)
            already_filled = self._decision_filled_quantity.get(order.decision_id, ZERO)
            if already_filled + quantity > approved:
                self._halt(
                    HaltCode.OVERFILL,
                    f"decision {order.decision_id} would overfill across replacement chain "
                    f"(order {order.client_order_id})",
                )
                return

        fee = calculate_fill_fee(
            notional=price * quantity,
            liquidity_role=liquidity_role,
            schedule=self._cost_schedule,
        )
        applied = self.portfolio.apply_fill(
            Fill(
                fill_id=fill_id,
                instrument=order.instrument,
                side=order.side,
                quantity=quantity,
                price=price,
                fee=fee,
                timestamp=now,
            )
        )
        if not applied:
            return  # duplicate fill id: no-op, never double count

        if self.fill_listener is not None:
            self.fill_listener(
                FillEvent(
                    fill_id=fill_id,
                    client_order_id=order.client_order_id,
                    decision_id=order.decision_id,
                    instrument=order.instrument,
                    side=order.side,
                    quantity=quantity,
                    price=price,
                    fee=fee,
                    liquidity_role=liquidity_role,
                    reduce_only=order.reduce_only,
                    timestamp=now,
                )
            )

        if not order.reduce_only:
            self._decision_filled_quantity[order.decision_id] = (
                self._decision_filled_quantity.get(order.decision_id, ZERO) + quantity
            )

        total_notional = order.avg_fill_price * order.filled_quantity + price * quantity
        order.filled_quantity += quantity
        order.avg_fill_price = (
            total_notional / order.filled_quantity if order.filled_quantity > ZERO else ZERO
        )

        if order.is_terminal():
            # Late fill arriving after a terminal transition (e.g. the
            # original half of a cancel/replace pair): booked at most once,
            # status must never regress from its terminal value. This fill
            # already increased real exposure above (portfolio.apply_fill
            # ran before this check) -- for an ENTRY order that means
            # protective orders must still be created/updated for it here.
            # Regression: this used to return before ever reaching the
            # ENTRY sync below, so a late fill on a canceled entry opened
            # real exposure with NO protective stop/take-profit at all.
            self._log("late_fill_booked", client_order_id=order.client_order_id, fill_id=fill_id)
            if order.role is ChildRole.ENTRY:
                self._sync_protective_orders(order, now)
            return

        target = (
            OrderStatus.FILLED if order.remaining_quantity == ZERO else OrderStatus.PARTIALLY_FILLED
        )
        self._transition(order, target, now)
        self._log(
            "fill",
            client_order_id=order.client_order_id,
            fill_id=fill_id,
            price=str(price),
            quantity=str(quantity),
        )

        if order.role is ChildRole.ENTRY:
            self._sync_protective_orders(order, now)
        elif order.status is OrderStatus.FILLED and order.oco_sibling_id:
            sibling = self._orders.get(order.oco_sibling_id)
            if sibling is not None and not sibling.is_terminal():
                self._transition(sibling, OrderStatus.CANCELED, now)
                self._log("oco_cancel", client_order_id=sibling.client_order_id)

        if order.reduce_only:
            position = self.portfolio.positions.get(order.instrument)
            residual_qty = position.quantity if position else ZERO
            if residual_qty == ZERO:
                # Position is flat: this reduce-only order can never fill any
                # further (there is nothing left to reduce), and neither can
                # any other reduce-only order resting on the same instrument
                # (e.g. an un-linked manual close order next to a protective
                # stop) -- cap the remainder before it can ever go negative.
                if not order.is_terminal():
                    self._transition(order, OrderStatus.CANCELED, now)
                    self._log(
                        "reduce_only_canceled_position_flat",
                        client_order_id=order.client_order_id,
                    )
                self._cancel_other_reduce_only_orders(
                    order.instrument, now, keep=order.client_order_id
                )

    def report_fill(
        self, client_order_id: str, trade_id: str, price: Decimal, quantity: Decimal, now: datetime
    ) -> None:
        """Book an externally reported fill against a known order (chaos-test hook)."""
        try:
            order = self._orders.get(client_order_id)
            if order is None:
                self._halt(
                    HaltCode.UNKNOWN_ORDER, f"fill referenced unknown order {client_order_id}"
                )
                return
            key = (client_order_id, trade_id)
            if key in self._seen_fill_keys:
                return
            self._seen_fill_keys.add(key)
            self._apply_fill(
                order,
                fill_id=f"{client_order_id}:{trade_id}",
                price=price,
                quantity=quantity,
                now=now,
                # Externally reported fill of unknown origin (chaos-test
                # hook): the conservative, higher-fee choice since we cannot
                # tell whether this order added or removed liquidity.
                liquidity_role=LiquidityRole.TAKER,
            )
        except Exception as exc:
            self._halt(HaltCode.INTERNAL_ERROR, f"exception applying reported fill: {exc}")

    # -- protective orders -------------------------------------------------

    def _sync_protective_orders(self, entry: Order, now: datetime) -> None:
        protective_qty = entry.filled_quantity
        protective_side = OrderSide.SELL if entry.side is OrderSide.BUY else OrderSide.BUY

        if entry.trigger_price is not None:
            self._ensure_protective(
                entry, ChildRole.STOP, entry.trigger_price, protective_side, protective_qty, now
            )
        if entry.take_profit_price is not None:
            self._ensure_protective(
                entry,
                ChildRole.TAKE_PROFIT,
                entry.take_profit_price,
                protective_side,
                protective_qty,
                now,
            )

        stop = self._resolve_live_order(self._child_id(entry, ChildRole.STOP))
        tp = self._resolve_live_order(self._child_id(entry, ChildRole.TAKE_PROFIT))
        if stop is not None and tp is not None:
            stop.oco_sibling_id = tp.client_order_id
            tp.oco_sibling_id = stop.client_order_id

    @staticmethod
    def _child_id(entry: Order, role: ChildRole) -> str:
        return f"{entry.client_order_id}:{role}"

    def _resolve_live_order(self, order_id: str) -> Order | None:
        """Follow `replaced_by_client_order_id` from `order_id` to the live
        (or last-known) descendant of a cancel/replace chain.

        Regression: `_ensure_protective`/`_sync_protective_orders` looked
        protective children up by their fixed deterministic id
        (f"{entry_id}:{role}") directly. Once that id had been
        cancel_replace()'d, the deterministic id still existed in
        `self._orders` but as a terminal (CANCELED) order -- the live
        replacement lives under a *different* id. Looking it up directly
        meant a later entry fill's resize/OCO-relink silently operated on
        the dead original (a no-op, since it's terminal) instead of the
        live replacement, undoing cancel_replace()'s own OCO relink and
        leaving the replacement's size stale.
        """
        order = self._orders.get(order_id)
        seen: set[str] = set()
        while order is not None and order.replaced_by_client_order_id:
            if order.client_order_id in seen:
                break  # defensive: never loop forever on a corrupt chain
            seen.add(order.client_order_id)
            next_order = self._orders.get(order.replaced_by_client_order_id)
            if next_order is None:
                break
            order = next_order
        return order

    def _ensure_protective(
        self,
        entry: Order,
        role: ChildRole,
        trigger_price: Decimal,
        side: OrderSide,
        quantity: Decimal,
        now: datetime,
    ) -> None:
        child_id = self._child_id(entry, role)
        existing = self._resolve_live_order(child_id)
        if existing is None:
            child = Order(
                client_order_id=child_id,
                request_id=entry.request_id,
                decision_id=entry.decision_id,
                instrument=entry.instrument,
                side=side,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=quantity,
                limit_price=trigger_price,
                reduce_only=True,
                created_at=now,
                updated_at=now,
                role=role,
                parent_client_order_id=entry.client_order_id,
                trigger_price=trigger_price,
            )
            self._orders[child_id] = child
            self._transition(child, OrderStatus.ACCEPTED, now)
            self._log("protective_order_created", client_order_id=child_id, role=str(role))
        elif not existing.is_terminal() and quantity > existing.quantity:
            existing.quantity = quantity

    def attach_trailing_stop(self, entry_client_order_id: str, policy: Any) -> None:
        stop_id = f"{entry_client_order_id}:{ChildRole.STOP}"
        stop = self._orders.get(stop_id)
        if stop is not None:
            stop.trailing_policy = policy

    # -- cancel / cancel-replace -------------------------------------------

    def cancel(self, client_order_id: str, now: datetime) -> CancelResult:
        order = self._orders.get(client_order_id)
        if order is None:
            return CancelResult(
                accepted=False,
                client_order_id=client_order_id,
                status=OrderStatus.REJECTED,
                reason="unknown order",
            )
        if order.is_terminal():
            return CancelResult(
                accepted=True,
                client_order_id=client_order_id,
                status=order.status,
                reason="already terminal (no-op)",
            )
        self._transition(order, OrderStatus.CANCELED, now)
        self._log("canceled", client_order_id=client_order_id)
        return CancelResult(accepted=True, client_order_id=client_order_id, status=order.status)

    def cancel_replace(
        self,
        client_order_id: str,
        new_client_order_id: str,
        now: datetime,
        *,
        new_price: Decimal | None = None,
        new_quantity: Decimal | None = None,
    ) -> CancelReplaceResult:
        original = self._orders.get(client_order_id)
        if original is None or original.is_terminal():
            return CancelReplaceResult(
                accepted=False,
                original_client_order_id=client_order_id,
                new_client_order_id=None,
                status=None,
                reject_code=RejectCode.UNKNOWN_ORDER_NOT_FOUND,
                reason="original order not found or already terminal",
            )

        # Regression: unlike submit(), this had no mode check at all, so a
        # cancel/replace while HALTED (or RECONCILING) could still create a
        # new ACCEPTED order -- including a non-reduce-only replacement that
        # opens new exposure -- bypassing the halt entirely. Apply the same
        # policy submit() uses: reduce-only replacements may still proceed
        # (they can only shrink exposure), anything else is rejected.
        if self.mode is not EngineMode.READY and not (
            self.mode is EngineMode.HALTED and original.reduce_only
        ):
            return CancelReplaceResult(
                accepted=False,
                original_client_order_id=client_order_id,
                new_client_order_id=None,
                status=original.status,
                reject_code=RejectCode.NOT_READY,
                reason=f"engine mode is {self.mode}",
            )

        # Regression: no uniqueness check on new_client_order_id existed at
        # all -- if it happened to match an existing order's id, that order
        # was silently overwritten in `self._orders` (plain dict
        # assignment) and vanished from tracking entirely, with no error.
        existing_new_id_owner = self._orders.get(new_client_order_id)
        if existing_new_id_owner is not None:
            return CancelReplaceResult(
                accepted=False,
                original_client_order_id=client_order_id,
                new_client_order_id=None,
                status=original.status,
                reject_code=RejectCode.DUPLICATE_CONFLICT,
                reason=f"new_client_order_id {new_client_order_id!r} is already in use",
            )

        remaining = original.remaining_quantity
        requested_qty = new_quantity if new_quantity is not None else remaining
        if requested_qty > remaining:
            return CancelReplaceResult(
                accepted=False,
                original_client_order_id=client_order_id,
                new_client_order_id=None,
                status=original.status,
                reject_code=RejectCode.CANCEL_REPLACE_EXCEEDS_REMAINING,
                reason="replacement quantity exceeds remaining approved quantity",
            )

        self._transition(original, OrderStatus.CANCELED, now)
        original.replaced_by_client_order_id = new_client_order_id
        self._log("canceled_for_replace", client_order_id=client_order_id)

        replacement = Order(
            client_order_id=new_client_order_id,
            request_id=original.request_id,
            decision_id=original.decision_id,
            instrument=original.instrument,
            side=original.side,
            order_type=original.order_type,
            time_in_force=original.time_in_force,
            quantity=requested_qty,
            limit_price=new_price if new_price is not None else original.limit_price,
            reduce_only=original.reduce_only,
            created_at=now,
            updated_at=now,
            role=original.role,
            parent_client_order_id=original.parent_client_order_id,
            replaces_client_order_id=client_order_id,
            # Regression: these were silently dropped, defaulting to None/empty
            # on the replacement. For an ENTRY order this meant a
            # cancel/replace before fill silently produced a position with NO
            # protective stop/take-profit at all once the replacement filled
            # (trigger_price/take_profit_price lost). For a STOP/TAKE_PROFIT
            # order, the replacement's trigger_price=None made the very next
            # crossing trade hit `_try_cross_protective`'s
            # `assert order.trigger_price is not None` and HALT with
            # INTERNAL_ERROR -- a confusing failure mode for what is really
            # "this order lost its trigger price on replace", and the
            # position was left unprotected in the meantime regardless.
            trigger_price=original.trigger_price,
            take_profit_price=original.take_profit_price,
            trailing_policy=original.trailing_policy,
            metadata=original.metadata,
        )
        self._orders[new_client_order_id] = replacement
        self._client_id_owner[new_client_order_id] = original.request_id
        self._transition(replacement, OrderStatus.ACCEPTED, now)
        self._log(
            "replacement_accepted", client_order_id=new_client_order_id, replaces=client_order_id
        )

        # Re-link the OCO relationship both ways: the replacement takes over
        # the original's sibling link, and the sibling's own back-reference
        # (which still points at the now-CANCELED original) is repointed at
        # the replacement -- otherwise the sibling filling first would try
        # to OCO-cancel a dead order id and the surviving replacement would
        # never get canceled, leaving both a stop and a take-profit
        # resting on the same position simultaneously.
        if original.oco_sibling_id:
            replacement.oco_sibling_id = original.oco_sibling_id
            sibling = self._orders.get(original.oco_sibling_id)
            if sibling is not None and not sibling.is_terminal():
                sibling.oco_sibling_id = new_client_order_id

        return CancelReplaceResult(
            accepted=True,
            original_client_order_id=client_order_id,
            new_client_order_id=new_client_order_id,
            status=replacement.status,
        )

    # -- transitions ---------------------------------------------------

    def _transition(self, order: Order, target: OrderStatus, now: datetime) -> None:
        if not is_legal_transition(order.status, target):
            self._log(
                "illegal_transition_ignored",
                client_order_id=order.client_order_id,
                from_status=str(order.status),
                to_status=str(target),
            )
            return
        order.status = target
        order.updated_at = now
        self._log("transition", client_order_id=order.client_order_id, status=str(target))

    # -- checkpointing ---------------------------------------------------

    def export_checkpoint(self) -> dict[str, Any]:
        return {
            "mode": str(self.mode),
            "halt_reason": self.halt_reason,
            "requests": {
                request_id: {
                    "accepted": result.accepted,
                    "client_order_id": result.client_order_id,
                    "status": str(result.status),
                    "reject_code": str(result.reject_code) if result.reject_code else None,
                    "reason": result.reason,
                }
                for request_id, result in sorted(self._requests.items())
            },
            "decision_usage": dict(sorted(self._decision_usage.items())),
            "decision_approved_quantity": {
                k: str(v) for k, v in sorted(self._decision_approved_quantity.items())
            },
            "decision_filled_quantity": {
                k: str(v) for k, v in sorted(self._decision_filled_quantity.items())
            },
            "client_id_owner": dict(sorted(self._client_id_owner.items())),
            "seen_fill_keys": sorted(f"{a}::{b}" for a, b in self._seen_fill_keys),
            "orders": {
                client_id: {
                    "request_id": order.request_id,
                    "decision_id": order.decision_id,
                    "instrument": order.instrument,
                    "side": str(order.side),
                    "order_type": str(order.order_type),
                    "time_in_force": str(order.time_in_force),
                    "quantity": str(order.quantity),
                    "limit_price": str(order.limit_price)
                    if order.limit_price is not None
                    else None,
                    "reduce_only": order.reduce_only,
                    "created_at": order.created_at.isoformat(),
                    "status": str(order.status),
                    "filled_quantity": str(order.filled_quantity),
                    "avg_fill_price": str(order.avg_fill_price),
                    "role": str(order.role),
                    "parent_client_order_id": order.parent_client_order_id,
                    "oco_sibling_id": order.oco_sibling_id,
                    "replaces_client_order_id": order.replaces_client_order_id,
                    "replaced_by_client_order_id": order.replaced_by_client_order_id,
                    "trigger_price": str(order.trigger_price)
                    if order.trigger_price is not None
                    else None,
                    "take_profit_price": (
                        str(order.take_profit_price)
                        if order.take_profit_price is not None
                        else None
                    ),
                }
                for client_id, order in sorted(self._orders.items())
            },
        }

    def import_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        self.mode = EngineMode(checkpoint["mode"])
        self.halt_reason = checkpoint["halt_reason"]
        self._decision_usage = dict(checkpoint["decision_usage"])
        # .get(..., {}) rather than direct indexing: a checkpoint captured
        # before these two maps existed (any real historical checkpoint, or
        # one from before this fix) has no such keys at all, and must still
        # import instead of raising KeyError on restart.
        self._decision_approved_quantity = {
            k: Decimal(v) for k, v in checkpoint.get("decision_approved_quantity", {}).items()
        }
        self._decision_filled_quantity = {
            k: Decimal(v) for k, v in checkpoint.get("decision_filled_quantity", {}).items()
        }
        self._client_id_owner = dict(checkpoint["client_id_owner"])
        self._seen_fill_keys = {
            (a, b) for a, b in (pair.split("::", 1) for pair in checkpoint["seen_fill_keys"])
        }
        self._requests = {
            request_id: SubmitResult(
                accepted=payload["accepted"],
                client_order_id=payload["client_order_id"],
                status=OrderStatus(payload["status"]),
                reject_code=RejectCode(payload["reject_code"]) if payload["reject_code"] else None,
                reason=payload["reason"],
            )
            for request_id, payload in checkpoint["requests"].items()
        }
        self._orders = {}
        for client_id, payload in checkpoint["orders"].items():
            order = Order(
                client_order_id=client_id,
                request_id=payload["request_id"],
                decision_id=payload["decision_id"],
                instrument=payload["instrument"],
                side=OrderSide(payload["side"]),
                order_type=OrderType(payload["order_type"]),
                time_in_force=TimeInForce(payload["time_in_force"]),
                quantity=Decimal(payload["quantity"]),
                limit_price=Decimal(payload["limit_price"]) if payload["limit_price"] else None,
                reduce_only=payload["reduce_only"],
                created_at=datetime.fromisoformat(payload["created_at"]),
                status=OrderStatus(payload["status"]),
                filled_quantity=Decimal(payload["filled_quantity"]),
                avg_fill_price=Decimal(payload["avg_fill_price"]),
                role=ChildRole(payload["role"]),
                parent_client_order_id=payload["parent_client_order_id"],
                oco_sibling_id=payload["oco_sibling_id"],
                replaces_client_order_id=payload["replaces_client_order_id"],
                replaced_by_client_order_id=payload["replaced_by_client_order_id"],
                trigger_price=Decimal(payload["trigger_price"])
                if payload["trigger_price"]
                else None,
                take_profit_price=(
                    Decimal(payload["take_profit_price"]) if payload["take_profit_price"] else None
                ),
            )
            self._orders[client_id] = order
