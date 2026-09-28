from datetime import UTC, datetime, timedelta
from decimal import Decimal

from exits.engine import ExitEngine, apply_evaluation
from exits.models import (
    ExitMarketState,
    ExitOutcome,
    ExitPolicy,
    ExitPosition,
    ExitReason,
    PositionSide,
    StopStage,
)
from risk.engine import RiskEngine
from risk.models import (
    AccountRiskState,
    RiskPolicy,
    RiskReason,
    RiskSide,
    RuntimeMode,
    RuntimeRiskState,
)

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _policy(**changes: object) -> ExitPolicy:
    values: dict[str, object] = {
        "policy_id": "exit-v1",
        "breakeven_trigger_r_multiple": Decimal("1"),
        "breakeven_buffer_bps": Decimal("5"),
        "trailing_activation_r_multiple": Decimal("1.5"),
        "trailing_distance_volatility_multiplier": Decimal("2"),
        "target_r_multiple": None,
        "partial_take_profit_fraction": Decimal("0.5"),
        "min_remaining_quantity": Decimal("0.01"),
        "quantity_step": Decimal("0.01"),
        "momentum_deterioration_threshold": Decimal("0"),
        "max_spread_bps": Decimal("50"),
        "min_liquidity_notional": Decimal("1000"),
        "max_holding_duration": timedelta(hours=4),
        "max_market_data_age": timedelta(seconds=30),
    }
    values.update(changes)
    return ExitPolicy(**values)


def _position(**changes: object) -> ExitPosition:
    values: dict[str, object] = {
        "position_id": "pos-1",
        "instrument": "BTCUSDT-PERP",
        "side": PositionSide.LONG,
        "entry_price": Decimal("100"),
        "quantity": Decimal("1"),
        "initial_stop_price": Decimal("95"),
        "current_stop_price": Decimal("95"),
        "stop_stage": StopStage.ORIGINAL,
        "high_water_mark": None,
        "fixed_target_price": None,
        "opened_at": NOW,
        "realized_partial_quantity": Decimal("0"),
        "pending_close_request_id": None,
    }
    values.update(changes)
    return ExitPosition(**values)


def _market(**changes: object) -> ExitMarketState:
    values: dict[str, object] = {
        "instrument": "BTCUSDT-PERP",
        "timestamp": NOW,
        "price": Decimal("100"),
        "bid": Decimal("99.9"),
        "ask": Decimal("100.1"),
        "volatility": Decimal("0.01"),
        "momentum_score": Decimal("1"),
        "signal_reversal": False,
        "risk_halt": False,
        "available_liquidity_notional": Decimal("5000"),
    }
    values.update(changes)
    return ExitMarketState(**values)


class _StubRiskGate:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, decision_id: str) -> None:
        self.released.append(decision_id)


def _engine(*, policy: ExitPolicy | None = None, risk_gate: object | None = None) -> ExitEngine:
    return ExitEngine(policy=policy or _policy(), risk_gate=risk_gate or _StubRiskGate())


# -- risk-engine test helpers (for the reduce-only release integration test) --


def _risk_policy(**changes: object) -> RiskPolicy:
    values: dict[str, object] = {
        "policy_id": "risk-v1",
        "risk_fraction": Decimal("0.01"),
        "max_leverage": Decimal("10"),
        "max_gross_notional": Decimal("10000"),
        "max_net_notional": Decimal("5000"),
        "max_daily_loss": Decimal("100"),
        "max_drawdown": Decimal("200"),
        "max_consecutive_losses": 3,
        "liquidity_fraction": Decimal("0.10"),
        "max_data_age": timedelta(seconds=5),
        "max_signal_age": timedelta(seconds=5),
    }
    values.update(changes)
    return RiskPolicy(**values)


def _risk_account(**changes: object) -> AccountRiskState:
    values: dict[str, object] = {
        "state_version": "account-1",
        "known": True,
        "reconciled": True,
        "equity": Decimal("1000"),
        "peak_equity": Decimal("1000"),
        "realized_pnl_today": Decimal("0"),
        "unrealized_pnl": Decimal("0"),
        "gross_notional": Decimal("0"),
        "net_notional": Decimal("0"),
        "instrument_notionals": {},
        "positions": {},
        "leverage_cap": Decimal("5"),
        "consecutive_losses": 0,
    }
    values.update(changes)
    return AccountRiskState(**values)


def _risk_runtime(**changes: object) -> RuntimeRiskState:
    values: dict[str, object] = {
        "state_version": "runtime-1",
        "mode": RuntimeMode.READY,
        "risk_ready": True,
        "kill_switch": False,
    }
    values.update(changes)
    return RuntimeRiskState(**values)


# -- individual rule tests -------------------------------------------------


