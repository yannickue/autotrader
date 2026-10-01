# ruff: noqa: E501
"""Lane V (Opus final audit): HIGH-1 the normal EOD sweep falls back to the broker-ticket close when Nautilus refuses;
MEDIUM-1 a close the broker already completed (TP filled first) is a SUCCESS, never a halt / failure count."""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from demo.execution.strategy import JobOutcome
from demo.opportunity.operating_policy import FLATTEN_CONFIRMED, load_operating_policy
from tests.unit.demo.execution.stack_harness import FAST, build_broker, make_intent, make_stack

BERLIN = ZoneInfo("Europe/Berlin")
CFG = dataclasses.replace(FAST, operating_policy=load_operating_policy(), flatten_wait_s=0.5)


def berlin(hh: int, mm: int = 0, ss: int = 0, day=(2026, 7, 15)) -> datetime:
    return datetime(*day, hh, mm, ss, tzinfo=BERLIN).astimezone(UTC)


@pytest.fixture
def env(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=CFG)
    stack.start()
    stack.submit(make_intent())
    assert len(broker.positions_get()) == 1
    yield broker, stack
    stack.stop()


def _refuse(stack, monkeypatch, outcome):
    monkeypatch.setattr(stack._strategy, "_start_flatten", lambda job: stack._strategy._resolve(job, outcome))


@pytest.mark.parametrize(
    "outcome",
    [
        JobOutcome("denied", "REDUCE_ONLY_LOCAL_BROKER_POSITION_MISMATCH"),
        JobOutcome("failed", "EXTERNAL_position_refused_under_NETTING"),
    ],
)
def test_H1_nautilus_refusal_in_the_eod_window_falls_back_to_the_ticket_close(env, monkeypatch, outcome):
    broker, stack = env
    _refuse(stack, monkeypatch, outcome)
    stack.on_clock(berlin(21, 56))
    assert broker.positions_get() == ()
    assert "position" in broker.request_log[-1]  # reduce-only close bound to the broker ticket
    assert stack._flatten_failures == {} and stack._fatal is None
    stack.on_clock(berlin(22, 0, 5))
    assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED


def test_H1_nautilus_timeout_in_the_eod_window_falls_back_to_the_ticket_close(env, monkeypatch):
    broker, stack = env
    monkeypatch.setattr(stack._strategy, "_start_flatten", lambda job: None)  # the future never resolves
    stack.on_clock(berlin(21, 56))
    assert broker.positions_get() == ()


def test_H1_foreign_magic_is_untouched_and_nothing_flips(env, monkeypatch):
    broker, stack = env
    own = broker.positions_get()[0]
    foreign = copy.copy(own)
    foreign.ticket = foreign.identifier = 999_001
    foreign.magic, foreign.volume = 0, 0.25
    broker.positions[999_001] = foreign
    _refuse(stack, monkeypatch, JobOutcome("denied", "REDUCE_ONLY_LOCAL_BROKER_POSITION_MISMATCH"))
    stack.on_clock(berlin(21, 56))
    left = broker.positions_get()
    assert [int(p.magic) for p in left] == [0] and float(left[0].volume) == 0.25
    for req in broker.request_log:
        if "position" in req and int(req["position"]) == int(own.ticket):
            assert int(req["type"]) != int(own.type)  # opposite side only


def test_H1_outside_the_eod_window_a_refusal_does_not_use_the_ticket_fallback(env, monkeypatch):
    broker, stack = env
    _refuse(stack, monkeypatch, JobOutcome("denied", "REDUCE_ONLY_LOCAL_BROKER_POSITION_MISMATCH"))
    info = stack._markets["GER40"]
    n0 = len(broker.request_log)
    assert stack._flatten(info, tag="t", escalate=False, now=berlin(11, 0)) is False
    assert len(broker.positions_get()) == 1 and len(broker.request_log) == n0


def test_M1_a_close_the_broker_already_completed_is_a_success(env, monkeypatch):
    broker, stack = env
    info = stack._markets["GER40"]

    def tp_filled_first(job):
        broker.positions.clear()  # the broker take-profit filled between the decision and the close
        stack._strategy._resolve(job, JobOutcome("denied", "REDUCE_ONLY_BROKER_POSITIONS_0"))

    monkeypatch.setattr(stack._strategy, "_start_flatten", tp_filled_first)
    for _ in range(4):  # escalate=True: would have counted 3 failures -> fatal before
        assert stack._flatten(info, tag="exit:x", hint="TARGET", escalate=True) is True
    assert stack._flatten_failures == {} and stack._fatal is None and stack._halt_reason is None


