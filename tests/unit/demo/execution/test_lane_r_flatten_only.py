# ruff: noqa: E501
"""Lane R: Mt5DemoStack in FLATTEN-ONLY (EOD recovery) mode against the multi-symbol fake broker (ZERO real MT5).

D existing broker position discovered, E closed reduce-only (any time of day / weekend), F partial close -> remainder closed,
G local persistence lost / not adopted -> broker-ticket fallback, H flat confirmed at/before the hard deadline,
I rejected closes -> bounded backoff, never given up, J the entry gate is permanently closed, K foreign magic untouched + reported."""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from demo.execution import gates as G
from demo.execution.events import Rejected
from demo.execution.live import StackConfig
from demo.execution.stack_port import StackFailClosed
from demo.execution.strategy import JobOutcome, ReduceJob
from demo.opportunity.operating_policy import (
    FLATTEN_CONFIRMED,
    FLATTEN_OVERDUE,
    load_operating_policy,
)
from nautilus_mt5.constants import Retcode
from tests.unit.demo.execution.stack_harness import FAST, build_broker, make_intent, make_stack

BERLIN = ZoneInfo("Europe/Berlin")
POL = load_operating_policy()
CFG_NORMAL = dataclasses.replace(FAST, operating_policy=POL)
CFG_FO = dataclasses.replace(FAST, operating_policy=POL, flatten_only=True)
OWN_MAGIC = CFG_NORMAL.magic


def berlin(hh: int, mm: int = 0, ss: int = 0, day=(2026, 7, 15)) -> datetime:  # Wed 2026-07-15 (CEST)
    return datetime(*day, hh, mm, ss, tzinfo=BERLIN).astimezone(UTC)


SATURDAY = (2026, 7, 18)


@pytest.fixture
def leftover(tmp_path):
    """A normal stack opened a position and then 'died' (stop() never flattens): the broker keeps an own-magic position."""
    broker = build_broker()
    first = make_stack(broker, tmp_path, config=CFG_NORMAL)
    first.start()
    first.submit(make_intent())
    assert len(broker.positions_get()) == 1
    first.stop()
    assert len(broker.positions_get()) == 1  # a stop leaves the broker position (protected by its stop) alone
    stack = make_stack(broker, tmp_path, config=CFG_FO)
    stack.start()
    yield broker, stack, tmp_path
    stack.stop()


def _requests_since(broker, n0):
    return broker.request_log[n0:]


def test_D_E_an_existing_broker_position_is_discovered_and_closed_reduce_only_at_any_time(leftover):
    broker, stack, _ = leftover
    n0 = len(broker.request_log)
    before = broker.positions_get()[0]
    stack.on_clock(berlin(10, 0))  # 10:00 Berlin: nowhere near the flatten window - flatten-only is always allowed
    assert broker.positions_get() == ()
    sent = _requests_since(broker, n0)
    assert sent and all("position" in r for r in sent), sent  # every request is a close bound to a position ticket (reduce-only)
    assert all(int(r["type"]) != int(before.type) for r in sent)  # opposite side: never an add / flip
    st = stack.eod_status()
    assert st["flatten_state"] == FLATTEN_CONFIRMED and st["eod_own_positions_open"] == 0 and st["eod_flat_confirmed_utc"]
    assert st["flatten_only"] is True


def test_E_the_same_on_a_weekend_day(leftover):
    broker, stack, _ = leftover
    stack.on_clock(berlin(3, 0, day=SATURDAY))
    assert broker.positions_get() == ()
    assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED


def test_F_partial_close_leaves_a_remainder_which_is_closed_too(leftover):
    broker, stack, _ = leftover
    (pos,) = broker.positions_get()
    before = Decimal(str(pos.volume))
    info = stack._markets["GER40"]
    job = ReduceJob(instrument_id=info.instrument_id, quantity=Decimal("0.75"), tag="partial-test")
    stack._strategy.enqueue(job)
    assert job.future.result(timeout=10).status not in ("denied", "failed")
    (rest,) = broker.positions_get()
    assert Decimal(str(rest.volume)) == before - Decimal("0.75")
    stack.on_clock(berlin(21, 58))
    assert broker.positions_get() == ()
    assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED


def test_G_persistence_lost_an_own_magic_position_without_any_registry_row_is_still_closed(tmp_path):
    broker = build_broker()
    broker.external_market_fill(is_buy=True, volume=0.25, comment="orphan-no-token", magic=OWN_MAGIC, sl=24900.0)  # never in any registry (protected: start leaves it)
    stack = make_stack(broker, tmp_path, config=CFG_FO)  # brand-new empty state dir
    stack.start()
    try:
        assert len(broker.positions_get()) == 1
        assert stack._registry.open_for_market("GER40") == []
        stack.on_clock(berlin(21, 40))
        assert broker.positions_get() == ()
        assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED
    finally:
        stack.stop()


