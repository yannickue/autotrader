# ruff: noqa: E501
"""Mt5DemoStack against the multi-symbol fake broker (real Nautilus kernel + MT5 lane threads)."""

from __future__ import annotations

import dataclasses
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from demo.execution.events import (
    Accepted,
    Fill,
    PositionClosed,
    ProtectionConfirmed,
    Rejected,
)
from demo.execution.live import ShadowGuardClient, ShadowModeViolation, StackConfig, _Reject
from demo.execution.stack_port import StackFailClosed
from nautilus_mt5.constants import Retcode
from tests.unit.demo.execution.stack_harness import (
    FAST,
    QUOTES,
    build_broker,
    inject_closed_trade,
    make_intent,
    make_stack,
)


@pytest.fixture
def env(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path)
    yield broker, stack
    stack.stop()


def kinds(events):
    return [type(e).__name__ for e in events]


def reason(events):
    return next(e.reason for e in events if isinstance(e, Rejected))


# -- happy path ---------------------------------------------------------------------------------


def test_demo_pass_start_submit_protect_and_stop_exit(env):
    broker, stack = env
    snap = stack.start()
    assert snap.is_demo and snap.reconciliation == "RECONCILED" and snap.connected
    assert snap.account_id_hash and str(broker.cfg.login) not in snap.account_id_hash
    events = stack.submit(make_intent())
    assert kinds(events) == ["Accepted", "Fill", "ProtectionConfirmed"], events
    accepted, fill, protection = events
    assert isinstance(accepted, Accepted) and isinstance(fill, Fill)
    assert accepted.equity == Decimal(10000) and accepted.leverage is not None
    assert Decimal("0.009") < accepted.risk_fraction <= Decimal("0.01")
    assert accepted.risk_budget == Decimal(100)
    assert fill.quantity == accepted.quantity and fill.price == Decimal("25001.5")
    assert fill.spread == Decimal("1.5") and fill.slippage == Decimal(0)
    assert isinstance(protection, ProtectionConfirmed) and protection.stop == Decimal(24950)
    assert broker.order_send_calls == 1
    request = broker.request_log[0]
    assert request["sl"] == 24950.0 and request["tp"] == 25150.0  # stop AND target broker-side
    assert stack.has_position("GER40") and stack.open_intents() == ("intent-1",)
    snap = stack.account_snapshot()
    assert snap.open_positions == 1 and snap.all_positions_protected
    broker.set_quote(24940.0, 24941.5)  # broker-side stop executes
    closed = [e for e in stack.poll_events() if isinstance(e, PositionClosed)]
    assert len(closed) == 1 and closed[0].exit_reason == "STOP"
    assert closed[0].profit_eur is not None and closed[0].profit_eur < 0
    assert closed[0].exit_price == Decimal(24950) and closed[0].exit_quantity == fill.quantity
    assert not stack.has_position("GER40") and stack.open_intents() == ()
    assert stack.poll_events() == []  # each closure is reported exactly once


def test_target_exit_and_no_target_means_no_broker_tp(env):
    broker, stack = env
    stack.start()
    stack.submit(make_intent(intent_id="a"))
    broker.set_quote(25160.0, 25161.5)
    (closed,) = [e for e in stack.poll_events() if isinstance(e, PositionClosed)]
    assert closed.exit_reason == "TARGET" and closed.profit_eur > 0
    broker.set_quote(*QUOTES["Ger40"])
    events = stack.submit(make_intent(intent_id="b", target=None))
    assert kinds(events) == ["Accepted", "Fill", "ProtectionConfirmed"]
    assert broker.request_log[-1]["tp"] == 0.0 and broker.request_log[-1]["sl"] == 24950.0


def test_all_mt5_io_runs_on_the_single_lane_thread(env):
    broker, stack = env
    stack.start()
    stack.submit(make_intent())
    stack.bar_source.latest_quote("NAS100")
    stack.poll_events()
    stack.account_snapshot()
    assert broker.max_concurrency == 1
    assert broker.all_call_threads() == stack._lane.stats.worker_thread_ids


