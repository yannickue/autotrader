"""Shared construction helpers and full-path Sprint 1 e2e scenario runners.

Each ``scenario_*`` function below drives the real, already-merged modules
(no mocks of risk/execution/portfolio) through one deterministic path and
returns a JSON-serializable evidence dict. `tests/integration/test_e2e_paper_path.py`
calls these as pytest tests; `scripts/sprint1_e2e_evidence.py` calls the same
functions to produce the evidence report, so the two can never drift apart.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from data.binance_usdm import InstrumentRules
from data.models import DataQuality, MarketSnapshot
from execution.events import TradeEvent
from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from execution.orders import OrderStatus, RejectCode
from execution.paper import ExecutionConfig, PaperExecutionEngine
from pipeline.paper import PaperTradingPipeline, PipelineOutcome, PipelineStage
from portfolio.ledger import Portfolio
from risk.engine import RiskEngine
from risk.models import (
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
from signals.models import Direction, Signal
from strategies.momentum import MomentumConfig, MomentumStrategy
from universe.selector import MarketCandidate, UniverseSelectionConfig, select_universe

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
INSTRUMENT = "BTCUSDT-PERP"


# -- construction helpers -----------------------------------------------


def make_snapshot(**overrides: Any) -> MarketSnapshot:
    values: dict[str, Any] = dict(
        instrument=INSTRUMENT,
        timestamp=NOW,
        bid=Decimal("99"),
        ask=Decimal("101"),
        last=Decimal("100"),
        volume=Decimal("1000"),
        volatility=Decimal("0.02"),
        liquidity=Decimal("100000"),
        source="synthetic",
        quality=DataQuality.LIVE,
    )
    values.update(overrides)
    return MarketSnapshot(**values)


def make_history(
    prices: Sequence[str],
    *,
    instrument: str = INSTRUMENT,
    start: datetime | None = None,
) -> tuple[MarketSnapshot, ...]:
    start = start if start is not None else NOW - timedelta(minutes=len(prices))
    return tuple(
        make_snapshot(
            instrument=instrument,
            timestamp=start + timedelta(minutes=i),
            bid=Decimal(p) - Decimal("0.1"),
            ask=Decimal(p) + Decimal("0.1"),
            last=Decimal(p),
        )
        for i, p in enumerate(prices)
    )


def make_policy(**overrides: Any) -> RiskPolicy:
    values: dict[str, Any] = dict(
        policy_id="risk-v1",
        risk_fraction=Decimal("0.01"),
        max_leverage=Decimal("10"),
        max_gross_notional=Decimal("1000000"),
        max_net_notional=Decimal("1000000"),
        max_daily_loss=Decimal("10000"),
        max_drawdown=Decimal("20000"),
        max_consecutive_losses=5,
        liquidity_fraction=Decimal("0.5"),
        max_data_age=timedelta(seconds=30),
        max_signal_age=timedelta(seconds=30),
    )
    values.update(overrides)
    return RiskPolicy(**values)


def make_limits(**overrides: Any) -> InstrumentRiskLimits:
    values: dict[str, Any] = dict(
        instrument=INSTRUMENT,
        max_leverage=Decimal("10"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("10"),
        max_notional=Decimal("1000000"),
        max_spread_bps=Decimal("250"),
        maintenance_margin_rate=Decimal("0.005"),
    )
    values.update(overrides)
    return InstrumentRiskLimits(**values)


def make_rules(instrument: str = INSTRUMENT, venue_symbol: str | None = None) -> InstrumentRules:
    return InstrumentRules(
        instrument=instrument,
        venue_symbol=venue_symbol or instrument,
        base_asset="BTC",
        quote_asset="USDT",
        margin_asset="USDT",
        status="TRADING",
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("10"),
        market_quantity_step=Decimal("0.001"),
    )


def build_universe(instrument: str = INSTRUMENT) -> frozenset[str]:
    """Exercises the real universe/selector module on synthetic candidates."""
    real = MarketCandidate(
        rules=make_rules(instrument),
        quote_volume=Decimal("5000000"),
        spread_bps=Decimal("5"),
        realized_volatility=Decimal("0.3"),
        movement=Decimal("0.05"),
        liquidity=Decimal("1000000"),
    )
    padding = [
        MarketCandidate(
            rules=make_rules(f"PAD{i}USDT-PERP", venue_symbol=f"PAD{i}USDT"),
            quote_volume=Decimal("1000000"),
            spread_bps=Decimal("10"),
            realized_volatility=Decimal("0.2"),
            movement=Decimal("0.01"),
            liquidity=Decimal("500000"),
        )
        for i in range(19)
    ]
    selected = select_universe([real, *padding], UniverseSelectionConfig(target_size=20))
    return frozenset(candidate.rules.instrument for candidate in selected)


def make_runtime(**overrides: Any) -> RuntimeRiskState:
    values: dict[str, Any] = dict(
        state_version="runtime-1", mode=RuntimeMode.READY, risk_ready=True, kill_switch=False
    )
    values.update(overrides)
    return RuntimeRiskState(**values)


@dataclass
class Harness:
    policy: RiskPolicy
    limits: InstrumentRiskLimits
    risk_engine: RiskEngine
    exec_config: ExecutionConfig
    execution_engine: PaperExecutionEngine
    portfolio: Portfolio
    pipeline: PaperTradingPipeline


def build_harness(
    *,
    starting_balance: Decimal = Decimal("100000"),
    policy: RiskPolicy | None = None,
    limits: InstrumentRiskLimits | None = None,
    leverage_cap: Decimal = Decimal("10"),
    exec_config: ExecutionConfig | None = None,
) -> Harness:
    policy = policy or make_policy()
    limits = limits or make_limits()
    risk_engine = RiskEngine(policy)
    exec_config = exec_config or ExecutionConfig(
        max_request_age=timedelta(seconds=30),
        max_decision_age=timedelta(seconds=30),
        max_quote_age=timedelta(seconds=30),
        max_order_age=timedelta(minutes=30),
        max_spread_bps=Decimal("250"),
        max_slippage_bps=Decimal("100"),
        slippage_bps=Decimal("2"),
    )
    portfolio = Portfolio(starting_balance=starting_balance)
    execution_engine = PaperExecutionEngine(exec_config, portfolio)
    execution_engine.reconcile({"orders": {}, "positions": {}}, NOW)
    pipeline = PaperTradingPipeline(
        risk_engine=risk_engine,
        execution_engine=execution_engine,
        portfolio=portfolio,
        instrument_limits={limits.instrument: limits},
        leverage_cap=leverage_cap,
    )
    return Harness(
        policy=policy,
        limits=limits,
        risk_engine=risk_engine,
        exec_config=exec_config,
        execution_engine=execution_engine,
        portfolio=portfolio,
        pipeline=pipeline,
    )


class FixedStrategy:
    """Deterministic test-double strategy returning a preset Signal (or None)."""

    def __init__(self, signal: Signal | None) -> None:
        self._signal = signal

    def evaluate(self, snapshots: Sequence[MarketSnapshot]) -> Signal | None:
        return self._signal


def make_signal(
    *,
    direction: Direction,
    invalidation_level: Decimal,
    instrument: str = INSTRUMENT,
    timestamp: datetime = NOW,
    signal_id: str | None = None,
    confidence: Decimal = Decimal("0.5"),
) -> Signal:
    return Signal(
        signal_id=signal_id or f"sig:{instrument}:{direction.value}:{timestamp.isoformat()}",
        instrument=instrument,
        direction=direction,
        timestamp=timestamp,
        strategy_id="fixed-v1",
        entry_zone=(Decimal("99"), Decimal("101")),
        invalidation_level=invalidation_level,
        expected_move=Decimal("0.02"),
        expected_horizon=timedelta(minutes=15),
        confidence=confidence,
        metadata={"family": "fixed"},
    )


def _qty(harness: Harness, instrument: str = INSTRUMENT) -> Decimal:
    view = harness.portfolio.positions.get(instrument)
    return view.quantity if view is not None else Decimal("0")


def evidence(
    scenario: str, outcome: PipelineOutcome | None, harness: Harness, **extra: Any
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "scenario": scenario,
        "signal_id": outcome.signal_id if outcome else None,
        "risk_reason": outcome.risk_reason if outcome else None,
        "execution_status": (
            outcome.execution_status.value if outcome and outcome.execution_status else None
        ),
        "execution_reject_code": (
            outcome.execution_reject_code.value
            if outcome and outcome.execution_reject_code
            else None
        ),
        "position_qty": str(_qty(harness)),
        "gross_notional": str(harness.portfolio.gross_notional),
    }
    d.update(extra)
    return d


# -- required scenarios 1-7 ----------------------------------------------


def scenario_accepted_long() -> dict[str, Any]:
    h = build_harness()
    strategy = MomentumStrategy(
        MomentumConfig(
            lookback=3,
            return_threshold=Decimal("0.01"),
            invalidation_fraction=Decimal("0.05"),
        )
    )
    history = make_history(["100", "101", "103"])
    universe = build_universe()
    outcome = h.pipeline.process(
        history=history,
        strategy=strategy,
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=history[-1].timestamp,
    )
    assert outcome.stage is PipelineStage.EXECUTED
    assert outcome.execution_status is OrderStatus.FILLED
    qty = _qty(h)
    assert qty > 0
    return evidence("accepted_long", outcome, h, qty_before="0")


def scenario_accepted_short() -> dict[str, Any]:
    h = build_harness()
    strategy = MomentumStrategy(
        MomentumConfig(
            lookback=3,
            return_threshold=Decimal("0.01"),
            invalidation_fraction=Decimal("0.05"),
        )
    )
    history = make_history(["100", "99", "97"])
    universe = build_universe()
    outcome = h.pipeline.process(
        history=history,
        strategy=strategy,
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=history[-1].timestamp,
    )
    assert outcome.stage is PipelineStage.EXECUTED
    assert outcome.execution_status is OrderStatus.FILLED
    qty = _qty(h)
    assert qty < 0
    return evidence("accepted_short", outcome, h, qty_before="0")


def scenario_risk_reject_wide_spread() -> dict[str, Any]:
    h = build_harness()
    # ~66% spread >> the 250bps instrument limit
    wide_snapshot = make_snapshot(bid=Decimal("50"), ask=Decimal("150"))
    signal = make_signal(direction=Direction.LONG, invalidation_level=Decimal("95"))
    strategy = FixedStrategy(signal)
    outcome = h.pipeline.process(
        history=(wide_snapshot,),
        strategy=strategy,
        universe=frozenset({INSTRUMENT}),
        runtime=make_runtime(),
        account_known=True,
        now=NOW,
    )
    assert outcome.stage is PipelineStage.RISK_REJECTED
    assert outcome.risk_reason == RiskReason.SPREAD_TOO_WIDE.value
    assert outcome.execution_status is None
    assert _qty(h) == 0
    return evidence("risk_reject_wide_spread", outcome, h)


def _before_after(
    h: Harness, label: str, action: Any, instrument: str = INSTRUMENT
) -> dict[str, Any]:
    """Run `action()` and capture position qty / gross notional either side of it."""
    qty_before = _qty(h, instrument)
    gross_before = h.portfolio.gross_notional
    result = action()
    qty_after = _qty(h, instrument)
    gross_after = h.portfolio.gross_notional
    return {
        "label": label,
        "result": result,
        "position_qty_before": str(qty_before),
        "position_qty_after": str(qty_after),
        "gross_notional_before": str(gross_before),
        "gross_notional_after": str(gross_after),
    }


def scenario_halt() -> dict[str, Any]:
    # 4a: runtime kill_switch halts at the risk layer.
    h_runtime = build_harness()
    signal = make_signal(direction=Direction.LONG, invalidation_level=Decimal("95"))

    def _run_runtime_kill_switch() -> dict[str, Any]:
        outcome = h_runtime.pipeline.process(
            history=(make_snapshot(),),
            strategy=FixedStrategy(signal),
            universe=frozenset({INSTRUMENT}),
            runtime=make_runtime(kill_switch=True),
            account_known=True,
            now=NOW,
        )
        assert outcome.stage is PipelineStage.RISK_REJECTED
        assert outcome.risk_reason == RiskReason.HALTED.value
        return {"stage": outcome.stage.value, "risk_reason": outcome.risk_reason}

    runtime_kill_switch = _before_after(h_runtime, "runtime_kill_switch", _run_runtime_kill_switch)
    assert runtime_kill_switch["position_qty_after"] == "0"

    # 4a': risk_engine.halt() has the same effect.
    h_engine_halt = build_harness()
    h_engine_halt.risk_engine.halt("manual halt for test")

    def _run_risk_engine_halt() -> dict[str, Any]:
        outcome = h_engine_halt.pipeline.process(
            history=(make_snapshot(),),
            strategy=FixedStrategy(signal),
            universe=frozenset({INSTRUMENT}),
            runtime=make_runtime(),
            account_known=True,
            now=NOW,
        )
        assert outcome.stage is PipelineStage.RISK_REJECTED
        assert outcome.risk_reason == RiskReason.HALTED.value
        return {"stage": outcome.stage.value, "risk_reason": outcome.risk_reason}

    risk_engine_halt = _before_after(h_engine_halt, "risk_engine_halt", _run_risk_engine_halt)
    assert risk_engine_halt["position_qty_after"] == "0"

    # 4b: execution engine HALTED (via a public reconcile mismatch). The
    # pipeline derives AccountRiskState.reconciled from the execution
    # engine's own mode, so a HALTED execution engine is caught one layer
    # earlier as ACCOUNT_UNRECONCILED -- the full pipeline path is blocked
    # before a request is ever built, which is the stronger fail-closed
    # guarantee.
    h_exec_halt = build_harness()
    h_exec_halt.execution_engine.reconcile(
        {"orders": {}, "positions": {INSTRUMENT: "999"}}, NOW
    )

    def _run_execution_engine_halted() -> dict[str, Any]:
        outcome = h_exec_halt.pipeline.process(
            history=(make_snapshot(),),
            strategy=FixedStrategy(signal),
            universe=frozenset({INSTRUMENT}),
            runtime=make_runtime(),
            account_known=True,
            now=NOW,
        )
        assert outcome.stage is PipelineStage.RISK_REJECTED
        assert outcome.risk_reason == RiskReason.ACCOUNT_UNRECONCILED.value
        return {"stage": outcome.stage.value, "risk_reason": outcome.risk_reason}

    execution_engine_halted = _before_after(
        h_exec_halt, "execution_engine_halted", _run_execution_engine_halted
    )
    assert execution_engine_halted["position_qty_after"] == "0"

    # The execution engine also independently refuses to submit while
    # HALTED, regardless of what any caller's risk layer decided -- proven
    # directly against the engine with a manually approved decision.
    approved_while_halted = RiskDecision(
        decision_id="risk:halted-direct",
        signal_id="halted-direct",
        instrument=INSTRUMENT,
        timestamp=NOW,
        approved=True,
        reason="approved",
        reason_code=RiskReason.APPROVED,
        quantity=Decimal("1"),
        notional=Decimal("100"),
        leverage=Decimal("1"),
        max_leverage=Decimal("10"),
        risk_budget=Decimal("1000"),
        stop_price=Decimal("95"),
        metadata={"side": "buy"},
    )
    direct_request = ExecutionRequest(
        request_id="exec:halted-direct",
        risk_decision_id="risk:halted-direct",
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        limit_price=None,
        reduce_only=False,
        client_order_id="coid:halted-direct",
        time_in_force=TimeInForce.IOC,
        metadata={},
    )

    def _run_direct_submit_while_halted() -> dict[str, Any]:
        direct_result = h_exec_halt.pipeline.submit_direct(
            direct_request, approved_while_halted, make_snapshot(), NOW
        )
        assert not direct_result.accepted
        assert direct_result.reject_code is RejectCode.NOT_READY
        return {
            "accepted": direct_result.accepted,
            "execution_reject_code": direct_result.reject_code.value,
        }

    execution_engine_halted_direct_submit = _before_after(
        h_exec_halt, "execution_engine_halted_direct_submit", _run_direct_submit_while_halted
    )
    assert execution_engine_halted_direct_submit["position_qty_after"] == "0"

    return {
        "scenario": "halt",
        "runtime_kill_switch": runtime_kill_switch,
        "risk_engine_halt": risk_engine_halt,
        "execution_engine_halted": execution_engine_halted,
        "execution_engine_halted_direct_submit": execution_engine_halted_direct_submit,
    }


def scenario_stale_signal() -> dict[str, Any]:
    h = build_harness()
    stale_timestamp = NOW - timedelta(minutes=10)  # policy.max_signal_age is 30s
    signal = make_signal(
        direction=Direction.LONG, invalidation_level=Decimal("95"), timestamp=stale_timestamp
    )
    snapshot = make_snapshot(timestamp=NOW)
    outcome = h.pipeline.process(
        history=(snapshot,),
        strategy=FixedStrategy(signal),
        universe=frozenset({INSTRUMENT}),
        runtime=make_runtime(),
        account_known=True,
        now=NOW,
    )
    assert outcome.stage is PipelineStage.RISK_REJECTED
    assert outcome.risk_reason == RiskReason.SIGNAL_STALE.value
    assert _qty(h) == 0
    return evidence("stale_signal", outcome, h)


def scenario_duplicate_request() -> dict[str, Any]:
    h = build_harness()
    strategy = MomentumStrategy(
        MomentumConfig(
            lookback=3,
            return_threshold=Decimal("0.01"),
            invalidation_fraction=Decimal("0.05"),
        )
    )
    history = make_history(["100", "101", "103"])
    universe = build_universe()
    kwargs = dict(
        history=history,
        strategy=strategy,
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=history[-1].timestamp,
    )
    first = h.pipeline.process(**kwargs)
    qty_after_first = _qty(h)
    second = h.pipeline.process(**kwargs)  # identical history -> identical signal_id
    qty_after_second = _qty(h)

    assert first.signal_id == second.signal_id
    assert qty_after_first == qty_after_second
    assert first.stage is PipelineStage.EXECUTED

    # Same ExecutionRequest resubmitted directly to the execution engine must
    # also be a no-op (request_id already recorded -> cached SubmitResult).
    decision = h.risk_engine.evaluate(
        request=PositionSizingRequest(
            signal_id=first.signal_id,
            instrument=INSTRUMENT,
            timestamp=history[-1].timestamp,
            side=RiskSide.BUY,
            entry_price=history[-1].ask,
            stop_price=history[-1].last * Decimal("0.95"),
            confidence=Decimal("0.5"),
            available_liquidity_notional=Decimal("100000"),
            metadata={},
        ),
        snapshot=history[-1],
        account=AccountRiskState(
            state_version="v",
            known=True,
            reconciled=True,
            equity=h.portfolio.equity,
            peak_equity=h.portfolio.equity,
            realized_pnl_today=h.portfolio.realized_pnl,
            unrealized_pnl=Decimal("0"),
            gross_notional=h.portfolio.gross_notional,
            net_notional=h.portfolio.net_notional,
            instrument_notionals=h.portfolio.instrument_notionals,
            positions={i: v.quantity for i, v in h.portfolio.positions.items()},
            leverage_cap=Decimal("10"),
            consecutive_losses=0,
        ),
        runtime=make_runtime(),
        instrument=h.limits,
        now=history[-1].timestamp,
    )
    request = ExecutionRequest(
        request_id=f"exec:{first.signal_id}",
        risk_decision_id=decision.decision_id,
        instrument=INSTRUMENT,
        timestamp=history[-1].timestamp,
        side=OrderSide.BUY,
        quantity=decision.quantity if decision.approved else Decimal("1"),
        order_type=OrderType.MARKET,
        limit_price=None,
        reduce_only=False,
        client_order_id=f"coid:{first.signal_id}",
        time_in_force=TimeInForce.IOC,
        metadata={},
    )
    resubmit_result = h.execution_engine.submit(
        request, decision, history[-1], history[-1].timestamp
    )
    qty_after_resubmit = _qty(h)
    assert qty_after_resubmit == qty_after_first

    return {
        "scenario": "duplicate_request",
        "first": evidence("duplicate_request_first", first, h),
        "second": evidence("duplicate_request_second", second, h),
        "resubmit_status": resubmit_result.status.value,
        "qty_after_resubmit": str(qty_after_resubmit),
    }


def scenario_duplicate_fill() -> dict[str, Any]:
    h = build_harness()
    limit_price = Decimal("100")
    decision = RiskDecision(
        decision_id="risk:limit-1",
        signal_id="limit-1",
        instrument=INSTRUMENT,
        timestamp=NOW,
        approved=True,
        reason="approved",
        reason_code=RiskReason.APPROVED,
        quantity=Decimal("1"),
        notional=Decimal("100"),
        leverage=Decimal("1"),
        max_leverage=Decimal("10"),
        risk_budget=Decimal("1000"),
        stop_price=Decimal("95"),
        metadata={"side": "buy"},
    )
    request = ExecutionRequest(
        request_id="exec:limit-1",
        risk_decision_id=decision.decision_id,
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=limit_price,
        reduce_only=False,
        client_order_id="coid:limit-1",
        time_in_force=TimeInForce.GTC,
        metadata={},
    )
    submit_result = h.pipeline.submit_direct(request, decision, make_snapshot(), NOW)
    assert submit_result.status is OrderStatus.ACCEPTED  # resting, not yet crossed

    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=NOW,
        trade_id="trade-1",
        price=Decimal("99"),
        quantity=Decimal("1"),
    )
    h.execution_engine.on_trade(event, NOW)
    qty_after_first_trade = _qty(h)
    h.execution_engine.on_trade(event, NOW)  # identical TradeEvent delivered twice
    qty_after_duplicate_trade = _qty(h)
    assert qty_after_first_trade == qty_after_duplicate_trade == Decimal("1")

    h.execution_engine.report_fill("coid:limit-1", "report-1", Decimal("99"), Decimal("1"), NOW)
    qty_after_report_fill = _qty(h)
    h.execution_engine.report_fill("coid:limit-1", "report-1", Decimal("99"), Decimal("1"), NOW)
    qty_after_duplicate_report = _qty(h)
    assert qty_after_report_fill == qty_after_duplicate_report

    return {
        "scenario": "duplicate_fill",
        "qty_after_first_trade": str(qty_after_first_trade),
        "qty_after_duplicate_trade": str(qty_after_duplicate_trade),
        "qty_after_report_fill": str(qty_after_report_fill),
        "qty_after_duplicate_report": str(qty_after_duplicate_report),
        "gross_notional": str(h.portfolio.gross_notional),
    }


# -- additional required checks ------------------------------------------


def scenario_leverage_cap() -> dict[str, Any]:
    """The 20x ceiling must actually bind (not just sit far below the cap).

    Modest equity (500) + a very tight stop + generous risk_fraction/
    liquidity/notional limits means the *only* things that can constrain
    sizing are `leverage_cap` / `portfolio_leverage_capacity` -- so the
    first fill lands exactly at 20x, and a same-sized second signal must be
    rejected (or sized down) rather than pushing the account over 20x.
    """
    policy = make_policy(
        risk_fraction=Decimal("1"),
        max_leverage=Decimal("20"),
        liquidity_fraction=Decimal("1"),
        max_gross_notional=Decimal("100000000"),
        max_net_notional=Decimal("100000000"),
        max_daily_loss=Decimal("100000000"),
        max_drawdown=Decimal("100000000"),
    )
    limits = make_limits(
        max_leverage=Decimal("20"),
        max_notional=Decimal("100000000"),
        min_notional=Decimal("1"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
    )
    # Zero spread/slippage: entry price == mark price exactly, so the
    # post-fill *marked* leverage matches the risk engine's pre-trade
    # decision leverage bit-for-bit (no spread/slippage-driven unrealized
    # loss to erode equity between sizing and marking).
    zero_cost_exec_config = ExecutionConfig(
        max_request_age=timedelta(seconds=30),
        max_decision_age=timedelta(seconds=30),
        max_quote_age=timedelta(seconds=30),
        max_order_age=timedelta(minutes=30),
        max_spread_bps=Decimal("250"),
        max_slippage_bps=Decimal("100"),
        slippage_bps=Decimal("0"),
    )
    h = build_harness(
        starting_balance=Decimal("500"),
        policy=policy,
        limits=limits,
        leverage_cap=Decimal("20"),
        exec_config=zero_cost_exec_config,
    )
    universe = build_universe()

    snapshot = make_snapshot(
        bid=Decimal("100"),
        ask=Decimal("100"),
        last=Decimal("100"),
        liquidity=Decimal("1000000000"),
    )
    signal = make_signal(
        direction=Direction.LONG,
        invalidation_level=Decimal("99.99"),  # very tight stop: 0.01 distance from entry
        timestamp=snapshot.timestamp,
        signal_id="leverage-bind-1",
    )

    # Evaluate (and thereby cache, keyed by decision_id) the exact decision
    # the pipeline will make for this signal, so its metadata can be
    # inspected -- PipelineOutcome does not carry the RiskDecision object.
    # The flat-account preview below matches what the pipeline itself would
    # derive for an empty portfolio (equity == starting balance, no
    # existing exposure), so the cached decision is identical either way.
    preview_request = PositionSizingRequest(
        signal_id=signal.signal_id,
        instrument=INSTRUMENT,
        timestamp=signal.timestamp,
        side=RiskSide.BUY,
        entry_price=snapshot.ask,
        stop_price=signal.invalidation_level,
        confidence=signal.confidence,
        available_liquidity_notional=snapshot.liquidity,
        metadata={},
    )
    flat_account = AccountRiskState(
        state_version="preview",
        known=True,
        reconciled=True,
        equity=h.portfolio.equity,
        peak_equity=h.portfolio.equity,
        realized_pnl_today=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        gross_notional=Decimal("0"),
        net_notional=Decimal("0"),
        instrument_notionals={},
        positions={},
        leverage_cap=Decimal("20"),
        consecutive_losses=0,
    )
    decision1 = h.risk_engine.evaluate(
        request=preview_request,
        snapshot=snapshot,
        account=flat_account,
        runtime=make_runtime(),
        instrument=h.limits,
        now=snapshot.timestamp,
    )
    assert decision1.approved
    assert decision1.leverage <= Decimal("20")
    assert decision1.metadata["binding_constraint"] in {
        "leverage_cap",
        "portfolio_leverage_capacity",
    }

    outcome1 = h.pipeline.process(
        history=(snapshot,),
        strategy=FixedStrategy(signal),
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=snapshot.timestamp,
    )
    assert outcome1.stage is PipelineStage.EXECUTED

    gross_after_first = h.portfolio.gross_notional
    equity_after_first = h.portfolio.equity
    account_leverage_after_first = gross_after_first / equity_after_first
    assert account_leverage_after_first <= Decimal("20")

    # A second, identically-sized signal must not be able to push the
    # account over 20x: the risk engine's remaining_portfolio_leverage /
    # remaining_gross terms -- fed by the pipeline's own marking of the
    # now-open position -- either reject it outright or size it down.
    second_signal = make_signal(
        direction=Direction.LONG,
        invalidation_level=Decimal("99.99"),
        timestamp=snapshot.timestamp,
        signal_id="leverage-bind-2",
    )
    outcome2 = h.pipeline.process(
        history=(snapshot,),
        strategy=FixedStrategy(second_signal),
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=snapshot.timestamp,
    )
    gross_after_second = h.portfolio.gross_notional
    equity_after_second = h.portfolio.equity
    account_leverage_after_second = (
        gross_after_second / equity_after_second if equity_after_second > 0 else Decimal("Infinity")
    )
    assert account_leverage_after_second <= Decimal("20")

    raised = False
    try:
        RiskPolicy(
            policy_id="over-cap",
            risk_fraction=Decimal("0.01"),
            max_leverage=Decimal("25"),
            max_gross_notional=Decimal("1000"),
            max_net_notional=Decimal("1000"),
            max_daily_loss=Decimal("100"),
            max_drawdown=Decimal("100"),
            max_consecutive_losses=3,
            liquidity_fraction=Decimal("0.5"),
            max_data_age=timedelta(seconds=30),
            max_signal_age=timedelta(seconds=30),
        )
    except ValueError:
        raised = True
    assert raised

    return {
        "scenario": "leverage_cap",
        "decision1_leverage": str(decision1.leverage),
        "decision1_binding_constraint": decision1.metadata["binding_constraint"],
        "account_leverage_after_first": str(account_leverage_after_first),
        "second_signal_stage": outcome2.stage.value,
        "account_leverage_after_second": str(account_leverage_after_second),
        "raised_over_20x_policy": raised,
    }


def scenario_gross_exposure_marked_after_open() -> dict[str, Any]:
    """After an accepted LONG, the *next* process() call must derive a
    correctly-marked, nonzero gross_notional for that instrument -- proving
    the pipeline's mark-before-risk step (not a stale/zero valuation) is
    what risk sees.
    """
    h = build_harness()
    strategy = MomentumStrategy(
        MomentumConfig(
            lookback=3,
            return_threshold=Decimal("0.01"),
            invalidation_fraction=Decimal("0.05"),
        )
    )
    history = make_history(["100", "101", "103"])
    universe = build_universe()

    open_outcome = h.pipeline.process(
        history=history,
        strategy=strategy,
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=history[-1].timestamp,
    )
    assert open_outcome.stage is PipelineStage.EXECUTED
    qty = _qty(h)
    assert qty > 0

    # A later tick for the same instrument that produces no new signal.
    next_snapshot = make_snapshot(
        timestamp=history[-1].timestamp + timedelta(minutes=1),
        bid=Decimal("102.9"),
        ask=Decimal("103.1"),
        last=Decimal("103"),
    )
    next_outcome = h.pipeline.process(
        history=(next_snapshot,),
        strategy=FixedStrategy(None),
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=next_snapshot.timestamp,
    )
    assert next_outcome.stage is PipelineStage.NO_SIGNAL

    gross = h.portfolio.gross_notional
    assert gross > 0
    assert gross >= qty * next_snapshot.last

    return {
        "scenario": "gross_exposure_marked_after_open",
        "qty": str(qty),
        "gross_notional": str(gross),
        "latest_last": str(next_snapshot.last),
    }


def scenario_exposure_limit_blocks_second_long() -> dict[str, Any]:
    """A second LONG on an instrument that already has open exposure must
    never push total gross notional past policy.max_gross_notional -- this
    only holds if the existing position's exposure is correctly marked and
    counted (not silently zero) when the second decision is sized.
    """
    max_gross_notional = Decimal("1050")
    policy = make_policy(
        risk_fraction=Decimal("1"),
        liquidity_fraction=Decimal("1"),
        max_gross_notional=max_gross_notional,
        max_net_notional=Decimal("100000000"),
    )
    limits = make_limits(max_notional=Decimal("100000000"), max_leverage=Decimal("20"))
    h = build_harness(
        starting_balance=Decimal("1000000"),
        policy=policy,
        limits=limits,
        leverage_cap=Decimal("20"),
    )
    universe = build_universe()

    target_quantity = Decimal("10")
    snapshot = make_snapshot(
        bid=Decimal("99.9"),
        ask=Decimal("100.1"),
        last=Decimal("100"),
        liquidity=Decimal("100.1") * target_quantity,
    )
    first_signal = make_signal(
        direction=Direction.LONG,
        invalidation_level=Decimal("90"),
        timestamp=snapshot.timestamp,
        signal_id="exposure-open",
    )
    open_outcome = h.pipeline.process(
        history=(snapshot,),
        strategy=FixedStrategy(first_signal),
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=snapshot.timestamp,
    )
    assert open_outcome.stage is PipelineStage.EXECUTED
    qty1 = _qty(h)
    assert qty1 > 0
    gross_after_first = h.portfolio.gross_notional
    assert gross_after_first <= max_gross_notional

    second_signal = make_signal(
        direction=Direction.LONG,
        invalidation_level=Decimal("90"),
        timestamp=snapshot.timestamp,
        signal_id="exposure-second",
    )
    second_outcome = h.pipeline.process(
        history=(snapshot,),
        strategy=FixedStrategy(second_signal),
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=snapshot.timestamp,
    )
    gross_after_second = h.portfolio.gross_notional
    qty2 = _qty(h) - qty1

    # Risk sizes against the ask; the portfolio marks at `last` (<= ask here), so the
    # marked gross exposure must respect the cap exactly — no tolerance.
    assert gross_after_second <= max_gross_notional

    return {
        "scenario": "exposure_limit_blocks_second_long",
        "max_gross_notional": str(max_gross_notional),
        "qty1": str(qty1),
        "qty2_added": str(qty2),
        "gross_after_first": str(gross_after_first),
        "gross_after_second": str(gross_after_second),
        "second_signal_stage": second_outcome.stage.value,
        "second_signal_risk_reason": second_outcome.risk_reason,
    }


def scenario_nan_inf_rejected() -> dict[str, Any]:
    h = build_harness()
    nan = Decimal("NaN")
    inf = Decimal("Infinity")

    snapshot_rejected = False
    try:
        make_snapshot(bid=nan)
    except ValueError:
        snapshot_rejected = True
    assert snapshot_rejected

    account = AccountRiskState(
        state_version="v",
        known=True,
        reconciled=True,
        equity=nan,
        peak_equity=Decimal("1000"),
        realized_pnl_today=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        gross_notional=Decimal("0"),
        net_notional=Decimal("0"),
        instrument_notionals={},
        positions={},
        leverage_cap=Decimal("10"),
        consecutive_losses=0,
    )
    request_nan_entry = PositionSizingRequest(
        signal_id="nan-entry",
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=RiskSide.BUY,
        entry_price=inf,
        stop_price=Decimal("95"),
        confidence=Decimal("0.5"),
        available_liquidity_notional=Decimal("1000"),
        metadata={},
    )
    decision_nan = h.risk_engine.evaluate(
        request=request_nan_entry,
        snapshot=make_snapshot(),
        account=account,
        runtime=make_runtime(),
        instrument=h.limits,
        now=NOW,
    )
    assert not decision_nan.approved
    assert decision_nan.reason_code == RiskReason.INVALID_INPUT
    assert decision_nan.quantity == 0
    assert _qty(h) == 0

    return {
        "scenario": "nan_inf_rejected",
        "market_snapshot_rejects_nan": snapshot_rejected,
        "risk_decision_reason": str(decision_nan.reason_code),
        "position_qty": str(_qty(h)),
    }


def scenario_invalid_stop() -> dict[str, Any]:
    h = build_harness()
    snapshot = make_snapshot()

    stop_equals_entry = h.risk_engine.evaluate(
        request=PositionSizingRequest(
            signal_id="stop-eq-entry",
            instrument=INSTRUMENT,
            timestamp=NOW,
            side=RiskSide.BUY,
            entry_price=snapshot.ask,
            stop_price=snapshot.ask,  # stop == entry
            confidence=Decimal("0.5"),
            available_liquidity_notional=Decimal("1000"),
            metadata={},
        ),
        snapshot=snapshot,
        account=AccountRiskState(
            state_version="v",
            known=True,
            reconciled=True,
            equity=Decimal("1000"),
            peak_equity=Decimal("1000"),
            realized_pnl_today=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            gross_notional=Decimal("0"),
            net_notional=Decimal("0"),
            instrument_notionals={},
            positions={},
            leverage_cap=Decimal("10"),
            consecutive_losses=0,
        ),
        runtime=make_runtime(),
        instrument=h.limits,
        now=NOW,
    )
    assert stop_equals_entry.reason_code == RiskReason.INVALID_STOP

    wrong_side = h.risk_engine.evaluate(
        request=PositionSizingRequest(
            signal_id="stop-wrong-side",
            instrument=INSTRUMENT,
            timestamp=NOW,
            side=RiskSide.BUY,
            entry_price=snapshot.ask,
            stop_price=snapshot.ask + Decimal("10"),  # stop above entry on a BUY
            confidence=Decimal("0.5"),
            available_liquidity_notional=Decimal("1000"),
            metadata={},
        ),
        snapshot=snapshot,
        account=AccountRiskState(
            state_version="v",
            known=True,
            reconciled=True,
            equity=Decimal("1000"),
            peak_equity=Decimal("1000"),
            realized_pnl_today=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            gross_notional=Decimal("0"),
            net_notional=Decimal("0"),
            instrument_notionals={},
            positions={},
            leverage_cap=Decimal("10"),
            consecutive_losses=0,
        ),
        runtime=make_runtime(),
        instrument=h.limits,
        now=NOW,
    )
    assert wrong_side.reason_code == RiskReason.INVALID_STOP

    return {
        "scenario": "invalid_stop",
        "stop_equals_entry_reason": str(stop_equals_entry.reason_code),
        "wrong_side_reason": str(wrong_side.reason_code),
    }


def scenario_account_unknown() -> dict[str, Any]:
    h = build_harness()
    signal = make_signal(direction=Direction.LONG, invalidation_level=Decimal("95"))
    outcome = h.pipeline.process(
        history=(make_snapshot(),),
        strategy=FixedStrategy(signal),
        universe=frozenset({INSTRUMENT}),
        runtime=make_runtime(),
        account_known=False,
        now=NOW,
    )
    assert outcome.stage is PipelineStage.RISK_REJECTED
    assert outcome.risk_reason == RiskReason.ACCOUNT_UNKNOWN.value
    assert _qty(h) == 0
    return evidence("account_unknown", outcome, h)


def scenario_execution_cannot_bypass_risk() -> dict[str, Any]:
    h = build_harness()
    base_request = ExecutionRequest(
        request_id="exec:bypass-1",
        risk_decision_id="risk:bypass-1",
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        limit_price=None,
        reduce_only=False,
        client_order_id="coid:bypass-1",
        time_in_force=TimeInForce.IOC,
        metadata={},
    )
    quote = make_snapshot()

    none_decision_result = h.pipeline.submit_direct(base_request, None, quote, NOW)
    assert not none_decision_result.accepted
    assert none_decision_result.reject_code is RejectCode.RISK_NOT_APPROVED

    rejected_decision = RiskDecision(
        decision_id="risk:bypass-1",
        signal_id="bypass-1",
        instrument=INSTRUMENT,
        timestamp=NOW,
        approved=False,
        reason="rejected",
        reason_code=RiskReason.SPREAD_TOO_WIDE,
        quantity=Decimal("0"),
        notional=Decimal("0"),
        leverage=Decimal("0"),
        max_leverage=Decimal("10"),
        risk_budget=Decimal("0"),
        stop_price=None,
        metadata={"side": "buy"},
    )
    rejected_decision_result = h.pipeline.submit_direct(base_request, rejected_decision, quote, NOW)
    assert not rejected_decision_result.accepted
    assert rejected_decision_result.reject_code is RejectCode.RISK_NOT_APPROVED

    approved_decision = RiskDecision(
        decision_id="risk:bypass-2",
        signal_id="bypass-2",
        instrument=INSTRUMENT,
        timestamp=NOW,
        approved=True,
        reason="approved",
        reason_code=RiskReason.APPROVED,
        quantity=Decimal("1"),
        notional=Decimal("100"),
        leverage=Decimal("1"),
        max_leverage=Decimal("10"),
        risk_budget=Decimal("1000"),
        stop_price=Decimal("95"),
        metadata={"side": "buy"},
    )

    mismatched_id_request = ExecutionRequest(
        request_id="exec:bypass-mismatch-id",
        risk_decision_id="risk:does-not-match",
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        limit_price=None,
        reduce_only=False,
        client_order_id="coid:bypass-mismatch-id",
        time_in_force=TimeInForce.IOC,
        metadata={},
    )
    mismatch_id_result = h.pipeline.submit_direct(
        mismatched_id_request, approved_decision, quote, NOW
    )
    assert not mismatch_id_result.accepted
    assert mismatch_id_result.reject_code is RejectCode.RISK_MISMATCH

    mismatched_side_request = ExecutionRequest(
        request_id="exec:bypass-2",
        risk_decision_id="risk:bypass-2",
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=OrderSide.SELL,  # decision metadata says "buy"
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        limit_price=None,
        reduce_only=False,
        client_order_id="coid:bypass-side",
        time_in_force=TimeInForce.IOC,
        metadata={},
    )
    mismatch_side_result = h.pipeline.submit_direct(
        mismatched_side_request, approved_decision, quote, NOW
    )
    assert not mismatch_side_result.accepted
    assert mismatch_side_result.reject_code is RejectCode.RISK_MISMATCH

    over_size_request = ExecutionRequest(
        request_id="exec:bypass-3",
        risk_decision_id="risk:bypass-2",
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=OrderSide.BUY,
        quantity=Decimal("999"),  # far above the approved quantity of 1
        order_type=OrderType.MARKET,
        limit_price=None,
        reduce_only=False,
        client_order_id="coid:bypass-oversize",
        time_in_force=TimeInForce.IOC,
        metadata={},
    )
    over_size_result = h.pipeline.submit_direct(over_size_request, approved_decision, quote, NOW)
    assert not over_size_result.accepted
    assert over_size_result.reject_code is RejectCode.EXCEEDS_APPROVED_SIZE

    assert _qty(h) == 0

    return {
        "scenario": "execution_cannot_bypass_risk",
        "none_decision_reject": none_decision_result.reject_code.value,
        "rejected_decision_reject": rejected_decision_result.reject_code.value,
        "mismatch_id_reject": mismatch_id_result.reject_code.value,
        "mismatch_side_reject": mismatch_side_result.reject_code.value,
        "over_size_reject": over_size_result.reject_code.value,
        "position_qty": str(_qty(h)),
    }


def scenario_metrics_json_serializable() -> dict[str, Any]:
    import json

    # Risk sizing is normally re-derived independently for every decision, so
    # an arbitrary open and an arbitrary close will not generally net to
    # exactly flat. To get a clean, deterministic LONG-open-then-close
    # (needed to exercise the pipeline's TradeOutcome bookkeeping), the
    # policy/limits here are tuned so `liquidity` is the sole binding sizing
    # constraint for both legs, and each snapshot's `liquidity` field is set
    # to request exactly one unit -- everything else (risk budget, leverage,
    # notional caps) is left generous so it never binds instead.
    #
    # Sizing keys off the risk_reference_price (ask + slippage buffer for a
    # BUY, bid - slippage buffer for a SELL; see RiskPolicy
    # reference_price_slippage_bps), never the raw bid/ask, so the requested
    # liquidity notional must match that reference price exactly for the
    # binding quantity to land on precisely `target_quantity`.
    policy = make_policy(risk_fraction=Decimal("1"), liquidity_fraction=Decimal("1"))
    _slippage_fraction = policy.reference_price_slippage_bps / Decimal("10000")
    limits = make_limits(max_notional=Decimal("100000000"), max_leverage=Decimal("20"))
    h = build_harness(
        starting_balance=Decimal("1000000"),
        policy=policy,
        limits=limits,
        leverage_cap=Decimal("20"),
    )
    universe = build_universe()
    target_quantity = Decimal("1")

    open_snapshot = make_snapshot(bid=Decimal("99.9"), ask=Decimal("100.1"), last=Decimal("100"))
    open_signal = make_signal(
        direction=Direction.LONG,
        invalidation_level=Decimal("90"),
        timestamp=open_snapshot.timestamp,
        signal_id="open-long",
        instrument=open_snapshot.instrument,
    )
    open_snapshot = make_snapshot(
        bid=open_snapshot.bid,
        ask=open_snapshot.ask,
        last=open_snapshot.last,
        liquidity=open_snapshot.ask * (Decimal("1") + _slippage_fraction) * target_quantity,
    )
    open_outcome = h.pipeline.process(
        history=(open_snapshot,),
        strategy=FixedStrategy(open_signal),
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=open_snapshot.timestamp,
    )
    assert open_outcome.stage is PipelineStage.EXECUTED
    assert _qty(h) == target_quantity

    close_timestamp = NOW + timedelta(minutes=5)
    close_snapshot = make_snapshot(
        timestamp=close_timestamp,
        bid=Decimal("104.9"),
        ask=Decimal("105.1"),
        last=Decimal("105"),
        liquidity=Decimal("104.9") * (Decimal("1") - _slippage_fraction) * target_quantity,
    )
    close_signal = make_signal(
        direction=Direction.SHORT,
        invalidation_level=Decimal("120"),
        timestamp=close_timestamp,
        signal_id="close-long",
    )
    close_outcome = h.pipeline.process(
        history=(close_snapshot,),
        strategy=FixedStrategy(close_signal),
        universe=universe,
        runtime=make_runtime(),
        account_known=True,
        now=close_timestamp,
    )
    assert close_outcome.stage is PipelineStage.EXECUTED
    assert _qty(h) == 0  # SHORT of the same size flattens the LONG

    metrics = h.pipeline.metrics()
    trade_metrics = metrics["trade_metrics"]

    def _to_jsonable(value: Any) -> Any:
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError("metrics must not contain non-finite Decimals")
            return str(value)
        if isinstance(value, dict):
            return {k: _to_jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_to_jsonable(v) for v in value]
        return value

    jsonable = {
        "trade_count": trade_metrics.trade_count,
        "net_pnl": _to_jsonable(trade_metrics.net_pnl),
        "max_drawdown": _to_jsonable(trade_metrics.max_drawdown),
        "ruined": trade_metrics.ruined,
        "equity": _to_jsonable(metrics["equity"]),
        "gross_notional": _to_jsonable(metrics["gross_notional"]),
        "positions": _to_jsonable(metrics["positions"]),
        "stage_counts": metrics["stage_counts"],
    }
    serialized = json.dumps(jsonable, allow_nan=False, sort_keys=True)
    assert serialized  # round-trips without raising on NaN/inf
    assert trade_metrics.trade_count == 1
    assert math.isfinite(float(trade_metrics.net_pnl))

    return {
        "scenario": "metrics_json_serializable",
        "trade_count": trade_metrics.trade_count,
        "net_pnl": str(trade_metrics.net_pnl),
        "json_length": len(serialized),
    }


ALL_SCENARIOS: dict[str, Any] = {
    "accepted_long": scenario_accepted_long,
    "accepted_short": scenario_accepted_short,
    "risk_reject_wide_spread": scenario_risk_reject_wide_spread,
    "halt": scenario_halt,
    "stale_signal": scenario_stale_signal,
    "duplicate_request": scenario_duplicate_request,
    "duplicate_fill": scenario_duplicate_fill,
    "leverage_cap": scenario_leverage_cap,
    "gross_exposure_marked_after_open": scenario_gross_exposure_marked_after_open,
    "exposure_limit_blocks_second_long": scenario_exposure_limit_blocks_second_long,
    "nan_inf_rejected": scenario_nan_inf_rejected,
    "invalid_stop": scenario_invalid_stop,
    "account_unknown": scenario_account_unknown,
    "execution_cannot_bypass_risk": scenario_execution_cannot_bypass_risk,
    "metrics_json_serializable": scenario_metrics_json_serializable,
}
