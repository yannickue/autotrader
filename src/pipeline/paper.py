"""Deterministic Sprint 1 integration glue for the paper trading path.

LEGACY_RUNTIME / SHADOW_ORACLE (Nautilus convergence, see
docs/ARCHITECTURE_AUDIT_2026-09-29.md): retained as a parity oracle; do not add features
except confirmed safety fixes.

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
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

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
from execution.orders import (
    TERMINAL_STATUSES,
    Order,
    OrderStatus,
    RejectCode,
    SubmitResult,
)
from execution.paper import EngineMode, FillEvent, PaperExecutionEngine
from exits.engine import ExitEngine, apply_evaluation
from exits.models import (
    ExitDecision,
    ExitMarketState,
    ExitOutcome,
    ExitPosition,
    ExitReason,
    PositionSide,
    StopStage,
)
from monitoring.metrics import TradeOutcome, calculate_trade_metrics
from persistence.models import (
    FillRecord,
    HaltStateRecord,
    OrderRecord,
    PortfolioStateRecord,
    PositionRecord,
    ReconciliationStateRecord,
    ReduceOnlyReservationRecord,
    ReservationRecord,
    StateSnapshot,
)
from persistence.store import SQLiteStore
from portfolio.ledger import Portfolio
from portfolio.models import Fill
from risk.engine import RiskEngine
from risk.models import (
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    ReconciliationSource,
    ReconciliationState,
    RiskDecision,
    RiskReason,
    RiskSide,
    RuntimeRiskState,
)
from risk.trading_day import TradingDayPolicy, realized_pnl_today
from signals.models import Direction, Signal

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
    # Slice 3b (exit engine integration, docs/OPEN_QUESTIONS.md #25 Q-X0/X1):
    # POSITION_ALREADY_OPEN is reached when a fresh signal would otherwise
    # open a new entry on an instrument that already has one open position
    # under exit management (Q-X1: one open position per instrument, no
    # pyramiding). SIGNAL_REVERSAL_EXIT is reached when a fresh signal
    # opposes the currently-open position's side: this tick closes the
    # position via the exit engine instead of opening/flipping a new one:
    # the earliest the same instrument may open a fresh position is the
    # NEXT process() call, once flat.
    POSITION_ALREADY_OPEN = "position_already_open"
    SIGNAL_REVERSAL_EXIT = "signal_reversal_exit"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitStepResult:
    """Result of one `PaperTradingPipeline._evaluate_exit()`/`process_exits()` tick.

    `decision` is `None` when the exit engine had nothing to do this tick
    (e.g. no trigger condition was met). `risk_decision`/`execution_result`/
    `outcome` are all `None` together with `decision` in that no-op case;
    once a `decision` was emitted, `risk_decision` is always populated
    (either approved or rejected), and `execution_result`/`outcome` are
    populated once a reduce-only request was actually submitted to
    execution (never submitted at all when risk rejected the request).
    """

    instrument: str
    decision: ExitDecision | None
    risk_decision: RiskDecision | None
    execution_result: SubmitResult | None
    outcome: ExitOutcome | None
    position_closed: bool


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
    exit_engine: ExitEngine

    # Slice 4b (persistence integration): optional durable store. `None`
    # (the default) preserves every existing caller/test/harness exactly --
    # no persistence occurs, `_persist()` is a no-op. When set, every public
    # mutating entry point (`process`, `process_exits`, `process_trade_event`,
    # `process_time_tick`, `process_reported_fill`) durably snapshots state
    # at the very end of its work (see `_persist()`), on EVERY return path
    # (not only the "happy path"), since even an early return can have
    # mutated mark prices, exit-managed positions, or reservations.
    store: SQLiteStore | None = None

    # Trading-day boundary used for `AccountRiskState.realized_pnl_today`.
    # Default: UTC midnight, explicitly PENDING broker calibration
    # (risk.trading_day) -- ActivTrades' real rollover is not yet known.
    trading_day_policy: TradingDayPolicy = field(default_factory=TradingDayPolicy)

    # instrument -> the ExitPosition currently under exit management. Only
    # ever populated by this pipeline's own entry path (`_open_exit_position`,
    # called from `_record_fill` when an entry fill opens a position from
    # flat) -- see PART 2 docstring on `_evaluate_exit` for the full
    # lifecycle. `ExitPosition.quantity` here is a snapshot only; the
    # portfolio's own position quantity is always the source of truth and is
    # re-derived every tick in `_evaluate_exit`.
    _exit_positions: dict[str, ExitPosition] = field(init=False, default_factory=dict)

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

    # Slice 4b: every real fill observed since the last `_persist()` call,
    # regardless of which internal code path produced it. Deliberately
    # SEPARATE from `_pending_fills` above: `_pending_fills` is cleared and
    # repopulated at specific points inside `process()`/`_evaluate_exit()`
    # for THEIR OWN accounting purposes (and is left untouched, i.e. stale,
    # on no-op branches that never reach a `submit()` call) -- it is not a
    # reliable "fills produced by this call" signal for persistence. This
    # list is only ever appended to (by `_on_fill`) and drained by
    # `_persist()` itself, so it always holds exactly the fills observed
    # since the last successful persist, independent of which branch of
    # which method produced them.
    _persist_pending_fills: list[FillEvent] = field(init=False, default_factory=list)

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
        self._persist_pending_fills.append(event)

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
                now=now,
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

        # -- exit management (docs/OPEN_QUESTIONS.md #25 Q-X0/X1/X3) --------
        # An instrument with an already-open position must have that
        # position exit-managed (stop/trailing/break-even/time/momentum/
        # liquidity/target) on EVERY process() call regardless of whether it
        # is in the universe or has a fresh signal this tick -- this runs
        # before the universe/signal-gated entry path below. The strategy
        # signal is only evaluated early (here) when there IS an open
        # position, so a reversal (Q-X1: opposite-direction signal closes
        # rather than flips) can be detected; `signal_evaluated` prevents a
        # redundant second `strategy.evaluate()` call further down.
        existing_exit_position = self._exit_positions.get(instrument)
        signal: Signal | None = None
        signal_evaluated = False
        if existing_exit_position is not None:
            signal = strategy.evaluate(history)
            signal_evaluated = True
            reversal = self._signal_opposes_position(signal, existing_exit_position)
            self._evaluate_exit(
                instrument=instrument,
                snapshot=latest,
                runtime=runtime,
                account_known=account_known,
                now=now,
                signal_reversal=reversal,
            )
            if reversal:
                # Never also process a new entry in the same tick, even if
                # the reversal fully closed the position -- the earliest a
                # fresh entry may be considered is the NEXT process() call.
                return self._finish(
                    PipelineStage.SIGNAL_REVERSAL_EXIT,
                    now=now,
                    signal_id=signal.signal_id if signal is not None else None,
                    risk_reason=None,
                    execution_status=None,
                    execution_reject_code=None,
                    position_after=self._position_qty(instrument),
                )

        if instrument not in universe:
            return self._finish(
                PipelineStage.NOT_IN_UNIVERSE,
                now=now,
                signal_id=None,
                risk_reason=None,
                execution_status=None,
                execution_reject_code=None,
                position_after=self._position_qty(instrument),
            )

        if not signal_evaluated:
            signal = strategy.evaluate(history)
        if signal is None:
            return self._finish(
                PipelineStage.NO_SIGNAL,
                now=now,
                signal_id=None,
                risk_reason=None,
                execution_status=None,
                execution_reject_code=None,
                position_after=self._position_qty(instrument),
            )

        if instrument in self._exit_positions:
            # Q-X1: one open position per instrument, no pyramiding. The
            # exit-management block above may have just closed it via a
            # non-reversal trigger (stop/target/time/...) in this very
            # call, in which case this instrument is no longer in
            # `_exit_positions` and this branch is correctly skipped.
            return self._finish(
                PipelineStage.POSITION_ALREADY_OPEN,
                now=now,
                signal_id=signal.signal_id,
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
                now=now,
                signal_id=signal.signal_id,
                risk_reason=RiskReason.INVALID_INPUT.value,
                execution_status=None,
                execution_reject_code=None,
                position_after=self._position_qty(instrument),
            )

        account = self._build_account_state(
            instrument=instrument, account_known=account_known, now=now
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
                now=now,
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
                # Only this, the pipeline's own signal-driven entry path, is
                # allowed to open a NEW exit-managed position (Q-X1); the
                # risk-approved stop threads through here so
                # `_open_exit_position` can construct the initial
                # `ExitPosition` with a real, risk-validated invalidation
                # stop rather than an inferred one.
                entry_stop_price=decision.stop_price,
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
            now=now,
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

    def process_exits(
        self,
        *,
        snapshot: MarketSnapshot,
        runtime: RuntimeRiskState,
        account_known: bool,
        now: datetime,
    ) -> ExitStepResult | None:
        """Tick exit management for `snapshot.instrument` outside `process()`.

        `process()` already exit-manages the current instrument on every
        call it makes for that instrument, but an instrument with no fresh
        signal this cycle (or that a driver simply isn't calling
        `process()` for this tick) still needs its stop/trailing/time exit
        managed every tick, not only on ticks where `process()` happens to
        run for it -- this is that passthrough. Returns `None` when the
        instrument has no open exit-managed position (a safe no-op).
        """
        instrument = snapshot.instrument
        self.portfolio.mark(instrument, snapshot.last)
        self._update_peak_equity()
        result = self._evaluate_exit(
            instrument=instrument,
            snapshot=snapshot,
            runtime=runtime,
            account_known=account_known,
            now=now,
            signal_reversal=False,
        )
        # Slice 4b: this method does not funnel through `_finish()` (it
        # returns `_evaluate_exit`'s own `ExitStepResult`/`None` directly),
        # so it is responsible for calling `_persist()` itself at the very
        # end of its work -- on every path, including the `None` no-op
        # (which can still follow a real `portfolio.mark()` call above).
        self._persist(now=now)
        return result

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
            now=now,
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
            now=now,
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
            now=now,
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

    def _build_account_state(
        self, *, instrument: str, account_known: bool, now: datetime
    ) -> AccountRiskState:
        positions = self.portfolio.positions
        unrealized_total = sum(
            (view.unrealized_pnl for view in positions.values()), start=ZERO
        )
        return AccountRiskState(
            state_version="pipeline:v1",
            known=account_known,
            reconciliation=self._reconciliation_state(),
            equity=self.portfolio.equity,
            peak_equity=self._peak_equity,
            # `equity` above is net of fees, so this figure is net of fees too
            # (G3). It is the realized PnL - fees of fills in the CURRENT
            # trading day only (risk.trading_day.TradingDayPolicy); it used to
            # be the all-time cumulative figure despite its name. The window
            # start is passed along so the risk policy can verify that.
            realized_pnl_today=realized_pnl_today(
                self.portfolio.pnl_entries, now=now, policy=self.trading_day_policy
            ),
            pnl_window_start=self.trading_day_policy.trading_day_start(now),
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

    # -- exit engine integration (docs/OPEN_QUESTIONS.md #25 Q-X0/X1/X2/X3) --

    @staticmethod
    def _signal_opposes_position(signal: Signal | None, position: ExitPosition) -> bool:
        """Q-X1: an opposite-direction signal is a reversal trigger for the
        exit engine (closes the position), never a same-tick flip."""
        if signal is None:
            return False
        signal_side = RiskSide.BUY if signal.direction is Direction.LONG else RiskSide.SELL
        position_side = RiskSide.BUY if position.side is PositionSide.LONG else RiskSide.SELL
        return signal_side != position_side

    def _reconciliation_state(self) -> ReconciliationState:
        """Reconciliation state of the execution engine, read verbatim.

        Deliberately NOT derived from `EngineMode`: READY is a permission
        mode, and a HALTED engine whose halt did not invalidate local state
        keeps its last (RECONCILED) outcome -- which is exactly what lets
        reduce-only exits stay allowed during such a halt (docs/OPEN_QUESTIONS.md
        #25 Q-X2) -- while UNKNOWN_ORDER / RECONCILIATION_MISMATCH /
        INTERNAL_ERROR / failed recovery set MISMATCH inside the engine
        itself. No halt-reason string is parsed here.
        """
        return self.execution_engine.reconciliation_state

    @staticmethod
    def _map_execution_status_to_exit_outcome(status: OrderStatus) -> ExitOutcome:
        if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            return ExitOutcome.FILLED
        if status is OrderStatus.CANCELED:
            return ExitOutcome.CANCELED
        return ExitOutcome.REJECTED  # REJECTED/EXPIRED: the reservation was never used

    def _open_exit_position(
        self,
        *,
        instrument: str,
        order_side: OrderSide,
        entry_price: Decimal,
        quantity: Decimal,
        stop_price: Decimal,
        now: datetime,
    ) -> None:
        """Construct the initial `ExitPosition` for a fresh entry fill that
        opened a position from flat, and start exit-managing it.

        Only ever called from `_record_fill` for THIS pipeline's own
        signal-driven entry path (`process()`), the only path allowed to
        open a new position under Q-X1's one-open-position-per-instrument
        rule.
        """
        side = PositionSide.LONG if order_side is OrderSide.BUY else PositionSide.SHORT
        try:
            position = ExitPosition(
                position_id=f"exit-pos:{instrument}:{now.isoformat()}",
                instrument=instrument,
                side=side,
                entry_price=entry_price,
                quantity=quantity,
                initial_stop_price=stop_price,
                current_stop_price=stop_price,
                opened_at=now,
            )
        except ValueError as exc:
            # Fail closed rather than propagate or silently leave a real,
            # unprotected position outside exit management. No real
            # market/runtime context is available this deep inside fill
            # accounting to safely fabricate a reduce-only
            # ExecutionRequest, so the safe action taken here is a
            # system-wide halt of all NEW exposure (every future
            # RiskEngine.evaluate() call fails closed against it) --
            # documented as a deviation from a literal "submit an
            # emergency reduce-only close" in the slice report.
            # `RiskEngine._stop_distance` already validates stop-price
            # side before any decision reaches this point, so this is a
            # should-never-happen defensive backstop.
            self.risk_engine.halt(
                f"exit position construction failed for {instrument} "
                f"(qty={quantity}, entry={entry_price}, stop={stop_price}) at "
                f"{now.isoformat()}: {exc}"
            )
            return
        self._exit_positions[instrument] = position

    def _evaluate_exit(
        self,
        *,
        instrument: str,
        snapshot: MarketSnapshot,
        runtime: RuntimeRiskState,
        account_known: bool,
        now: datetime,
        signal_reversal: bool = False,
    ) -> ExitStepResult | None:
        """One exit-management tick for `instrument`'s open position, if any.

        No-op (returns `None`) when `instrument` has no open exit-managed
        position. Otherwise: re-derives the position's quantity from the
        portfolio (the source of truth, never a locally-tracked counter),
        builds `ExitMarketState` from `snapshot`, calls
        `self.exit_engine.evaluate()`, folds the ratcheted stop/high-water
        mark via `apply_evaluation()`, and -- if a decision was emitted --
        routes it through `risk_engine.evaluate_reduce_only()` and, if
        approved, `execution_engine.submit()`, reconciling any resulting
        fill through the same `_record_fill` accounting `process()` uses.
        `exit_engine.notify_terminal()` is called on every terminal
        execution outcome (approved-and-resolved, or risk-rejected), never
        skipped, so a reduce-only reservation is never leaked.

        `market.risk_halt` is always False here (deliberate: Q-X3 forbids
        wiring an execution/runtime halt or kill switch into an automatic
        flatten -- see docs/OPEN_QUESTIONS.md #25). This method itself
        never checks `runtime.kill_switch`/`execution_engine.mode` before
        evaluating a trigger, so a halt by itself never creates a decision;
        it only affects whether risk APPROVES a decision the exit engine
        independently decided to emit (via `_reconciliation_state()`, Q-X2).
        """
        stored = self._exit_positions.get(instrument)
        if stored is None:
            return None

        current_qty = abs(self._position_qty(instrument))
        if current_qty <= ZERO:
            # Portfolio is the source of truth for quantity: if some other
            # path already flattened this instrument, drop the stale
            # exit-management record rather than trust a local counter.
            del self._exit_positions[instrument]
            return None

        materialized = replace(stored, quantity=current_qty)
        market_state = ExitMarketState(
            instrument=instrument,
            timestamp=snapshot.timestamp,
            price=snapshot.last,
            bid=snapshot.bid,
            ask=snapshot.ask,
            volatility=snapshot.volatility,
            momentum_score=None,
            signal_reversal=signal_reversal,
            risk_halt=False,
            available_liquidity_notional=snapshot.liquidity,
        )

        evaluation = self.exit_engine.evaluate(position=materialized, market=market_state, now=now)
        new_position = apply_evaluation(materialized, evaluation)

        decision = evaluation.decision
        if decision is None:
            self._exit_positions[instrument] = new_position
            return ExitStepResult(
                instrument=instrument,
                decision=None,
                risk_decision=None,
                execution_result=None,
                outcome=None,
                position_closed=False,
            )

        # Persist the pending-close guard BEFORE risk/execution are even
        # consulted, mirroring `ExitPosition.pending_close_request_id`'s own
        # purpose: a reentrant exit tick for this instrument (e.g. a driver
        # calling `process_exits()` again before this call returns) must
        # see it and no-op via `ExitEngine._no_op_if_already_closing`,
        # never submit a second overlapping reduce-only request.
        self._exit_positions[instrument] = replace(
            new_position, pending_close_request_id=decision.request_id
        )

        account = self._build_account_state(
            instrument=instrument, account_known=account_known, now=now
        )
        risk_decision = self.risk_engine.evaluate_reduce_only(
            request_id=decision.request_id,
            instrument=decision.instrument,
            side=decision.close_side,
            quantity=decision.quantity,
            account=account,
            runtime=runtime,
            now=now,
        )

        if not risk_decision.approved:
            # Nothing was submitted: release()'s underlying no-op is safe
            # regardless (see `ExitEngine.notify_terminal` docstring), and
            # clearing the pending guard lets the next tick retry once the
            # blocking condition (e.g. Q-X2 unreconciled account) clears.
            self.exit_engine.notify_terminal(risk_decision.decision_id, ExitOutcome.REJECTED)
            self._exit_positions[instrument] = replace(new_position, pending_close_request_id=None)
            return ExitStepResult(
                instrument=instrument,
                decision=decision,
                risk_decision=risk_decision,
                execution_result=None,
                outcome=ExitOutcome.REJECTED,
                position_closed=False,
            )

        order_side = OrderSide.BUY if decision.close_side is RiskSide.BUY else OrderSide.SELL
        request = ExecutionRequest(
            request_id=f"exec:{decision.request_id}",
            risk_decision_id=risk_decision.decision_id,
            instrument=instrument,
            timestamp=now,
            side=order_side,
            quantity=risk_decision.quantity,
            order_type=OrderType.MARKET,
            limit_price=None,
            reduce_only=True,
            client_order_id=f"coid:{decision.request_id}",
            time_in_force=TimeInForce.IOC,
            metadata={"reference_price": str(snapshot.last)},
        )

        before_qty = self._position_qty(instrument)
        realized_before = self.portfolio.realized_pnl
        fees_before = self.portfolio.fees
        self._pending_fills.clear()
        self._fill_deltas.clear()

        result = self.execution_engine.submit(request, risk_decision, snapshot, now)

        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            after_qty = self._position_qty(instrument)
            realized_after = self.portfolio.realized_pnl
            fees_after = self.portfolio.fees

            own_fills = [
                f for f in self._pending_fills if f.client_order_id == request.client_order_id
            ]
            if own_fills:
                fill_notional = sum((f.price * f.quantity for f in own_fills), ZERO)
                fill_quantity = sum((f.quantity for f in own_fills), ZERO)
                actual_fill_price = fill_notional / fill_quantity
            else:
                actual_fill_price = snapshot.last

            self._record_fill(
                instrument=instrument,
                fill_price=actual_fill_price,
                expected_price=snapshot.last,
                quote_bid=snapshot.bid,
                quote_ask=snapshot.ask,
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

            remaining_qty = abs(after_qty)
            pending = (
                None if result.status in TERMINAL_STATUSES else decision.request_id
            )
            if remaining_qty <= ZERO:
                self._exit_positions.pop(instrument, None)
            else:
                closed_quantity = abs(before_qty) - remaining_qty
                stage_index = decision.metadata.get("stage_index")
                self._exit_positions[instrument] = replace(
                    new_position,
                    quantity=remaining_qty,
                    realized_partial_quantity=(
                        new_position.realized_partial_quantity + closed_quantity
                    ),
                    stages_completed=(
                        stage_index + 1
                        if (
                            decision.reason == ExitReason.TAKE_PROFIT
                            and stage_index is not None
                        )
                        else new_position.stages_completed
                    ),
                    pending_close_request_id=pending,
                )
        else:
            pending = None if result.status in TERMINAL_STATUSES else decision.request_id
            self._exit_positions[instrument] = replace(
                new_position, pending_close_request_id=pending
            )

        outcome: ExitOutcome | None = None
        if result.status in TERMINAL_STATUSES:
            outcome = self._map_execution_status_to_exit_outcome(result.status)
            self.exit_engine.notify_terminal(risk_decision.decision_id, outcome)

        return ExitStepResult(
            instrument=instrument,
            decision=decision,
            risk_decision=risk_decision,
            execution_result=result,
            outcome=outcome,
            position_closed=instrument not in self._exit_positions,
        )

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
        entry_stop_price: Decimal | None = None,
    ) -> None:
        filled_quantity = abs(after_qty - before_qty)
        if filled_quantity == ZERO:
            return  # duplicate/no-op fill: nothing new to attribute

        if before_qty == ZERO and after_qty != ZERO and entry_stop_price is not None:
            self._open_exit_position(
                instrument=instrument,
                order_side=order_side,
                entry_price=fill_price,
                quantity=abs(after_qty),
                stop_price=entry_stop_price,
                now=now,
            )

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
            # Portfolio is the source of truth for exit-management state
            # too: drop it here regardless of which path drove the
            # flattening fill (this pipeline's own `_evaluate_exit`, or a
            # protective-child fill observed through
            # `_reconcile_background_fills`) -- `_evaluate_exit` already
            # does this for its own path, but a protective STOP/
            # TAKE_PROFIT child filling via `on_trade()`/`on_time()`/
            # `report_fill()` never goes through `_evaluate_exit` at all.
            self._exit_positions.pop(instrument, None)
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

    # -- persistence (Slice 4b) -------------------------------------------
    #
    # Single commit point: builds ONE `store.write_snapshot(...)` call
    # (itself one `store.transaction()`) covering every mutable-state table
    # PLUS this call's newly observed fills, so "the fills this call
    # produced were durably recorded" and "the rest of this call's state was
    # snapshotted" commit or roll back together atomically. `None` `store`
    # (the default for every pre-existing caller/test) makes this a no-op.

    def _persist(self, *, now: datetime) -> None:
        if self.store is None:
            return

        positions = tuple(
            PositionRecord(
                instrument=instrument,
                quantity=view.quantity,
                avg_entry_price=view.avg_entry_price,
                mark_price=view.mark_price,
                updated_at=now,
            )
            for instrument, view in self.portfolio.positions.items()
        )
        orders = tuple(
            _order_record(order, now) for order in self.execution_engine.orders.values()
        )

        risk_state = self.risk_engine.export_state()
        reservations = tuple(
            ReservationRecord(
                decision_id=decision_id,
                side=payload["side"],
                abs_notional=Decimal(payload["abs_notional"]),
                signed_notional=Decimal(payload["signed_notional"]),
                updated_at=now,
            )
            for decision_id, payload in risk_state["reservations"].items()
        )
        reduce_only_reservations = tuple(
            ReduceOnlyReservationRecord(
                decision_id=decision_id,
                instrument=payload["instrument"],
                quantity=Decimal(payload["quantity"]),
                updated_at=now,
            )
            for decision_id, payload in risk_state["reduce_only_reservations"].items()
        )

        portfolio_snapshot = self.portfolio.export_state()
        portfolio_state = PortfolioStateRecord(
            starting_balance=Decimal(portfolio_snapshot["starting_balance"]),
            realized_pnl=Decimal(portfolio_snapshot["realized_pnl"]),
            fees=Decimal(portfolio_snapshot["fees"]),
            updated_at=now,
        )

        # Two DIFFERENT halts, deliberately split across the store's two
        # singleton rows rather than sharing one ambiguous row (flagged by
        # the original design analysis as ambiguous in the v2 schema):
        # `risk_engine`'s own kill-switch-style HALT (`halt_state`) is not
        # the same thing as `execution_engine.mode`/`halt_reason`
        # (`reconciliation_state`) -- either can be true independent of the
        # other (e.g. risk can HALT without execution ever having halted,
        # and vice versa: execution can be RECONCILING/HALTED on a fresh
        # engine that risk never touched).
        halt_state = HaltStateRecord(
            halted=self.risk_engine.halted,
            reason=self.risk_engine.halt_reason,
            updated_at=now,
        )
        execution_mode = self.execution_engine.mode
        reconciliation_state = ReconciliationStateRecord(
            mode=str(execution_mode),
            reconciled=(
                self.execution_engine.reconciliation_state is ReconciliationState.RECONCILED
            ),
            state=self.execution_engine.reconciliation_state.value,
            source=(
                None
                if self.execution_engine.reconciliation_source is None
                else self.execution_engine.reconciliation_source.value
            ),
            mismatch_reason=self.execution_engine.halt_reason,
            # The engine's own timestamp of its last successful comparison
            # (None if none happened since start/restart) -- never `now`
            # merely because the engine is READY.
            last_reconciled_at=self.execution_engine.last_reconciled_at,
            updated_at=now,
        )

        component_state = {
            # The FULL `export_checkpoint()` (including its own "orders" key)
            # is stored verbatim here, not just the parts not already
            # covered by the typed `orders` table. Deliberate: recovery
            # restores execution state via
            # `execution_engine.import_checkpoint(component_state["execution"])`
            # -- the already-tested, single existing deserialization path --
            # rather than hand-reconstructing `Order` objects a second time
            # from the typed `orders` table rows, which would duplicate
            # logic and risk the two representations silently drifting. The
            # typed `orders` table is still populated (below, from the same
            # live `execution_engine.orders` view) for schema-intended
            # typed-query/tooling access; it is simply not what recovery
            # itself reads back from.
            "execution": self.execution_engine.export_checkpoint(),
            "pipeline": {
                "peak_equity": str(self._peak_equity),
                "initial_equity": str(self._initial_equity),
                "consecutive_losses": self._consecutive_losses,
                "trade_outcomes": [
                    {
                        "gross_pnl": str(outcome.gross_pnl),
                        "notional": str(outcome.notional),
                        "fees": str(outcome.fees),
                    }
                    for outcome in self._trade_outcomes
                ],
                "stage_counts": dict(self._stage_counts),
                "reason_counts": dict(self._reason_counts),
                # `_cost_attributions` is deliberately NOT persisted: it is
                # reporting-only (never feeds back into TradeOutcome/
                # portfolio PnL/risk/exposure -- see `_attribute_costs`
                # docstring), so losing it across a crash is strictly lower
                # stakes than losing any risk/position/exposure state, and
                # skipping it keeps every snapshot smaller and simpler.
            },
            "exits": {
                instrument: _exit_position_payload(position)
                for instrument, position in self._exit_positions.items()
            },
        }

        fills = tuple(_fill_record(event) for event in self._persist_pending_fills)

        self.store.write_snapshot(
            positions=positions,
            orders=orders,
            reservations=reservations,
            reduce_only_reservations=reduce_only_reservations,
            portfolio_state=portfolio_state,
            halt_state=halt_state,
            reconciliation_state=reconciliation_state,
            component_state=component_state,
            fills=fills,
        )
        self._persist_pending_fills.clear()

    def _finish(
        self,
        stage: PipelineStage,
        *,
        now: datetime,
        signal_id: str | None,
        risk_reason: str | None,
        execution_status: OrderStatus | None,
        execution_reject_code: RejectCode | None,
        position_after: Decimal | None,
    ) -> PipelineOutcome:
        self._stage_counts[stage.value] = self._stage_counts.get(stage.value, 0) + 1
        if risk_reason is not None:
            self._reason_counts[risk_reason] = self._reason_counts.get(risk_reason, 0) + 1
        # Slice 4b: single choke point for every return path of process(),
        # process_trade_event(), process_time_tick(), and
        # process_reported_fill() (every one of them returns via `_finish`),
        # so persistence happens at the very end of each call regardless of
        # which stage it reached -- including early-return stages
        # (NOT_IN_UNIVERSE, NO_SIGNAL, POSITION_ALREADY_OPEN, ...) that can
        # still have mutated mark prices, exit-managed positions, or
        # reservations before returning. `process_exits()` does not funnel
        # through `_finish` (it returns `_evaluate_exit`'s `ExitStepResult`
        # directly), so it calls `_persist()` itself at its own end.
        self._persist(now=now)
        return PipelineOutcome(
            stage=stage,
            signal_id=signal_id,
            risk_reason=risk_reason,
            execution_status=execution_status,
            execution_reject_code=execution_reject_code,
            position_after=position_after,
        )


# -- persistence helpers (module-level; Slice 4b) --------------------------


def _order_record(order: Order, now: datetime) -> OrderRecord:
    """Build a persistence.models.OrderRecord from a live execution.orders.Order."""
    return OrderRecord(
        client_order_id=order.client_order_id,
        request_id=order.request_id,
        decision_id=order.decision_id,
        instrument=order.instrument,
        side=str(order.side),
        order_type=str(order.order_type),
        time_in_force=str(order.time_in_force),
        quantity=order.quantity,
        limit_price=order.limit_price,
        reduce_only=order.reduce_only,
        created_at=order.created_at,
        status=str(order.status),
        filled_quantity=order.filled_quantity,
        avg_fill_price=order.avg_fill_price,
        role=str(order.role),
        parent_client_order_id=order.parent_client_order_id,
        oco_sibling_id=order.oco_sibling_id,
        replaces_client_order_id=order.replaces_client_order_id,
        replaced_by_client_order_id=order.replaced_by_client_order_id,
        trigger_price=order.trigger_price,
        take_profit_price=order.take_profit_price,
        updated_at=order.updated_at if order.updated_at is not None else order.created_at,
        metadata=dict(order.metadata),
    )


def _fill_record(event: FillEvent) -> FillRecord:
    """Build a persistence.models.FillRecord from an observed FillEvent.

    applied_at mirrors timestamp: this pipeline has no separate
    observed-at clock distinct from the injected now the fill was produced
    under, unlike a real venue where a fill trade timestamp and the local
    application time can differ.
    """
    return FillRecord(
        fill_id=event.fill_id,
        instrument=event.instrument,
        side=str(event.side),
        quantity=event.quantity,
        price=event.price,
        fee=event.fee,
        timestamp=event.timestamp,
        applied_at=event.timestamp,
    )


def _exit_position_payload(position: ExitPosition) -> dict[str, Any]:
    """JSON-serializable payload for one ExitPosition, for component_state["exits"]."""
    return {
        "position_id": position.position_id,
        "instrument": position.instrument,
        "side": position.side.value,
        "entry_price": str(position.entry_price),
        "quantity": str(position.quantity),
        "initial_stop_price": str(position.initial_stop_price),
        "current_stop_price": str(position.current_stop_price),
        "stop_stage": position.stop_stage.value,
        "high_water_mark": (
            str(position.high_water_mark) if position.high_water_mark is not None else None
        ),
        "fixed_target_price": (
            str(position.fixed_target_price) if position.fixed_target_price is not None else None
        ),
        "opened_at": position.opened_at.isoformat(),
        "realized_partial_quantity": str(position.realized_partial_quantity),
        "stages_completed": position.stages_completed,
        "pending_close_request_id": position.pending_close_request_id,
    }


def _exit_position_from_payload(payload: Mapping[str, Any]) -> ExitPosition:
    """Inverse of _exit_position_payload."""
    return ExitPosition(
        position_id=payload["position_id"],
        instrument=payload["instrument"],
        side=PositionSide(payload["side"]),
        entry_price=Decimal(payload["entry_price"]),
        quantity=Decimal(payload["quantity"]),
        initial_stop_price=Decimal(payload["initial_stop_price"]),
        current_stop_price=Decimal(payload["current_stop_price"]),
        stop_stage=StopStage(payload["stop_stage"]),
        high_water_mark=(
            Decimal(payload["high_water_mark"]) if payload["high_water_mark"] is not None else None
        ),
        fixed_target_price=(
            Decimal(payload["fixed_target_price"])
            if payload["fixed_target_price"] is not None
            else None
        ),
        opened_at=datetime.fromisoformat(payload["opened_at"]),
        realized_partial_quantity=Decimal(payload["realized_partial_quantity"]),
        stages_completed=payload["stages_completed"],
        pending_close_request_id=payload["pending_close_request_id"],
    )


def _cross_check_replay(replay_portfolio: Portfolio, snapshot: StateSnapshot) -> str | None:
    """Compare a from-scratch fill-replayed Portfolio against persisted rows.

    Returns a human-readable mismatch description, or None if everything
    agrees. This is recover_pipeline's primary defense against any
    crash-boundary leaving inconsistent state: replay is trusted as the
    ground truth derivation (rebuilt fill-by-fill from the append-only,
    fill_id-deduplicated fills table), and persisted positions/
    portfolio_state rows are trusted only insofar as they agree with it.
    """
    if snapshot.portfolio_state is not None:
        if replay_portfolio.realized_pnl != snapshot.portfolio_state.realized_pnl:
            return (
                "realized_pnl mismatch: replayed="
                f"{replay_portfolio.realized_pnl} persisted={snapshot.portfolio_state.realized_pnl}"
            )
        if replay_portfolio.fees != snapshot.portfolio_state.fees:
            return (
                f"fees mismatch: replayed={replay_portfolio.fees} "
                f"persisted={snapshot.portfolio_state.fees}"
            )

    persisted_positions = {p.instrument: p for p in snapshot.positions}
    replayed_positions = replay_portfolio.positions
    instruments = set(persisted_positions) | set(replayed_positions)
    for instrument in sorted(instruments):
        persisted_qty = (
            persisted_positions[instrument].quantity if instrument in persisted_positions else ZERO
        )
        replayed_qty = (
            replayed_positions[instrument].quantity if instrument in replayed_positions else ZERO
        )
        if persisted_qty != replayed_qty:
            return (
                f"position quantity mismatch for {instrument}: "
                f"replayed={replayed_qty} persisted={persisted_qty}"
            )
        if replayed_qty != ZERO:
            persisted_avg = (
                persisted_positions[instrument].avg_entry_price
                if instrument in persisted_positions
                else ZERO
            )
            replayed_avg = replayed_positions[instrument].avg_entry_price
            if persisted_avg != replayed_avg:
                return (
                    f"avg_entry_price mismatch for {instrument}: "
                    f"replayed={replayed_avg} persisted={persisted_avg}"
                )
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineRecoveryResult:
    """Outcome of recover_pipeline().

    ok=False means recovery could not safely proceed -- the store itself
    failed SQLiteStore.recover(), a component_state payload failed to
    parse, a from-scratch fills replay disagreed with persisted state, or a
    non-terminal non-reduce-only order was found with no matching risk
    reservation. In every ok=False case the returned pipeline's
    risk_engine is halted AND its execution_engine.mode is forced to
    EngineMode.HALTED, so it is guaranteed unable to accept any new
    exposure regardless of what a caller does with it next.
    """

    ok: bool
    halted: bool
    orphan_reservations_released: tuple[str, ...] = ()
    mismatch_detail: str | None = None


def _halt_recovery(
    *, risk_engine: RiskEngine, execution_engine: PaperExecutionEngine, reason: str
) -> None:
    risk_engine.halt(reason)
    execution_engine.halt_state_unreliable(reason)


def recover_pipeline(
    *,
    store: SQLiteStore,
    risk_engine: RiskEngine,
    execution_engine: PaperExecutionEngine,
    portfolio: Portfolio,
    exit_engine: ExitEngine,
    instrument_limits: Mapping[str, InstrumentRiskLimits],
    leverage_cap: Decimal,
    cost_schedule: VenueCostSchedule,
    now: datetime,
) -> tuple[PaperTradingPipeline, PipelineRecoveryResult]:
    """Reconstruct a PaperTradingPipeline from store's persisted state.

    Takes FRESH, not-yet-populated risk_engine/execution_engine/portfolio/
    exit_engine instances and populates them from store. Never trusts
    persisted positions/portfolio_state rows blindly: they are
    cross-checked (_cross_check_replay) against a from-scratch replay of
    every persisted fill (in true chronological/insertion order) applied to
    a brand-new Portfolio, and any disagreement is a HALT, not a silent
    "trust whichever" choice.

    Fail-closed at every step: a store.recover() failure, a component_state
    parse failure, a replay/persisted mismatch, or an orphaned-exposure
    order/reservation inconsistency each independently HALT both
    risk_engine and execution_engine and return
    PipelineRecoveryResult(ok=False, halted=True, ...) -- never a
    best-effort partial recovery. execution_engine.mode never
    auto-advances past RECONCILING to READY on its own; the one exception
    is this function's own final, explicitly-weak paper self-check
    reconcile() call (see below), which is NOT a claim of real venue
    reconciliation.
    """
    result = store.recover()
    if not result.ok:
        reason = f"PERSISTENCE_RECOVERY_FAILED: {result.error}"
        _halt_recovery(risk_engine=risk_engine, execution_engine=execution_engine, reason=reason)
        pipeline = PaperTradingPipeline(
            risk_engine=risk_engine,
            execution_engine=execution_engine,
            portfolio=portfolio,
            instrument_limits=instrument_limits,
            leverage_cap=leverage_cap,
            cost_schedule=cost_schedule,
            exit_engine=exit_engine,
            store=store,
        )
        return pipeline, PipelineRecoveryResult(ok=False, halted=True, mismatch_detail=result.error)

    snapshot = result.snapshot
    if snapshot is None:  # pragma: no cover - RecoveryResult invariant: ok=True implies snapshot
        raise AssertionError("store.recover() returned ok=True with snapshot=None")

    starting_balance = (
        snapshot.portfolio_state.starting_balance
        if snapshot.portfolio_state is not None
        else Decimal(portfolio.export_state()["starting_balance"])
    )
    replay_portfolio = Portfolio(starting_balance=starting_balance)
    for fill_record in snapshot.fills:
        replay_portfolio.apply_fill(
            Fill(
                fill_id=fill_record.fill_id,
                instrument=fill_record.instrument,
                side=OrderSide(fill_record.side),
                quantity=fill_record.quantity,
                price=fill_record.price,
                fee=fill_record.fee,
                timestamp=fill_record.timestamp,
            )
        )

    mismatch = _cross_check_replay(replay_portfolio, snapshot)
    if mismatch is not None:
        reason = f"PERSISTENCE_RECOVERY_MISMATCH: {mismatch}"
        _halt_recovery(risk_engine=risk_engine, execution_engine=execution_engine, reason=reason)
        pipeline = PaperTradingPipeline(
            risk_engine=risk_engine,
            execution_engine=execution_engine,
            portfolio=portfolio,
            instrument_limits=instrument_limits,
            leverage_cap=leverage_cap,
            cost_schedule=cost_schedule,
            exit_engine=exit_engine,
            store=store,
        )
        return pipeline, PipelineRecoveryResult(ok=False, halted=True, mismatch_detail=mismatch)

    # Replay agreed with the persisted rows: adopt the replayed (now
    # verified-consistent) state as the caller portfolio's real state,
    # then restore the last-observed mark prices from the persisted
    # positions rows (mark price is not derivable from fills alone, and
    # is non-critical to solvency -- the next tick re-marks it anyway).
    portfolio.import_state(replay_portfolio.export_state())
    for position_record in snapshot.positions:
        if position_record.mark_price is not None:
            portfolio.mark(position_record.instrument, position_record.mark_price)

    try:
        execution_component = store.get_component_state("execution")
        pipeline_component = store.get_component_state("pipeline")
        exits_component = store.get_component_state("exits")
    except Exception as exc:
        # Fail-closed: a corrupt component_state payload is exactly as
        # unsafe here as a corrupt typed row would have been inside
        # store.recover() itself.
        reason = f"PERSISTENCE_RECOVERY_FAILED: component_state failed to parse: {exc}"
        _halt_recovery(risk_engine=risk_engine, execution_engine=execution_engine, reason=reason)
        pipeline = PaperTradingPipeline(
            risk_engine=risk_engine,
            execution_engine=execution_engine,
            portfolio=portfolio,
            instrument_limits=instrument_limits,
            leverage_cap=leverage_cap,
            cost_schedule=cost_schedule,
            exit_engine=exit_engine,
            store=store,
        )
        return pipeline, PipelineRecoveryResult(ok=False, halted=True, mismatch_detail=reason)

    if execution_component is not None:
        # Already forces RECONCILING (or stays HALTED if persisted HALTED) --
        # see PaperExecutionEngine.import_checkpoint's own docstring. Never
        # weakened here.
        execution_engine.import_checkpoint(execution_component)
    # else: never persisted (crash before the very first _persist() call)
    # -- execution_engine stays at its constructor default (RECONCILING,
    # no orders), which is exactly correct for that case.

    risk_state = {
        "halted": snapshot.halt_state.halted if snapshot.halt_state is not None else False,
        "halt_reason": snapshot.halt_state.reason if snapshot.halt_state is not None else None,
        "reservations": {
            r.decision_id: {
                "side": r.side,
                "abs_notional": str(r.abs_notional),
                "signed_notional": str(r.signed_notional),
            }
            for r in snapshot.reservations
        },
        "reduce_only_reservations": {
            r.decision_id: {"instrument": r.instrument, "quantity": str(r.quantity)}
            for r in snapshot.reduce_only_reservations
        },
    }
    risk_engine.import_state(risk_state)

    pipeline = PaperTradingPipeline(
        risk_engine=risk_engine,
        execution_engine=execution_engine,
        portfolio=portfolio,
        instrument_limits=instrument_limits,
        leverage_cap=leverage_cap,
        cost_schedule=cost_schedule,
        exit_engine=exit_engine,
        store=store,
    )

    if exits_component is not None:
        for instrument, payload in exits_component.items():
            pipeline._exit_positions[instrument] = _exit_position_from_payload(payload)

    if pipeline_component is not None:
        pipeline._peak_equity = Decimal(pipeline_component["peak_equity"])
        pipeline._initial_equity = Decimal(pipeline_component["initial_equity"])
        pipeline._consecutive_losses = pipeline_component["consecutive_losses"]
        pipeline._trade_outcomes = [
            TradeOutcome(
                gross_pnl=Decimal(entry["gross_pnl"]),
                notional=Decimal(entry["notional"]),
                fees=Decimal(entry["fees"]),
            )
            for entry in pipeline_component["trade_outcomes"]
        ]
        pipeline._stage_counts = dict(pipeline_component["stage_counts"])
        pipeline._reason_counts = dict(pipeline_component["reason_counts"])
    # else: never persisted -- __post_init__ already derived sane
    # from-current-portfolio defaults (_initial_equity/_peak_equity), and
    # _consecutive_losses/_trade_outcomes/_stage_counts/_reason_counts
    # correctly stay at their empty dataclass defaults.

    # -- reservation/order consistency check --------------------------------
    orders_by_decision: dict[str, list[Order]] = {}
    for order in execution_engine.orders.values():
        orders_by_decision.setdefault(order.decision_id, []).append(order)

    reserved_decision_ids = set(risk_engine.export_state()["reservations"])
    orphans: list[str] = []
    for decision_id in sorted(reserved_decision_ids):
        associated = [o for o in orders_by_decision.get(decision_id, []) if not o.reduce_only]
        if not associated or all(o.is_terminal() for o in associated):
            # A reservation whose non-reduce-only order(s) are all terminal
            # (or there never was one) can never be filled against again --
            # an orphan, safe to release, but never silently: recorded in
            # the returned RecoveryResult as an explicit audit entry.
            risk_engine.release(decision_id)
            orphans.append(decision_id)

    missing_reservation: list[str] = []
    for decision_id, orders_for_decision in sorted(orders_by_decision.items()):
        open_non_reduce_only = [
            o for o in orders_for_decision if not o.reduce_only and not o.is_terminal()
        ]
        if open_non_reduce_only and decision_id not in reserved_decision_ids:
            missing_reservation.append(decision_id)

    if missing_reservation:
        reason = (
            "PERSISTENCE_RECOVERY_ORPHAN_EXPOSURE: non-terminal, non-reduce-only order(s) for "
            f"decision(s) {sorted(missing_reservation)} have no matching risk reservation after "
            "recovery -- exposure may exist that risk never approved, or the approval was lost"
        )
        _halt_recovery(risk_engine=risk_engine, execution_engine=execution_engine, reason=reason)
        return pipeline, PipelineRecoveryResult(
            ok=False,
            halted=True,
            orphan_reservations_released=tuple(orphans),
            mismatch_detail=reason,
        )

    # -- weak paper self-check reconciliation --------------------------------
    # Paper trading has no independent venue to reconcile against. Using the
    # now-verified persisted/replayed positions as a self-check "venue"
    # snapshot satisfies reconcile()'s existing mechanical gate (local ==
    # "venue" by construction) so execution_engine.mode can advance out of
    # RECONCILING -- this is explicitly NOT real venue reconciliation, only
    # documented parity with PaperExecutionEngine.reconcile()'s expected
    # input shape, matching the original design analysis's recommendation
    # for paper. A persisted HALTED execution mode is left untouched (the
    # `is EngineMode.RECONCILING` guard below only ever fires for
    # RECONCILING, never for HALTED) -- never silently auto-recovered by a
    # restart alone.
    if execution_engine.mode is EngineMode.RECONCILING:
        venue_state = {
            "orders": {
                client_id: True
                for client_id, order in execution_engine.orders.items()
                if not order.is_terminal()
            },
            "positions": {
                instrument: str(view.quantity) for instrument, view in portfolio.positions.items()
            },
        }
        execution_engine.reconcile(
            venue_state, now, source=ReconciliationSource.PAPER_SELF_CHECK
        )

    return pipeline, PipelineRecoveryResult(
        ok=True, halted=False, orphan_reservations_released=tuple(orphans)
    )

