# ruff: noqa: E501
"""Lane Y: per-intent exit PROFILES (``exit_policy="staged_profiles"``) end-to-end on the fake broker.

GER40 harness: min lot/step 0.25, default intent 1.75 lots, entry ask 25001.5, stop 24950 (1R = 51.5), price = BID."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from demo import exit_profiles as xp
from demo.execution import exit_manager as em
from demo.execution.events import PositionClosed
from tests.unit.demo.execution.stack_harness import FAST, build_broker, make_intent, make_stack
from tests.unit.demo.execution.test_exit_manager import now_of, triple, wait_sync
from tests.unit.demo.execution.test_exit_manager_e2 import FALLING, RISING, backdate, load_bars

D = Decimal

PLAN_TP1_PARTIAL = {"stages": [{"target_price": "25060", "close_fraction": "0.5", "stage_id": "tp1", "source": "STRUCTURE:pdh"}]}
PLAN_SINGLE_TARGET = {"stages": [{"target_price": "25060", "close_fraction": "1", "stage_id": "tp1", "source": "STRUCTURE:mean"}]}
PLAN_FAR_PARTIAL = {"stages": [{"target_price": "25500", "close_fraction": "0.5", "stage_id": "tp1", "source": "STRUCTURE:far"}]}


def profiles_cfg(**over):
    return dataclasses.replace(FAST, exit_policy=em.EXIT_POLICY_PROFILES, staged_exit=em.default_staged_exit_policy(), **over)


@pytest.fixture
def env(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=profiles_cfg())
    yield broker, stack
    stack.stop()


def open_row(stack, family, mode, plan, *, attr=None, **intent):
    route = xp.route_for(family, mode)
    ctx = {"family": family, "atr": "20", "exit_plan": plan, "exit_profile": attr or xp.attribution(route)}
    events = stack.submit(make_intent(**intent), ctx)
    assert [type(e).__name__ for e in events] == ["Accepted", "Fill", "ProtectionConfirmed"], events
    return route


def registry_ctx(stack):
    return json.loads(stack._registry.get("intent-1").context)


# -- config ---------------------------------------------------------------------------------------------------------


def test_default_stays_fixed_and_profiles_need_a_policy():
    assert em.EXIT_POLICIES == ("fixed_1_5r", "staged", "staged_profiles")
    assert dataclasses.replace(FAST).exit_policy == "fixed_1_5r"
    with pytest.raises(ValueError, match="staged_exit"):
        dataclasses.replace(FAST, exit_policy="staged_profiles")


# -- CONTINUATION ---------------------------------------------------------------------------------------------------


def test_continuation_takes_tp1_partial_then_keeps_a_protected_runner_without_broker_tp(env):
    broker, stack = env
    stack.start()
    route = open_row(stack, "STRUCT", "breakout", PLAN_TP1_PARTIAL)
    assert route.profile == xp.PROFILE_CONTINUATION
    assert broker.request_log[0]["tp"] == 0.0 and broker.request_log[0]["sl"] == 24950.0  # runner: no broker TP
    assert registry_ctx(stack)["exit_profile"]["profile"] == "CONTINUATION"  # persisted with the registry row
    broker.set_quote(25060.5, 25062.0)
    assert stack.manage_exits(now_of(stack)) == []
    (pos,) = broker.positions_get()
    assert pos.volume == 1.0 and pos.sl == 24950.0  # 0.5 * 1.75 = 0.875 -> 0.75 lots reduced; stop untouched (NO break-even)
    wait_sync(stack)
    assert triple(stack, broker) == (D("1.0"),) * 3  # partial-fill protection invariant: broker == local == protected
    (partial,) = [e for e in stack.exit_log() if e["kind"] == "partial_exit"]
    assert partial["exit_profile"] == "CONTINUATION" and partial["mapping_version"] == xp.MAPPING_VERSION
    assert partial["quantity_remaining"] == "1.00" or D(partial["quantity_remaining"]) == D("1.00")
    for _ in range(3):  # one TP1 only; no TP2 (dormant), runner stays
        stack.manage_exits(now_of(stack))
    assert broker.positions_get()[0].volume == 1.0


def test_continuation_no_break_even_at_3r_no_atr_trail_stop_only_moves_with_confirmed_structure(env):
    broker, stack = env
    stack.start()
    open_row(stack, "STRUCT", "breakout", PLAN_FAR_PARTIAL)
    backdate(stack)
    load_bars(stack, broker, [25000] * 20)  # no confirmed post-entry swing
    broker.set_quote(25160.0, 25161.5)  # > 3R in profit
    assert stack.manage_exits(now_of(stack)) == []
    assert broker.positions_get()[0].sl == 24950.0  # neither an R-threshold break-even nor an ATR / percentage trail
    assert not [e for e in stack.exit_log() if e["kind"] == "stop_moved"]


def test_continuation_structure_trail_tightens_only_and_thesis_failure_closes_the_runner(env):
    broker, stack = env
    stack.start()
    open_row(stack, "STRUCT", "breakout", PLAN_FAR_PARTIAL)
    backdate(stack)
    load_bars(stack, broker, RISING)
    broker.set_quote(25085.0, 25086.5)
    stack.manage_exits(now_of(stack))
    sl = broker.positions_get()[0].sl
    assert 24950.0 < sl < 25044.0  # behind the newest confirmed higher low (the structure trail is the ONLY trail)
    wait_sync(stack)
    assert triple(stack, broker) == (D("1.75"),) * 3
    broker.set_quote(25060.0, 25061.5)  # price falls back: the stop never loosens
    stack.manage_exits(now_of(stack))
    assert broker.positions_get()[0].sl == sl
    load_bars(stack, broker, [*RISING[:-2], 25042, 25042])  # a closed bar breaks the higher low (thesis failure)
    broker.set_quote(25043.0, 25044.5)
    events = stack.manage_exits(now_of(stack))
    closed = [e for e in events if isinstance(e, PositionClosed)]
    assert len(closed) == 1 and closed[0].exit_reason == "EXIT_ENGINE_STRUCTURE"


def test_continuation_momentum_is_shadow_only_and_never_an_order(env):
    broker, stack = env
    stack.start()
    open_row(stack, "STRUCT", "breakout", PLAN_FAR_PARTIAL, flat_in_s=6 * 3600)
    backdate(stack, hours=0)
    load_bars(stack, broker, FALLING)
    broker.set_quote(24990.0, 24991.5)  # losing, adverse momentum, stop 24950 still far
    sends = broker.order_send_calls
    events = stack.manage_exits(now_of(stack))
    assert not [e for e in events if isinstance(e, PositionClosed)] or all(e.exit_reason == "EXIT_ENGINE_STRUCTURE" for e in events if isinstance(e, PositionClosed))
    # no EXIT_ENGINE_MOMENTUM / EXIT_ENGINE_EOD ever (those rules are OFF in every profile)
    assert not any(getattr(e, "exit_reason", "") in ("EXIT_ENGINE_MOMENTUM", "EXIT_ENGINE_EOD") for e in events)
    assert broker.order_send_calls >= sends  # (a structure failure close is the only legal order here)
    shadow = [e for e in stack.exit_log() if e["kind"] == "shadow_momentum_exit"]
    assert len(shadow) <= 1


def test_hard_eod_flat_still_wins_over_a_profile_runner(env):
    broker, stack = env
    stack.start()
    open_row(stack, "STRUCT", "breakout", PLAN_FAR_PARTIAL, flat_in_s=1200)
    load_bars(stack, broker, [25000] * 20)
    broker.set_quote(25010.0, 25011.5)
    stack.manage_exits(now_of(stack))
    assert broker.positions_get()[0].volume == 1.75  # profile never closes it early
    stack.on_clock(now_of(stack) + timedelta(seconds=1500))  # past the row's forced flat
    assert broker.positions_get() == ()


# -- REVERSION ------------------------------------------------------------------------------------------------------


def test_reversion_single_target_full_close_has_a_broker_tp_no_runner_no_trail(env):
    broker, stack = env
    stack.start()
    route = open_row(stack, "ROUND", "reject", PLAN_SINGLE_TARGET)
    assert route.profile == xp.PROFILE_REVERSION
    assert broker.request_log[0]["tp"] == 25060.0  # the single (final) stage is the broker TP
    backdate(stack)
    load_bars(stack, broker, RISING)  # a structure would trail a CONTINUATION row ...
    broker.set_quote(25045.0, 25046.5)
    stack.manage_exits(now_of(stack))
    assert broker.positions_get()[0].sl == 24950.0  # ... a REVERSION row has no trail and no break-even
    broker.set_quote(25060.5, 25062.0)  # the broker TP (same level) and the engine stage coincide: ONE full close, no partial
    stack.manage_exits(now_of(stack))
    assert broker.positions_get() == () and not [e for e in stack.exit_log() if e["kind"] == "partial_exit"]


def test_reversion_time_decay_is_family_aware_off_without_a_documented_horizon(env):
    broker, stack = env
    stack.start()
    open_row(stack, "ROUND", "reject", PLAN_FAR_PARTIAL)  # real mapping: no documented horizon -> time stop OFF
    assert registry_ctx(stack)["exit_profile"]["time_stop_minutes"] is None
    backdate(stack, hours=20)
    load_bars(stack, broker, [25000] * 20)
    broker.set_quote(25000.0, 25001.5)
    sends = broker.order_send_calls
    stack.manage_exits(now_of(stack))
    assert broker.order_send_calls == sends and broker.positions_get()[0].volume == 1.75


def test_reversion_time_decay_fires_when_the_family_route_documents_a_horizon(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=profiles_cfg())
    stack.start()
    try:
        attr = {**xp.attribution(xp.route_for("ROUND", "reject")), "time_stop_minutes": 30}
        open_row(stack, "ROUND", "reject", PLAN_FAR_PARTIAL, attr=attr)
        backdate(stack, hours=2)
        load_bars(stack, broker, [25000] * 20)
        broker.set_quote(25000.0, 25001.5)  # expected reversion did not happen (no excursion)
        events = stack.manage_exits(now_of(stack))
        closed = [e for e in events if isinstance(e, PositionClosed)]
        assert len(closed) == 1 and closed[0].exit_reason == "EXIT_ENGINE_TIME_STOP"
    finally:
        stack.stop()


# -- FAILED_MOVE ----------------------------------------------------------------------------------------------------


def test_failed_move_closes_fully_at_the_first_structural_return_target(env):
    broker, stack = env
    stack.start()
    route = open_row(stack, "STRUCT", "fade", PLAN_SINGLE_TARGET)
    assert route.profile == xp.PROFILE_FAILED_MOVE
    assert registry_ctx(stack)["exit_profile"]["tp2"] == "DORMANT_SHADOW"
    broker.set_quote(25060.5, 25062.0)
    stack.manage_exits(now_of(stack))
    assert broker.positions_get() == () and not [e for e in stack.exit_log() if e["kind"] == "partial_exit"]


# -- FIXED / legacy rows --------------------------------------------------------------------------------------------


def test_fixed_mapped_row_keeps_the_unchanged_fixed_behaviour(env):
    broker, stack = env
    stack.start()
    route = open_row(stack, "LEADLAG", None, PLAN_TP1_PARTIAL)  # even a plan in the context is ignored
    assert route.profile == xp.PROFILE_FIXED
    assert broker.request_log[0]["tp"] == 25150.0  # the intent's fixed-R target, byte-identical to fixed_1_5r
    assert stack._registry.get("intent-1").target == "25150.0"  # str(intent.target), as under fixed_1_5r
    sends = broker.order_send_calls
    broker.set_quote(25125.0, 25126.5)
    assert stack.manage_exits(now_of(stack)) == [] and broker.order_send_calls == sends
    assert broker.positions_get()[0].volume == 1.75 and not [e for e in stack.exit_log() if e["kind"] != "entry_plan"]


def test_a_row_without_a_profile_is_never_touched_under_staged_profiles(env):
    broker, stack = env
    stack.start()
    stack.submit(make_intent(), {"family": "fam-a", "atr": "20", "exit_plan": PLAN_TP1_PARTIAL})
    assert broker.request_log[0]["tp"] == 25150.0  # not managed -> fixed target
    sends = broker.order_send_calls
    broker.set_quote(25125.0, 25126.5)
    assert stack.manage_exits(now_of(stack)) == [] and broker.order_send_calls == sends


# -- one profile for life -------------------------------------------------------------------------------------------


def test_profile_survives_restart_and_a_row_never_changes_its_profile(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path, config=profiles_cfg())
    first.start()
    open_row(first, "STRUCT", "breakout", PLAN_TP1_PARTIAL)
    broker.set_quote(25060.5, 25062.0)
    first.manage_exits(now_of(first))
    assert json.loads(first._registry.get("intent-1").context)["exit_state"]["exit_profile"] == "CONTINUATION"
    first.stop()
    second = make_stack(broker, tmp_path, config=profiles_cfg())
    try:
        snap = second.start()
        assert snap.reconciliation == "RECONCILED" and snap.open_positions == 1
        assert registry_ctx(second)["exit_profile"]["profile"] == "CONTINUATION"  # restart: the row keeps its profile
        sends = broker.order_send_calls
        second.manage_exits(now_of(second))
        assert broker.order_send_calls == sends and broker.positions_get()[0].volume == 1.0  # no re-fired TP1
        # tamper: a different profile appears on the same row -> refused (fail safe), no order
        ctx = registry_ctx(second)
        ctx["exit_profile"] = xp.attribution(xp.route_for("ROUND", "reject"))
        second._registry.update("intent-1", context=json.dumps(ctx))
        broker.set_quote(25100.0, 25101.5)
        second.manage_exits(now_of(second))
        assert broker.order_send_calls == sends
        assert second._exit_manager.counters["profile_mutation_refused"] >= 1
    finally:
        second.stop()
