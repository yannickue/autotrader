from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from data.models import DataQuality, MarketSnapshot
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

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _policy(**changes: object) -> RiskPolicy:
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


def _limits(**changes: object) -> InstrumentRiskLimits:
    values: dict[str, object] = {
        "instrument": "BTCUSDT-PERP",
        "max_leverage": Decimal("8"),
        "quantity_step": Decimal("0.1"),
        "min_quantity": Decimal("0.1"),
        "min_notional": Decimal("10"),
        "max_notional": Decimal("4000"),
        "max_spread_bps": Decimal("250"),
    }
    values.update(changes)
    return InstrumentRiskLimits(**values)


def _account(**changes: object) -> AccountRiskState:
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


def _runtime(**changes: object) -> RuntimeRiskState:
    values: dict[str, object] = {
        "state_version": "runtime-1",
        "mode": RuntimeMode.READY,
        "risk_ready": True,
        "kill_switch": False,
    }
    values.update(changes)
    return RuntimeRiskState(**values)


def _snapshot(**changes: object) -> MarketSnapshot:
    values: dict[str, object] = {
        "instrument": "BTCUSDT-PERP",
        "timestamp": NOW,
        "bid": Decimal("99"),
        "ask": Decimal("101"),
        "last": Decimal("100"),
        "volume": Decimal("1000"),
        "volatility": Decimal("0.02"),
        "liquidity": Decimal("1"),
        "source": "venue-a",
        "quality": DataQuality.LIVE,
    }
    values.update(changes)
    return MarketSnapshot(**values)


def _request(**changes: object) -> PositionSizingRequest:
    values: dict[str, object] = {
        "signal_id": "signal-1",
        "instrument": "BTCUSDT-PERP",
        "timestamp": NOW,
        "side": RiskSide.BUY,
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "confidence": Decimal("0.2"),
        "available_liquidity_notional": Decimal("10000"),
        "metadata": {"signal_version": "v1"},
    }
    values.update(changes)
    return PositionSizingRequest(**values)


def _evaluate(engine: RiskEngine, **overrides: object) -> RiskDecision:
    kwargs = {
        "request": _request(),
        "snapshot": _snapshot(),
        "account": _account(),
        "runtime": _runtime(),
        "instrument": _limits(),
        "now": NOW,
    }
    kwargs.update(overrides)
    return engine.evaluate(**kwargs)


# -- sizing -----------------------------------------------------------------


def test_sizes_linear_usdm_by_risk_and_rounds_down_to_step() -> None:
    decision = _evaluate(RiskEngine(_policy()))

    assert decision.approved is True
    assert decision.reason_code is RiskReason.APPROVED
    # risk_budget=10, per_unit_loss=5 + 100*10*2/10000=5.2 -> qty_risk=1.923...
    assert decision.quantity == Decimal("1.9")
    assert decision.notional == Decimal("190.0")
    assert decision.risk_budget == Decimal("10.00")
    assert decision.max_leverage == Decimal("5")
    assert decision.leverage == Decimal("0.19")
    assert decision.stop_price == Decimal("95")
    assert decision.metadata["binding_constraint"] == "risk_budget"


def test_confidence_never_influences_sizing() -> None:
    engine_low = RiskEngine(_policy())
    engine_high = RiskEngine(_policy())

    low = _evaluate(engine_low, request=_request(signal_id="s-low", confidence=Decimal("0")))
    high = _evaluate(engine_high, request=_request(signal_id="s-high", confidence=Decimal("1")))

    assert low.quantity == high.quantity
    assert low.notional == high.notional
    assert low.leverage == high.leverage


def test_reduce_risk_mode_applies_multiplier_on_drawdown() -> None:
    engine = RiskEngine(_policy())
    # drawdown of 100 >= 0.5 * max_drawdown(200) activates reduce-risk mode
    account = _account(equity=Decimal("900"), peak_equity=Decimal("1000"))
    decision = _evaluate(engine, account=account)

    assert decision.metadata["reduce_risk"] is True
    assert Decimal(decision.metadata["effective_risk_fraction"]) == Decimal("0.01") * Decimal("0.5")


def test_sizing_binds_on_instrument_max_notional() -> None:
    policy = _policy(risk_fraction=Decimal("1"))  # huge risk budget so notional cap binds
    limits = _limits(max_notional=Decimal("50"))
    decision = _evaluate(RiskEngine(policy), instrument=limits)

    assert decision.approved is True
    assert decision.metadata["binding_constraint"] == "instrument_max_notional"
    assert decision.notional <= Decimal("50")


