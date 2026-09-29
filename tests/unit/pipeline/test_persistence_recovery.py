"""Integration Slice 4b: crash-boundary persistence/recovery tests.

Every scenario below runs a `PaperTradingPipeline` WITH a real `SQLiteStore`
backed by a `tmp_path` SQLite file, deliberately stops short of (or simulates
a crash at) a specific point, then either reads the raw store or calls
`recover_pipeline()` against a FRESH set of engine instances built from the
SAME db file, and asserts recovery produces correct, non-duplicated,
non-lost state.

Since this is paper trading (no independent external venue), "crash before
persist" means that operation's effects, from the system's own point of
view, either fully happened (if persisted) or did not happen at all (if
not) -- there is no partial state, by construction of the single-commit-
point design (`PaperTradingPipeline._persist()` builds exactly one
`store.write_snapshot(...)` call per invocation, itself one
`store.transaction()`). These tests prove that atomicity holds at each named
boundary, not just assert a vague "no duplication".
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from execution.orders import OrderStatus
from execution.paper import EngineMode, PaperExecutionEngine
from exits.engine import ExitEngine
from exits.models import ExitPolicy, StopStage, TakeProfitStage
from persistence.store import SQLiteStore
from pipeline.paper import recover_pipeline
from portfolio.ledger import Portfolio
from risk.engine import RiskEngine
from risk.models import PositionSizingRequest, RiskSide
from signals.models import Direction
from tests.integration import e2e_scenarios as scenarios

INSTRUMENT = scenarios.INSTRUMENT
NOW = scenarios.NOW


# -- shared helpers --------------------------------------------------------


def _db_path(tmp_path) -> str:
    return str(tmp_path / "state.db")


def _fresh_recovery_inputs(h: scenarios.Harness) -> tuple[
    RiskEngine, PaperExecutionEngine, Portfolio, ExitEngine
]:
    """Build brand-new, not-yet-populated engine instances matching `h`'s
    configuration (policy/exec_config/cost_schedule/exit_policy), simulating
    a fresh process restart reading the same on-disk store."""
    risk_engine = RiskEngine(h.policy)
    # A real restart's caller constructs this fresh Portfolio with whatever
    # the deploy's actual configured starting balance is; `recover_pipeline`
    # overrides it from persisted `portfolio_state` whenever one exists
    # (proven separately below), and falls back to this value only when
    # nothing has ever been persisted yet (there is nothing else it could
    # use in that case).
    portfolio = Portfolio(starting_balance=Decimal("100000"))
    execution_engine = PaperExecutionEngine(h.exec_config, portfolio, h.cost_schedule)
    exit_engine = ExitEngine(policy=h.exit_policy, risk_gate=risk_engine)
    return risk_engine, execution_engine, portfolio, exit_engine


def _recover(h: scenarios.Harness, db_path: str, *, now=NOW):
    store = SQLiteStore(db_path)
    risk_engine, execution_engine, portfolio, exit_engine = _fresh_recovery_inputs(h)
    pipeline, result = recover_pipeline(
        store=store,
        risk_engine=risk_engine,
        execution_engine=execution_engine,
        portfolio=portfolio,
        exit_engine=exit_engine,
        instrument_limits={h.limits.instrument: h.limits},
        leverage_cap=Decimal("10"),
        cost_schedule=h.cost_schedule,
        now=now,
    )
    return pipeline, result, store


def _open_long(h: scenarios.Harness, *, now=NOW, signal_id: str | None = None) -> Decimal:
    strategy = scenarios.FixedStrategy(
        scenarios.make_signal(
            direction=Direction.LONG,
            invalidation_level=Decimal("95"),
            timestamp=now,
            signal_id=signal_id,
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
    return h.portfolio.positions[INSTRUMENT].quantity


# -- Scenario 1: before order persistence -----------------------------------


def test_recovery_shows_nothing_when_crash_precedes_any_persist(tmp_path) -> None:
    """An order/decision/reservation that was never persisted at all (the
    process died before `_persist()` ever ran) must leave the store exactly
    as it was before -- recovering shows no trace of it: no position, no
    order, no reservation, no exposure."""
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness()
    # Deliberately no `h.pipeline.store = store`: every mutation below stays
    # purely in-memory, exactly like a process that crashes before its first
    # successful `_persist()` call.
    quantity = _open_long(h)
    assert quantity > 0

    # A fresh, never-written store (nothing was ever persisted to db_path).
    store = SQLiteStore(db_path)
    store.close()
    h2 = scenarios.build_harness()
    pipeline, result, store2 = _recover(h2, db_path)

    assert result.ok is True
    assert result.halted is False
    assert pipeline.portfolio.positions == {}
    assert pipeline.execution_engine.orders == {}
    assert pipeline.risk_engine.export_state()["reservations"] == {}
    assert pipeline._exit_positions == {}
    store2.close()


# -- Scenario 2: order created but not filled --------------------------------


def test_recovery_restores_resting_unfilled_order_and_reservation(tmp_path) -> None:
    """A resting (unfilled) LIMIT GTC order that WAS persisted must come back
    exactly as it was: still resting, no phantom fill, no phantom position,
    and its risk reservation intact."""
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness()
    store = SQLiteStore(db_path)
    h.pipeline.store = store

    sizing_request = PositionSizingRequest(
        signal_id="sig-resting",
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=RiskSide.BUY,
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        confidence=Decimal("0.5"),
        available_liquidity_notional=Decimal("100000"),
        metadata={},
    )
    quote = scenarios.make_snapshot(bid=Decimal("99"), ask=Decimal("101"))
    decision = h.risk_engine.evaluate(
        request=sizing_request,
        snapshot=quote,
        account=h.pipeline._build_account_state(
        instrument=INSTRUMENT, account_known=True, now=NOW
    ),
        runtime=scenarios.make_runtime(),
        instrument=h.limits,
        now=NOW,
    )
    assert decision.approved

    request = ExecutionRequest(
        request_id="exec:sig-resting",
        risk_decision_id=decision.decision_id,
        instrument=INSTRUMENT,
        timestamp=NOW,
        side=OrderSide.BUY,
        quantity=decision.quantity,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("50"),  # far below the touch: never crosses, stays resting
        reduce_only=False,
        client_order_id="coid:sig-resting",
        time_in_force=TimeInForce.GTC,
        metadata={"reference_price": "100"},
    )
    result = h.pipeline.submit_direct(request, decision, quote, NOW)
    assert result.status is OrderStatus.ACCEPTED
    position = h.portfolio.positions.get(INSTRUMENT)
    assert position is None or position.quantity == 0

    # `submit_direct` bypasses `process()`/`_finish()`, so persist explicitly
    # here -- this stands in for "the process's normal flow reached and
    # completed its own `_persist()` call for this state".
    h.pipeline._persist(now=NOW)
    store.close()

    pipeline, recovery, store2 = _recover(h, db_path)
    assert recovery.ok is True
    assert recovery.halted is False

    restored_order = pipeline.execution_engine.orders["coid:sig-resting"]
    assert restored_order.status is OrderStatus.ACCEPTED
    assert restored_order.filled_quantity == Decimal("0")
    assert restored_order.quantity == decision.quantity

    reservations = pipeline.risk_engine.export_state()["reservations"]
    assert decision.decision_id in reservations

    assert (
        INSTRUMENT not in pipeline.portfolio.positions
        or pipeline.portfolio.positions[INSTRUMENT].quantity == Decimal("0")
    )
    store2.close()


# -- Scenario 3: fill received before portfolio checkpoint -------------------


def test_uncommitted_fill_is_invisible_then_applies_exactly_once_on_real_retry(tmp_path) -> None:
    """A fill applied in-memory but never durably committed (simulated
    crash: `store` detached before the entry fill's own `_persist()` would
    have run) must not exist after recovery -- and the SAME signal, redriven
    for real afterward (as a driver naturally would, having received no
    persisted confirmation the first attempt survived), must apply exactly
    once: no duplicate fill, no duplicate PnL, no phantom exposure."""
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness()
    # No store attached: the entry fill below happens purely in-memory and
    # is never durably committed -- exactly the "crash before this call's
    # `_persist()`" scenario.
    quantity = _open_long(h, signal_id="sig-retry")
    assert quantity > 0

    # Nothing was ever persisted: recovering from the (still-empty) store
    # must show the pre-fill (flat) state.
    store = SQLiteStore(db_path)
    store.close()
    h_recovered = scenarios.build_harness()
    pipeline, recovery, store2 = _recover(h_recovered, db_path)
    assert recovery.ok is True
    assert pipeline.portfolio.positions == {}
    assert pipeline._exit_positions == {}

    # Now attach the SAME real db to the recovered (currently empty/flat)
    # pipeline and genuinely redrive the identical signal -- this is what a
    # real driver does after getting no confirmation the first attempt
    # persisted.
    pipeline.store = store2
    strategy = scenarios.FixedStrategy(
        scenarios.make_signal(
            direction=Direction.LONG,
            invalidation_level=Decimal("95"),
            timestamp=NOW,
            signal_id="sig-retry",
        )
    )
    history = scenarios.make_history(["100", "100", "100"], start=NOW - timedelta(minutes=2))
    outcome = pipeline.process(
        history=history,
        strategy=strategy,
        universe=frozenset({INSTRUMENT}),
        runtime=scenarios.make_runtime(),
        account_known=True,
        now=NOW,
    )
    assert outcome.stage.value == "executed"
    assert outcome.execution_status is OrderStatus.FILLED
    assert pipeline.portfolio.positions[INSTRUMENT].quantity == quantity
    assert pipeline.portfolio.realized_pnl == Decimal("0")  # no closes yet, no PnL invented

    # Redriving AGAIN (a second retry, e.g. a duplicate driver call) must be
    # a pure no-op via `PaperExecutionEngine`'s own request_id dedup --
    # proving no double-fill even under a repeated retry.
    outcome2 = pipeline.process(
        history=history,
        strategy=strategy,
        universe=frozenset({INSTRUMENT}),
        runtime=scenarios.make_runtime(),
        account_known=True,
        now=NOW,
    )
    assert pipeline.portfolio.positions[INSTRUMENT].quantity == quantity  # unchanged: still once

    # And a final recovery from the db must match the live (post-retry)
    # in-memory state exactly -- one fill, one position, correctly durable.
    store2.close()
    h_final = scenarios.build_harness()
    pipeline_final, recovery_final, store3 = _recover(h_final, db_path)
    assert recovery_final.ok is True
    assert pipeline_final.portfolio.positions[INSTRUMENT].quantity == quantity
    store3.close()
    del outcome2


# -- Scenario 4: after partial take-profit -----------------------------------


def _tick_at(price: Decimal, *, timestamp=NOW):
    """A tight-spread quote centered on `price`, mirroring
    `e2e_scenarios.make_history`'s own bid/ask convention -- keeps the
    slippage guard's reference price (built from `snapshot.last`) and the
    actual fill price (from `snapshot.bid`/`snapshot.ask`) close enough
    together that a reduce-only market exit is never spuriously rejected by
    `PaperExecutionEngine`'s slippage guard."""
    return scenarios.make_snapshot(
        timestamp=timestamp,
        bid=price - Decimal("0.1"),
        ask=price + Decimal("0.1"),
        last=price,
    )