@pytest.mark.parametrize(
    ("market", "symbol", "entry", "stop", "target"),
    [
        ("NAS100", "UsaTec", 21002.0, 20950.0, 21150.0),
        ("SPX500", "Usa500", 6001.0, 5980.0, 6050.0),
        ("XAUUSD", "GOLD", 4170.0, 4150.0, 4215.0),
        ("EURUSD", "EURUSD", 1.1701, 1.1650, 1.1800),
    ],
)
def test_every_market_sizes_by_contract_size_and_fx_and_carries_a_stop(
    env, market, symbol, entry, stop, target
):
    broker, stack = env
    stack.start()
    events = stack.submit(
        make_intent(market=market, broker_symbol=symbol, entry_ref=entry, stop=stop, target=target)
    )
    assert kinds(events) == ["Accepted", "Fill", "ProtectionConfirmed"], events
    accepted = events[0]
    assert accepted.risk_fraction <= Decimal("0.01") and accepted.leverage <= 30
    position = broker.positions_get(symbol=symbol)[0]
    assert position.sl == stop and position.magic == 740_003
    # the loss at the stop, in EUR, is within the 1 % budget (the broker computes profit exactly)
    broker.set_symbol_quote(symbol, stop - 0.0001, stop + 0.0001)
    (closed,) = [e for e in stack.poll_events() if isinstance(e, PositionClosed)]
    assert closed.exit_reason == "STOP"
    assert -Decimal(100) <= closed.profit_eur < 0


# -- identity / account gates -------------------------------------------------------------------------


def test_non_demo_account_is_refused_at_start_and_holds_no_lock(tmp_path):
    broker = build_broker(trade_mode=2)  # ACCOUNT_TRADE_MODE_REAL
    stack = make_stack(broker, tmp_path)
    with pytest.raises(StackFailClosed, match="non_demo_account"):
        stack.start()
    assert broker.order_send_calls == 0
    assert not (tmp_path / "terminal.lock").exists()


def test_unexpected_server_and_wrong_login_are_refused(tmp_path):
    broker = build_broker()
    broker.cfg.server = "SomeLive-Server"
    stack = make_stack(broker, tmp_path)
    stack.config_server = None
    with pytest.raises(StackFailClosed):
        # the connection expects the ORIGINAL server name
        from demo.execution.live import Mt5DemoStack
        from tests.unit.demo.execution.stack_harness import connection

        conn = connection(broker)
        object.__setattr__(conn, "server", "FakeBroker-Demo")
        Mt5DemoStack(
            client=broker, connection=conn, state_dir=tmp_path / "s2",
            lock_path=tmp_path / "l2", config=FAST,
        ).start()
    broker2 = build_broker()
    stack2 = make_stack(broker2, tmp_path / "x")
    broker2.cfg.login = 1  # terminal attached to another account than expected
    from tests.unit.demo.execution.stack_harness import connection as conn_of

    stack3 = make_stack(broker2, tmp_path / "y")
    stack3._connection = conn_of(build_broker())  # expects 900001
    with pytest.raises(StackFailClosed, match="connect_failed"):
        stack3.start()
    assert stack2 is not None


def test_login_is_refused_by_environment_and_by_config(tmp_path, monkeypatch):
    broker = build_broker()
    monkeypatch.setenv("MT5_ALLOW_ACCOUNT_LOGIN", "1")
    with pytest.raises(StackFailClosed, match="account_login_enabled"):
        make_stack(broker, tmp_path).start()
    monkeypatch.delenv("MT5_ALLOW_ACCOUNT_LOGIN")
    stack = make_stack(broker, tmp_path / "b")
    object.__setattr__(stack._connection, "allow_account_login", True)
    with pytest.raises(StackFailClosed, match="account_login_enabled"):
        stack.start()
    assert broker.initialize_calls == []  # nothing ever touched the terminal


def test_leverage_above_ceiling_and_foreign_currency_are_refused(tmp_path):
    with pytest.raises(StackFailClosed, match="broker_leverage_above_ceiling"):
        make_stack(build_broker(leverage=500), tmp_path).start()
    with pytest.raises(StackFailClosed, match="unsupported_account_currency"):
        make_stack(build_broker(currency="USD"), tmp_path / "b").start()


def test_unreconciled_book_rejects_new_exposure_fail_closed(env):
    broker, stack = env
    stack.start()
    broker.orders[9001] = SimpleNamespace(
        ticket=9001, symbol="Ger40", type=2, state=1, volume_initial=0.25, volume_current=0.25,
        price_open=24_000.0, price_current=25_000.0, sl=0.0, tp=0.0, time_setup=1,
        time_expiration=0, magic=0, position_id=0, comment="foreign", external_id="",
    )  # a working order nobody in this system placed: reconciliation cannot succeed
    with pytest.raises(StackFailClosed, match="not_reconciled"):
        stack.submit(make_intent())
    assert broker.order_send_calls == 0
    assert stack.account_snapshot().reconciliation == "MISMATCH"