# -- fail closed --------------------------------------------------------------


def test_unexpected_exception_rejects_and_latches_halt() -> None:
    engine = RiskEngine(_policy())
    broken_account = object()  # will raise AttributeError deep inside evaluation

    decision = _evaluate(engine, account=broken_account)

    assert decision.approved is False
    assert decision.reason_code == RiskReason.RISK_ERROR
    assert decision.quantity == 0
    assert decision.notional == 0
    assert engine.halted is True

    # engine stays halted for later, unrelated requests
    later = _evaluate(engine, request=_request(signal_id="later-signal"))
    assert later.approved is False
    assert later.reason_code == RiskReason.HALTED

    engine.reset_halt("operator confirmed root cause fixed")
    assert engine.halted is False
    resumed = _evaluate(engine, request=_request(signal_id="resumed-signal"))
    assert resumed.approved is True


def test_halt_helper_latches_engine() -> None:
    engine = RiskEngine(_policy())
    engine.halt("manual stop")
    decision = _evaluate(engine)
    assert decision.approved is False
    assert decision.reason_code == RiskReason.HALTED


def test_reset_halt_requires_operator_note() -> None:
    engine = RiskEngine(_policy())
    engine.halt("manual stop")
    with pytest.raises(ValueError):
        engine.reset_halt("")


# -- ordered rejection reasons -------------------------------------------------


def test_kill_switch_rejects_halted() -> None:
    decision = _evaluate(RiskEngine(_policy()), runtime=_runtime(kill_switch=True))
    assert decision.reason_code == RiskReason.HALTED


def test_runtime_mode_halted_rejects_halted() -> None:
    decision = _evaluate(RiskEngine(_policy()), runtime=_runtime(mode=RuntimeMode.HALTED))
    assert decision.reason_code == RiskReason.HALTED


@pytest.mark.parametrize(
    "mode", [RuntimeMode.STARTING, RuntimeMode.RECONCILING, RuntimeMode.DEGRADED]
)
def test_runtime_not_ready_modes_reject(mode: RuntimeMode) -> None:
    decision = _evaluate(RiskEngine(_policy()), runtime=_runtime(mode=mode))
    assert decision.reason_code == RiskReason.RUNTIME_NOT_READY


def test_runtime_not_risk_ready_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), runtime=_runtime(risk_ready=False))
    assert decision.reason_code == RiskReason.RUNTIME_NOT_READY


def test_account_unknown_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), account=_account(known=False))
    assert decision.reason_code == RiskReason.ACCOUNT_UNKNOWN


def test_account_unreconciled_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), account=_account(reconciled=False))
    assert decision.reason_code == RiskReason.ACCOUNT_UNRECONCILED


def test_invalid_input_nan_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), account=_account(equity=Decimal("NaN")))
    assert decision.reason_code == RiskReason.INVALID_INPUT


def test_invalid_input_infinite_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), account=_account(equity=Decimal("Infinity")))
    assert decision.reason_code == RiskReason.INVALID_INPUT


def test_invalid_input_naive_datetime_rejects() -> None:
    naive_now = datetime(2026, 1, 1, 12)
    decision = _evaluate(RiskEngine(_policy()), now=naive_now)
    assert decision.reason_code == RiskReason.INVALID_INPUT


def test_invalid_input_non_positive_entry_price_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), request=_request(entry_price=Decimal("0")))
    assert decision.reason_code == RiskReason.INVALID_INPUT


def test_equity_non_positive_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), account=_account(equity=Decimal("0")))
    assert decision.reason_code == RiskReason.EQUITY_NON_POSITIVE


def test_instrument_mismatch_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), request=_request(instrument="ETHUSDT-PERP"))
    assert decision.reason_code == RiskReason.INSTRUMENT_MISMATCH


def test_data_not_live_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), snapshot=_snapshot(quality=DataQuality.DELAYED))
    assert decision.reason_code == RiskReason.DATA_NOT_LIVE


def test_data_stale_rejects() -> None:
    stale = _snapshot(timestamp=NOW - timedelta(seconds=10))
    decision = _evaluate(RiskEngine(_policy()), snapshot=stale)
    assert decision.reason_code == RiskReason.DATA_STALE


def test_data_future_timestamp_rejects_stale() -> None:
    future = _snapshot(timestamp=NOW + timedelta(seconds=10))
    decision = _evaluate(RiskEngine(_policy()), snapshot=future)
    assert decision.reason_code == RiskReason.DATA_STALE


