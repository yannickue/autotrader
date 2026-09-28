"""Slice 2 pipeline tests: G3 (fees in realized_pnl_today), G4 (actual fill
price/fee recorded, not the quote-side reference), and reporting-only cost
attribution never leaking into TradeOutcome/portfolio PnL.

Reuses the real construction helpers from `tests/integration/e2e_scenarios.py`
(the same ones the full e2e suite drives) rather than re-implementing a
second harness, so this exercises the real risk/execution/portfolio wiring.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from execution.events import TradeEvent
from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from execution.orders import ChildRole, OrderStatus
from pipeline.paper import PipelineStage
from portfolio.models import Fill
from risk.models import RiskDecision, RiskReason
from signals.models import Direction
from tests.integration import e2e_scenarios as scenarios

INSTRUMENT = scenarios.INSTRUMENT
NOW = scenarios.NOW


# -- G3: realized_pnl_today must be net of fees --------------------------


def test_build_account_state_realized_pnl_today_is_net_of_fees() -> None:
    h = scenarios.build_harness()

    h.portfolio.apply_fill(
        Fill(
            fill_id="f1",
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("3"),
            timestamp=NOW,
        )
    )
    h.portfolio.apply_fill(
        Fill(
            fill_id="f2",
            instrument=INSTRUMENT,
            side=OrderSide.SELL,
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("3"),
            timestamp=NOW,
        )
    )

    # Zero price PnL (bought and sold at the same price), but 6 of real fees.
    assert h.portfolio.realized_pnl == Decimal("0")
    assert h.portfolio.fees == Decimal("6")

    account = h.pipeline._build_account_state(instrument=INSTRUMENT, account_known=True)
    assert account.realized_pnl_today == Decimal("-6")


def test_daily_loss_limit_trips_from_fees_alone_with_zero_price_pnl() -> None:
    """Core regression for G3: before the fix, `realized_pnl_today` was
    gross of fees, so a trade with ~zero price PnL but real fee cost would
    never trip the daily-loss guard no matter how large fees got."""
    policy = scenarios.make_policy(max_daily_loss=Decimal("5"))
    h = scenarios.build_harness(policy=policy)

    h.portfolio.apply_fill(
        Fill(
            fill_id="f1",
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("3"),
            timestamp=NOW,
        )
    )
    h.portfolio.apply_fill(
        Fill(
            fill_id="f2",
            instrument=INSTRUMENT,
            side=OrderSide.SELL,
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("3"),
            timestamp=NOW,
        )
    )
    assert h.portfolio.realized_pnl == Decimal("0")  # no price PnL at all

    account = h.pipeline._build_account_state(instrument=INSTRUMENT, account_known=True)
    sizing_request = scenarios.PositionSizingRequest(
        signal_id="sig-1",
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=scenarios.RiskSide.BUY,
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        confidence=Decimal("0.5"),
        available_liquidity_notional=Decimal("100000"),
        metadata={},
    )
    decision = h.risk_engine.evaluate(
        request=sizing_request,
        snapshot=scenarios.make_snapshot(bid=Decimal("99.9"), ask=Decimal("100.1")),
        account=account,
        runtime=scenarios.make_runtime(),
        instrument=h.limits,
        now=NOW,
    )

    assert decision.approved is False
    assert decision.reason_code == RiskReason.DAILY_LOSS_LIMIT


# -- G4 + cost attribution: full round trip through the real pipeline ----


def _open_long(h: scenarios.Harness, *, now) -> Decimal:
    strategy = scenarios.FixedStrategy(
        scenarios.make_signal(
            direction=Direction.LONG, invalidation_level=Decimal("95"), timestamp=now
        )
    )
    history = scenarios.make_history(["100", "100", "100"], start=now - timedelta(minutes=2))
    outcome = h.pipeline.process(
        history=history,
        strategy=strategy,
        universe=frozenset({INSTRUMENT}),
        runtime=scenarios.make_runtime(),
        account_known=True,
        now=now,
    )
    assert outcome.stage.value == "executed"
    assert outcome.execution_status is OrderStatus.FILLED
    view = h.portfolio.positions[INSTRUMENT]
    return view.quantity


def _close_with_reduce_only(h: scenarios.Harness, *, quantity: Decimal, now) -> None:
    """Manually close the open long via a reduce-only fill.

    Sprint 1 has no exit engine yet (docs/OPEN_QUESTIONS.md #24): nothing
    calls `evaluate_reduce_only()`/submits a reduce-only order on its own,
    so this drives the same real `execution_engine.submit()` +
    `pipeline._record_fill()` path a future exit engine will use, instead
    of inventing a second bookkeeping mechanism.
    """
    quote = scenarios.make_snapshot(
        instrument=INSTRUMENT, timestamp=now, bid=Decimal("100"), ask=Decimal("100.2")
    )
    decision = RiskDecision(
        decision_id="decision:close",
        signal_id="signal:close",
        instrument=INSTRUMENT,
        timestamp=now,
        approved=True,
        reason="manual reduce-only close",
        quantity=quantity,
        notional=quantity * Decimal("100"),
        leverage=Decimal("1"),
        max_leverage=Decimal("10"),
        risk_budget=Decimal("1000"),
        stop_price=None,
        metadata={"side": "sell", "reduce_only": True},
    )
    request = ExecutionRequest(
        request_id="exec:close",
        risk_decision_id=decision.decision_id,
        instrument=INSTRUMENT,
        timestamp=now,
        side=OrderSide.SELL,
        quantity=quantity,
        order_type=OrderType.MARKET,
        limit_price=None,
        reduce_only=True,
        client_order_id="coid:close",
        time_in_force=TimeInForce.IOC,
        metadata={"reference_price": str(quote.bid)},
    )

    before_qty = h.portfolio.positions[INSTRUMENT].quantity
    realized_before = h.portfolio.realized_pnl
    fees_before = h.portfolio.fees
    h.pipeline._pending_fills.clear()

    result = h.execution_engine.submit(request, decision, quote, now)
    assert result.status == OrderStatus.FILLED

    after_qty = h.portfolio.positions[INSTRUMENT].quantity
    assert after_qty == Decimal("0")
    realized_after = h.portfolio.realized_pnl
    fees_after = h.portfolio.fees

    own_fills = [f for f in h.pipeline._pending_fills if f.client_order_id == "coid:close"]
    assert len(own_fills) == 1
    actual_fill_price = own_fills[0].price

    h.pipeline._record_fill(
        instrument=INSTRUMENT,
        fill_price=actual_fill_price,
        expected_price=quote.bid,
        quote_bid=quote.bid,
        quote_ask=quote.ask,
        before_qty=before_qty,
        after_qty=after_qty,
        realized_before=realized_before,
        realized_after=realized_after,
        fees_before=fees_before,
        fees_after=fees_after,
        order_side=OrderSide.SELL,
        now=now,
    )


def test_trade_outcome_net_pnl_equals_delta_realized_minus_delta_fees() -> None:
    cost_schedule = scenarios.make_cost_schedule(
        maker_fee_rate=Decimal("0.0002"), taker_fee_rate=Decimal("0.0005")
    )
    h = scenarios.build_harness(cost_schedule=cost_schedule)
    now1 = NOW
    now2 = NOW + timedelta(minutes=5)

    quantity = _open_long(h, now=now1)
    assert h.portfolio.fees > Decimal("0")  # entry fee was really debited

    _close_with_reduce_only(h, quantity=quantity, now=now2)

    outcome = h.pipeline._trade_outcomes[-1]
    # Recompute against the true starting point (before the entry fill) for
    # an exact, unambiguous invariant check.
    total_realized_delta = h.portfolio.realized_pnl - Decimal("0")
    total_fees_delta = h.portfolio.fees - Decimal("0")

    assert outcome.net_pnl == total_realized_delta - total_fees_delta
    assert outcome.fees == total_fees_delta
    assert outcome.gross_pnl == total_realized_delta


def test_closing_trade_produces_one_cost_attribution_without_affecting_pnl() -> None:
    zero_schedule = scenarios.make_cost_schedule()
    h_zero = scenarios.build_harness(cost_schedule=zero_schedule)
    now1, now2 = NOW, NOW + timedelta(minutes=5)
    quantity = _open_long(h_zero, now=now1)
    _close_with_reduce_only(h_zero, quantity=quantity, now=now2)

    metrics = h_zero.pipeline.metrics()
    assert len(metrics["cost_attributions"]) == 1
    breakdown = metrics["cost_attributions"][0]
    assert breakdown.trade_id.startswith(INSTRUMENT)

    outcome = h_zero.pipeline._trade_outcomes[-1]
    # funding_or_swap from the CostBreakdown must never be mapped into
    # TradeOutcome -- TradeOutcome has no funding-carrying field sourced
    # from CostBreakdown at all (fees comes from real portfolio fee deltas).
    assert outcome.fees == h_zero.portfolio.fees


def test_attribution_only_schedule_fields_never_change_net_pnl_or_equity() -> None:
    """Changing spread/slippage bps used ONLY for cost attribution must not
    move `TradeOutcome.net_pnl` or portfolio equity by a single unit --
    those numbers come exclusively from the real fill price already
    embedded in realized PnL, plus the real per-fill fee."""
    now1, now2 = NOW, NOW + timedelta(minutes=5)

    plain_schedule = scenarios.make_cost_schedule(
        maker_fee_rate=Decimal("0.0002"), taker_fee_rate=Decimal("0.0005")
    )
    h_plain = scenarios.build_harness(cost_schedule=plain_schedule)
    quantity = _open_long(h_plain, now=now1)
    _close_with_reduce_only(h_plain, quantity=quantity, now=now2)

    loaded_schedule = scenarios.make_cost_schedule(
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        estimated_spread_bps=Decimal("50"),
        entry_slippage_bps=Decimal("25"),
        exit_slippage_bps=Decimal("25"),
    )
    h_loaded = scenarios.build_harness(cost_schedule=loaded_schedule)
    quantity2 = _open_long(h_loaded, now=now1)
    _close_with_reduce_only(h_loaded, quantity=quantity2, now=now2)

    assert h_plain.portfolio.equity == h_loaded.portfolio.equity
    plain_outcome = h_plain.pipeline._trade_outcomes[-1]
    loaded_outcome = h_loaded.pipeline._trade_outcomes[-1]
    assert plain_outcome.net_pnl == loaded_outcome.net_pnl
    assert plain_outcome.fees == loaded_outcome.fees
    assert plain_outcome.spread == Decimal("0")
    assert loaded_outcome.spread == Decimal("0")
    assert plain_outcome.slippage == Decimal("0")
    assert loaded_outcome.slippage == Decimal("0")

    # The reporting-only breakdown is computed from the REAL bid/ask and
    # expected-price data the pipeline always supplies (see
    # `_attribute_costs`), which takes priority over the schedule's
    # `estimated_spread_bps`/`*_slippage_bps` fallbacks (costs/engine.py
    # `_spread_cost`/`_entry_slippage_cost`/`_exit_slippage_cost` only fall
    # back to the schedule when bid/ask or an expected price is missing).
    # So the schedule's fallback fields have zero effect here either --
    # both breakdowns' spread/slippage costs are identical, driven purely
    # by the real quotes, and neither ever reaches TradeOutcome/equity.
    loaded_breakdown = h_loaded.pipeline.metrics()["cost_attributions"][0]
    plain_breakdown = h_plain.pipeline.metrics()["cost_attributions"][0]
    assert loaded_breakdown.spread_cost > Decimal("0")
    assert loaded_breakdown.spread_cost == plain_breakdown.spread_cost
    assert loaded_breakdown.slippage_cost == plain_breakdown.slippage_cost


# -- Slice 3a (G1 fix): on_trade()/on_time()/report_fill() reachability --
#
# Before this slice, `PaperExecutionEngine.on_trade()`, `on_time()`, and
# `report_fill()` had zero callers anywhere in `src/` -- only `submit()`
# was ever called (from `PaperTradingPipeline.process()`). That meant the
# protective STOP/TAKE_PROFIT child orders `submit()` creates alongside
# every entry could never actually trigger. These tests exercise the new
# `process_trade_event()`/`process_time_tick()`/`process_reported_fill()`
# passthroughs and prove they reconcile fills through the exact same
# `_record_fill` accounting `process()` already uses for its own entry
# fill (correct fees/net_pnl, correct `_consecutive_losses`/peak-equity
# side effects, no phantom `TradeOutcome` on a zero-fill call).

INSTRUMENT2 = "ETHUSDT-PERP"


def _open_long_on(h: scenarios.Harness, *, instrument: str, now) -> Decimal:
    strategy = scenarios.FixedStrategy(
        scenarios.make_signal(
            direction=Direction.LONG,
            invalidation_level=Decimal("95"),
            instrument=instrument,
            timestamp=now,
        )
    )
    history = scenarios.make_history(
        ["100", "100", "100"], instrument=instrument, start=now - timedelta(minutes=2)
    )
    outcome = h.pipeline.process(
        history=history,
        strategy=strategy,
        universe=frozenset({instrument}),
        runtime=scenarios.make_runtime(),
        account_known=True,
        now=now,
    )
    assert outcome.stage.value == "executed"
    assert outcome.execution_status is OrderStatus.FILLED
    view = h.portfolio.positions[instrument]
    return view.quantity


def _build_two_instrument_harness(*, cost_schedule=None) -> scenarios.Harness:
    """A harness with `instrument_limits` for two instruments -- `build_harness()`
    only ever configures one, and the multi-instrument bucketing test below
    needs open positions on two distinct instruments simultaneously."""
    policy = scenarios.make_policy()
    limits1 = scenarios.make_limits()
    limits2 = scenarios.make_limits(instrument=INSTRUMENT2)
    risk_engine = scenarios.RiskEngine(policy)
    exec_config = scenarios.ExecutionConfig(
        max_request_age=timedelta(seconds=30),
        max_decision_age=timedelta(seconds=30),
        max_quote_age=timedelta(seconds=30),
        max_order_age=timedelta(minutes=30),
        max_spread_bps=Decimal("250"),
        max_slippage_bps=Decimal("100"),
        slippage_bps=Decimal("2"),
    )
    cost_schedule = cost_schedule or scenarios.make_cost_schedule()
    portfolio = scenarios.Portfolio(starting_balance=Decimal("100000"))
    execution_engine = scenarios.PaperExecutionEngine(exec_config, portfolio, cost_schedule)
    execution_engine.reconcile({"orders": {}, "positions": {}}, NOW)
    pipeline = scenarios.PaperTradingPipeline(
        risk_engine=risk_engine,
        execution_engine=execution_engine,
        portfolio=portfolio,
        instrument_limits={INSTRUMENT: limits1, INSTRUMENT2: limits2},
        leverage_cap=Decimal("10"),
        cost_schedule=cost_schedule,
    )
    return scenarios.Harness(
        policy=policy,
        limits=limits1,
        risk_engine=risk_engine,
        exec_config=exec_config,
        execution_engine=execution_engine,
        portfolio=portfolio,
        pipeline=pipeline,
        cost_schedule=cost_schedule,
    )


def _submit_reduce_only_close(
    h: scenarios.Harness, *, instrument: str, quantity: Decimal, coid: str, decision_id: str, now
) -> None:
    quote = scenarios.make_snapshot(
        instrument=instrument, timestamp=now, bid=Decimal("100"), ask=Decimal("100.2")
    )
    decision = RiskDecision(
        decision_id=decision_id,
        signal_id=f"signal:{decision_id}",
        instrument=instrument,
        timestamp=now,
        approved=True,
        reason="manual reduce-only close",
        quantity=quantity,
        notional=quantity * Decimal("100"),
        leverage=Decimal("1"),
        max_leverage=Decimal("10"),
        risk_budget=Decimal("1000"),
        stop_price=None,
        metadata={"side": "sell", "reduce_only": True},
    )
    request = ExecutionRequest(
        request_id=f"exec:{decision_id}",
        risk_decision_id=decision.decision_id,
        instrument=instrument,
        timestamp=now,
        side=OrderSide.SELL,
        quantity=quantity,
        order_type=OrderType.MARKET,
        limit_price=None,
        reduce_only=True,
        client_order_id=coid,
        time_in_force=TimeInForce.IOC,
        metadata={"reference_price": str(quote.bid)},
    )
    result = h.execution_engine.submit(request, decision, quote, now)
    assert result.status == OrderStatus.FILLED


def test_process_trade_event_triggers_protective_stop_and_records_trade_outcome() -> None:
    """A resting protective STOP created alongside an entry (Slice 1) must
    actually be reachable by a real trade print now that `on_trade()` has a
    caller -- and the resulting close must go through the identical
    `_record_fill`/`TradeOutcome` treatment as a manually-submitted close."""
    cost_schedule = scenarios.make_cost_schedule(
        maker_fee_rate=Decimal("0.0002"), taker_fee_rate=Decimal("0.0005")
    )
    h = scenarios.build_harness(cost_schedule=cost_schedule)
    now1 = NOW
    quantity = _open_long(h, now=now1)
    assert quantity > 0

    entry_order = next(o for o in h.execution_engine._orders.values() if o.role == ChildRole.ENTRY)
    stop_id = f"{entry_order.client_order_id}:{ChildRole.STOP}"
    stop_order = h.execution_engine._orders[stop_id]
    assert stop_order.status is OrderStatus.ACCEPTED
    assert stop_order.trigger_price is not None

    now2 = now1 + timedelta(minutes=5)
    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now2,
        trade_id="stop-trigger-1",
        price=stop_order.trigger_price - Decimal("1"),  # trades through the stop
        quantity=quantity,
    )
    outcome = h.pipeline.process_trade_event(event=event, now=now2)

    assert outcome.stage is PipelineStage.EXECUTION_EVENT_FILLED
    assert outcome.position_after == Decimal("0")
    assert stop_order.status is OrderStatus.FILLED
    assert h.portfolio.positions[INSTRUMENT].quantity == Decimal("0")

    assert len(h.pipeline._trade_outcomes) == 1
    outcome_trade = h.pipeline._trade_outcomes[-1]
    total_realized_delta = h.portfolio.realized_pnl - Decimal("0")
    total_fees_delta = h.portfolio.fees - Decimal("0")
    assert outcome_trade.net_pnl == total_realized_delta - total_fees_delta
    assert outcome_trade.fees == total_fees_delta
    assert outcome_trade.gross_pnl == total_realized_delta
    # the accumulator must be cleared after a full close, same as a manual close
    assert INSTRUMENT not in h.pipeline._trade_accum


def test_process_trade_event_zero_fill_is_a_safe_no_op() -> None:
    """A trade event that crosses nothing must never fabricate a phantom fill
    or `TradeOutcome`."""
    h = scenarios.build_harness()
    decision = RiskDecision(
        decision_id="risk:no-cross",
        signal_id="no-cross",
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
        request_id="exec:no-cross",
        risk_decision_id=decision.decision_id,
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
        reduce_only=False,
        client_order_id="coid:no-cross",
        time_in_force=TimeInForce.GTC,
        metadata={},
    )
    submit_result = h.pipeline.submit_direct(request, decision, scenarios.make_snapshot(), NOW)
    assert submit_result.status is OrderStatus.ACCEPTED

    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=NOW,
        trade_id="no-cross-trade-1",
        price=Decimal("200"),  # far above the BUY limit's 100: never crosses
        quantity=Decimal("1"),
    )
    outcome = h.pipeline.process_trade_event(event=event, now=NOW)

    assert outcome.stage is PipelineStage.EXECUTION_EVENT_NO_FILL
    assert outcome.position_after == Decimal("0")
    assert h.pipeline._trade_outcomes == []
    assert h.pipeline._pending_fills == []
    assert h.portfolio.positions.get(INSTRUMENT) is None or (
        h.portfolio.positions[INSTRUMENT].quantity == Decimal("0")
    )


def test_process_time_tick_expires_stale_resting_order_as_safe_no_op() -> None:
    """`PaperExecutionEngine.on_time(self, now)` takes no `instrument` (it
    walks every open order across every instrument) and only ever expires
    stale entry LIMIT orders -- it never produces a fill by itself. This
    proves `process_time_tick()` correctly reconciles that (real, expected)
    zero-fill case rather than treating it as an error, while still driving
    the real order-expiry transition through the real engine."""
    h = scenarios.build_harness()
    decision = RiskDecision(
        decision_id="risk:tick-1",
        signal_id="tick-1",
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
        request_id="exec:tick-1",
        risk_decision_id=decision.decision_id,
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("50"),  # far below market: never crosses
        reduce_only=False,
        client_order_id="coid:tick-1",
        time_in_force=TimeInForce.GTC,
        metadata={},
    )
    submit_result = h.pipeline.submit_direct(request, decision, scenarios.make_snapshot(), NOW)
    assert submit_result.status is OrderStatus.ACCEPTED

    later = NOW + timedelta(minutes=31)  # exceeds exec_config.max_order_age (30m)
    outcome = h.pipeline.process_time_tick(now=later)

    assert outcome.stage is PipelineStage.EXECUTION_EVENT_NO_FILL
    assert outcome.position_after is None
    assert h.execution_engine._orders["coid:tick-1"].status is OrderStatus.EXPIRED
    assert h.pipeline._trade_outcomes == []
    assert h.pipeline._pending_fills == []


def test_process_reported_fill_reconciles_externally_reported_fill() -> None:
    """Mirrors `tests/chaos/test_execution_chaos.py`'s direct
    `engine.report_fill(client_order_id, trade_id, price, quantity, now)`
    usage, routed through the new `process_reported_fill()` passthrough."""
    h = scenarios.build_harness()
    decision = RiskDecision(
        decision_id="risk:report-1",
        signal_id="report-1",
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
        request_id="exec:report-1",
        risk_decision_id=decision.decision_id,
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100"),
        reduce_only=False,
        client_order_id="coid:report-1",
        time_in_force=TimeInForce.GTC,
        metadata={},
    )
    submit_result = h.pipeline.submit_direct(request, decision, scenarios.make_snapshot(), NOW)
    assert submit_result.status is OrderStatus.ACCEPTED

    outcome = h.pipeline.process_reported_fill(
        client_order_id="coid:report-1",
        trade_id="report-trade-1",
        price=Decimal("99"),
        quantity=Decimal("1"),
        now=NOW,
    )

    assert outcome.stage is PipelineStage.EXECUTION_EVENT_FILLED
    assert h.execution_engine._orders["coid:report-1"].status is OrderStatus.FILLED
    assert h.portfolio.positions[INSTRUMENT].quantity == Decimal("1")
    assert INSTRUMENT in h.pipeline._trade_accum  # position opened, not yet closed
    assert h.pipeline._trade_outcomes == []


def test_reconcile_background_fills_buckets_multi_instrument_fills_independently() -> None:
    """A single `on_trade()` call can only ever match orders for its own
    `event.instrument` (`PaperExecutionEngine._process_trade` filters every
    candidate order with `order.instrument != event.instrument: continue`),
    so a literal single on_trade()/on_time()/report_fill() call that fills
    orders across two different instruments cannot actually be constructed
    through the real engine's public API today -- `on_time()` is the one
    entry point that already walks every instrument's orders in a single
    call, and a future engine change could plausibly let one background
    call span instruments. This test instead assembles the exact scenario
    the bucketing logic must handle correctly (two different instruments'
    `FillEvent`s sitting together in `_pending_fills` when
    `_reconcile_background_fills` runs) with two real, back-to-back
    `execution_engine.submit()` closes -- the pipeline's own reconciliation
    deliberately deferred until after both -- proving the per-instrument
    before/after quantity and realized/fee-delta bucketing in
    `_reconcile_background_fills` is correct and does not cross-contaminate
    between instruments, using the exact same code path
    `process_trade_event`/`process_time_tick`/`process_reported_fill` call.
    """
    cost_schedule = scenarios.make_cost_schedule(
        maker_fee_rate=Decimal("0.0002"), taker_fee_rate=Decimal("0.0005")
    )
    h = _build_two_instrument_harness(cost_schedule=cost_schedule)
    now1 = NOW
    fees_before = h.portfolio.fees  # zero: fresh harness, no fills yet at all
    assert fees_before == Decimal("0")

    qty1 = _open_long_on(h, instrument=INSTRUMENT, now=now1)
    qty2 = _open_long_on(h, instrument=INSTRUMENT2, now=now1)
    assert qty1 > 0
    assert qty2 > 0

    now2 = now1 + timedelta(minutes=5)
    before_positions = h.pipeline._position_snapshot()
    h.pipeline._pending_fills.clear()
    h.pipeline._fill_deltas.clear()

    _submit_reduce_only_close(
        h,
        instrument=INSTRUMENT,
        quantity=qty1,
        coid="coid:close-1",
        decision_id="risk:close-1",
        now=now2,
    )
    _submit_reduce_only_close(
        h,
        instrument=INSTRUMENT2,
        quantity=qty2,
        coid="coid:close-2",
        decision_id="risk:close-2",
        now=now2,
    )

    # Both fills sit together, un-reconciled, exactly like a hypothetical
    # single background call spanning both instruments would leave them.
    pending_before_reconcile = list(h.pipeline._pending_fills)
    deltas_before_reconcile = dict(h.pipeline._fill_deltas)
    assert len(pending_before_reconcile) == 2
    assert {f.instrument for f in pending_before_reconcile} == {INSTRUMENT, INSTRUMENT2}
    assert pending_before_reconcile[0].instrument == INSTRUMENT  # closed first, below

    filled = h.pipeline._reconcile_background_fills(before_positions=before_positions, now=now2)
    assert filled is True

    assert h.portfolio.positions[INSTRUMENT].quantity == Decimal("0")
    assert h.portfolio.positions[INSTRUMENT2].quantity == Decimal("0")
    assert INSTRUMENT not in h.pipeline._trade_accum
    assert INSTRUMENT2 not in h.pipeline._trade_accum
    assert len(h.pipeline._trade_outcomes) == 2

    outcome_first, outcome_second = h.pipeline._trade_outcomes[-2], h.pipeline._trade_outcomes[-1]

    # Position-quantity bucketing (the actual G1 concern: correctly
    # detecting/attributing zero, one, or multiple fills per instrument)
    # and fee bucketing are both exactly isolated per instrument,
    # regardless of interleaving: fees accumulate as a per-fill delta on
    # every `_record_fill` call (never a shared anchor), unlike gross_pnl
    # below.
    total_fees_delta = h.portfolio.fees - fees_before
    assert outcome_first.fees + outcome_second.fees == total_fees_delta
    assert outcome_first.fees > Decimal("0")
    assert outcome_second.fees > Decimal("0")

    # The instrument processed FIRST in this batch (INSTRUMENT, submitted
    # first) is unaffected by the other instrument's close and must be
    # exactly correct: its own realized-pnl delta, no more.
    first_fill_delta = deltas_before_reconcile[pending_before_reconcile[0].fill_id][0]
    second_fill_delta = deltas_before_reconcile[pending_before_reconcile[1].fill_id][0]
    assert outcome_first.gross_pnl == first_fill_delta

    # Fixed (was a KNOWN GAP when this test was first written): gross_pnl
    # is now `_TradeAccumulator.realized_pnl_delta`, a per-fill accumulated
    # sum (mirroring how `fees` was always tracked), never a
    # position-open-vs-close anchor diff against the global
    # `portfolio.realized_pnl` counter -- so the second instrument's close
    # is NOT polluted by the first instrument's close reconciled earlier in
    # this same batch, regardless of how many other instruments' fills
    # interleave between this instrument's own open and close.
    assert outcome_second.gross_pnl == second_fill_delta