def _tp_exit_policy() -> ExitPolicy:
    return scenarios.make_exit_policy(
        breakeven_trigger_r_multiple=Decimal("100"),  # inert: never fires in this test
        trailing_activation_r_multiple=Decimal("100"),  # inert
        take_profit_stages=(
            TakeProfitStage(r_multiple=Decimal("1"), close_fraction=Decimal("0.5")),
            TakeProfitStage(r_multiple=Decimal("2"), close_fraction=Decimal("0.5")),
        ),
    )


def test_recovery_after_partial_take_profit_restores_stage_and_remaining_size(tmp_path) -> None:
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness(exit_policy=_tp_exit_policy())
    store = SQLiteStore(db_path)
    h.pipeline.store = store

    original_quantity = _open_long(h)
    entry_position = h.pipeline._exit_positions[INSTRUMENT]
    initial_risk = entry_position.initial_risk
    stage1_price = entry_position.entry_price + initial_risk  # 1R: stage 0 trigger

    tick_snapshot = _tick_at(stage1_price + Decimal("0.5"))
    step = h.pipeline.process_exits(
        snapshot=tick_snapshot,
        runtime=scenarios.make_runtime(),
        account_known=True,
        now=NOW,
    )
    assert step is not None
    assert step.decision is not None
    assert step.outcome is not None and step.outcome.value == "FILLED"

    remaining = h.portfolio.positions[INSTRUMENT].quantity
    assert remaining > 0
    assert remaining < original_quantity
    exit_position_after = h.pipeline._exit_positions[INSTRUMENT]
    assert exit_position_after.stages_completed == 1
    assert exit_position_after.realized_partial_quantity > 0
    store.close()

    pipeline, recovery, store2 = _recover(h, db_path)
    assert recovery.ok is True
    assert recovery.halted is False

    restored = pipeline._exit_positions[INSTRUMENT]
    assert restored.stages_completed == 1
    assert restored.realized_partial_quantity == exit_position_after.realized_partial_quantity
    assert restored.quantity == remaining
    assert pipeline.portfolio.positions[INSTRUMENT].quantity == remaining

    # The NEXT stage (stage index 1, r_multiple=2), not a repeat of stage 0,
    # is what evaluates on the next tick: stage 0 must never refire.
    stage2_price = entry_position.entry_price + initial_risk * Decimal("2")
    next_tick = _tick_at(stage2_price + Decimal("0.5"))
    step2 = pipeline.process_exits(
        snapshot=next_tick,
        runtime=scenarios.make_runtime(),
        account_known=True,
        now=NOW + timedelta(seconds=1),
    )
    assert step2 is not None
    assert step2.decision is not None
    assert step2.decision.metadata.get("stage_index") == 1  # stage 1, never stage 0 again
    store2.close()