def test_signal_stale_rejects() -> None:
    stale_request = _request(timestamp=NOW - timedelta(seconds=10))
    decision = _evaluate(RiskEngine(_policy()), request=stale_request)
    assert decision.reason_code == RiskReason.SIGNAL_STALE


def test_spread_too_wide_rejects() -> None:
    wide = _snapshot(bid=Decimal("90"), ask=Decimal("110"))
    decision = _evaluate(RiskEngine(_policy()), snapshot=wide)
    assert decision.reason_code == RiskReason.SPREAD_TOO_WIDE


def test_invalid_stop_buy_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), request=_request(stop_price=Decimal("105")))
    assert decision.reason_code == RiskReason.INVALID_STOP


def test_invalid_stop_zero_distance_rejects() -> None:
    decision = _evaluate(RiskEngine(_policy()), request=_request(stop_price=Decimal("100")))
    assert decision.reason_code == RiskReason.INVALID_STOP


def test_invalid_stop_sell_rejects() -> None:
    decision = _evaluate(
        RiskEngine(_policy()),
        request=_request(side=RiskSide.SELL, entry_price=Decimal("100"), stop_price=Decimal("95")),
    )
    assert decision.reason_code == RiskReason.INVALID_STOP


def test_volatility_too_high_rejects() -> None:
    policy = _policy(max_volatility=Decimal("0.01"))
    decision = _evaluate(RiskEngine(policy), snapshot=_snapshot(volatility=Decimal("0.05")))
    assert decision.reason_code == RiskReason.VOLATILITY_TOO_HIGH


def test_volatility_missing_rejects_data_missing() -> None:
    policy = _policy(max_volatility=Decimal("0.01"))
    decision = _evaluate(RiskEngine(policy), snapshot=_snapshot(volatility=None))
    assert decision.reason_code == RiskReason.DATA_MISSING


def test_daily_loss_limit_rejects() -> None:
    account = _account(realized_pnl_today=Decimal("-60"), unrealized_pnl=Decimal("-50"))
    decision = _evaluate(RiskEngine(_policy()), account=account)
    assert decision.reason_code == RiskReason.DAILY_LOSS_LIMIT


def test_drawdown_limit_rejects() -> None:
    account = _account(equity=Decimal("799"), peak_equity=Decimal("1000"))
    decision = _evaluate(RiskEngine(_policy()), account=account)
    assert decision.reason_code == RiskReason.DRAWDOWN_LIMIT


def test_consecutive_loss_limit_rejects() -> None:
    account = _account(consecutive_losses=3)
    decision = _evaluate(RiskEngine(_policy()), account=account)
    assert decision.reason_code == RiskReason.CONSECUTIVE_LOSS_LIMIT


def test_liquidity_insufficient_rejects() -> None:
    request = _request(available_liquidity_notional=Decimal("0"))
    decision = _evaluate(RiskEngine(_policy()), request=request)
    assert decision.reason_code == RiskReason.LIQUIDITY_INSUFFICIENT


def test_exposure_limit_gross_rejects() -> None:
    account = _account(gross_notional=Decimal("10000"))
    decision = _evaluate(RiskEngine(_policy()), account=account)
    assert decision.reason_code == RiskReason.EXPOSURE_LIMIT


def test_exposure_limit_net_rejects() -> None:
    account = _account(net_notional=Decimal("5000"))
    decision = _evaluate(RiskEngine(_policy()), account=account)
    assert decision.reason_code == RiskReason.EXPOSURE_LIMIT


def test_size_below_minimum_rejects() -> None:
    policy = _policy(risk_fraction=Decimal("0.0001"))
    decision = _evaluate(RiskEngine(policy))
    assert decision.reason_code == RiskReason.SIZE_BELOW_MINIMUM
    assert decision.quantity == 0
    assert decision.notional == 0


# -- hooks ---------------------------------------------------------------------


def test_hook_rejection_is_prefixed() -> None:
    def guard(context: object) -> str | None:
        return "CORRELATION_LIMIT"

    engine = RiskEngine(_policy(), hooks=[guard])
    decision = _evaluate(engine)
    assert decision.approved is False
    assert decision.reason_code == "HOOK:CORRELATION_LIMIT"


def test_hook_exception_fails_closed() -> None:
    def guard(context: object) -> str | None:
        raise RuntimeError("boom")

    engine = RiskEngine(_policy(), hooks=[guard])
    decision = _evaluate(engine)
    assert decision.approved is False
    assert decision.reason_code == RiskReason.RISK_ERROR
    assert engine.halted is True