def test_low_a_vanished_stop_is_restored_to_the_last_tightened_stop_never_looser(env):
    import json
    from decimal import Decimal

    broker, stack = env
    row = stack._registry.get("intent-1")
    initial = Decimal(row.stop)
    tightened = initial + Decimal("10")  # LONG: higher stop = tighter
    ctx = json.loads(row.context or "{}")
    ctx["exit_state"] = {"current_stop": str(tightened), "stop_moves": 1}
    stack._registry.update(row.intent_id, context=json.dumps(ctx))
    (pos,) = broker.positions_get()
    pos.sl = 0.0  # the stop vanished
    stack._repair_protection(row, pos)  # a stale row object (as held by the manager) must still see the tightened stop
    assert float(broker.positions_get()[0].sl) == float(tightened)


def test_low_restore_never_goes_looser_than_the_initial_stop(env):
    import json
    from decimal import Decimal

    broker, stack = env
    row = stack._registry.get("intent-1")
    initial = Decimal(row.stop)
    ctx = json.loads(row.context or "{}")
    ctx["exit_state"] = {"current_stop": str(initial - Decimal("50"))}  # corrupt / looser state must be ignored
    stack._registry.update(row.intent_id, context=json.dumps(ctx))
    (pos,) = broker.positions_get()
    pos.sl = 0.0
    stack._repair_protection(row, pos)
    assert float(broker.positions_get()[0].sl) == float(initial)


# -- MEDIUM-1 design guard: a profile stage that coincides with the broker TP is owned by the broker ---------------------


@pytest.fixture
def penv(tmp_path):
    from tests.unit.demo.execution.test_exit_profiles_stack import profiles_cfg

    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=profiles_cfg())
    stack.start()
    yield broker, stack
    stack.stop()


def test_M1_engine_skips_the_stage_that_equals_the_broker_tp_but_keeps_thesis_failure(penv, monkeypatch):
    from tests.unit.demo.execution.test_exit_manager import now_of
    from tests.unit.demo.execution.test_exit_profiles_stack import PLAN_SINGLE_TARGET, open_row

    broker, stack = penv
    open_row(stack, "ROUND", "reject", PLAN_SINGLE_TARGET)
    assert broker.request_log[0]["tp"] == 25060.0  # broker TP == the engine's only stage
    monkeypatch.setattr(broker, "_evaluate_stops", lambda: None)  # the broker TP has not (yet) filled
    broker.set_quote(25060.5, 25062.0)
    sends = broker.order_send_calls
    stack.manage_exits(now_of(stack))
    assert broker.order_send_calls == sends and len(broker.positions_get()) == 1  # no engine close racing the broker TP
    assert stack._exit_manager.counters["stage_left_to_broker_tp"] >= 1


def test_M1_tp_filled_first_then_the_engine_close_is_denied_no_halt_no_failure_row_terminal_no_double_close(penv, monkeypatch):
    from types import SimpleNamespace

    from exits.models import ExitReason
    from tests.unit.demo.execution.test_exit_profiles_stack import PLAN_SINGLE_TARGET, open_row

    broker, stack = penv
    open_row(stack, "ROUND", "reject", PLAN_SINGLE_TARGET)
    info = stack._markets["GER40"]
    row = stack._registry.get("intent-1")

    def tp_first(job):
        broker.set_quote(25060.5, 25062.0)  # the broker take-profit fills now
        stack._strategy._resolve(job, JobOutcome("denied", "REDUCE_ONLY_BROKER_POSITIONS_0"))

    monkeypatch.setattr(stack._strategy, "_start_flatten", tp_first)
    decision = SimpleNamespace(reason=ExitReason.TAKE_PROFIT, request_id="r-1", metadata={})
    events = []
    import time

    for _ in range(20):  # _lane_build_closed waits its (short) grace for the deals
        events = stack._exit_manager._close_fully(stack._registry.get("intent-1") or row, info, decision)
        if events:
            break
        time.sleep(0.1)
    assert broker.positions_get() == () and len(events) == 1 and events[0].exit_reason == "TARGET"
    assert stack._flatten_failures == {} and stack._fatal is None and stack._halt_reason is None
    n = broker.order_send_calls
    stack._exit_manager._close_fully(stack._registry.get("intent-1") or row, info, decision)  # no second close at the broker
    assert broker.order_send_calls == n