def test_recovery_before_partial_take_profit_persist_shows_pre_stage_state_and_refires_once(
    tmp_path,
) -> None:
    """Crash BEFORE the partial-TP fill's own `_persist()` call: recovery
    must show the pre-TP1 state (nothing committed), and TP1 must then
    correctly fire again for real on the next real market tick -- exactly
    once, not skipped and not duplicated."""
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness(exit_policy=_tp_exit_policy())
    store = SQLiteStore(db_path)
    h.pipeline.store = store

    original_quantity = _open_long(h)  # persisted: entry is durable
    entry_position = h.pipeline._exit_positions[INSTRUMENT]
    initial_risk = entry_position.initial_risk
    stage1_price = entry_position.entry_price + initial_risk

    # Detach the store BEFORE the TP1 tick -- its fill/position/exit-state
    # changes happen only in-memory, simulating a crash before `_persist()`
    # for this call ever ran.
    h.pipeline.store = None
    tick_snapshot = _tick_at(stage1_price + Decimal("0.5"))
    step = h.pipeline.process_exits(
        snapshot=tick_snapshot,
        runtime=scenarios.make_runtime(),
        account_known=True,
        now=NOW,
    )
    assert step is not None and step.decision is not None  # TP1 really fired in-memory
    store.close()

    # Recovering must show pre-TP1 state: full original quantity, stage 0.
    pipeline, recovery, store2 = _recover(h, db_path)
    assert recovery.ok is True
    restored = pipeline._exit_positions[INSTRUMENT]
    assert restored.stages_completed == 0
    assert restored.quantity == original_quantity
    assert pipeline.portfolio.positions[INSTRUMENT].quantity == original_quantity

    # Redrive the SAME tick for real against the recovered, store-attached
    # pipeline: TP1 must fire exactly once now.
    pipeline.store = store2
    step_real = pipeline.process_exits(
        snapshot=tick_snapshot,
        runtime=scenarios.make_runtime(),
        account_known=True,
        now=NOW,
    )
    assert step_real is not None and step_real.decision is not None
    assert step_real.decision.metadata.get("stage_index") == 0  # stage 0, fired for the first time
    remaining = pipeline.portfolio.positions[INSTRUMENT].quantity
    assert remaining < original_quantity
    assert pipeline._exit_positions[INSTRUMENT].stages_completed == 1
    store2.close()