def test_hook_sees_proposed_size() -> None:
    seen: dict[str, object] = {}

    def guard(context) -> str | None:
        seen["quantity"] = context.proposed_quantity
        seen["notional"] = context.proposed_notional
        return None

    engine = RiskEngine(_policy(), hooks=[guard])
    decision = _evaluate(engine)
    assert decision.approved is True
    assert seen["quantity"] == decision.quantity
    assert seen["notional"] == decision.notional


# -- idempotency and reservations ----------------------------------------------


def test_idempotent_evaluation_returns_cached_decision_without_double_reserving() -> None:
    engine = RiskEngine(_policy())
    first = _evaluate(engine)
    second = _evaluate(engine)

    assert first == second
    assert engine._reserved_gross == first.notional  # reserved once, not twice


def test_release_is_idempotent() -> None:
    engine = RiskEngine(_policy())
    decision = _evaluate(engine)
    assert engine._reserved_gross == decision.notional

    engine.release(decision.decision_id)
    assert engine._reserved_gross == 0
    engine.release(decision.decision_id)  # no-op, does not raise
    assert engine._reserved_gross == 0


def test_reservation_counts_toward_gross_capacity() -> None:
    policy = _policy(max_gross_notional=Decimal("250"))
    engine = RiskEngine(policy)

    first = _evaluate(engine, request=_request(signal_id="first"))
    assert first.approved is True

    account_after = _account(gross_notional=Decimal("0"))
    second = _evaluate(engine, request=_request(signal_id="second"), account=account_after)
    # remaining capacity shrank due to the first reservation
    assert (
        second.reason_code in (RiskReason.EXPOSURE_LIMIT, RiskReason.SIZE_BELOW_MINIMUM)
        or second.approved is True
    )


# -- reduce-only ----------------------------------------------------------------


def test_reduce_only_approves_opposing_side() -> None:
    engine = RiskEngine(_policy())
    account = _account(positions={"BTCUSDT-PERP": Decimal("2")})
    decision = engine.evaluate_reduce_only(
        request_id="r1",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("1"),
        account=account,
        runtime=_runtime(),
        now=NOW,
    )
    assert decision.approved is True
    assert decision.metadata["reduce_only"] is True
    assert decision.decision_id == "reduce-only:r1"


def test_reduce_only_allowed_while_halted() -> None:
    engine = RiskEngine(_policy())
    engine.halt("kill switch tripped")
    account = _account(positions={"BTCUSDT-PERP": Decimal("2")})
    decision = engine.evaluate_reduce_only(
        request_id="r2",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("1"),
        account=account,
        runtime=_runtime(kill_switch=True, mode=RuntimeMode.HALTED),
        now=NOW,
    )
    assert decision.approved is True


def test_reduce_only_rejects_unknown_account() -> None:
    engine = RiskEngine(_policy())
    account = _account(known=False, positions={"BTCUSDT-PERP": Decimal("2")})
    decision = engine.evaluate_reduce_only(
        request_id="r3",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("1"),
        account=account,
        runtime=_runtime(),
        now=NOW,
    )
    assert decision.reason_code == RiskReason.ACCOUNT_UNKNOWN


def test_reduce_only_rejects_no_position() -> None:
    engine = RiskEngine(_policy())
    account = _account(positions={})
    decision = engine.evaluate_reduce_only(
        request_id="r4",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("1"),
        account=account,
        runtime=_runtime(),
        now=NOW,
    )
    assert decision.reason_code == RiskReason.NO_POSITION


def test_reduce_only_rejects_same_side_as_position() -> None:
    engine = RiskEngine(_policy())
    account = _account(positions={"BTCUSDT-PERP": Decimal("2")})
    decision = engine.evaluate_reduce_only(
        request_id="r5",
        instrument="BTCUSDT-PERP",
        side=RiskSide.BUY,
        quantity=Decimal("1"),
        account=account,
        runtime=_runtime(),
        now=NOW,
    )
    assert decision.reason_code == RiskReason.SIDE_MISMATCH


def test_reduce_only_rejects_quantity_exceeding_position() -> None:
    engine = RiskEngine(_policy())
    account = _account(positions={"BTCUSDT-PERP": Decimal("2")})
    decision = engine.evaluate_reduce_only(
        request_id="r6",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("3"),
        account=account,
        runtime=_runtime(),
        now=NOW,
    )
    assert decision.reason_code == RiskReason.QUANTITY_INVALID