def test_G_wrongly_adopted_state_the_nautilus_cache_holds_nothing_the_broker_ticket_fallback_closes(leftover, monkeypatch):
    broker, stack, _ = leftover
    monkeypatch.setattr(
        stack._strategy, "_start_flatten", lambda job: stack._strategy._resolve(job, JobOutcome("flat", "no_open_position"))
    )
    stack.on_clock(berlin(10, 0))
    assert broker.positions_get() == ()
    assert "position" in broker.request_log[-1]
    assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED


def test_H_flat_is_confirmed_at_or_before_the_hard_deadline_when_the_broker_is_reachable(leftover):
    broker, stack, _ = leftover
    day = berlin(21, 55).astimezone(BERLIN).date()
    start = POL.flatten_start_utc(day)
    stack.on_clock(start + timedelta(seconds=5))
    st = stack.eod_status()
    confirmed = datetime.fromisoformat(st["eod_flat_confirmed_utc"])
    assert broker.positions_get() == () and confirmed <= POL.deadline_utc(day)
    assert st["flatten_state"] == FLATTEN_CONFIRMED


def test_I_rejected_closes_are_retried_with_bounded_backoff_and_never_given_up(leftover):
    broker, stack, _ = leftover
    broker.send_retcode_override = [int(Retcode.REJECT)] * 4
    t = berlin(22, 5)  # past the deadline already: OVERDUE is reported loudly, the sweep still keeps retrying
    for step in (0, 6, 17, 40, 90, 200):
        stack.on_clock(t + timedelta(seconds=step))
        assert stack._fatal is None
    assert broker.positions_get() == ()  # rejections exhausted -> the retrying sweep closed it
    assert stack.eod_status()["flatten_state"] == FLATTEN_CONFIRMED


def test_I_overdue_is_reported_while_the_close_keeps_failing(leftover):
    broker, stack, _ = leftover
    broker.send_retcode_override = [int(Retcode.REJECT)] * 50
    stack.on_clock(berlin(22, 5))
    st = stack.eod_status()
    assert st["flatten_state"] == FLATTEN_OVERDUE and "still open" in st["eod_detail"]
    assert len(broker.positions_get()) == 1  # nothing opened, nothing flipped, the broker stop is untouched


def test_J_the_entry_gate_is_permanently_closed(leftover):
    broker, stack, _ = leftover
    calls = broker.order_send_calls
    for moment in (berlin(10, 0), berlin(14, 30), berlin(21, 0), berlin(3, 0, day=SATURDAY)):
        assert stack._pre_reject(make_intent(flat_in_s=None), moment) == G.R_FLATTEN_ONLY
    events = stack.submit(make_intent(intent_id="would-be-entry", flat_in_s=None, valid_s=3600))
    assert [type(e) for e in events] == [Rejected] and events[0].reason == G.R_FLATTEN_ONLY
    assert stack._registry.get("would-be-entry") is None  # not even a registry row
    assert broker.order_send_calls == calls  # nothing reached the broker


def test_J_even_with_every_clock_and_the_halt_cleared_no_entry_is_possible(leftover):
    broker, stack, _ = leftover
    stack.on_clock(berlin(10, 0))
    assert broker.positions_get() == ()
    sent = broker.order_send_calls
    stack._halt_reason = None
    for i in range(3):
        ev = stack.submit(make_intent(intent_id=f"e{i}", flat_in_s=None, valid_s=3600))
        assert ev[0].reason == G.R_FLATTEN_ONLY
    assert broker.order_send_calls == sent and broker.positions_get() == ()


def test_K_foreign_magic_positions_are_reported_and_never_closed(leftover):
    broker, stack, _ = leftover
    foreign = copy.copy(broker.positions_get()[0])  # the real account is hedging-capable: a second ticket, other magic
    foreign.ticket = foreign.identifier = 999_001
    foreign.magic, foreign.volume = 0, 0.25
    broker.positions[999_001] = foreign
    assert len(broker.positions_get()) == 2
    stack.on_clock(berlin(10, 0))
    left = broker.positions_get()
    assert [int(p.magic) for p in left] == [0]
    st = stack.eod_status()
    assert st["flatten_state"] == FLATTEN_CONFIRMED and st["eod_own_positions_open"] == 0
    assert [f["magic"] for f in st["eod_foreign_positions"]] == [0] and st["eod_foreign_positions"][0]["volume"] == 0.25


def test_flatten_only_needs_the_operating_policy():
    with pytest.raises(ValueError):
        StackConfig(flatten_only=True)


def test_an_unsafe_fatal_still_blocks_any_broker_action_in_recovery_mode(leftover):
    broker, stack, _ = leftover
    stack._set_fatal("non_demo_account")
    with pytest.raises(StackFailClosed):
        stack.on_clock(berlin(10, 0))
    assert len(broker.positions_get()) == 1