def test_manual_trade_fails_closed_now_and_blocks_exposure_after_a_restart(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path)
    first.start()
    broker.external_market_fill(is_buy=True, volume=0.25, comment="manual desktop trade")
    with pytest.raises(StackFailClosed, match="not_reconciled"):
        first.submit(make_intent())  # external activity is flagged as a reconciliation mismatch
    first.stop()
    second = make_stack(broker, tmp_path)
    try:
        second.start()
        events = second.submit(make_intent())
        assert reason(events) in ("foreign_position_at_broker", "position_exists")
        assert broker.order_send_calls == 0 and len(broker.positions_get()) == 1  # never touched
    finally:
        second.stop()


# -- exactly once / idempotency ---------------------------------------------------------------------


def test_duplicate_intent_id_creates_exactly_one_order(env):
    broker, stack = env
    stack.start()
    first = stack.submit(make_intent())
    second = stack.submit(make_intent())
    assert second == first and broker.order_send_calls == 1
    assert len(broker.positions_get()) == 1


def test_duplicate_intent_is_rejected_after_a_restart_too(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path)
    first.start()
    first.submit(make_intent())
    first.stop()
    second = make_stack(broker, tmp_path)
    try:
        second.start()
        events = second.submit(make_intent())
        assert reason(events) == "duplicate_intent" and broker.order_send_calls == 1
    finally:
        second.stop()


def test_client_order_id_matches_the_recorder_store_rule():
    from demo.execution.strategy import client_order_id_for
    from demo.store import client_order_id_for as store_rule

    assert client_order_id_for("intent-42") == store_rule("intent-42")


# -- protection -----------------------------------------------------------------------------------------


def test_missing_broker_stop_flattens_immediately_and_halts_new_exposure(env):
    broker, stack = env
    stack.start()
    broker.strip_stops_on_entry = True  # broker forgets the attached SL/TP
    events = stack.submit(make_intent())
    assert kinds(events) == ["Accepted", "Fill", "PositionClosed", "Rejected"], events
    assert reason(events) == "protection_unconfirmed"
    assert broker.positions_get() == ()  # flattened by a reduce-only order
    assert broker.request_log[-1].get("position") and "sl" not in broker.request_log[-1]
    closed = events[2]
    assert closed.exit_reason == "MANUAL"
    assert stack.account_snapshot().kill_switch
    broker.strip_stops_on_entry = False
    again = stack.submit(make_intent(intent_id="next"))
    assert reason(again) == "halted" and broker.order_send_calls == 2


def test_unprotected_position_of_ours_is_repaired_or_flattened_on_restart(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path)
    first.start()
    first.submit(make_intent())
    first.stop()
    (position,) = broker.positions_get()
    position.sl = 0.0  # someone removed the stop while we were down
    second = make_stack(broker, tmp_path)
    try:
        second.start()
        assert broker.positions_get()[0].sl == 24950.0  # re-protected from the registry stop
        assert any(isinstance(e, ProtectionConfirmed) for e in second.poll_events())
        assert second.account_snapshot().all_positions_protected
    finally:
        second.stop()


def test_stop_removed_at_runtime_is_restored_by_the_watchdog(env):
    broker, stack = env
    stack.start()
    stack.submit(make_intent())
    broker.positions_get()[0].sl = 0.0
    events = stack.poll_events()
    assert any(isinstance(e, ProtectionConfirmed) for e in events)
    assert broker.positions_get()[0].sl == 24950.0


# -- broker outcomes ----------------------------------------------------------------------------------------


def test_broker_reject_leaves_nothing_open(env):
    broker, stack = env
    stack.start()
    broker.send_retcode_override = [int(Retcode.REJECT)]
    events = stack.submit(make_intent())
    assert kinds(events) == ["Accepted", "Rejected"] and reason(events) == "broker_reject"
    assert broker.positions_get() == () and stack.open_intents() == ()


# no sync loop racing the assertions: the unknown-outcome path must not depend on scheduling
QUIET = dataclasses.replace(FAST, sync_interval_s=3600.0, submit_wait_s=1.0)


@pytest.fixture
def quiet_env(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=QUIET)
    yield broker, stack
    stack.stop()