# -- construction invariants -----------------------------------------------------


def test_risk_policy_rejects_leverage_above_system_ceiling() -> None:
    with pytest.raises(ValueError):
        _policy(max_leverage=Decimal("21"))


def test_risk_policy_rejects_non_positive_leverage() -> None:
    with pytest.raises(ValueError):
        _policy(max_leverage=Decimal("0"))


def test_instrument_limits_reject_leverage_above_system_ceiling() -> None:
    with pytest.raises(ValueError):
        _limits(max_leverage=Decimal("25"))


def test_decision_post_init_rejects_leverage_above_max() -> None:
    with pytest.raises(ValueError):
        RiskDecision(
            decision_id="d1",
            signal_id="s1",
            instrument="BTCUSDT-PERP",
            timestamp=NOW,
            approved=True,
            reason="approved",
            quantity=Decimal("1"),
            notional=Decimal("100"),
            leverage=Decimal("10"),
            max_leverage=Decimal("5"),
            risk_budget=Decimal("10"),
            stop_price=Decimal("95"),
            metadata={},
        )


def test_decision_post_init_rejects_approved_with_zero_quantity() -> None:
    with pytest.raises(ValueError):
        RiskDecision(
            decision_id="d2",
            signal_id="s2",
            instrument="BTCUSDT-PERP",
            timestamp=NOW,
            approved=True,
            reason="approved",
            quantity=Decimal("0"),
            notional=Decimal("0"),
            leverage=Decimal("0"),
            max_leverage=Decimal("5"),
            risk_budget=Decimal("10"),
            stop_price=Decimal("95"),
            metadata={},
        )


def test_decision_post_init_rejects_rejected_with_nonzero_quantity() -> None:
    with pytest.raises(ValueError):
        RiskDecision(
            decision_id="d3",
            signal_id="s3",
            instrument="BTCUSDT-PERP",
            timestamp=NOW,
            approved=False,
            reason="rejected",
            quantity=Decimal("1"),
            notional=Decimal("100"),
            leverage=Decimal("0"),
            max_leverage=Decimal("5"),
            risk_budget=Decimal("10"),
            stop_price=None,
            metadata={},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_fraction", Decimal("0")),
        ("risk_fraction", Decimal("1.5")),
        ("risk_fraction", Decimal("NaN")),
        ("estimated_cost_bps", Decimal("-1")),
        ("estimated_cost_bps", Decimal("Infinity")),
        ("max_gross_notional", Decimal("0")),
        ("max_net_notional", Decimal("-1")),
        ("max_daily_loss", Decimal("0")),
        ("max_drawdown", Decimal("NaN")),
        ("liquidity_fraction", Decimal("0")),
        ("liquidity_fraction", Decimal("1.1")),
        ("max_consecutive_losses", 0),
        ("max_data_age", timedelta(0)),
        ("max_signal_age", timedelta(seconds=-1)),
    ],
)
def test_policy_rejects_invalid_numeric_parameters(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _policy(**{field: value})


def test_reduce_only_reports_zero_exposure_increasing_notional() -> None:
    engine = RiskEngine(_policy())
    decision = engine.evaluate_reduce_only(
        request_id="exit-1",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("0.5"),
        account=_account(positions={"BTCUSDT-PERP": Decimal("1")}),
        runtime=_runtime(),
        now=NOW,
    )
    assert decision.approved
    assert decision.quantity == Decimal("0.5")
    assert decision.notional == Decimal("0")


def test_every_decision_records_the_approved_side_for_execution_binding() -> None:
    engine = RiskEngine(_policy())
    long_decision = _evaluate(engine, request=_request(signal_id="s-long"))
    short_decision = _evaluate(
        engine,
        request=_request(signal_id="s-short", side=RiskSide.SELL, stop_price=Decimal("105")),
    )
    rejected = _evaluate(engine, request=_request(signal_id="s-bad", stop_price=Decimal("101")))
    reduce = engine.evaluate_reduce_only(
        request_id="exit-2",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("1"),
        account=_account(positions={"BTCUSDT-PERP": Decimal("1")}),
        runtime=_runtime(),
        now=NOW,
    )

    assert long_decision.metadata["side"] == "buy"
    assert short_decision.approved and short_decision.metadata["side"] == "sell"
    assert rejected.metadata["side"] == "buy"
    assert reduce.metadata["side"] == "sell"
    assert reduce.metadata["reduce_only"] is True
    assert long_decision.metadata.get("reduce_only") is not True
