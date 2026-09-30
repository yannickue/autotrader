# ruff: noqa: E501
"""Lane E1: ExitEngine wired into the DEMO stack (``exit_policy="staged"``) against the fake broker.

Real Nautilus kernel + MT5 lane threads, zero real MT5. GER40 in the harness: min lot/step 0.25, the
default intent sizes 1.75 lots, entry ask 25001.5, stop 24950 (1R = 51.5), price = BID for a long.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from nautilus_trader.model.enums import OrderType

from demo.execution import exit_manager as em
from demo.execution.events import PositionClosed
from demo.execution.live import StackConfig
from exits.models import ExitPolicy
from nautilus_mt5.constants import Retcode
from tests.unit.demo.execution.stack_harness import (
    FAST,
    QUOTES,
    build_broker,
    make_intent,
    make_stack,
)

D = Decimal


def policy(**over) -> ExitPolicy:
    values = dict(
        policy_id="lane-e-test",
        breakeven_trigger_r_multiple=D("99"),  # off unless a test turns it on
        trailing_activation_r_multiple=D("99"),
        trailing_distance_volatility_multiplier=D("1"),
        max_market_data_age=timedelta(seconds=30),
    )
    values.update(over)
    return ExitPolicy(**values)


def staged_cfg(**over) -> StackConfig:
    pol = over.pop("pol", None) or policy()
    return dataclasses.replace(FAST, exit_policy="staged", staged_exit=pol, **over)


PLAN_TP1_RUNNER = {
    "stages": [
        {"target_price": "25060", "close_fraction": "0.4", "stage_id": "tp1", "source": "STRUCTURE:pdh"},
    ]
}
PLAN_TP1_TP2 = {
    "stages": [
        {"target_price": "25060", "close_fraction": "0.4", "stage_id": "tp1", "source": "STRUCTURE:pdh"},
        {"target_price": "25120", "close_fraction": "0.3", "stage_id": "tp2", "source": "STRUCTURE:r1"},
    ]
}


@pytest.fixture
def env(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=staged_cfg())
    yield broker, stack
    stack.stop()


def open_position(stack, plan=PLAN_TP1_TP2, **intent):
    events = stack.submit(make_intent(**intent), {"family": "fam-a", "atr": "20", "exit_plan": plan})
    assert [type(e).__name__ for e in events] == ["Accepted", "Fill", "ProtectionConfirmed"], events
    return events


def triple(stack, broker):
    """(broker volume, nautilus position qty, stop-child qty) - must always be equal."""
    (pos,) = broker.positions_get()
    strategy = stack._strategy
    cache = strategy.cache
    local = sum((abs(Decimal(str(p.signed_qty))) for p in cache.positions_open()), D(0))
    stops = [o for o in cache.orders_open() if o.order_type == OrderType.STOP_MARKET]
    assert len(stops) == 1
    return D(str(pos.volume)), local, D(str(stops[0].quantity))


def wait_sync(stack):
    stack.poll_events()  # ingest broker deals -> Nautilus truth (and protective resize)


def now_of(stack):
    return stack._now()


# -- default: fixed_1_5r stays bit-identical ---------------------------------------------------------------


def test_default_policy_is_fixed_and_manage_exits_is_a_noop(tmp_path):
    assert StackConfig().exit_policy == "fixed_1_5r" == em.EXIT_POLICY_FIXED
    broker = build_broker()
    stack = make_stack(broker, tmp_path)  # default config
    try:
        stack.start()
        stack.submit(make_intent(), {"family": "fam-a", "atr": "20", "exit_plan": PLAN_TP1_TP2})
        sends = broker.order_send_calls
        assert broker.request_log[0]["tp"] == 25150.0  # the intent's fixed-R target, plan ignored
        broker.set_quote(25125.0, 25126.5)  # far beyond every stage
        assert stack.manage_exits(now_of(stack)) == []
        assert stack.exit_log() == []
        assert broker.order_send_calls == sends
        assert broker.positions_get()[0].volume == 1.75
    finally:
        stack.stop()


def test_config_validation():
    with pytest.raises(ValueError, match="exit_policy"):
        StackConfig(exit_policy="bogus")
    with pytest.raises(ValueError, match="staged_exit"):
        StackConfig(exit_policy="staged")
    assert StackConfig(exit_policy="staged", staged_exit=policy()).exit_policy == "staged"


# -- broker TP under staged ------------------------------------------------------------------------------------


def test_staged_broker_tp_is_absent_when_a_runner_remains(env):
    broker, stack = env
    stack.start()
    open_position(stack, PLAN_TP1_RUNNER)
    assert broker.request_log[0]["tp"] == 0.0 and broker.request_log[0]["sl"] == 24950.0
    assert stack._registry.get("intent-1").target is None


def test_staged_broker_tp_is_the_final_stage_only(env):
    broker, stack = env
    stack.start()
    plan = {"stages": [
        {"target_price": "25060", "close_fraction": "0.5", "stage_id": "tp1", "source": "STRUCTURE:a"},
        {"target_price": "25120", "close_fraction": "0.5", "stage_id": "tp2", "source": "STRUCTURE:b"},
    ]}
    open_position(stack, plan)
    assert broker.request_log[0]["tp"] == 25120.0  # final stage, NOT the intent's 25150 fixed-R target
    assert stack._registry.get("intent-1").target == "25120"


def test_broker_target_helper_rules():
    f = em.broker_target_for_staged
    assert f(None, direction=1, entry_ref=D("100")) is None
    assert f({"stages": [{"r_multiple": "2", "close_fraction": "1"}]}, direction=1, entry_ref=D("100")) is None
    wrong_side = {"stages": [{"target_price": "99", "close_fraction": "1", "source": "STRUCTURE:x"}]}
    assert f(wrong_side, direction=1, entry_ref=D("100")) is None
    assert f({"stages": "garbage"}, direction=1, entry_ref=D("100")) is None


# -- partial exit -------------------------------------------------------------------------------------------------


def test_tp1_takes_a_partial_keeps_the_stop_and_invariant_holds(env):
    broker, stack = env
    stack.start()
    open_position(stack)
    assert triple(stack, broker) == (D("1.75"),) * 3
    broker.set_quote(25059.0, 25060.5)
    assert stack.manage_exits(now_of(stack)) == []  # just below TP1 (bid 25059 < 25060)
    assert broker.positions_get()[0].volume == 1.75
    broker.set_quote(25060.5, 25062.0)
    assert stack.manage_exits(now_of(stack)) == []  # a partial is not a closure
    (pos,) = broker.positions_get()
    assert pos.volume == 1.25 and pos.sl == 24950.0  # 0.4 * 1.75 = 0.70 -> 0.50 lots (step 0.25)
    wait_sync(stack)
    assert triple(stack, broker) == (D("1.25"),) * 3  # broker == local == stop-protected
    (partial,) = [e for e in stack.exit_log() if e["kind"] == "partial_exit"]
    assert partial["quantity_before"] == "1.75" and partial["quantity_reduced"] == "0.50"
    assert partial["quantity_remaining"] == "1.25" and partial["tranche_id"] == "intent-1"
    assert partial["strategy_family"] == "fam-a" and partial["stage_id"] == "tp1"
    assert partial["realized_r_of_position"] is not None and partial["remaining_risk_r"] is not None
    # persisted only after the verified fill

    assert json.loads(stack._registry.get("intent-1").context)["exit_state"]["stages_completed"] == 1
    # the ExitEngine was told the terminal outcome (release-equivalent)
    assert stack._exit_manager.released


def test_a_stage_never_fires_twice_and_tp2_takes_its_own_share(env):
    broker, stack = env
    stack.start()
    open_position(stack)
    broker.set_quote(25060.5, 25062.0)
    stack.manage_exits(now_of(stack))
    wait_sync(stack)
    for _ in range(3):  # repeated re-evaluation each cycle: no second TP1
        stack.manage_exits(now_of(stack))
    assert broker.positions_get()[0].volume == 1.25
    broker.set_quote(25120.5, 25122.0)
    stack.manage_exits(now_of(stack))
    wait_sync(stack)
    assert broker.positions_get()[0].volume == 0.75  # 0.3 * 1.75 = 0.525 -> 0.50 of the ORIGINAL
    assert triple(stack, broker) == (D("0.75"),) * 3
    assert broker.positions_get()[0].sl == 24950.0
    for _ in range(3):
        stack.manage_exits(now_of(stack))
    assert broker.positions_get()[0].volume == 0.75  # TP1+TP2 done, runner stays


def test_restart_after_partial_does_not_refire_the_stage(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path, config=staged_cfg())
    first.start()
    open_position(first)
    broker.set_quote(25060.5, 25062.0)
    first.manage_exits(now_of(first))
    first.stop()
    second = make_stack(broker, tmp_path, config=staged_cfg())
    try:
        snap = second.start()
        assert snap.reconciliation == "RECONCILED" and snap.open_positions == 1
        sends = broker.order_send_calls
        second.manage_exits(now_of(second))
        assert broker.order_send_calls == sends and broker.positions_get()[0].volume == 1.25
        assert broker.positions_get()[0].sl == 24950.0  # broker stop still in force after restart
    finally:
        second.stop()


def test_stop_move_after_restart_uses_the_broker_verified_protect_path(tmp_path):
    """Adopted position has no local stop child: the tighten goes through emergency_protect."""
    broker = build_broker()
    pol = policy(breakeven_trigger_r_multiple=D("1"))
    first = make_stack(broker, tmp_path, config=staged_cfg(pol=pol))
    first.start()
    open_position(first, PLAN_TP1_RUNNER)
    first.stop()
    second = make_stack(broker, tmp_path, config=staged_cfg(pol=pol))
    try:
        second.start()
        broker.set_quote(25055.0, 25056.5)
        second.manage_exits(now_of(second))
        assert broker.positions_get()[0].sl > 24950.0 and broker.positions_get()[0].volume == 1.75
        assert [e for e in second.exit_log() if e["kind"] == "stop_moved"]
        broker.set_quote(25010.0, 25011.5)
        moved = broker.positions_get()[0].sl
        second.manage_exits(now_of(second))
        assert broker.positions_get()[0].sl == moved  # never loosened
    finally:
        second.stop()


def test_realized_volume_lower_bounds_stage_progress_even_if_state_was_lost(tmp_path):
    """Crash between fill and persist: the stage is derived from volume, never re-fired."""
    broker = build_broker()
    first = make_stack(broker, tmp_path, config=staged_cfg())
    first.start()
    open_position(first)
    broker.set_quote(25060.5, 25062.0)
    first.manage_exits(now_of(first))
    row = first._registry.get("intent-1")

    ctx = json.loads(row.context)
    ctx.pop("exit_state")  # simulate the lost persist
    first._registry.update("intent-1", context=json.dumps(ctx))
    sends = broker.order_send_calls
    first.manage_exits(now_of(first))
    assert broker.order_send_calls == sends and broker.positions_get()[0].volume == 1.25
    first.stop()


# -- reduce failures ------------------------------------------------------------------------------------------------


def test_rejected_reduce_changes_nothing_and_keeps_protection(env):
    broker, stack = env
    stack.start()
    open_position(stack)
    broker.set_quote(25060.5, 25062.0)
    broker.send_retcode_override = [int(Retcode.REJECT)]
    stack.manage_exits(now_of(stack))
    (pos,) = broker.positions_get()
    assert pos.volume == 1.75 and pos.sl == 24950.0
    assert [e for e in stack.exit_log() if e["kind"] == "reduce_refused"]
    assert stack._exit_manager.counters["partials"] == 0
    stack.manage_exits(now_of(stack))  # the next cycle retries from broker truth
    assert broker.positions_get()[0].volume == 1.25


def test_reduce_quantity_below_min_lot_is_skipped_not_sent(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=staged_cfg())
    stack.start()
    plan = {"stages": [{"target_price": "25060", "close_fraction": "0.1", "stage_id": "tiny", "source": "STRUCTURE:x"}]}
    try:
        open_position(stack, plan)
        sends = broker.order_send_calls
        broker.set_quote(25060.5, 25062.0)
        stack.manage_exits(now_of(stack))  # 0.1 * 1.75 = 0.175 -> rounds to 0 lots: engine emits nothing
        assert broker.order_send_calls == sends and broker.positions_get()[0].volume == 1.75
    finally:
        stack.stop()


# -- stop moves (tighten only) -------------------------------------------------------------------------------------


def test_break_even_moves_the_broker_stop_and_never_loosens_it(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=staged_cfg(pol=policy(breakeven_trigger_r_multiple=D("1"), breakeven_buffer_bps=D("0"))))
    stack.start()
    try:
        open_position(stack, PLAN_TP1_RUNNER)
        broker.set_quote(25055.0, 25056.5)  # +1.04R: break-even, below TP1
        stack.manage_exits(now_of(stack))
        (pos,) = broker.positions_get()
        assert pos.sl == pytest.approx(25001.5, abs=0.26)  # entry (ask) - tick rounding
        assert pos.sl > 24950.0 and pos.volume == 1.75
        wait_sync(stack)
        moved = pos.sl
        broker.set_quote(25010.0, 25011.5)  # price falls back: the stop must not move down
        stack.manage_exits(now_of(stack))
        assert broker.positions_get()[0].sl == moved
        assert triple(stack, broker) == (D("1.75"),) * 3
        assert [e for e in stack.exit_log() if e["kind"] == "stop_moved"]
    finally:
        stack.stop()


def test_rejected_stop_modify_keeps_the_previous_stop_in_force(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=staged_cfg(pol=policy(breakeven_trigger_r_multiple=D("1"))))
    stack.start()
    try:
        open_position(stack, PLAN_TP1_RUNNER)
        broker.set_quote(25055.0, 25056.5)
        broker.send_retcode_override = [int(Retcode.REJECT)]
        stack.manage_exits(now_of(stack))
        assert broker.positions_get()[0].sl == 24950.0  # modify rejected -> old stop stays
        assert [e for e in stack.exit_log() if e["kind"] == "stop_move_failed"]
        stack.manage_exits(now_of(stack))  # retried next cycle from broker truth
        assert broker.positions_get()[0].sl > 24950.0
    finally:
        stack.stop()


# -- full close + limitations + degraded inputs -----------------------------------------------------------------


def test_full_close_decision_flattens_and_reports_the_closure(tmp_path):
    broker = build_broker()
    # an R-based final stage has no broker TP (entry slippage unknown): the engine closes at market
    plan = {"stages": [{"r_multiple": "1", "close_fraction": "1", "stage_id": "tp1", "source": "R"}]}
    stack = make_stack(broker, tmp_path, config=staged_cfg())
    stack.start()
    try:
        open_position(stack, plan)
        assert broker.request_log[0]["tp"] == 0.0
        broker.set_quote(25055.0, 25056.5)
        events = stack.manage_exits(now_of(stack))
        closed = [e for e in events if isinstance(e, PositionClosed)]
        assert len(closed) == 1 and closed[0].exit_quantity == D("1.75")
        assert broker.positions_get() == () and stack.open_intents() == ()
        assert [e for e in stack.exit_log() if e["kind"] == "full_close"]
    finally:
        stack.stop()


def test_multiple_live_tranches_on_one_market_are_an_explicit_limitation(env):
    broker, stack = env
    stack.start()
    open_position(stack)
    stack._registry.insert(
        intent_id="other", client_order_id="dt-other", market="GER40", direction=1, stop="24900",
        target=None, forced_flat_utc=None, status="OPEN", created_utc="2026-01-01T00:00:00+00:00",
    )
    broker.set_quote(25125.0, 25126.5)
    sends = broker.order_send_calls
    assert stack.manage_exits(now_of(stack)) == []
    assert broker.order_send_calls == sends
    assert any(e.get("code") == em.R_EXIT_TRANCHE_LIMITATION for e in stack.exit_log())


def test_stale_quote_takes_no_decision_and_leaves_the_broker_stop(env):
    broker, stack = env
    stack.start()
    open_position(stack)
    broker.set_quote(25125.0, 25126.5)
    later = now_of(stack).replace(year=now_of(stack).year + 1)  # the quote is a year old
    sends = broker.order_send_calls
    assert stack.manage_exits(later) == []
    assert broker.order_send_calls == sends and broker.positions_get()[0].sl == 24950.0
    assert stack._exit_manager.counters["quote_stale"] == 1


def test_manage_exits_without_a_plan_uses_the_policy_ladder_or_does_nothing(env):
    broker, stack = env
    stack.start()
    stack.submit(make_intent(), {"family": "fam-a", "atr": "20"})  # no exit_plan, no policy ladder
    broker.set_quote(*QUOTES["Ger40"])
    broker.set_quote(25125.0, 25126.5)
    sends = broker.order_send_calls
    assert stack.manage_exits(now_of(stack)) == []
    assert broker.order_send_calls == sends


def test_partially_filled_reduce_records_the_actual_quantity_and_keeps_the_invariant(env):
    broker, stack = env
    stack.start()
    open_position(stack)
    broker.set_quote(25060.5, 25062.0)
    broker.fill_plan = [[(0.25, 25060.5)]]  # asked 0.50 lots, the broker fills 0.25, IOC remainder dies
    stack.manage_exits(now_of(stack))
    (pos,) = broker.positions_get()
    assert pos.volume == 1.5 and pos.sl == 24950.0
    wait_sync(stack)
    assert triple(stack, broker) == (D("1.5"),) * 3
    (partial,) = [e for e in stack.exit_log() if e["kind"] == "partial_exit"]
    assert partial["quantity_reduced"] == "0.25" and partial["quantity_remaining"] == "1.5"
    sends = broker.order_send_calls
    stack.manage_exits(now_of(stack))  # an under-filled stage counts as done: no chasing
    assert broker.order_send_calls == sends


def test_disconnect_during_management_fails_closed_and_leaves_the_broker_stop(env):
    from demo.execution.stack_port import StackFailClosed

    broker, stack = env
    stack.start()
    open_position(stack)
    broker.set_quote(25060.5, 25062.0)
    sends = broker.order_send_calls
    broker.disconnect()
    with pytest.raises(StackFailClosed):
        stack.manage_exits(now_of(stack))
    assert broker.order_send_calls == sends  # nothing was sent while the broker state was unknowable