@pytest.mark.parametrize("repeat", range(int(os.environ.get("DEMO_REPEAT", "2"))))
def test_exposure_changing_request_is_never_retried_when_the_outcome_is_unknown(
    tmp_path, repeat
):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=QUIET)
    try:
        stack.start()
        broker.raise_on_send = [RuntimeError("IPC died")]
        events = stack.submit(make_intent())
        assert reason(events) == "order_outcome_unknown"
        assert events[-1].risk_detail is not None
        assert broker.order_send_calls == 1  # no automatic retry
        assert stack.account_snapshot().kill_switch
        assert stack._registry.get("intent-1").status == "IN_DOUBT"
        assert reason(stack.submit(make_intent(intent_id="n2"))) == "halted"
        assert broker.order_send_calls == 1
    finally:
        stack.stop()


@pytest.mark.parametrize("failure", ["disconnect", "soft_reject", "degraded_session"])
def test_a_failing_confirmation_never_raises_past_submit_and_stays_in_doubt(quiet_env, failure):
    """H3: a disconnect / lane error while confirming an unknown send is order_outcome_unknown."""
    broker, stack = quiet_env
    stack.start()
    broker.raise_on_send = [RuntimeError("IPC died")]
    if failure == "disconnect":
        def boom(*a, **k):
            raise StackFailClosed("broker_disconnect:positions_get")
    elif failure == "soft_reject":
        def boom(*a, **k):
            raise _Reject("broker_call_failed")
    else:
        boom = None
    real = stack._lane_confirm_entry
    if boom is not None:
        stack._lane_confirm_entry = boom
    try:
        events = stack.submit(make_intent())
    finally:
        stack._lane_confirm_entry = real
    assert kinds(events) == ["Accepted", "Rejected"], events
    assert reason(events) == "order_outcome_unknown"
    assert events[-1].risk_detail["reject_code"] == "order_outcome_unknown"
    assert broker.order_send_calls == 1
    assert stack._registry.get("intent-1").status == "IN_DOUBT"
    assert stack.halted_reason == "order_outcome_unknown"


def test_unknown_outcome_is_recorded_and_halted_before_the_broker_is_read(quiet_env):
    broker, stack = quiet_env
    stack.start()
    broker.raise_on_send = [RuntimeError("IPC died")]
    seen = {}
    real = stack._lane_confirm_entry

    def spy(*args, **kwargs):
        seen["row"] = stack._registry.get("intent-1").status
        seen["halt"] = stack.halted_reason
        return real(*args, **kwargs)

    stack._lane_confirm_entry = spy
    stack.submit(make_intent())
    assert seen == {"row": "IN_DOUBT", "halt": "order_outcome_unknown"}


def test_partial_fill_reports_the_actual_quantity_and_protects_it(env):
    broker, stack = env
    stack.start()
    # the sized quantity is 1.50 lots; the broker fills 0.5 and cancels the IOC remainder
    broker.fill_plan = [[(0.5, 25001.5)]]
    events = stack.submit(make_intent())
    assert kinds(events) == ["Accepted", "Fill", "ProtectionConfirmed"]
    assert events[1].quantity == Decimal("0.5") < events[0].quantity
    assert broker.positions_get()[0].volume == 0.5 and broker.positions_get()[0].sl == 24950.0


# -- parity re-validation -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "quote", "expected"),
    [
        ({"valid_s": -5}, None, "stale_signal"),
        ({}, (25010.0, 25011.5), "entry_overshoot"),
        ({}, (24940.0, 24941.0), "structural_invalidation_crossed"),
        ({"target": 25010.0, "entry_ref": 25012.0}, (25011.0, 25012.0), "target_crossed_at_fill"),
        ({"min_space_r": 6.0}, None, "min_space_r"),
        ({}, (25000.0, 25012.5), "spread_cap"),
        ({"flat_in_s": -1}, None, "past_forced_flat"),
        ({"broker_symbol": "Wrong"}, None, "symbol_mismatch"),
        ({"market": "FOO"}, None, "unknown_market"),
    ],
)
def test_stale_or_moved_intents_are_rejected_before_any_order(env, kw, quote, expected):
    broker, stack = env
    stack.start()
    if quote:
        broker.set_quote(*quote)
    events = stack.submit(make_intent(**kw))
    assert kinds(events) == ["Rejected"] and reason(events) == expected
    assert broker.order_send_calls == 0


