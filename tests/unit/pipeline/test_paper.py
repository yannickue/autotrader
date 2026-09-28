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

from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from execution.orders import OrderStatus
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
