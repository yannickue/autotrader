"""Two confirmed semantic bugs fixed before Nautilus convergence (slice C1/C2).

Bug 1 -- daily PnL. Old: `realized_pnl_today` was ALL-TIME realized PnL minus
all-time fees. New: realized PnL - fees of fills inside the current trading day
(`risk.trading_day.TradingDayPolicy`, default UTC midnight, broker calibration
pending).

Bug 2 -- reconciliation. Old: the pipeline treated `EngineMode.READY` as
"reconciled", persisted `last_reconciled_at=now` whenever READY, and after
recovery "reconciled" against a snapshot built from the engine's own state.
New: a separate `ReconciliationState`; only `reconcile()` (an explicit
comparison) can produce RECONCILED; restarts begin NOT_RECONCILED; the
paper self-check is labelled `PAPER_SELF_CHECK`, never a venue reconciliation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from execution.models import OrderSide
from execution.orders import HaltCode
from execution.paper import EngineMode, PaperExecutionEngine
from persistence.store import SQLiteStore
from pipeline.paper import PipelineStage
from portfolio.ledger import Portfolio
from portfolio.models import Fill
from risk.models import (
    PositionSizingRequest,
    ReconciliationSource,
    ReconciliationState,
    RiskReason,
    RiskSide,
)
from risk.trading_day import TradingDayPolicy
from tests.integration import e2e_scenarios as scenarios
from tests.unit.pipeline.test_persistence_recovery import _db_path, _open_long, _recover

INSTRUMENT = scenarios.INSTRUMENT
NOW = scenarios.NOW  # 2026-01-01 12:00 UTC
YESTERDAY = NOW - timedelta(days=1)


def _fill(fid: str, side: OrderSide, qty: str, price: str, ts: datetime, fee: str = "0") -> Fill:
    return Fill(
        fill_id=fid,
        instrument=INSTRUMENT,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        timestamp=ts,
    )


def _round_trip_loss(h: scenarios.Harness, ts: datetime, tag: str) -> None:
    """Realize -12000 (> policy max_daily_loss 10000, < max_drawdown 20000), flat afterwards."""
    h.portfolio.apply_fill(_fill(f"{tag}-b", OrderSide.BUY, "1000", "100", ts))
    h.portfolio.apply_fill(_fill(f"{tag}-s", OrderSide.SELL, "1000", "88", ts))
    assert h.portfolio.realized_pnl == Decimal("-12000")


def _request(ts: datetime = NOW, signal_id: str = "sig-x") -> PositionSizingRequest:
    return PositionSizingRequest(
        signal_id=signal_id,
        instrument=INSTRUMENT,
        timestamp=ts,
        side=RiskSide.BUY,
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        confidence=Decimal("0.5"),
        available_liquidity_notional=Decimal("100000"),
        metadata={},
    )


def _evaluate(h: scenarios.Harness, now: datetime = NOW):
    account = h.pipeline._build_account_state(instrument=INSTRUMENT, account_known=True, now=now)
    decision = h.risk_engine.evaluate(
        request=_request(now),
        snapshot=scenarios.make_snapshot(timestamp=now),
        account=account,
        runtime=scenarios.make_runtime(),
        instrument=h.limits,
        now=now,
    )
    return account, decision


# =========================== Bug 1: daily PnL ================================


def test_old_days_loss_is_not_todays_pnl_and_does_not_trip_daily_loss_gate() -> None:
    h = scenarios.build_harness()
    _round_trip_loss(h, YESTERDAY, "old")

    account, decision = _evaluate(h)

    assert account.realized_pnl_today == Decimal("0")  # old: -12000 (all-time)
    assert account.pnl_window_start == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert decision.reason_code is not RiskReason.DAILY_LOSS_LIMIT
    assert decision.approved is True


def test_same_size_loss_today_does_trip_daily_loss_gate() -> None:
    h = scenarios.build_harness()
    _round_trip_loss(h, NOW - timedelta(hours=1), "today")

    account, decision = _evaluate(h)

    assert account.realized_pnl_today == Decimal("-12000")
    assert decision.approved is False
    assert decision.reason_code is RiskReason.DAILY_LOSS_LIMIT


def test_configured_trading_day_boundary_moves_the_window() -> None:
    h = scenarios.build_harness()
    # Rollover 17:00 New York (EST, UTC-5, in January) -> NOW (12:00Z = 07:00 local)
    # is in the day that began 2025-12-31 17:00 local = 22:00 UTC.
    h.pipeline.trading_day_policy = TradingDayPolicy(
        timezone="America/New_York", rollover_time=time(17, 0)
    )
    h.portfolio.apply_fill(
        _fill("pre", OrderSide.BUY, "1", "100", datetime(2025, 12, 31, 21, 0, tzinfo=UTC), "7")
    )  # 21:00Z = previous trading day
    h.portfolio.apply_fill(
        _fill("post", OrderSide.BUY, "1", "100", datetime(2025, 12, 31, 23, 0, tzinfo=UTC), "3")
    )  # 23:00Z = current trading day

    account = h.pipeline._build_account_state(instrument=INSTRUMENT, account_known=True, now=NOW)

    assert account.realized_pnl_today == Decimal("-3")
    assert account.pnl_window_start == datetime(2025, 12, 31, 22, 0, tzinfo=UTC)


def test_risk_rejects_an_all_time_figure_smuggled_in_under_realized_pnl_today() -> None:
    """The risk policy refuses a `realized_pnl_today` whose window is not a
    single trading day, so an epoch/all-time window cannot pass as 'today'."""
    h = scenarios.build_harness()
    account, _ = _evaluate(h)
    smuggled = replace(account, pnl_window_start=datetime(1970, 1, 1, tzinfo=UTC))
    decision = h.risk_engine.evaluate(
        request=_request(signal_id="sig-smuggled"),
        snapshot=scenarios.make_snapshot(),
        account=smuggled,
        runtime=scenarios.make_runtime(),
        instrument=h.limits,
        now=NOW,
    )
    assert decision.approved is False
    assert decision.reason_code is RiskReason.INVALID_INPUT

    future = replace(account, pnl_window_start=NOW + timedelta(seconds=1))
    decision2 = h.risk_engine.evaluate(
        request=_request(signal_id="sig-y"),
        snapshot=scenarios.make_snapshot(),
        account=future,
        runtime=scenarios.make_runtime(),
        instrument=h.limits,
        now=NOW,
    )
    assert decision2.reason_code is RiskReason.INVALID_INPUT


def test_restart_recovers_same_day_pnl_and_new_day_resets_it(tmp_path) -> None:
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness(
        cost_schedule=scenarios.make_cost_schedule(taker_fee_rate=Decimal("0.0005"))
    )
    store = SQLiteStore(db_path)
    h.pipeline.store = store
    _open_long(h)  # entry fill at NOW carries a fee -> negative realized_pnl_today
    before = h.pipeline._build_account_state(instrument=INSTRUMENT, account_known=True, now=NOW)
    assert before.realized_pnl_today < 0
    store.close()

    fee_harness = scenarios.build_harness(
        cost_schedule=scenarios.make_cost_schedule(taker_fee_rate=Decimal("0.0005"))
    )
    recovered, result, store2 = _recover(fee_harness, db_path, now=NOW)
    assert result.ok is True
    same_day = recovered._build_account_state(instrument=INSTRUMENT, account_known=True, now=NOW)
    assert same_day.realized_pnl_today == before.realized_pnl_today

    next_day = recovered._build_account_state(
        instrument=INSTRUMENT, account_known=True, now=NOW + timedelta(days=1)
    )
    assert next_day.realized_pnl_today == Decimal("0")
    store2.close()


# ========================= Bug 2: reconciliation =============================


def _unreconciled_harness() -> scenarios.Harness:
    h = scenarios.build_harness()
    fresh = PaperExecutionEngine(h.exec_config, h.portfolio, h.cost_schedule)
    h.execution_engine = fresh
    h.pipeline.execution_engine = fresh
    fresh.fill_listener = h.pipeline._on_fill
    return h


def _process(h: scenarios.Harness):
    return h.pipeline.process(
        history=scenarios.make_history(["100", "100", "100"], start=NOW - timedelta(minutes=2)),
        strategy=scenarios.FixedStrategy(
            scenarios.make_signal(
                direction=scenarios.Direction.LONG,
                invalidation_level=Decimal("95"),
                timestamp=NOW,
            )
        ),
        universe=frozenset({INSTRUMENT}),
        runtime=scenarios.make_runtime(),
        account_known=True,
        now=NOW,
    )


def test_fresh_engine_starts_not_reconciled_and_blocks_new_exposure() -> None:
    h = _unreconciled_harness()
    assert h.execution_engine.mode is EngineMode.RECONCILING
    assert h.execution_engine.reconciliation_state is ReconciliationState.NOT_RECONCILED
    assert h.execution_engine.last_reconciled_at is None

    outcome = _process(h)

    assert outcome.stage is PipelineStage.RISK_REJECTED
    assert outcome.risk_reason == RiskReason.ACCOUNT_UNRECONCILED.value


def test_ready_mode_is_not_reconciled() -> None:
    """READY assigned without a comparison must NOT satisfy the reconciled gate
    (old behavior: READY was mapped to reconciled=True)."""
    h = _unreconciled_harness()
    h.execution_engine.mode = EngineMode.READY  # permission mode only, no comparison happened

    account = h.pipeline._build_account_state(instrument=INSTRUMENT, account_known=True, now=NOW)

    assert account.reconciliation is ReconciliationState.NOT_RECONCILED
    assert _process(h).risk_reason == RiskReason.ACCOUNT_UNRECONCILED.value


def test_explicit_reconcile_is_the_only_way_to_reconciled() -> None:
    h = _unreconciled_harness()
    assert h.execution_engine.reconcile({"orders": {}, "positions": {}}, NOW) is True
    assert h.execution_engine.reconciliation_state is ReconciliationState.RECONCILED
    assert h.execution_engine.reconciliation_source is ReconciliationSource.VENUE_SNAPSHOT
    assert h.execution_engine.last_reconciled_at == NOW
    assert h.execution_engine.mode is EngineMode.READY
    assert _process(h).stage is PipelineStage.EXECUTED


def test_mismatch_blocks_new_exposure() -> None:
    h = _unreconciled_harness()
    ok = h.execution_engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, NOW)
    assert ok is False
    assert h.execution_engine.reconciliation_state is ReconciliationState.MISMATCH

    outcome = _process(h)

    assert outcome.stage is PipelineStage.RISK_REJECTED
    assert outcome.risk_reason == RiskReason.ACCOUNT_UNRECONCILED.value


def _restart_engine(h: scenarios.Harness) -> PaperExecutionEngine:
    portfolio = Portfolio(starting_balance=Decimal("1"))
    return PaperExecutionEngine(h.exec_config, portfolio, h.cost_schedule)


def test_restart_from_a_ready_checkpoint_begins_not_reconciled() -> None:
    h = scenarios.build_harness()  # reconciled, READY
    assert h.execution_engine.reconciliation_state is ReconciliationState.RECONCILED
    checkpoint = h.execution_engine.export_checkpoint()

    restarted = _restart_engine(h)
    restarted.import_checkpoint(checkpoint)

    assert restarted.reconciliation_state is ReconciliationState.NOT_RECONCILED
    assert restarted.last_reconciled_at is None
    assert restarted.mode is EngineMode.RECONCILING


def test_restart_from_a_halted_checkpoint_also_begins_not_reconciled() -> None:
    h = scenarios.build_harness()
    h.execution_engine._halt(HaltCode.OVERFILL, "test")
    restarted = _restart_engine(h)
    restarted.import_checkpoint(h.execution_engine.export_checkpoint())

    assert restarted.mode is EngineMode.HALTED
    assert restarted.reconciliation_state is ReconciliationState.NOT_RECONCILED


def test_state_unreliable_halts_set_mismatch_but_overfill_keeps_reconciled() -> None:
    """Q-X2: reduce-only stays allowed when a halt did not invalidate account
    state (OVERFILL is raised before any portfolio mutation); it is blocked
    (MISMATCH) for UNKNOWN_ORDER / RECONCILIATION_MISMATCH / INTERNAL_ERROR."""
    for code, expected in (
        (HaltCode.OVERFILL, ReconciliationState.RECONCILED),
        (HaltCode.UNKNOWN_ORDER, ReconciliationState.MISMATCH),
        (HaltCode.RECONCILIATION_MISMATCH, ReconciliationState.MISMATCH),
        (HaltCode.INTERNAL_ERROR, ReconciliationState.MISMATCH),
    ):
        h = scenarios.build_harness()
        h.execution_engine._halt(code, "test")
        assert h.execution_engine.mode is EngineMode.HALTED
        account = h.pipeline._build_account_state(
            instrument=INSTRUMENT, account_known=True, now=NOW
        )
        assert account.reconciliation is expected, code


def test_exception_during_reconcile_fails_closed_never_leaves_engine_ready() -> None:
    h = scenarios.build_harness()  # READY + RECONCILED
    assert h.execution_engine.mode is EngineMode.READY
    bad_venue = {"orders": {}, "positions": {INSTRUMENT: "not-a-number"}}
    ok = h.execution_engine.reconcile(bad_venue, NOW)
    assert ok is False
    assert h.execution_engine.mode is EngineMode.HALTED
    assert h.execution_engine.reconciliation_state is ReconciliationState.MISMATCH
    assert _process(h).risk_reason == RiskReason.ACCOUNT_UNRECONCILED.value


def test_resume_after_reconcile_returns_true_only_when_actually_reconciled() -> None:
    h = _unreconciled_harness()
    h.execution_engine.mode = EngineMode.READY  # permission mode without a comparison
    assert h.execution_engine.resume_after_reconcile({"orders": {}, "positions": {}}, NOW) is False


def test_recovery_self_check_is_labelled_paper_and_persisted_timestamp_is_the_real_one(
    tmp_path,
) -> None:
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness()
    store = SQLiteStore(db_path)
    h.pipeline.store = store
    _open_long(h)  # persisted while engine RECONCILED at NOW (reconcile happened in harness)
    later = NOW + timedelta(hours=3)
    h.pipeline._persist(now=later)
    record = store.get_reconciliation_state()
    assert record is not None
    assert record.reconciled is True
    assert record.last_reconciled_at == NOW  # old behavior: `later` (i.e. "now")
    store.close()

    recovered, result, store2 = _recover(scenarios.build_harness(), db_path, now=later)
    assert result.ok is True
    engine = recovered.execution_engine
    assert engine.reconciliation_state is ReconciliationState.RECONCILED
    assert engine.reconciliation_source is ReconciliationSource.PAPER_SELF_CHECK
    assert engine.last_reconciled_at == later
    store2.close()


def test_failed_recovery_leaves_state_unreliable_not_reconciled(tmp_path) -> None:
    db_path = _db_path(tmp_path)
    h = scenarios.build_harness()
    store = SQLiteStore(db_path)
    h.pipeline.store = store
    _open_long(h)
    store.close()

    from persistence.models import PositionRecord

    corrupt = SQLiteStore(db_path)
    corrupt.upsert_position(
        PositionRecord(
            instrument=INSTRUMENT,
            quantity=Decimal("999999"),
            avg_entry_price=Decimal("100"),
            mark_price=Decimal("100"),
            updated_at=NOW,
        )
    )
    corrupt.close()

    recovered, result, store2 = _recover(scenarios.build_harness(), db_path)

    assert result.ok is False
    assert recovered.execution_engine.mode is EngineMode.HALTED
    # Old behavior: HALTED with a non-"unreliable" halt code counted as reconciled.
    assert recovered.execution_engine.reconciliation_state is ReconciliationState.MISMATCH
    store2.close()