def test_short_uses_the_bid_for_the_executable_price(env):
    broker, stack = env
    stack.start()
    events = stack.submit(
        make_intent(direction=-1, entry_ref=24999.0, stop=25050.0, target=24850.0)
    )
    assert kinds(events) == ["Accepted", "Fill", "ProtectionConfirmed"]
    assert events[1].price == Decimal(25000)
    assert broker.request_log[0]["type"] == 1


def test_daily_loss_stop_halts_new_exposure(env):
    broker, stack = env
    inject_closed_trade(broker, profit=-650.0)  # start of day equity 10 650 -> -6.1 %
    stack.start()
    assert reason(stack.submit(make_intent())) == "daily_loss_limit"
    assert broker.order_send_calls == 0


def test_eight_consecutive_losses_halt_and_a_win_resets_the_streak(tmp_path):
    broker = build_broker()
    for _ in range(8):
        inject_closed_trade(broker, profit=-1.0)
    stack = make_stack(broker, tmp_path)
    try:
        stack.start()
        assert reason(stack.submit(make_intent())) == "consecutive_loss_limit"
    finally:
        stack.stop()
    broker2 = build_broker()
    for _ in range(7):
        inject_closed_trade(broker2, profit=-1.0)
    stack2 = make_stack(broker2, tmp_path / "b")
    try:
        stack2.start()
        assert kinds(stack2.submit(make_intent()))[-1] == "ProtectionConfirmed"
    finally:
        stack2.stop()


def test_manual_and_previous_day_losses_never_block_new_exposure(tmp_path):
    """M3: the streak counts OUR OWN trades closed in the CURRENT UTC day only."""
    broker = build_broker()
    for _ in range(8):
        inject_closed_trade(broker, profit=-0.1, magic=0)  # manual trades: not ours
    for _ in range(8):
        inject_closed_trade(broker, profit=-0.1, age_s=3 * 86_400)  # ours, but days ago
    stack = make_stack(broker, tmp_path)
    try:
        stack.start()
        assert stack.submit(make_intent())[-1].__class__.__name__ == "ProtectionConfirmed"
    finally:
        stack.stop()


def test_foreign_win_neither_breaks_nor_extends_our_streak(tmp_path):
    broker = build_broker()
    for i in range(8):
        inject_closed_trade(broker, profit=-0.1)
        if i == 3:
            inject_closed_trade(broker, profit=50.0, magic=0)  # a manual win in between
    stack = make_stack(broker, tmp_path)
    try:
        stack.start()
        assert reason(stack.submit(make_intent())) == "consecutive_loss_limit"
    finally:
        stack.stop()


def test_max_drawdown_halt_is_25_percent_of_peak_equity(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path)
    try:
        stack.start()
        stack._registry.set_meta("peak_equity", "20000")  # equity 10 000 = 50 % below the peak
        assert reason(stack.submit(make_intent(intent_id="dd"))) == "drawdown_limit"
        stack._registry.set_meta("peak_equity", "12000")  # -16.7 %: still trading
        assert kinds(stack.submit(make_intent(intent_id="ok")))[-1] == "ProtectionConfirmed"
    finally:
        stack.stop()


# -- lifecycle: restart, forced flat, disconnect --------------------------------------------------------------------


def test_restart_readopts_the_open_position_without_duplicating_it(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path)
    first.start()
    first.submit(make_intent())
    first.stop()  # process dies with the position open
    assert len(broker.positions_get()) == 1
    second = make_stack(broker, tmp_path)
    try:
        snap = second.start()
        assert snap.reconciliation == "RECONCILED" and snap.open_positions == 1
        assert second.open_intents() == ("intent-1",) and second.has_position("GER40")
        assert broker.order_send_calls == 1  # nothing re-sent
        assert reason(second.submit(make_intent())) == "duplicate_intent"
        broker.set_quote(24940.0, 24941.5)  # the adopted position's stop is hit
        (closed,) = [e for e in second.poll_events() if isinstance(e, PositionClosed)]
        assert closed.intent_id == "intent-1" and closed.exit_reason == "STOP"
        assert second.open_intents() == ()
    finally:
        second.stop()


def test_position_closed_while_down_is_reported_after_restart(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path)
    first.start()
    first.submit(make_intent())
    first.stop()
    broker.set_quote(25160.0, 25161.5)  # target hit while the process was down
    second = make_stack(broker, tmp_path)
    try:
        second.start()
        (closed,) = [e for e in second.poll_events() if isinstance(e, PositionClosed)]
        assert closed.exit_reason == "TARGET" and closed.intent_id == "intent-1"
    finally:
        second.stop()


