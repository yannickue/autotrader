# ruff: noqa: E501
"""Bugfix: the broker rounds the stop to the LOOSER tick -> ``ExitPosition`` (initial vs current stop) used to raise and the
exit manager silently skipped the row for its whole life (no TP1 / break-even / trail / structure exit / time stop).

Harness: real Nautilus kernel + fake MT5 broker (GER40 tick 0.01, EURUSD tick 0.00001)."""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal

import pytest

from demo import exit_profiles as xp
from demo.execution import exit_manager as em
from tests.unit.demo.execution.stack_harness import FAST, build_broker, make_intent, make_stack
from tests.unit.demo.execution.test_exit_manager import now_of, triple, wait_sync

D = Decimal

LONG_STOP = 24927.720714285715  # tick-unaligned family stop; the broker keeps 24927.72 (looser for a long)
SHORT_STOP = 1.1787971428571428  # tick-unaligned family stop; the broker keeps 1.17880 (looser for a short)

PLAN_GER = {"stages": [{"target_price": "25060", "close_fraction": "0.5", "stage_id": "tp1", "source": "STRUCTURE:pdh"}]}
PLAN_EUR = {"stages": [{"target_price": "1.15668", "close_fraction": "0.5", "stage_id": "tp1", "source": "STRUCTURE:pdl"}]}


def cfg():
    return dataclasses.replace(FAST, exit_policy=em.EXIT_POLICY_PROFILES, staged_exit=em.default_staged_exit_policy())


def make_env(tmp_path, **broker_cfg):
    broker = build_broker(**broker_cfg)
    stack = make_stack(broker, tmp_path, config=cfg())
    stack.start()
    return broker, stack


def open_row(stack, plan, **intent):
    ctx = {"family": "STRUCT", "atr": "20", "exit_plan": plan, "exit_profile": xp.attribution(xp.route_for("STRUCT", "breakout"))}
    events = stack.submit(make_intent(**intent), ctx)
    assert [type(e).__name__ for e in events] == ["Accepted", "Fill", "ProtectionConfirmed"], events


def ctx_of(stack):
    return json.loads(stack._registry.get("intent-1").context)


@pytest.fixture
def env(tmp_path):
    broker, stack = make_env(tmp_path)
    yield broker, stack
    stack.stop()


@pytest.fixture
def env_looser(tmp_path):
    broker, stack = make_env(tmp_path, sl_tick_rounding="looser")
    yield broker, stack
    stack.stop()


def open_short_eur(stack, **kw):
    open_row(stack, PLAN_EUR, market="EURUSD", broker_symbol="EURUSD", direction=-1, entry_ref=1.16995, stop=SHORT_STOP, target=1.14, **kw)


# -- (1) SHORT, broker rounds the stop to the looser tick ----------------------------------------------------------------


def test_short_looser_rounded_stop_is_still_managed_tp1_partial_and_hwm_saved(env_looser):
    broker, stack = env_looser
    open_short_eur(stack)
    (pos,) = broker.positions_get()
    row = stack._registry.get("intent-1")
    assert D(str(pos.sl)) > D(row.stop)  # the broker stop is LOOSER than the stored raw stop (the bug precondition)
    assert abs(D(str(pos.sl)) - D(row.stop)) < D("0.00001")
    broker.set_symbol_quote("EURUSD", 1.15650, 1.15660)  # ask <= 1.15668 (past 1.5R)
    stack.manage_exits(now_of(stack))
    assert not [e for e in stack.exit_log() if e["kind"] == "skip_invalid_position"]
    (partial,) = [e for e in stack.exit_log() if e["kind"] == "partial_exit"]
    assert partial["stage_id"] == "tp1"
    assert "high_water_mark" in ctx_of(stack)["exit_state"]
    assert ctx_of(stack)["exit_state"]["stages_completed"] == 1


# -- (2) LONG ----------------------------------------------------------------------------------------------------------------