# -- Scenario 5: after stop update (trailing/break-even ratchet) -------------


def _breakeven_exit_policy() -> ExitPolicy:
    return scenarios.make_exit_policy(
        breakeven_trigger_r_multiple=Decimal("1"),
        breakeven_buffer_bps=Decimal("10"),
        trailing_activation_r_multiple=Decimal("100"),  # inert for this test
    )


def test_recovery_restores_ratcheted_stop_not_the_stale_original(tmp_path) -> None:
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness(exit_policy=_breakeven_exit_policy())
    store = SQLiteStore(db_path)
    h.pipeline.store = store

    _open_long(h)
    entry_position = h.pipeline._exit_positions[INSTRUMENT]
    initial_risk = entry_position.initial_risk
    original_stop = entry_position.current_stop_price
    breakeven_trigger_price = entry_position.entry_price + initial_risk  # 1R

    tick = scenarios.make_snapshot(
        timestamp=NOW,
        bid=breakeven_trigger_price - Decimal("0.05"),
        ask=breakeven_trigger_price + Decimal("0.05"),
        last=breakeven_trigger_price,
    )
    step = h.pipeline.process_exits(
        snapshot=tick, runtime=scenarios.make_runtime(), account_known=True, now=NOW
    )
    assert step is not None
    ratcheted = h.pipeline._exit_positions[INSTRUMENT]
    assert ratcheted.stop_stage == StopStage.BREAK_EVEN
    assert ratcheted.current_stop_price > original_stop  # strictly improved, a long's stop rose
    ratcheted_stop = ratcheted.current_stop_price
    ratcheted_hwm = ratcheted.high_water_mark
    store.close()

    pipeline, recovery, store2 = _recover(h, db_path)
    assert recovery.ok is True
    restored = pipeline._exit_positions[INSTRUMENT]
    assert restored.current_stop_price == ratcheted_stop
    assert restored.stop_stage == StopStage.BREAK_EVEN
    assert restored.high_water_mark == ratcheted_hwm
    # Never a worse (less protective) stop than what was actually live.
    assert restored.current_stop_price >= original_stop
    store2.close()