def test_forced_flat_closes_reduce_only_at_the_intent_deadline(env):
    broker, stack = env
    stack.start()
    stack.submit(make_intent(flat_in_s=600))
    assert stack.on_clock(datetime.now(UTC) + timedelta(seconds=300)) == []
    assert len(broker.positions_get()) == 1
    (closed,) = stack.on_clock(datetime.now(UTC) + timedelta(seconds=601))
    assert isinstance(closed, PositionClosed) and closed.exit_reason == "SESSION_END"
    assert closed.exit_price is not None and closed.profit_eur is not None
    assert broker.positions_get() == () and "position" in broker.request_log[-1]
    assert stack.open_intents() == ()


def test_broker_disconnect_fails_closed(env):
    broker, stack = env
    stack.start()
    broker.disconnect()
    with pytest.raises(StackFailClosed, match="broker_disconnect"):
        stack.submit(make_intent())
    stack.poll_events()  # inside the grace period nothing raises
    import time as _time

    _time.sleep(FAST.disconnect_grace_s + 0.3)
    with pytest.raises(StackFailClosed, match="broker_disconnect"):
        stack.poll_events()
    assert not stack.account_snapshot().connected


def test_stale_feed_and_clock_anomaly(env):
    broker, stack = env
    stack.start()
    broker.live_offset_s -= 45  # quote 45 s old
    assert reason(stack.submit(make_intent(intent_id="old"))) == "stale_feed"
    broker.live_offset_s -= 300  # 6 minutes old: the feed is dead
    with pytest.raises(StackFailClosed, match="stale_feed"):
        stack.submit(make_intent(intent_id="dead"))
    broker.live_offset_s += 345  # back to a live feed
    assert kinds(stack.submit(make_intent(intent_id="ok")))[-1] == "ProtectionConfirmed"


def test_small_clock_skew_rejects_that_market_temporarily_and_recovers(tmp_path):
    """M4: a PC clock 6-30 s slow used to kill the whole stack; now it is a metric + a reject."""
    broker = build_broker()
    stack = make_stack(broker, tmp_path)
    try:
        stack.start()
        broker.live_offset_s += 20  # quote 20 s in the future
        assert reason(stack.submit(make_intent(intent_id="skewed"))) == "clock_skew"
        assert broker.order_send_calls == 0 and stack.max_clock_skew_s >= 19
        stack.poll_events()  # the stack is NOT latched
        broker.live_offset_s -= 20
        assert kinds(stack.submit(make_intent(intent_id="fine")))[-1] == "ProtectionConfirmed"
    finally:
        stack.stop()


def test_only_a_sustained_large_clock_skew_is_fatal(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path)
    try:
        stack.start()
        broker.live_offset_s += 120  # two minutes ahead
        n = FAST.clock_skew_sustain_obs
        for i in range(n - 1):  # not yet sustained: temporary rejects
            assert reason(stack.submit(make_intent(intent_id=f"s{i}"))) == "clock_skew"
        with pytest.raises(StackFailClosed, match="clock_anomaly"):
            stack.submit(make_intent(intent_id="sustained"))
        with pytest.raises(StackFailClosed):
            stack.poll_events()  # the anomaly latched the stack
        assert broker.order_send_calls == 0
    finally:
        stack.stop()


def test_a_single_large_skew_observation_is_not_fatal(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path)
    try:
        stack.start()
        broker.live_offset_s += 120
        assert reason(stack.submit(make_intent(intent_id="blip"))) == "clock_skew"
        broker.live_offset_s -= 120
        assert kinds(stack.submit(make_intent(intent_id="ok")))[-1] == "ProtectionConfirmed"
        broker.live_offset_s += 120  # a blip again: the window was reset by the healthy observation
        assert reason(stack.submit(make_intent(intent_id="blip2"))) == "clock_skew"
    finally:
        stack.stop()


# -- shadow mode ---------------------------------------------------------------------------------------------------


