"""Deterministic Sprint 1 integration glue for the paper trading path.

Wires together, without any LLM call, network access, or hidden global state:

    MarketSnapshot history -> universe membership check -> strategy.evaluate()
    -> Signal -> PositionSizingRequest -> RiskEngine.evaluate() -> RiskDecision
    -> ExecutionRequest -> PaperExecutionEngine.submit() -> Portfolio fill
    -> TradeOutcome bookkeeping -> monitoring metrics.

Every method that is time-sensitive takes an injected ``now`` so replays are
byte-for-byte reproducible. This module must stay free of I/O: it only calls
into already-merged, already-tested modules (data, signals, strategies, risk,
execution, portfolio, monitoring) and never reaches across those boundaries
(see repository CLAUDE.md: strategies emit Signal only, only risk-approved
ExecutionRequest objects may reach execution, and the hot path must be
deterministic).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from costs.engine import calculate_trade_costs
from costs.models import (
    CostBreakdown,
    CostCalculationRequest,
    InstrumentClass,
    LiquidityRole,
    TradeSide,
    VenueCostSchedule,
)
from data.models import MarketSnapshot
from execution.events import TradeEvent
from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from execution.orders import TERMINAL_STATUSES, OrderStatus, RejectCode, SubmitResult
from execution.paper import FillEvent, PaperExecutionEngine
from monitoring.metrics import TradeOutcome, calculate_trade_metrics
from portfolio.ledger import Portfolio
from risk.engine import RiskEngine
from risk.models import (
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    RiskDecision,
    RiskReason,
    RiskSide,
    RuntimeRiskState,
)
from signals.models import Direction, Signal

if TYPE_CHECKING:
    from datetime import datetime

ZERO = Decimal("0")


class Strategy(Protocol):
    """The minimal contract the pipeline requires from any strategy family."""

    def evaluate(self, snapshots: Sequence[MarketSnapshot]) -> Signal | None: ...


class PipelineStage(StrEnum):
    """The terminal stage a single ``process()`` call reached."""

    NOT_IN_UNIVERSE = "not_in_universe"
    NO_SIGNAL = "no_signal"
    RISK_REJECTED = "risk_rejected"
    EXECUTION_REJECTED = "execution_rejected"
    EXECUTED = "executed"
    # Slice 3a (G1 fix): reached by process_trade_event()/process_time_tick()/
    # process_reported_fill() -- the background-event counterparts of
    # EXECUTED/NO_SIGNAL above. These are not driven by a new trading
    # signal (no universe/risk gating), only by an execution-engine-side
    # event (a trade print, a clock tick, or an externally reported fill),
    # so they get their own stage values rather than reusing EXECUTED/
    # NO_SIGNAL, which both imply a signal was evaluated.
    EXECUTION_EVENT_FILLED = "execution_event_filled"
    EXECUTION_EVENT_NO_FILL = "execution_event_no_fill"


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineOutcome:
    """Immutable record of how far one instrument/history pair traveled."""

    stage: PipelineStage
    signal_id: str | None
    risk_reason: str | None
    execution_status: OrderStatus | None
    execution_reject_code: RejectCode | None
    position_after: Decimal | None


@dataclass(slots=True)
class _TradeAccumulator:
    """Running notional/fees for one instrument's currently-open position.

    Also separately tracks entry-side and exit-side fill notional/quantity
    (and the quote-side "expected" reference price at each fill) so that,
    once the position fully closes, a reporting-only `CostBreakdown` can be
    attributed via `costs.engine.calculate_trade_costs`. This is pure
    bookkeeping alongside the existing realized-PnL-based `notional`/`fees`
    tracking -- it never feeds back into `TradeOutcome` or the portfolio.
    """

    notional: Decimal
    fees: Decimal
    # Sum of this instrument's OWN per-fill realized-pnl deltas
    # (realized_after - realized_before for each of ITS OWN fills),
    # accumulated fill-by-fill exactly like `fees` above -- never a
    # start-of-position-vs-end-of-position anchor diff against the global
    # `portfolio.realized_pnl` counter. `portfolio.realized_pnl` is a
    # cross-instrument cumulative sum, so an anchor diffed across a wide
    # open-to-close window would silently fold in any OTHER instrument's
    # realized PnL that landed on that same global counter in between.
    # Per-fill accumulation is immune to that regardless of how many other
    # instruments' fills interleave, or how many events apart open/close are.
    realized_pnl_delta: Decimal
    open_time: datetime
    position_side: TradeSide | None = None
    entry_bid: Decimal | None = None
    entry_ask: Decimal | None = None
    entry_notional: Decimal = ZERO
    entry_quantity: Decimal = ZERO
    entry_expected_notional: Decimal = ZERO
    exit_notional: Decimal = ZERO
    exit_quantity: Decimal = ZERO
    exit_expected_notional: Decimal = ZERO


@dataclass(kw_only=True)
class PaperTradingPipeline:
    """Glue object; holds no market data or credentials, only injected collaborators."""

    risk_engine: RiskEngine
    execution_engine: PaperExecutionEngine
    portfolio: Portfolio
    instrument_limits: Mapping[str, InstrumentRiskLimits]
    leverage_cap: Decimal
    cost_schedule: VenueCostSchedule

    _peak_equity: Decimal = field(init=False, default=ZERO)
    _initial_equity: Decimal = field(init=False, default=ZERO)
    _consecutive_losses: int = field(init=False, default=0)
    _trade_outcomes: list[TradeOutcome] = field(init=False, default_factory=list)
    _trade_accum: dict[str, _TradeAccumulator] = field(init=False, default_factory=dict)
    _stage_counts: dict[str, int] = field(init=False, default_factory=dict)
    _reason_counts: dict[str, int] = field(init=False, default_factory=dict)
    _cost_attributions: list[CostBreakdown] = field(init=False, default_factory=list)
    _pending_fills: list[FillEvent] = field(init=False, default_factory=list)
    # fill_id -> (realized_pnl delta, fees delta) attributable to exactly
    # that one fill, captured synchronously in `_on_fill` from a running
    # global anchor (`_last_realized_pnl`/`_last_fees`). `portfolio.
    # realized_pnl`/`fees` are portfolio-wide cumulative counters, not
    # per-instrument, so a single before/after snapshot taken around a
    # whole on_trade()/on_time()/report_fill() call cannot be safely split
    # across instruments when more than one instrument's fills land in
    # that same call -- this per-fill delta is what makes that split exact
    # (see `_reconcile_background_fills`). `process()`'s own submit()-driven
    # single-instrument path does not need this (it still uses its own
    # before/after snapshot), but every fill still populates this dict for
    # consistency; unused entries are cleared alongside `_pending_fills`.
    _fill_deltas: dict[str, tuple[Decimal, Decimal]] = field(init=False, default_factory=dict)
    _last_realized_pnl: Decimal = field(init=False, default=ZERO)
    _last_fees: Decimal = field(init=False, default=ZERO)

    def __post_init__(self) -> None:
        equity = self.portfolio.equity
        self._initial_equity = equity
        self._peak_equity = equity if equity > ZERO else ZERO
        self._last_realized_pnl = self.portfolio.realized_pnl
        self._last_fees = self.portfolio.fees
        # Observe the actual fill price/fee for every real fill the
        # execution engine applies (G4 fix: `_record_fill` used to reuse the
        # quote-side reference price instead of the real fill price). This
        # engine instance is owned exclusively by this pipeline instance in
        # every constructed harness, so overwriting any prior listener here
        # is safe and intentional.
        self.execution_engine.fill_listener = self._on_fill

    def _on_fill(self, event: FillEvent) -> None:
        realized_now = self.portfolio.realized_pnl
        fees_now = self.portfolio.fees
        self._fill_deltas[event.fill_id] = (
            realized_now - self._last_realized_pnl,
            fees_now - self._last_fees,
        )
        self._last_realized_pnl = realized_now
        self._last_fees = fees_now
        self._pending_fills.append(event)

    # -- public API ---------------------------------------------------

    def process(
        self,
        *,
        history: Sequence[MarketSnapshot],
        strategy: Strategy,
        universe: frozenset[str],
        runtime: RuntimeRiskState,
        account_known: bool,
        now: datetime,
    ) -> PipelineOutcome:
        if not history:
            # No market data at all: nothing to check the universe against.
            return self._finish(
                PipelineStage.NO_SIGNAL,
                signal_id=None,
                risk_reason=None,
                execution_status=None,
                execution_reject_code=None,
                position_after=None,
            )

        latest = history[-1]
        instrument = latest.instrument

        # Mark-to-market happens regardless of universe/signal outcome, and
        # BEFORE any AccountRiskState is derived: every open portfolio
        # position whose instrument has a snapshot in this history gets its
        # mark_price refreshed from the latest `last` trade price, so risk
        # always sees current (not stale/zero) gross/net exposure. An
        # unmarked position values at zero in the ledger, which would let
        # exposure limits silently under-count real risk.
        self._mark_positions_from_history(history)
        self._update_peak_equity()

        if instrument not in universe:
            return self._finish(
                PipelineStage.NOT_IN_UNIVERSE,
                signal_id=None,
                risk_reason=None,
                execution_status=None,
                execution_reject_code=None,
                position_after=self._position_qty(instrument),
            )

        signal = strategy.evaluate(history)
        if signal is None:
            return self._finish(
                PipelineStage.NO_SIGNAL,
                signal_id=None,
                risk_reason=None,
                execution_status=None,
                execution_reject_code=None,
                position_after=self._position_qty(instrument),
            )

        side = RiskSide.BUY if signal.direction is Direction.LONG else RiskSide.SELL
        entry_price = latest.ask if signal.direction is Direction.LONG else latest.bid
        liquidity = latest.liquidity if latest.liquidity is not None else ZERO

        sizing_request = PositionSizingRequest(
            signal_id=signal.signal_id,
            instrument=instrument,
            timestamp=signal.timestamp,
            side=side,
            entry_price=entry_price,
            stop_price=signal.invalidation_level,
            confidence=signal.confidence,
            available_liquidity_notional=liquidity,
            metadata={},
        )

        instrument_limits = self.instrument_limits.get(instrument)
        if instrument_limits is None:
            # Fail closed: an instrument with no configured risk limits can
            # never be sized, regardless of what the strategy proposes.
            return self._finish(
                PipelineStage.RISK_REJECTED,
                signal_id=signal.signal_id,
                risk_reason=RiskReason.INVALID_INPUT.value,
                execution_status=None,
                execution_reject_code=None,
                position_after=self._position_qty(instrument),
            )

        account = self._build_account_state(
            instrument=instrument, account_known=account_known
        )

        decision = self.risk_engine.evaluate(
            request=sizing_request,
            snapshot=latest,
            account=account,
            runtime=runtime,
            instrument=instrument_limits,
            now=now,
        )

        if not decision.approved:
            reason_code = decision.reason_code
            risk_reason = (
                reason_code.value
                if isinstance(reason_code, RiskReason)
                else (reason_code or decision.reason)
            )
            return self._finish(
                PipelineStage.RISK_REJECTED,
                signal_id=signal.signal_id,
                risk_reason=risk_reason,
                execution_status=None,
                execution_reject_code=None,
                position_after=self._position_qty(instrument),
            )

        order_side = OrderSide.BUY if side is RiskSide.BUY else OrderSide.SELL
        request = ExecutionRequest(
            request_id=f"exec:{signal.signal_id}",
            risk_decision_id=decision.decision_id,
            instrument=instrument,
            timestamp=signal.timestamp,
            side=order_side,
            quantity=decision.quantity,
            order_type=OrderType.MARKET,
            limit_price=None,
            reduce_only=False,
            client_order_id=f"coid:{signal.signal_id}",
            time_in_force=TimeInForce.IOC,
            metadata={"reference_price": str(entry_price)},
        )

        before_qty = self._position_qty(instrument)
        realized_before = self.portfolio.realized_pnl
        fees_before = self.portfolio.fees
        self._pending_fills.clear()
        self._fill_deltas.clear()

        result = self.execution_engine.submit(request, decision, latest, now)

        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            after_qty = self._position_qty(instrument)
            realized_after = self.portfolio.realized_pnl
            fees_after = self.portfolio.fees

            # G4 fix: use the ACTUAL fill price (from the fill_listener hook
            # `_on_fill` populated during `submit()` above), not the
            # quote-side reference price (`entry_price`) the request was
            # built from. `submit()` for a MARKET order is fully
            # synchronous and produces exactly one fill here (no partial
            # fills for MARKET orders in this engine), but this still
            # quantity-weights defensively across every fill this call
            # produced instead of assuming exactly one.
            own_fills = [
                f for f in self._pending_fills if f.client_order_id == request.client_order_id
            ]
            if own_fills:
                fill_notional = sum((f.price * f.quantity for f in own_fills), ZERO)
                fill_quantity = sum((f.quantity for f in own_fills), ZERO)
                actual_fill_price = fill_notional / fill_quantity
            else:
                # Defensive fallback only: should not happen when status is
                # FILLED/PARTIALLY_FILLED, since `_apply_fill` always fires
                # the listener before transitioning to a filled status.
                actual_fill_price = entry_price

            self._record_fill(
                instrument=instrument,
                fill_price=actual_fill_price,
                expected_price=entry_price,
                quote_bid=latest.bid,
                quote_ask=latest.ask,
                before_qty=before_qty,
                after_qty=after_qty,
                realized_before=realized_before,
                realized_after=realized_after,
                fees_before=fees_before,
                fees_after=fees_after,
                order_side=order_side,
                now=now,
            )
            self._update_peak_equity()

        if result.status in TERMINAL_STATUSES:
            # Reservation is only meaningful until the order is finally
            # resolved one way or the other; release it so it is never
            # double-counted against the now-real (or now-abandoned) position.
            self.risk_engine.release(decision.decision_id)

        stage = PipelineStage.EXECUTED if result.accepted else PipelineStage.EXECUTION_REJECTED
        return self._finish(
            stage,
            signal_id=signal.signal_id,
            risk_reason=None,
            execution_status=result.status,
            execution_reject_code=result.reject_code,
            position_after=self._position_qty(instrument),
        )

    def submit_direct(
        self,
        request: ExecutionRequest,
        decision: RiskDecision | None,
        quote: MarketSnapshot,
        now: datetime,
    ) -> SubmitResult:
        """Test-only passthrough proving execution cannot bypass risk.

        This still routes through ``PaperExecutionEngine.submit`` and all of
        its validation (decision approval, id/side/quantity matching, staleness,
        spread, reduce-only rules) -- it never writes to the portfolio directly.
        """
        return self.execution_engine.submit(request, decision, quote, now)

    # -- background execution-engine event passthroughs ------------------
    #
    # Slice 3a (G1 fix): `PaperExecutionEngine.on_trade()`, `on_time()`, and
    # `report_fill()` previously had zero callers anywhere in `src/` --
    # only `submit()` was ever called (from `process()` above). That meant
    # the protective STOP/TAKE_PROFIT child orders `submit()` creates
    # alongside every entry could never actually trigger: nothing fed a
    # subsequent trade event or time tick into the execution engine after
    # the initial entry. These three methods make those paths reachable.
    #
    # They are orchestration passthroughs only -- the accounting
    # consequences of a fill are identical regardless of whether it
    # arrived via submit(), on_trade(), on_time(), or report_fill(), so
    # all three route through the same `_reconcile_background_fills` /
    # `_record_fill` treatment `process()` already uses for its own entry
    # fill, rather than inventing new treatment. Unlike `process()`, none
    # of these are gated behind a universe/signal/risk check: they are
    # driven by execution-engine-side events, not new trading signals, and
    # must stay callable at any time so `_consecutive_losses`/daily-loss
    # risk inputs/peak-equity stay correct regardless of which path closed
    # a position.

    def process_trade_event(self, *, event: TradeEvent, now: datetime) -> PipelineOutcome:
        """Feed one `TradeEvent` into `execution_engine.on_trade()` and
        reconcile whatever fills it produces (e.g. a resting entry LIMIT
        crossing, or a protective STOP/TAKE_PROFIT triggering)."""
        before_positions = self._position_snapshot()
        self._pending_fills.clear()
        self._fill_deltas.clear()

        self.execution_engine.on_trade(event, now)

        filled = self._reconcile_background_fills(before_positions=before_positions, now=now)
        return self._finish(
            self._background_stage(filled),
            signal_id=None,
            risk_reason=None,
            execution_status=None,
            execution_reject_code=None,
            position_after=self._position_qty(event.instrument),
        )

    def process_time_tick(self, *, now: datetime) -> PipelineOutcome:
        """Feed one clock tick into `execution_engine.on_time()` and
        reconcile whatever fills it produces.

        Note: the real `PaperExecutionEngine.on_time(self, now: datetime)`
        signature takes no `instrument` (it walks every open order across
        every instrument, expiring stale entry LIMIT orders per
        `config.max_order_age` -- protective STOP/TAKE_PROFIT children are
        explicitly exempted from expiry, see `on_time`'s own docstring), so
        this passthrough has no `instrument` parameter either; a background
        clock tick is never scoped to one instrument in this engine. Order
        expiry alone never produces a fill, so this will typically reconcile
        zero fills -- that is the expected, safe no-op case, not an error.
        """
        before_positions = self._position_snapshot()
        self._pending_fills.clear()
        self._fill_deltas.clear()

        self.execution_engine.on_time(now)

        filled = self._reconcile_background_fills(before_positions=before_positions, now=now)
        return self._finish(
            self._background_stage(filled),
            signal_id=None,
            risk_reason=None,
            execution_status=None,
            execution_reject_code=None,
            position_after=None,
        )

    def process_reported_fill(
        self,
        *,
        client_order_id: str,
        trade_id: str,
        price: Decimal,
        quantity: Decimal,
        now: datetime,
    ) -> PipelineOutcome:
        """Feed one externally reported fill into
        `execution_engine.report_fill()` and reconcile whatever fill it
        produces (chaos-test hook; mirrors `tests/chaos/test_execution_chaos.py`'s
        direct-engine usage of the same call)."""
        before_positions = self._position_snapshot()
        self._pending_fills.clear()
        self._fill_deltas.clear()

        self.execution_engine.report_fill(client_order_id, trade_id, price, quantity, now)

        filled = self._reconcile_background_fills(before_positions=before_positions, now=now)
        return self._finish(
            self._background_stage(filled),
            signal_id=None,
            risk_reason=None,
            execution_status=None,
            execution_reject_code=None,
            position_after=None,
        )

    @staticmethod
    def _background_stage(filled: bool) -> PipelineStage:
        if filled:
            return PipelineStage.EXECUTION_EVENT_FILLED
        return PipelineStage.EXECUTION_EVENT_NO_FILL

    def metrics(self) -> dict[str, Any]:
        trade_metrics = calculate_trade_metrics(
            tuple(self._trade_outcomes), initial_equity=self._initial_equity
        )
        positions = {
            instrument: view.quantity for instrument, view in self.portfolio.positions.items()
        }
        return {
            "trade_metrics": trade_metrics,
            "equity": self.portfolio.equity,
            "gross_notional": self.portfolio.gross_notional,
            "net_notional": self.portfolio.net_notional,
            "peak_equity": self._peak_equity,
            "positions": positions,
            "consecutive_losses": self._consecutive_losses,
            "stage_counts": dict(self._stage_counts),
            "reason_counts": dict(self._reason_counts),
            "cost_attributions": list(self._cost_attributions),
        }

    # -- internal helpers -----------------------------------------------

    def _build_account_state(self, *, instrument: str, account_known: bool) -> AccountRiskState:
        positions = self.portfolio.positions
        unrealized_total = sum(
            (view.unrealized_pnl for view in positions.values()), start=ZERO
        )
        return AccountRiskState(
            state_version="pipeline:v1",
            known=account_known,
            reconciled=self.execution_engine.mode.value == "ready",
            equity=self.portfolio.equity,
            peak_equity=self._peak_equity,
            # G3 fix: `equity` above is already net of fees
            # (`Portfolio.equity` = starting_balance + realized_pnl - fees +
            # unrealized), but `realized_pnl_today` was gross of fees --
            # once real per-fill fees exist (this slice), the daily-loss
            # check in RiskEngine would under-count losses driven purely by
            # fees. Sprint 1 has no authoritative UTC daily-reset boundary
            # yet (docs/OPEN_QUESTIONS.md #13, unresolved): `realized_pnl`
            # itself is already all-time cumulative, not reset daily, and
            # "today" is a misnomer carried over from that open question.
            # Consistent with that existing (lack of) windowing, this
            # subtracts the same all-time cumulative `portfolio.fees`
            # rather than inventing a new, separate daily-fee-reset
            # mechanism that nothing else in the codebase has yet.
            realized_pnl_today=self.portfolio.realized_pnl - self.portfolio.fees,
            unrealized_pnl=unrealized_total,
            gross_notional=self.portfolio.gross_notional,
            net_notional=self.portfolio.net_notional,
            instrument_notionals=self.portfolio.instrument_notionals,
            positions={i: v.quantity for i, v in positions.items()},
            leverage_cap=self.leverage_cap,
            consecutive_losses=self._consecutive_losses,
        )

    def _mark_positions_from_history(self, history: Sequence[MarketSnapshot]) -> None:
        latest_last_by_instrument: dict[str, Decimal] = {}
        for snapshot in history:
            latest_last_by_instrument[snapshot.instrument] = snapshot.last
        for instrument, last_price in latest_last_by_instrument.items():
            self.portfolio.mark(instrument, last_price)

    def _position_qty(self, instrument: str) -> Decimal:
        view = self.portfolio.positions.get(instrument)
        return view.quantity if view is not None else ZERO

    def _position_snapshot(self) -> dict[str, Decimal]:
        """Every currently-known instrument's position quantity, captured
        immediately before a background execution-engine call whose fills
        may land on any instrument (unlike `process()`, which only ever
        touches the one instrument its own signal/history is for)."""
        return {
            instrument: view.quantity for instrument, view in self.portfolio.positions.items()
        }

    def _reconcile_background_fills(
        self, *, before_positions: dict[str, Decimal], now: datetime
    ) -> bool:
        """Reconcile every fill `on_trade()`/`on_time()`/`report_fill()`
        produced (`_pending_fills`) into the same `_record_fill` accounting
        `process()` uses for its own submit()-driven entry fill.

        Unlike `process()` (exactly one instrument, exactly one `submit()`
        call), a single background call can in principle produce fills
        across multiple instruments and multiple orders. Each fill is
        replayed through `_record_fill` individually, in the exact order
        the engine produced it, with a per-instrument running position
        quantity and a per-fill realized-pnl/fees delta (`_fill_deltas`,
        populated by `_on_fill`) -- not a single global before/after
        snapshot of `portfolio.realized_pnl`/`fees` taken around the whole
        call, since those are portfolio-wide cumulative counters that
        cannot be safely split across instruments once more than one
        instrument's fills interleave within the same call. This is what
        lets two different instruments filled within one call each get
        their own correct before/after quantities and realized/fee deltas
        rather than double-counting or misattributing a shared global
        delta.

        Background fills have no quote-side reference price/bid/ask of
        their own (only `FillEvent.price`), unlike `process()`'s
        quote-driven entry -- the fill price itself is used as a
        zero-spread stand-in for `expected_price`/quote bid/ask. This only
        ever affects the reporting-only `CostBreakdown` attribution
        (`docs/EXECUTION_CONTRACT.md`), never `TradeOutcome`/realized PnL.

        Returns True if at least one fill was reconciled (False is a safe
        no-op: no phantom `TradeOutcome`, no accounting side effects).
        """
        if not self._pending_fills:
            return False

        running_qty = dict(before_positions)
        running_realized = self.portfolio.realized_pnl
        running_fees = self.portfolio.fees
        # Walk backwards from the current (post-call) totals to the
        # absolute realized/fees anchor immediately before the FIRST
        # pending fill, so absolute per-fill before/after values can be
        # reconstructed walking forward, in original order, below.
        for fill in reversed(self._pending_fills):
            realized_delta, fees_delta = self._fill_deltas.get(fill.fill_id, (ZERO, ZERO))
            running_realized -= realized_delta
            running_fees -= fees_delta

        for fill in self._pending_fills:
            instrument = fill.instrument
            before_qty = running_qty.get(instrument, ZERO)
            signed_delta = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
            after_qty = before_qty + signed_delta
            running_qty[instrument] = after_qty

            realized_delta, fees_delta = self._fill_deltas.get(fill.fill_id, (ZERO, ZERO))
            realized_before = running_realized
            fees_before = running_fees
            running_realized += realized_delta
            running_fees += fees_delta

            self._record_fill(
                instrument=instrument,
                fill_price=fill.price,
                expected_price=fill.price,
                quote_bid=fill.price,
                quote_ask=fill.price,
                before_qty=before_qty,
                after_qty=after_qty,
                realized_before=realized_before,
                realized_after=running_realized,
                fees_before=fees_before,
                fees_after=running_fees,
                order_side=fill.side,
                now=now,
            )

        self._pending_fills.clear()
        self._fill_deltas.clear()
        self._update_peak_equity()
        return True

    def _update_peak_equity(self) -> None:
        equity = self.portfolio.equity
        if equity > self._peak_equity:
            self._peak_equity = equity

    def _record_fill(
        self,
        *,
        instrument: str,
        fill_price: Decimal,
        expected_price: Decimal,
        quote_bid: Decimal,
        quote_ask: Decimal,
        before_qty: Decimal,
        after_qty: Decimal,
        realized_before: Decimal,
        realized_after: Decimal,
        fees_before: Decimal,
        fees_after: Decimal,
        order_side: OrderSide,
        now: datetime,
    ) -> None:
        filled_quantity = abs(after_qty - before_qty)
        if filled_quantity == ZERO:
            return  # duplicate/no-op fill: nothing new to attribute

        accumulator = self._trade_accum.get(instrument)
        if accumulator is None:
            # Either a fresh position opening from flat, or a position that
            # was already open before this pipeline instance observed it
            # (e.g. imported checkpoint): either way, open a fresh
            # accumulator anchored to the current realized PnL so we never
            # invent a phantom trade.
            accumulator = _TradeAccumulator(
                notional=ZERO,
                fees=ZERO,
                realized_pnl_delta=ZERO,
                open_time=now,
                position_side=TradeSide.LONG if order_side is OrderSide.BUY else TradeSide.SHORT,
                entry_bid=quote_bid,
                entry_ask=quote_ask,
            )
            self._trade_accum[instrument] = accumulator

        accumulator.notional += filled_quantity * fill_price
        accumulator.fees += fees_after - fees_before
        accumulator.realized_pnl_delta += realized_after - realized_before

        # Cost-attribution (reporting only) bookkeeping: classify this fill
        # as adding to (entry-side) or reducing (exit-side) the position's
        # absolute size, quantity-weighting the actual fill price and the
        # quote-side "expected" reference price separately. See
        # `_TradeAccumulator`'s docstring -- this never feeds back into
        # `TradeOutcome`/`realized_pnl`, only into the `CostBreakdown` built
        # below once the position fully closes.
        if abs(after_qty) >= abs(before_qty):
            accumulator.entry_notional += filled_quantity * fill_price
            accumulator.entry_quantity += filled_quantity
            accumulator.entry_expected_notional += filled_quantity * expected_price
        else:
            accumulator.exit_notional += filled_quantity * fill_price
            accumulator.exit_quantity += filled_quantity
            accumulator.exit_expected_notional += filled_quantity * expected_price

        if after_qty == ZERO:
            gross_pnl = accumulator.realized_pnl_delta
            outcome = TradeOutcome(
                gross_pnl=gross_pnl,
                notional=accumulator.notional,
                fees=accumulator.fees,
            )
            self._trade_outcomes.append(outcome)
            self._attribute_costs(
                instrument=instrument,
                accumulator=accumulator,
                exit_bid=quote_bid,
                exit_ask=quote_ask,
                now=now,
            )
            del self._trade_accum[instrument]
            if outcome.net_pnl < ZERO:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

    def _attribute_costs(
        self,
        *,
        instrument: str,
        accumulator: _TradeAccumulator,
        exit_bid: Decimal,
        exit_ask: Decimal,
        now: datetime,
    ) -> None:
        """Build a reporting-only `CostBreakdown` for a just-closed trade.

        Never subtracted from `TradeOutcome`/portfolio PnL (Q-C1,
        docs/OPEN_QUESTIONS.md #25) -- purely additive attribution exposed
        via `metrics()["cost_attributions"]`. Skipped (not an error) when
        the accumulator has no clean entry+exit vwap to price (e.g. a
        position that flipped through zero without ever fully closing via
        this pipeline's own fills, or a checkpoint-imported position with
        an incomplete entry side).
        """
        if (
            accumulator.entry_quantity <= ZERO
            or accumulator.exit_quantity <= ZERO
            or accumulator.position_side is None
        ):
            return

        entry_vwap = accumulator.entry_notional / accumulator.entry_quantity
        exit_vwap = accumulator.exit_notional / accumulator.exit_quantity
        expected_entry_vwap = accumulator.entry_expected_notional / accumulator.entry_quantity
        expected_exit_vwap = accumulator.exit_expected_notional / accumulator.exit_quantity
        quantity = min(accumulator.entry_quantity, accumulator.exit_quantity)

        request = CostCalculationRequest(
            trade_id=f"{instrument}:{accumulator.open_time.isoformat()}:{now.isoformat()}",
            instrument=instrument,
            instrument_class=InstrumentClass.PERPETUAL,
            side=accumulator.position_side,
            # Round-trip attribution mixes maker/taker fills across the
            # trade's life; TAKER is the conservative reporting-only
            # default (per Slice 2 plan).
            liquidity_role=LiquidityRole.TAKER,
            quantity=quantity,
            entry_price=entry_vwap,
            exit_price=exit_vwap,
            expected_entry_price=expected_entry_vwap,
            expected_exit_price=expected_exit_vwap,
            entry_bid=accumulator.entry_bid,
            entry_ask=accumulator.entry_ask,
            exit_bid=exit_bid,
            exit_ask=exit_ask,
            holding_period=now - accumulator.open_time,
        )
        breakdown = calculate_trade_costs(request, self.cost_schedule)
        self._cost_attributions.append(breakdown)

    def _finish(
        self,
        stage: PipelineStage,
        *,
        signal_id: str | None,
        risk_reason: str | None,
        execution_status: OrderStatus | None,
        execution_reject_code: RejectCode | None,
        position_after: Decimal | None,
    ) -> PipelineOutcome:
        self._stage_counts[stage.value] = self._stage_counts.get(stage.value, 0) + 1
        if risk_reason is not None:
            self._reason_counts[risk_reason] = self._reason_counts.get(risk_reason, 0) + 1
        return PipelineOutcome(
            stage=stage,
            signal_id=signal_id,
            risk_reason=risk_reason,
            execution_status=execution_status,
            execution_reject_code=execution_reject_code,
            position_after=position_after,
        )