def test_normal_stop_trigger_closes_full_position():
    engine = _engine()
    position = _position()
    market = _market(price=Decimal("94"))

    result = engine.evaluate(position=position, market=market, now=NOW)

    assert result.decision is not None
    assert result.decision.reason == ExitReason.INVALIDATION_STOP
    assert result.decision.is_partial is False
    assert result.decision.quantity == Decimal("1")
    assert result.decision.close_side == RiskSide.SELL


def test_take_profit_trigger_is_partial_by_default():
    engine = _engine()
    position = _position(fixed_target_price=Decimal("110"))
    market = _market(price=Decimal("111"))

    result = engine.evaluate(position=position, market=market, now=NOW)

    assert result.decision is not None
    assert result.decision.reason == ExitReason.TAKE_PROFIT
    assert result.decision.is_partial is True
    assert result.decision.quantity == Decimal("0.50")
    assert result.decision.metadata["target_source"] == "fixed"


def test_trailing_stop_ratchets_then_triggers_on_pullback():
    engine = _engine()
    position = _position()

    # Tick 1: a strong favorable move activates break-even and trailing.
    tick1_market = _market(price=Decimal("110"))
    tick1 = engine.evaluate(position=position, market=tick1_market, now=NOW)
    assert tick1.decision is None
    assert tick1.updated_stop_stage == StopStage.TRAILING
    position = apply_evaluation(position, tick1)

    # Tick 2: price pulls back through the ratcheted trailing stop.
    later = NOW + timedelta(seconds=1)
    tick2_market = _market(price=Decimal("107"), timestamp=later)
    tick2 = engine.evaluate(position=position, market=tick2_market, now=later)

    assert tick2.decision is not None
    assert tick2.decision.reason == ExitReason.TRAILING_STOP
    assert tick2.decision.is_partial is False


def test_break_even_transition_moves_stop_to_entry_without_closing():
    engine = _engine()
    position = _position()
    # r_multiple = 6/5 = 1.2: above the break-even trigger (1) but below the
    # trailing activation threshold (1.5), isolating the break-even rule.
    market = _market(price=Decimal("106"))

    result = engine.evaluate(position=position, market=market, now=NOW)

    assert result.decision is None
    assert result.break_even_activated is True
    assert result.updated_stop_stage == StopStage.BREAK_EVEN
    assert result.updated_stop_price == Decimal("100.05")


def test_partial_close_does_not_retrigger_after_target_already_taken():
    engine = _engine()
    position = _position(fixed_target_price=Decimal("110"))
    market = _market(price=Decimal("111"))

    first = engine.evaluate(position=position, market=market, now=NOW)
    assert first.decision is not None
    assert first.decision.is_partial is True

    # Simulate the partial fill: quantity drops, realized_partial_quantity is
    # recorded, and the pending-close guard is cleared once the fill lands.
    reduced = _position(
        fixed_target_price=Decimal("110"),
        quantity=position.quantity - first.decision.quantity,
        realized_partial_quantity=first.decision.quantity,
        current_stop_price=first.updated_stop_price,
        stop_stage=first.updated_stop_stage,
        high_water_mark=first.updated_high_water_mark,
    )
    later_market = _market(price=Decimal("130"), timestamp=NOW)
    second = engine.evaluate(position=reduced, market=later_market, now=NOW)

    assert second.decision is None or second.decision.reason != ExitReason.TAKE_PROFIT


def test_runner_scenario_large_move_does_not_fully_close_on_fixed_target_alone():
    engine = _engine()
    position = _position(fixed_target_price=Decimal("110"))
    # Price continues far beyond the fixed target -- a naive fixed-target
    # rule would fully close here and cut the runner short.
    market = _market(price=Decimal("150"))

    result = engine.evaluate(position=position, market=market, now=NOW)

    assert result.decision is not None
    assert result.decision.reason == ExitReason.TAKE_PROFIT
    assert result.decision.is_partial is True
    assert result.decision.quantity < position.quantity


def test_time_stop_closes_after_max_holding_duration():
    engine = _engine()
    position = _position()
    now = NOW + timedelta(hours=4, seconds=1)
    market = _market(price=Decimal("100"), timestamp=now)

    result = engine.evaluate(position=position, market=market, now=now)

    assert result.decision is not None
    assert result.decision.reason == ExitReason.TIME_STOP
    assert result.decision.quantity == Decimal("1")


def test_liquidity_deterioration_exit_on_wide_spread():
    engine = _engine()
    position = _position()
    market = _market(bid=Decimal("95"), ask=Decimal("105"))

    result = engine.evaluate(position=position, market=market, now=NOW)

    assert result.decision is not None
    assert result.decision.reason == ExitReason.LIQUIDITY_DETERIORATION