def test_shadow_mode_never_reaches_order_send(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, dry_run=True)
    try:
        snap = stack.start()
        assert snap.extra["shadow"] is True
        events = stack.submit(make_intent())
        assert kinds(events) == ["Accepted"]
        assert broker.order_send_calls == 0 and broker.request_log == []
        assert any(c == "order_check" for c in broker.calls)  # order_check IS allowed
        assert broker.positions_get() == ()
        assert stack._registry.get("intent-1").status == "SHADOW"
    finally:
        stack.stop()


def test_shadow_guard_is_a_hard_wall_even_if_something_tries_to_send(tmp_path):
    broker = build_broker()
    guard = ShadowGuardClient(broker)
    with pytest.raises(ShadowModeViolation):
        guard.order_send({"action": 1})
    assert broker.order_send_calls == 0 and guard.blocked_sends == 1
    assert guard.symbol_info("Ger40") is not None  # reads pass through


# -- terminal lock -------------------------------------------------------------------------------------------------


def test_terminal_lock_is_single_owner_heartbeated_and_released(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path)
    first.start()
    lock = tmp_path / "terminal.lock"
    assert lock.exists()
    second = make_stack(build_broker(), tmp_path / "other")
    second._lock_path = lock
    with pytest.raises(StackFailClosed, match="connect_failed"):
        second.start()
    import os
    import time as _time

    old = _time.time() - 80  # 10 s from being considered stale
    os.utime(lock, (old, old))
    _time.sleep(0.6)  # the heartbeat (0.2 s) refreshes it
    assert _time.time() - lock.stat().st_mtime < 5
    first.stop()
    assert not lock.exists()


def test_config_defaults_document_the_thresholds():
    cfg = StackConfig()
    assert cfg.max_quote_age_s == 30.0 and cfg.lock_heartbeat_s < 90.0 / 2
    assert cfg.magic == 740_003


# -- additional safety properties ----------------------------------------------------------------------------------


def test_stop_inside_the_broker_stop_level_is_refused_locally(env):
    broker, stack = env
    stack.start()
    # broker stops level = 100 points x 0.01 = 1.0: a stop 0.5 below the bid is not placeable
    events = stack.submit(make_intent(stop=24999.5, target=25150.0))
    assert kinds(events) == ["Accepted", "Rejected"]
    assert reason(events) == "stop_inside_broker_stop_level"
    assert broker.order_send_calls == 0 and broker.positions_get() == ()


def test_restarted_process_can_still_flatten_an_adopted_position_through_nautilus(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path)
    first.start()
    first.submit(make_intent(flat_in_s=600))
    first.stop()
    second = make_stack(broker, tmp_path)
    try:
        second.start()
        (closed,) = second.on_clock(datetime.now(UTC) + timedelta(seconds=700))
        assert closed.exit_reason == "SESSION_END" and closed.intent_id == "intent-1"
        assert broker.positions_get() == () and "position" in broker.request_log[-1]
    finally:
        second.stop()


def test_stop_hit_before_forced_flat_is_reported_once_whichever_path_sees_it_first(env):
    broker, stack = env
    stack.start()
    stack.submit(make_intent(flat_in_s=600))
    broker.set_quote(24940.0, 24941.5)
    (closed,) = stack.on_clock(datetime.now(UTC) + timedelta(seconds=700))
    assert closed.exit_reason == "STOP"
    assert [e for e in stack.poll_events() if isinstance(e, PositionClosed)] == []


def test_losing_the_terminal_lock_is_fatal(env, tmp_path):
    _broker, stack = env
    stack.start()
    (tmp_path / "terminal.lock").write_text("424242")  # another process now owns the lock file
    import time as _time

    _time.sleep(0.6)  # heartbeat interval 0.2 s
    with pytest.raises(StackFailClosed, match="terminal_lock_lost"):
        stack.submit(make_intent())


def test_a_hung_mt5_call_fails_closed_instead_of_blocking_the_runner(tmp_path):
    broker = build_broker()
    from dataclasses import replace

    stack = make_stack(broker, tmp_path, config=replace(FAST, lane_call_timeout_s=0.5))
    try:
        stack.start()
        broker.call_latency_s = 1.2  # every MT5 call now blocks longer than the bound
        with pytest.raises(StackFailClosed, match="mt5_lane_timeout"):
            stack.submit(make_intent())
        broker.call_latency_s = 0.0
        assert broker.order_send_calls == 0
        with pytest.raises(StackFailClosed):
            stack.poll_events()  # latched
    finally:
        stack.stop()