# -- Scenario 6: restart/reconciliation encountering inconsistent state -----


def test_recovery_halts_on_inconsistent_store_and_stays_safe_on_retry(tmp_path) -> None:
    """A persisted `positions` row that disagrees with the replayed-fills
    truth must HALT recovery rather than guess/silently accept the
    mismatch -- and calling `recover_pipeline()` again (idempotency of the
    recovery attempt itself) must not compound the problem."""
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness()
    store = SQLiteStore(db_path)
    h.pipeline.store = store

    _open_long(h)
    store.close()

    # Directly corrupt the persisted positions row: quantity does not match
    # what the fills table (source of truth) would replay to.
    corrupt_store = SQLiteStore(db_path)
    from persistence.models import PositionRecord

    corrupt_store.upsert_position(
        PositionRecord(
            instrument=INSTRUMENT,
            quantity=Decimal("999999"),
            avg_entry_price=Decimal("100"),
            mark_price=Decimal("100"),
            updated_at=NOW,
        )
    )
    corrupt_store.close()

    pipeline, recovery, store2 = _recover(h, db_path)
    assert recovery.ok is False
    assert recovery.halted is True
    assert recovery.mismatch_detail is not None
    assert "mismatch" in recovery.mismatch_detail.lower()
    assert pipeline.risk_engine.halted is True
    assert pipeline.execution_engine.mode is EngineMode.HALTED
    first_halt_reason = pipeline.execution_engine.halt_reason
    store2.close()

    # Retry (idempotency): a second `recover_pipeline()` attempt against the
    # same still-corrupt store must reach the exact same safe outcome, never
    # compound (no stacked halt reasons, no exception, no partial recovery).
    pipeline2, recovery2, store3 = _recover(h, db_path)
    assert recovery2.ok is False
    assert recovery2.halted is True
    assert pipeline2.risk_engine.halted is True
    assert pipeline2.execution_engine.mode is EngineMode.HALTED
    assert pipeline2.execution_engine.halt_reason == first_halt_reason
    store3.close()
