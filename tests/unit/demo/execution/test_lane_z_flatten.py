# ruff: noqa: E501
"""Lane Z (Opus audit C1 / M1): the zero-overnight sweep must never be defeated by flatten escalation, a foreign
magic on the symbol, a latched fatal, or a position Nautilus never adopted."""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from demo.execution.stack_port import StackFailClosed
from demo.execution.strategy import JobOutcome
from demo.opportunity.operating_policy import FLATTEN_CONFIRMED, load_operating_policy
from nautilus_mt5.constants import Retcode
from tests.unit.demo.execution.stack_harness import FAST, build_broker, make_intent, make_stack

BERLIN = ZoneInfo("Europe/Berlin")
CFG = dataclasses.replace(FAST, operating_policy=load_operating_policy())


def berlin(hh: int, mm: int = 0, ss: int = 0, day=(2026, 7, 15)) -> datetime:
    return datetime(*day, hh, mm, ss, tzinfo=BERLIN).astimezone(UTC)


@pytest.fixture
def env(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=CFG)
    stack.start()
    yield broker, stack
    stack.stop()


def _open(stack, broker):
    stack.submit(make_intent())
    assert len(broker.positions_get()) == 1


FUTURE = (2027, 7, 15)  # the row's real-clock forced flat (now + 1h) is long past on this date: the ROW loop is due too


def test_C1_four_consecutive_close_rejects_do_not_latch_fatal_and_the_sweep_keeps_retrying(env):
    broker, stack = env
    _open(stack, broker)
    broker.send_retcode_override = [int(Retcode.REJECT)] * 4
    t = berlin(21, 55, day=FUTURE)
    for step in (0, 1, 2, 8, 20, 40):  # never raises: no fatal flatten_failed after 3 failed closes
        stack.on_clock(t + timedelta(seconds=step))
    assert stack._fatal is None, stack._fatal
    assert broker.positions_get() == ()  # rejections exhausted -> the retrying sweep closed it
    stack.on_clock(berlin(22, 0, 5, day=FUTURE))
    assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED


def test_C1_sweep_failures_do_not_touch_the_shared_flatten_failure_counter(env):
    broker, stack = env
    _open(stack, broker)
    broker.send_retcode_override = [int(Retcode.REJECT)] * 10
    for step in (0, 6, 17, 40, 90):
        stack.on_clock(berlin(21, 55) + timedelta(seconds=step))
    assert stack._flatten_failures == {} and stack._fatal is None
    assert stack._eod_failures.get("GER40", 0) >= 3


def test_a_foreign_magic_position_on_the_symbol_does_not_make_our_flatten_fail(env):
    broker, stack = env
    _open(stack, broker)
    foreign = copy.copy(broker.positions_get()[0])  # the real account is hedging-capable: a second ticket, other magic
    foreign.ticket = foreign.identifier = 999_001
    foreign.magic, foreign.volume = 0, 0.25
    broker.positions[999_001] = foreign
    assert len(broker.positions_get()) == 2
    stack.on_clock(berlin(21, 56))
    remaining = broker.positions_get()
    assert [int(p.magic) for p in remaining] == [0], remaining  # ours closed, the foreign one untouched
    assert stack._flatten_failures == {} and stack._fatal is None
    stack.on_clock(berlin(22, 0, 5))
    assert stack.eod_status()["eod_own_positions_open"] == 0


def test_the_sweep_runs_even_when_a_fatal_is_latched(env):
    broker, stack = env
    _open(stack, broker)
    stack._set_fatal("flatten_failed:GER40")  # e.g. latched by a protection-repair flatten
    stack.on_clock(berlin(21, 56))  # must NOT raise before the sweep, and must flatten
    assert broker.positions_get() == ()
    assert stack._fatal == "flatten_failed:GER40"  # the fatal stays latched (no new exposure)


@pytest.mark.parametrize("fatal", ["non_demo_account", "account_identity_changed", "mt5_lane_timeout"])
def test_unsafe_fatals_still_block_any_broker_action(env, fatal):
    broker, stack = env
    _open(stack, broker)
    stack._set_fatal(fatal)
    with pytest.raises(StackFailClosed):
        stack.on_clock(berlin(21, 56))
    assert len(broker.positions_get()) == 1


def test_M1_position_at_the_broker_but_not_in_the_nautilus_cache_is_closed_by_ticket(env, monkeypatch):
    broker, stack = env
    _open(stack, broker)
    # Nautilus did not adopt it: the strategy resolves 'flat / no_open_position' without closing anything.
    monkeypatch.setattr(
        stack._strategy, "_start_flatten", lambda job: stack._strategy._resolve(job, JobOutcome("flat", "no_open_position"))
    )
    events = stack.on_clock(berlin(21, 56))
    assert broker.positions_get() == (), events
    assert "position" in broker.request_log[-1]  # a reduce-only close bound to the broker position ticket
    stack.on_clock(berlin(22, 0, 5))
    assert stack.eod_status()["eod_own_positions_open"] == 0


def test_M1_the_ticket_close_never_touches_a_foreign_magic_position(env, monkeypatch):
    broker, stack = env
    broker.external_market_fill(is_buy=True, volume=0.25, comment="manual", magic=0)
    monkeypatch.setattr(
        stack._strategy, "_start_flatten", lambda job: stack._strategy._resolve(job, JobOutcome("flat", "no_open_position"))
    )
    stack.on_clock(berlin(21, 56))
    assert [int(p.magic) for p in broker.positions_get()] == [0]
    assert broker.order_send_calls == 0