def test_threads_are_gone_after_stop(tmp_path):
    import threading

    broker = build_broker()
    stack = make_stack(broker, tmp_path)
    stack.start()
    stack.submit(make_intent())
    stack.stop()
    stack.stop()  # idempotent
    alive = [t.name for t in threading.enumerate() if t.name.startswith(("demo-", "mt5-lane"))]
    assert alive == [] or all(not t.is_alive() for t in threading.enumerate() if t.name in alive)
    assert not (tmp_path / "terminal.lock").exists()
    with pytest.raises(StackFailClosed):
        stack.submit(make_intent(intent_id="after-stop"))


def test_rows_that_never_reached_the_broker_do_not_block_their_market_after_a_crash(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path)
    first.start()
    first._registry.insert(
        intent_id="crashed", client_order_id="dt-crashed", market="GER40", direction=1,
        stop="24950", target=None, forced_flat_utc=None, status="ACCEPTED",
        created_utc=datetime.now(UTC).isoformat(),
    )
    assert first.has_position("GER40")  # an in-flight intent counts while the process lives
    first.stop()
    second = make_stack(broker, tmp_path)
    try:
        second.start()
        assert not second.has_position("GER40") and second.open_intents() == ()
        assert kinds(second.submit(make_intent(intent_id="fresh")))[-1] == "ProtectionConfirmed"
    finally:
        second.stop()


def test_flatten_of_our_unprotected_position_survives_a_reconciliation_mismatch(env):
    """H2 audit scenario: SL lost, price beyond the structural stop, emergency_protect refused
    (INVALID_STOPS), reconciliation MISMATCH: the position must still be flattened, not stay open."""
    broker, stack = env
    stack.start()
    stack.submit(make_intent())
    (position,) = broker.positions_get()
    position.sl = 0.0
    broker.set_quote(24940.0, 24941.0)  # beyond the 24950 stop: a stop can no longer be placed
    from risk.models import ReconciliationState

    recon = stack._adapter.exec_client.recon
    real_reconcile = stack._adapter.exec_client.reconcile

    def failing_reconcile():
        recon.state = ReconciliationState.MISMATCH
        return recon

    stack._adapter.exec_client.reconcile = failing_reconcile
    recon.state = ReconciliationState.MISMATCH
    try:
        events = stack.poll_events()
    finally:
        stack._adapter.exec_client.reconcile = real_reconcile
    assert broker.positions_get() == (), events
    assert any(isinstance(e, PositionClosed) for e in events)


# -- M6: bounded retry of lane READS (never of writes) -------------------------------------------


def _flaky_positions(broker, failures: int):
    """positions_get returns None (=> Mt5CallError) for the next ``failures`` calls."""
    real = broker.positions_get
    state = {"left": failures, "calls": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["left"] > 0:
            state["left"] -= 1
            return None
        return real(*args, **kwargs)

    broker.positions_get = flaky
    return state


def test_a_transient_read_failure_is_retried_and_does_not_fail_the_runner(quiet_env):
    broker, stack = quiet_env
    stack.start()
    state = _flaky_positions(broker, 2)
    assert stack.has_position("GER40") is False  # no _Reject / StackFailClosed
    assert state["calls"] == 3


def test_a_persistent_read_failure_fails_closed_after_the_bound(quiet_env):
    broker, stack = quiet_env
    stack.start()
    state = _flaky_positions(broker, 99)
    with pytest.raises(StackFailClosed, match="broker_call_failed_persistent"):
        stack.has_position("GER40")
    assert state["calls"] == QUIET.read_retry_attempts


def test_forced_flat_clock_survives_a_transient_read_failure(quiet_env):
    broker, stack = quiet_env
    stack.start()
    stack.submit(make_intent(flat_in_s=3600))
    state = _flaky_positions(broker, 1)
    events = stack.on_clock(datetime.now(UTC) + timedelta(hours=2))
    assert state["calls"] >= 2
    assert any(isinstance(e, PositionClosed) for e in events)
    assert broker.positions_get() == ()


def test_writes_are_never_retried_by_the_lane_helper(quiet_env):
    from nautilus_mt5.session import Mt5CallError

    _, stack = quiet_env
    stack.start()
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        raise Mt5CallError("positions_get", None)

    with pytest.raises(_Reject):
        stack._on_lane(failing)
    assert calls["n"] == QUIET.read_retry_attempts
    calls["n"] = 0
    with pytest.raises(_Reject):
        stack._on_lane(failing, retry_reads=False)
    assert calls["n"] == 1
