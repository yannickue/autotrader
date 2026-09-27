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

from data.models import MarketSnapshot
from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from execution.orders import TERMINAL_STATUSES, OrderStatus, RejectCode, SubmitResult
from execution.paper import PaperExecutionEngine
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
    """Running notional/fees for one instrument's currently-open position."""

    notional: Decimal
    fees: Decimal
    realized_at_open: Decimal


@dataclass(kw_only=True)
class PaperTradingPipeline:
    """Glue object; holds no market data or credentials, only injected collaborators."""

    risk_engine: RiskEngine
    execution_engine: PaperExecutionEngine
    portfolio: Portfolio
    instrument_limits: Mapping[str, InstrumentRiskLimits]
    leverage_cap: Decimal

    _peak_equity: Decimal = field(init=False, default=ZERO)
    _initial_equity: Decimal = field(init=False, default=ZERO)
    _consecutive_losses: int = field(init=False, default=0)
    _trade_outcomes: list[TradeOutcome] = field(init=False, default_factory=list)
    _trade_accum: dict[str, _TradeAccumulator] = field(init=False, default_factory=dict)
    _stage_counts: dict[str, int] = field(init=False, default_factory=dict)
    _reason_counts: dict[str, int] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        equity = self.portfolio.equity
        self._initial_equity = equity
        self._peak_equity = equity if equity > ZERO else ZERO

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

        result = self.execution_engine.submit(request, decision, latest, now)

        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            after_qty = self._position_qty(instrument)
            realized_after = self.portfolio.realized_pnl
            fees_after = self.portfolio.fees
            self._record_fill(
                instrument=instrument,
                fill_price=entry_price,
                before_qty=before_qty,
                after_qty=after_qty,
                realized_before=realized_before,
                realized_after=realized_after,
                fees_before=fees_before,
                fees_after=fees_after,
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
            realized_pnl_today=self.portfolio.realized_pnl,
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

    def _update_peak_equity(self) -> None:
        equity = self.portfolio.equity
        if equity > self._peak_equity:
            self._peak_equity = equity

    def _record_fill(
        self,
        *,
        instrument: str,
        fill_price: Decimal,
        before_qty: Decimal,
        after_qty: Decimal,
        realized_before: Decimal,
        realized_after: Decimal,
        fees_before: Decimal,
        fees_after: Decimal,
    ) -> None:
        filled_quantity = abs(after_qty - before_qty)
        if filled_quantity == ZERO:
            return  # duplicate/no-op fill: nothing new to attribute

        accumulator = self._trade_accum.get(instrument)
        if before_qty == ZERO and accumulator is None:
            accumulator = _TradeAccumulator(
                notional=ZERO, fees=ZERO, realized_at_open=realized_before
            )
            self._trade_accum[instrument] = accumulator
        elif accumulator is None:
            # Position was already open before this pipeline instance observed
            # it (e.g. imported checkpoint): open a fresh accumulator anchored
            # to the current realized PnL so we never invent a phantom trade.
            accumulator = _TradeAccumulator(
                notional=ZERO, fees=ZERO, realized_at_open=realized_before
            )
            self._trade_accum[instrument] = accumulator

        accumulator.notional += filled_quantity * fill_price
        accumulator.fees += fees_after - fees_before

        if after_qty == ZERO:
            gross_pnl = realized_after - accumulator.realized_at_open
            outcome = TradeOutcome(
                gross_pnl=gross_pnl,
                notional=accumulator.notional,
                fees=accumulator.fees,
            )
            self._trade_outcomes.append(outcome)
            del self._trade_accum[instrument]
            if outcome.net_pnl < ZERO:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

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