def test_long_looser_rounded_stop_is_still_managed_tp1_partial(env_looser):
    broker, stack = env_looser
    open_row(stack, PLAN_GER, stop=LONG_STOP)
    (pos,) = broker.positions_get()
    assert D(str(pos.sl)) < D(stack._registry.get("intent-1").stop)  # looser for a long
    broker.set_quote(25060.5, 25062.0)
    stack.manage_exits(now_of(stack))
    assert not [e for e in stack.exit_log() if e["kind"] == "skip_invalid_position"]
    assert [e for e in stack.exit_log() if e["kind"] == "partial_exit"]
    assert "high_water_mark" in ctx_of(stack)["exit_state"]


# -- (3) more than one tick looser is still refused -- and now VISIBLE ---------------------------------------------------


def test_three_ticks_looser_broker_stop_is_refused_but_counted_and_visible(env):
    broker, stack = env
    open_row(stack, PLAN_GER, stop=24950.0)
    (pos,) = broker.positions_get()
    vol0 = pos.volume
    pos.sl = 24949.97  # 3 ticks (0.01) looser than the stored 24950
    broker.set_quote(25060.5, 25062.0)
    for _ in range(3):
        stack.manage_exits(now_of(stack))
    assert broker.positions_get()[0].volume == vol0  # nothing was done
    assert not [e for e in stack.exit_log() if e["kind"] == "partial_exit"]
    h = stack.exit_manager_health()
    assert h["rows_skipped"] == 1 and h["rows_managed"] == 0
    assert h["consecutive_skips_max"] == 3 and h["skipped_rows"] == {"intent-1": 3}
    assert h["last_skip_reason"].startswith("skip_invalid_position")
    assert stack._exit_manager.counters["skip_invalid_position"] == 3


def test_skip_streak_resets_when_the_row_is_managed_again(env):
    broker, stack = env
    open_row(stack, PLAN_GER, stop=24950.0)
    (pos,) = broker.positions_get()
    pos.sl = 24949.97
    stack.manage_exits(now_of(stack))
    assert stack.exit_manager_health()["consecutive_skips_max"] == 1
    pos.sl = 24950.0
    stack.manage_exits(now_of(stack))
    h = stack.exit_manager_health()
    assert h["consecutive_skips_max"] == 0 and h["rows_managed"] == 1 and h["rows_skipped"] == 0


# -- (4) a tighter broker stop works as before ---------------------------------------------------------------------------


def test_tighter_broker_stop_is_managed_as_before(env):
    broker, stack = env
    open_row(stack, PLAN_GER, stop=24950.0)
    (pos,) = broker.positions_get()
    pos.sl = 24960.0  # tighter than the stored stop
    broker.set_quote(25060.5, 25062.0)
    stack.manage_exits(now_of(stack))
    assert [e for e in stack.exit_log() if e["kind"] == "partial_exit"]
    assert stack.exit_manager_health()["rows_skipped"] == 0


# -- (5) tick-aligned equal stops: golden, unchanged ---------------------------------------------------------------------


def test_tick_aligned_equal_stops_unchanged_behaviour(env):
    broker, stack = env
    open_row(stack, PLAN_GER, stop=24950.0)
    vol0 = broker.positions_get()[0].volume
    broker.set_quote(25060.5, 25062.0)
    stack.manage_exits(now_of(stack))
    (pos,) = broker.positions_get()
    assert pos.volume < vol0 and pos.sl == 24950.0  # TP1 partial taken, stop untouched
    assert len([e for e in stack.exit_log() if e["kind"] == "partial_exit"]) == 1


# -- (7) end to end: broker volume == local remaining == protected remaining ---------------------------------------------


def test_e2e_looser_rounded_continuation_entry_partial_keeps_protection_invariant(env_looser):
    broker, stack = env_looser
    open_row(stack, PLAN_GER, stop=LONG_STOP)
    before = triple(stack, broker)
    assert before[0] == before[1] == before[2]
    broker.set_quote(25060.5, 25062.0)
    stack.manage_exits(now_of(stack))
    (pos,) = broker.positions_get()
    assert D(str(pos.volume)) < before[0]  # the TP1 partial executed
    wait_sync(stack)
    after = triple(stack, broker)
    assert after[0] == after[1] == after[2] == D(str(pos.volume))  # broker == local == SL child
    assert D(str(broker.positions_get()[0].sl)) == D("24927.72")
