# ruff: noqa: E501
"""Lane V2 (Opus re-check): per-market sweep start gets the ticket fallback, broker truth beats a Nautilus 'flat', the row loop
respects the EOD backoff, and a wedged MT5 lane is exposed as ``lane_wedged`` whatever fatal came first."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from demo.execution.live import StackFailClosed
from demo.execution.strategy import JobOutcome
from demo.opportunity.operating_policy import load_operating_policy
from nautilus_mt5.executor import LaneTimeout
from tests.unit.demo.execution.stack_harness import (
    FAST,
    add_phase2_symbols,
    build_broker,
    make_intent,
    make_stack,
)

BERLIN = ZoneInfo("Europe/Berlin")
CFG = dataclasses.replace(FAST, operating_policy=load_operating_policy(), flatten_wait_s=0.5)
REFUSED = JobOutcome("denied", "REDUCE_ONLY_LOCAL_BROKER_POSITION_MISMATCH")


def berlin(hh, mm=0, ss=0, day=(2026, 7, 15)):
    return datetime(*day, hh, mm, ss, tzinfo=BERLIN).astimezone(UTC)


def _refuse(stack, monkeypatch, outcome=REFUSED, counter=None):
    def start(job):
        if counter is not None:
            counter.append(1)
        stack._strategy._resolve(job, outcome)

    monkeypatch.setattr(stack._strategy, "_start_flatten", start)


@pytest.fixture
def ger(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=CFG)
    stack.start()
    stack.submit(make_intent())
    assert len(broker.positions_get()) == 1
    yield broker, stack
    stack.stop()


def _brent_stack(tmp_path, market, symbol, entry, stop, target):
    broker = build_broker()
    add_phase2_symbols(broker)
    stack = make_stack(broker, tmp_path, config=CFG, extra_markets=("BRENT", "BTCUSD"))
    stack.start()
    stack.submit(make_intent(market=market, broker_symbol=symbol, entry_ref=entry, stop=stop, target=target))
    assert len(broker.positions_get()) == 1
    return broker, stack


@pytest.mark.parametrize(
    ("market", "symbol", "entry", "stop", "target", "day", "hh", "mm"),
    [
        ("BRENT", "Brent", 97.78, 96.78, 100.3, (2026, 12, 2), 21, 51),  # winter Wed: broker close 21:55 Berlin -> sweep from 21:50
        ("BTCUSD", "BTCUSD", 83746.28, 83146.28, 85000.0, (2026, 12, 4), 21, 52),  # winter Friday
    ],
)
def test_V2_per_market_sweep_start_before_the_global_window_gets_the_ticket_fallback(
    tmp_path, monkeypatch, market, symbol, entry, stop, target, day, hh, mm
):
    broker, stack = _brent_stack(tmp_path, market, symbol, entry, stop, target)
    try:
        assert not CFG.operating_policy.flatten_active(berlin(hh, mm, day=day))  # the global window is NOT open yet
        _refuse(stack, monkeypatch)
        stack.on_clock(berlin(hh, mm, day=day))
        assert broker.positions_get() == ()
        assert "position" in broker.request_log[-1] and stack._fatal is None
    finally:
        stack.stop()


def test_V2_outside_every_eod_window_a_refused_close_gets_no_fallback(ger, monkeypatch):
    broker, stack = ger
    _refuse(stack, monkeypatch)
    stack._flatten(stack._markets["GER40"], tag="x", escalate=False, now=berlin(15, 0))  # mid-day, not an EOD sweep instant
    assert len(broker.positions_get()) == 1


def test_V2_nautilus_says_flat_but_the_broker_still_has_it_in_the_eod_window(ger, monkeypatch):
    broker, stack = ger
    _refuse(stack, monkeypatch, JobOutcome("flat", "closed"))  # Nautilus claims flat; broker truth wins
    stack.on_clock(berlin(21, 56))
    assert broker.positions_get() == ()


def test_V2_row_loop_respects_the_eod_backoff_one_attempt_per_step(ger, monkeypatch):
    _broker, stack = ger
    calls: list[int] = []
    _refuse(stack, monkeypatch, counter=calls)
    monkeypatch.setattr(stack, "_lane_close_by_ticket", lambda info, tag: ["x:denied"])  # the ticket close fails too
    day = (2026, 12, 2)
    stack.on_clock(berlin(21, 56, 0, day))  # row (forced flat due) + sweep: ONE attempt, not two
    assert len(calls) == 1
    stack.on_clock(berlin(21, 56, 1, day))  # inside the backoff: nothing
    assert len(calls) == 1
    stack.on_clock(berlin(21, 56, 6, day))  # backoff (5 s) elapsed: exactly one more
    assert len(calls) == 2


def test_V2_lane_wedged_flag_survives_an_earlier_fatal_and_teardown_does_not_wait(ger, monkeypatch):
    _broker, stack = ger
    assert stack.lane_wedged is False
    stack._set_fatal("flatten_failed:GER40")  # an earlier fatal: _fatal keeps only this first reason
    monkeypatch.setattr(stack._lane, "run_sync", lambda *a, **k: (_ for _ in ()).throw(LaneTimeout("blocked")))
    with pytest.raises(StackFailClosed):
        stack._on_lane(lambda: 1)
    assert stack._fatal == "flatten_failed:GER40" and stack.lane_wedged is True
    seen: list[bool] = []
    real = stack._adapter.lane.shutdown
    monkeypatch.setattr(stack._adapter.lane, "shutdown", lambda wait=True: (seen.append(wait), real(wait=False))[1])
    stack.stop()
    assert seen == [False]  # a wedged C call must never be joined
