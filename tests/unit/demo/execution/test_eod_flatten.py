# ruff: noqa: E501
"""Lane P: end-of-day flatten sweep of Mt5DemoStack (defence in depth behind the row-based forced flat).

G partial exit near the deadline, H rejected close, I restart inside the flatten window, J zero strategy-managed
positions at 22:00 Berlin, C entry gate; against the multi-symbol fake broker."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from demo.execution import registry as reg
from demo.execution.events import PositionClosed, Rejected
from demo.execution.strategy import ReduceJob
from demo.opportunity.operating_policy import (
    FLATTEN_CONFIRMED,
    FLATTEN_IDLE,
    FLATTEN_OVERDUE,
    FLATTEN_WINDOW,
    load_operating_policy,
)
from nautilus_mt5.constants import Retcode
from tests.unit.demo.execution.stack_harness import FAST, build_broker, make_intent, make_stack

BERLIN = ZoneInfo("Europe/Berlin")
POL = load_operating_policy()
CFG = dataclasses.replace(FAST, operating_policy=POL)


def berlin(hh: int, mm: int = 0, ss: int = 0, day=(2026, 7, 15)) -> datetime:
    # A past summer date: the rows' real-clock forced-flat (now + 1h) has NOT arrived, so ONLY the sweep can act.
    return datetime(*day, hh, mm, ss, tzinfo=BERLIN).astimezone(UTC)


@pytest.fixture
def env(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=CFG)
    stack.start()
    yield broker, stack
    stack.stop()


def _open_position(stack, broker, **kw):
    events = stack.submit(make_intent(**kw))
    assert len(broker.positions_get()) == 1, events
    return events


def test_C_entries_are_refused_from_the_flatten_start_and_inside_the_runway(env):
    _broker, stack = env
    intent = make_intent(flat_in_s=None)
    assert stack._pre_reject(intent, berlin(21, 54)) is None  # before the flatten window (A/B at stack level)
    assert stack._pre_reject(intent, berlin(21, 55)) == "flatten_window_active"
    assert stack._pre_reject(intent, berlin(23, 30)) == "flatten_window_active"
    now = berlin(21, 40)
    inside = make_intent(now=now, flat_in_s=9 * 60)  # 9 min to its forced flat < 10 min runway
    outside = make_intent(now=now, flat_in_s=15 * 60)
    assert stack._pre_reject(inside, now) == "entry_runway_too_short"
    assert stack._pre_reject(outside, now) is None
    assert stack._pre_reject(make_intent(now=berlin(21, 10), flat_in_s=45 * 60), berlin(21, 10)) is None  # A: 21:10 allowed


def test_C_a_submit_inside_the_flatten_window_is_a_rejected_event_with_no_order(env, monkeypatch):
    broker, stack = env
    monkeypatch.setattr(stack, "_now", lambda: berlin(21, 56))
    events = stack.submit(make_intent(now=berlin(21, 56)))
    assert [type(e).__name__ for e in events] == ["Rejected"]
    assert isinstance(events[0], Rejected) and events[0].reason == "flatten_window_active"
    assert broker.order_send_calls == 0 and broker.positions_get() == ()


def test_J_zero_strategy_managed_positions_at_2200_berlin(env):
    broker, stack = env
    _open_position(stack, broker)
    assert stack.eod_status()["flatten_state"] == FLATTEN_IDLE
    assert stack.on_clock(berlin(21, 50)) == [] and len(broker.positions_get()) == 1  # not yet
    events = stack.on_clock(berlin(21, 56))  # sweep: the row-based flat (real-clock +1h) has NOT fired
    assert [e.exit_reason for e in events if isinstance(e, PositionClosed)] == ["SESSION_END"]
    assert broker.positions_get() == () and "position" in broker.request_log[-1]
    stack.on_clock(berlin(22, 0, 1))
    status = stack.eod_status()
    assert status["flatten_state"] == FLATTEN_CONFIRMED and status["eod_flat_confirmed_utc"] is not None
    assert status["eod_own_positions_open"] == 0 and stack.open_intents() == ()
    assert stack.account_snapshot().open_positions == 0


def test_sweep_flattens_an_own_magic_position_without_an_open_registry_row(env):
    """Defence in depth: registry row lost / not OPEN (restart, adoption, manual registry damage)."""
    broker, stack = env
    _open_position(stack, broker)
    (row,) = stack._registry.with_status(reg.OPEN)
    stack._registry.update(row.intent_id, status=reg.CLOSED)
    assert stack._registry.with_status(reg.OPEN) == []
    stack.on_clock(berlin(21, 57))
    assert broker.positions_get() == ()
    assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED


def test_G_partial_exit_near_the_deadline_the_remainder_is_still_flattened(env):
    broker, stack = env
    _open_position(stack, broker)
    (pos,) = broker.positions_get()
    volume_before = Decimal(str(pos.volume))
    info = stack._markets["GER40"]
    half = Decimal("0.75")  # GER40 volume step 0.25; the position is 1.75 lots
    job = ReduceJob(instrument_id=info.instrument_id, quantity=half, tag="partial-test")
    stack._strategy.enqueue(job)
    outcome = job.future.result(timeout=10)
    assert outcome.status not in ("denied", "failed"), outcome
    remaining = broker.positions_get()
    assert len(remaining) == 1 and Decimal(str(remaining[0].volume)) == volume_before - half
    stack.on_clock(berlin(21, 58))
    assert broker.positions_get() == ()
    assert stack.eod_status()["flatten_state"] in (FLATTEN_CONFIRMED, FLATTEN_WINDOW)
    stack.on_clock(berlin(22, 1))
    assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED


def test_H_rejected_close_is_retried_with_backoff_without_flipping_exposure(env):
    broker, stack = env
    _open_position(stack, broker)
    (before,) = broker.positions_get()
    direction, volume = int(before.type), float(before.volume)
    broker.send_retcode_override = [int(Retcode.REJECT)]
    stack.on_clock(berlin(21, 55, 0))
    (still,) = broker.positions_get()  # close refused: nothing changed, in particular NOT flipped
    assert (int(still.type), float(still.volume)) == (direction, volume)
    st = stack.eod_status()
    assert st["flatten_state"] == FLATTEN_WINDOW and "close_failed:GER40:x1" in st["eod_detail"]
    sent = broker.order_send_calls
    stack.on_clock(berlin(21, 55, 2))  # inside the 5 s backoff: no new order
    assert broker.order_send_calls == sent and len(broker.positions_get()) == 1
    stack.on_clock(berlin(21, 55, 7))  # backoff over: retried, now accepted, broker-truth flat
    assert broker.positions_get() == ()
    stack.on_clock(berlin(21, 56))
    assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED


def test_rejected_closes_are_retried_past_the_deadline_until_flat(env):
    broker, stack = env
    _open_position(stack, broker)
    broker.send_retcode_override = [int(Retcode.REJECT)] * 4
    t = berlin(21, 55)
    for step in (0, 6, 17, 40):  # 21:55:00, :06, :17, :40: each attempt rejected
        stack.on_clock(t + timedelta(seconds=step))
    assert len(broker.positions_get()) == 1
    stack.on_clock(berlin(22, 0, 30))  # past the deadline and the backoff: retried, rejection list exhausted -> flat
    assert broker.positions_get() == ()


def test_overdue_state_is_reported_and_the_sweep_keeps_trying(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=CFG)
    try:
        stack.start()
        stack.submit(make_intent())
        broker.send_retcode_override = [int(Retcode.REJECT)] * 50
        for minute_s in range(0, 600, 30):  # 21:55 .. 22:05
            stack.on_clock(berlin(21, 55) + timedelta(seconds=minute_s))
        st = stack.eod_status()
        assert st["flatten_state"] == FLATTEN_OVERDUE and "still open" in st["eod_detail"]
        assert stack.halted_reason is not None and len(broker.positions_get()) == 1
        broker.send_retcode_override = []
        stack.on_clock(berlin(22, 6))
        stack.on_clock(berlin(22, 7))
        assert broker.positions_get() == () and stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED
    finally:
        stack.stop()


def test_I_restart_inside_the_flatten_window_still_flattens_the_recovered_position(tmp_path):
    broker = build_broker()
    first = make_stack(broker, tmp_path, config=CFG)
    first.start()
    first.submit(make_intent())
    first.stop()
    assert len(broker.positions_get()) == 1
    second = make_stack(broker, tmp_path, config=CFG)
    try:
        second.start()  # adopts the broker position (broker truth first)
        assert len(broker.positions_get()) == 1
        events = second.on_clock(berlin(21, 59))
        assert any(isinstance(e, PositionClosed) for e in events)
        assert broker.positions_get() == ()
        second.on_clock(berlin(22, 0, 5))
        assert second.eod_status()["flatten_state"] == FLATTEN_CONFIRMED
    finally:
        second.stop()


def test_a_new_berlin_day_starts_clean_and_no_policy_means_no_sweep(tmp_path, env):
    _broker, stack = env
    stack.on_clock(berlin(22, 1))
    assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED
    stack.on_clock(berlin(8, 0, day=(2026, 7, 16)))
    assert stack.eod_status() == {"flatten_state": FLATTEN_IDLE, "eod_flat_confirmed_utc": None, "eod_detail": None, "eod_own_positions_open": 0}
    # default stack (no policy): the sweep does not exist, behaviour unchanged
    broker2 = build_broker()
    plain = make_stack(broker2, tmp_path / "plain")
    try:
        plain.start()
        plain.submit(make_intent())
        assert plain.on_clock(berlin(21, 59)) == [] and len(broker2.positions_get()) == 1
        assert plain.eod_status()["flatten_state"] == FLATTEN_IDLE
    finally:
        plain.stop()


def test_an_own_magic_position_the_stack_cannot_map_is_reported_loudly_never_silently_skipped(env, monkeypatch):
    """Genuinely unknown exposure stays fail-closed (the foreign-position halt of poll_events is unchanged); the sweep
    additionally names it in the heartbeat state and halts new exposure instead of pretending the book is flat."""
    broker, stack = env
    _open_position(stack, broker)
    monkeypatch.setattr(stack, "_canonical_of", lambda _symbol: None)
    stack.on_clock(berlin(21, 56))
    st = stack.eod_status()
    assert st["flatten_state"] == FLATTEN_WINDOW and "unmapped_own_position:Ger40" in st["eod_detail"]
    assert len(broker.positions_get()) == 1 and stack.halted_reason == "eod_unmapped_own_position"
    stack.on_clock(berlin(22, 1))
    assert stack.eod_status()["flatten_state"] == FLATTEN_OVERDUE