def test_signal_reversal_exit():
    engine = _engine()
    position = _position()
    market = _market(price=Decimal("100"), signal_reversal=True)

    result = engine.evaluate(position=position, market=market, now=NOW)

    assert result.decision is not None
    assert result.decision.reason == ExitReason.SIGNAL_REVERSAL
    assert result.decision.quantity == Decimal("1")


def test_emergency_exit_overrides_every_other_rule():
    engine = _engine()
    position = _position()
    # Also stale-looking price movement that would otherwise not trigger
    # anything, plus risk_halt set: emergency must still win.
    market = _market(price=Decimal("100"), risk_halt=True)

    result = engine.evaluate(position=position, market=market, now=NOW)

    assert result.decision is not None
    assert result.decision.reason == ExitReason.EMERGENCY_RISK_EXIT
    assert result.decision.is_partial is False
    assert result.decision.quantity == Decimal("1")


def test_no_double_close_when_a_close_is_already_pending():
    engine = _engine()
    position = _position()
    market = _market(price=Decimal("94"))  # would normally trigger a stop exit

    first = engine.evaluate(position=position, market=market, now=NOW)
    assert first.decision is not None

    pending = _position(
        current_stop_price=position.current_stop_price,
        pending_close_request_id=first.decision.request_id,
    )
    second = engine.evaluate(position=pending, market=market, now=NOW)

    assert second.decision is None


def test_no_double_close_when_position_already_flat():
    engine = _engine()
    position = _position(quantity=Decimal("0"))
    market = _market(price=Decimal("50"))  # far below stop; would trigger if open

    result = engine.evaluate(position=position, market=market, now=NOW)

    assert result.decision is None


def test_stale_market_data_forces_emergency_close_fail_closed():
    engine = _engine()
    position = _position()
    stale_timestamp = NOW - timedelta(minutes=5)
    market = _market(price=Decimal("100"), timestamp=stale_timestamp)

    result = engine.evaluate(position=position, market=market, now=NOW)

    assert result.decision is not None
    assert result.decision.reason == ExitReason.EMERGENCY_RISK_EXIT


# -- reduce-only reservation release (docs/OPEN_QUESTIONS.md #24) -----------


def test_notify_terminal_releases_reduce_only_reservation_on_every_terminal_outcome():
    gate = _StubRiskGate()
    engine = _engine(risk_gate=gate)

    engine.notify_terminal("reduce-only:req-fill", ExitOutcome.FILLED)
    engine.notify_terminal("reduce-only:req-cancel", ExitOutcome.CANCELED)
    engine.notify_terminal("reduce-only:req-reject", ExitOutcome.REJECTED)

    assert gate.released == [
        "reduce-only:req-fill",
        "reduce-only:req-cancel",
        "reduce-only:req-reject",
    ]


def test_notify_terminal_release_is_idempotent():
    gate = _StubRiskGate()
    engine = _engine(risk_gate=gate)

    engine.notify_terminal("reduce-only:req-1", ExitOutcome.FILLED)
    engine.notify_terminal("reduce-only:req-1", ExitOutcome.FILLED)

    assert gate.released == ["reduce-only:req-1", "reduce-only:req-1"]


def test_notify_terminal_releases_real_risk_engine_reduce_only_reservation():
    """End-to-end proof against the real `RiskEngine`, not just a stub.

    Mirrors `src/pipeline/paper.py`'s handling of the main `evaluate()` path
    (release on every terminal outcome) for the reduce-only path that
    `docs/OPEN_QUESTIONS.md` #24 says has no caller yet.
    """
    risk_engine = RiskEngine(policy=_risk_policy())
    account = _risk_account(positions={"BTCUSDT-PERP": Decimal("1")})
    runtime = _risk_runtime()

    decision = risk_engine.evaluate_reduce_only(
        request_id="exit:pos-1:invalidation_stop:t1",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("1"),
        account=account,
        runtime=runtime,
        now=NOW,
    )
    assert decision.approved

    # Before release, the full-quantity reservation blocks a further
    # reduce-only request against the same position.
    blocked = risk_engine.evaluate_reduce_only(
        request_id="exit:pos-1:retry:t2",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("0.5"),
        account=account,
        runtime=runtime,
        now=NOW,
    )
    assert blocked.approved is False
    assert blocked.reason_code == RiskReason.QUANTITY_INVALID

    exit_engine = ExitEngine(policy=_policy(), risk_gate=risk_engine)
    exit_engine.notify_terminal(decision.decision_id, ExitOutcome.FILLED)

    # After release, a fresh reduce-only request against the same instrument
    # is no longer blocked by the stale reservation.
    released_ok = risk_engine.evaluate_reduce_only(
        request_id="exit:pos-1:after-release:t3",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("0.5"),
        account=account,
        runtime=runtime,
        now=NOW,
    )
    assert released_ok.approved is True
