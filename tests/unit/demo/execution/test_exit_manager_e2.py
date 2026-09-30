# ruff: noqa: E501
"""Lane E2: staged exits end-to-end on the fake broker (structure trailing / failure, late-session rules,
partial ENTRY fill accounting, engine exit reasons) + the exit-plan producer + the runner wiring."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from demo import structure as st
from demo.contracts import ENGINE_EXIT_REASONS
from demo.execution import exit_manager as em
from demo.execution.events import PositionClosed
from demo.store import UNCENSORED_EXITS
from tests.unit.demo.execution.stack_harness import (
    berlin_offset_s,
    build_broker,
    make_intent,
    make_stack,
)
from tests.unit.demo.execution.test_exit_manager import (
    PLAN_TP1_RUNNER,
    PLAN_TP1_TP2,
    now_of,
    policy,
    staged_cfg,
    triple,
    wait_sync,
)
from tests.unit.nautilus_mt5.harness import make_rates

D = Decimal


@pytest.fixture
def env(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=staged_cfg())
    yield broker, stack
    stack.stop()


def stack_with(tmp_path, **policy_over):
    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=staged_cfg(pol=policy(**policy_over)))
    return broker, stack


def load_bars(stack, broker, closes, *, wick=1.0):
    """Closed M5 bars ending with the FORMING bar (the last close is the forming one, never used)."""
    end = datetime.now(UTC)
    offset = berlin_offset_s(end)
    last_open = int(end.timestamp() // 300 * 300)
    n = len(closes)
    rows = [
        (last_open - (n - 1 - i) * 300 + offset, c, c + wick, c - wick, c, 100, 12, 0)
        for i, c in enumerate(closes)
    ]
    broker.rates[5] = make_rates(rows)
    stack.bar_source._cache.clear()  # the live source caches a fresh frame: serve the new rates


def backdate(stack, intent_id="intent-1", hours=2):
    """Entry happened `hours` ago (so the bars loaded by the test count as POST-entry structure)."""
    reg = stack._registry
    with reg._lock:
        reg._db.execute(
            "UPDATE intents SET created_utc=? WHERE intent_id=?",
            ((datetime.now(UTC) - timedelta(hours=hours)).isoformat(), intent_id),
        )


def open_position(stack, plan=PLAN_TP1_RUNNER, **intent):
    events = stack.submit(make_intent(**intent), {"family": "fam-a", "atr": "20", "exit_plan": plan})
    assert [type(e).__name__ for e in events] == ["Accepted", "Fill", "ProtectionConfirmed"], events


PLAN_FAR = {"stages": [{"target_price": "25500", "close_fraction": "0.4", "stage_id": "tp1", "source": "STRUCTURE:far"}]}
RISING = [25000, 25010, 25020, 25030, 25040, 25030, 25020, 25015, 25025, 25040, 25055, 25050, 25045, 25060, 25070, 25080, 25078, 25085, 25085]
FALLING = [25100 - 20 * i for i in range(20)]


# -- partial ENTRY fill ---------------------------------------------------------------------------------------


def test_partial_entry_fill_does_not_look_like_a_taken_partial_exit(env):
    broker, stack = env
    stack.start()
    broker.fill_plan = [[(1.0, 25001.5)]]  # requested 1.75 lots, the broker fills 1.0 (IOC remainder dies)
    open_position(stack, PLAN_TP1_TP2)
    ctx = json.loads(stack._registry.get("intent-1").context)
    assert ctx["initial_quantity"] == "1.0" or Decimal(str(ctx["initial_quantity"])) == D("1.0")
    assert Decimal(str(ctx["requested_quantity"])) == D("1.75")
    wait_sync(stack)
    assert triple(stack, broker) == (D("1.0"),) * 3  # broker == local == stop-protected (child resized to the fill)
    broker.set_quote(25060.5, 25062.0)  # TP1
    stack.manage_exits(now_of(stack))
    # 0.4 * FILLED 1.0 = 0.4 -> 0.25 lot. (Bug: original=1.75 made realized 0.75 >= stage qty 0.5 => TP1 skipped.)
    assert broker.positions_get()[0].volume == 0.75
    wait_sync(stack)
    assert triple(stack, broker) == (D("0.75"),) * 3
    (entry_plan,) = [e for e in stack.exit_log() if e["kind"] == "entry_plan"]
    assert entry_plan["initial_quantity"] == "1.0" and entry_plan["requested_quantity"] == "1.75"
    (partial,) = [e for e in stack.exit_log() if e["kind"] == "partial_exit"]
    assert partial["initial_quantity"] == "1.0" and partial["quantity_before"] == "1.0"
    assert partial["plan_fractions"] is None  # hand-written plan without fractions metadata


# -- structure trailing + structural failure --------------------------------------------------------------------


def test_structure_trailing_tightens_behind_a_confirmed_swing_then_structure_failure_exits(tmp_path):
    broker, stack = stack_with(tmp_path, structure_trailing=True, structure_failure_exit=True)
    stack.start()
    try:
        open_position(stack, PLAN_FAR)
        backdate(stack)
        load_bars(stack, broker, RISING)
        broker.set_quote(25085.0, 25086.5)
        stack.manage_exits(now_of(stack))
        sl = broker.positions_get()[0].sl
        assert 24950.0 < sl < 25044.0  # behind the newest confirmed higher low (25044) minus the ATR buffer
        assert [e for e in stack.exit_log() if e["kind"] == "stop_moved"]
        wait_sync(stack)
        assert triple(stack, broker) == (D("1.75"),) * 3
        # price falls back: the stop never loosens
        load_bars(stack, broker, RISING)
        broker.set_quote(25060.0, 25061.5)
        stack.manage_exits(now_of(stack))
        assert broker.positions_get()[0].sl == sl
        # a closed bar breaks the higher low while the price is still above the stop: structural failure
        load_bars(stack, broker, [*RISING[:-2], 25042, 25042])
        broker.set_quote(25043.0, 25044.5)
        events = stack.manage_exits(now_of(stack))
        closed = [e for e in events if isinstance(e, PositionClosed)]
        assert len(closed) == 1 and closed[0].exit_reason == "EXIT_ENGINE_STRUCTURE"
        assert closed[0].exit_reason in UNCENSORED_EXITS and closed[0].exit_reason in ENGINE_EXIT_REASONS
        assert broker.positions_get() == ()
    finally:
        stack.stop()


# -- D / F: late session ---------------------------------------------------------------------------------------


def test_D_profitable_late_position_ratchets_protection_to_cost_adjusted_break_even(tmp_path):
    broker, stack = stack_with(tmp_path, late_window=timedelta(minutes=45), late_loser_momentum_threshold=D("-0.3"))
    stack.start()
    try:
        open_position(stack, flat_in_s=1200)
        load_bars(stack, broker, [25000] * 20)
        broker.set_quote(25010.0, 25011.5)
        assert stack.manage_exits(now_of(stack)) == []  # tightened, NOT closed: it may run until the forced flat
        pos = broker.positions_get()[0]
        assert pos.volume == 1.75 and 25001.5 <= pos.sl < 25010.0  # >= entry: cost-adjusted, never loosened below
        assert pos.sl >= 25001.5 + 1.0  # includes (at least most of) the exit spread 1.5
    finally:
        stack.stop()


def test_F_a_deteriorating_loser_exits_before_the_mandatory_deadline(tmp_path):
    broker, stack = stack_with(tmp_path, late_window=timedelta(minutes=45), late_loser_momentum_threshold=D("-0.3"))
    stack.start()
    try:
        open_position(stack, flat_in_s=1200)
        load_bars(stack, broker, FALLING)
        broker.set_quote(24990.0, 24991.5)  # a loser, stop 24950 still far away
        events = stack.manage_exits(now_of(stack))
        closed = [e for e in events if isinstance(e, PositionClosed)]
        assert len(closed) == 1 and closed[0].exit_reason == "EXIT_ENGINE_EOD"
        assert broker.positions_get() == () and stack.open_intents() == ()
    finally:
        stack.stop()


def test_a_losing_trade_is_not_cut_by_the_late_rule_when_the_session_is_not_late(tmp_path):
    broker, stack = stack_with(tmp_path, late_window=timedelta(minutes=45), late_loser_momentum_threshold=D("-0.3"))
    stack.start()
    try:
        open_position(stack, flat_in_s=6 * 3600)
        load_bars(stack, broker, FALLING)
        broker.set_quote(24990.0, 24991.5)
        sends = broker.order_send_calls
        assert stack.manage_exits(now_of(stack)) == [] and broker.order_send_calls == sends
    finally:
        stack.stop()


# -- exit reasons / accounting ----------------------------------------------------------------------------------


def test_engine_exit_reason_mapping_is_explicit_and_uncensored_except_emergency():
    from exits.models import ExitDecision, ExitReason
    from risk.models import RiskSide

    def decision(reason, **meta):
        return ExitDecision(request_id="r", position_id="p", instrument="X", close_side=RiskSide.SELL, quantity=D("1"),
                            is_partial=False, reason=reason, reason_detail="", timestamp=datetime.now(UTC), metadata=meta)

    f = em.engine_exit_reason
    assert f(decision(ExitReason.TAKE_PROFIT, stage_index=0)) == "EXIT_ENGINE_TP1"
    assert f(decision(ExitReason.TAKE_PROFIT, stage_index=1)) == "EXIT_ENGINE_TP2"
    assert f(decision(ExitReason.TRAILING_STOP)) == "EXIT_ENGINE_TRAIL"
    assert f(decision(ExitReason.INVALIDATION_STOP, stop_stage="break_even")) == "EXIT_ENGINE_BREAK_EVEN"
    assert f(decision(ExitReason.INVALIDATION_STOP, stop_stage="original")) == "EXIT_ENGINE_STOP"
    assert f(decision(ExitReason.STRUCTURE_FAILURE)) == "EXIT_ENGINE_STRUCTURE"
    assert f(decision(ExitReason.TIME_STOP)) == "EXIT_ENGINE_TIME_STOP"
    assert f(decision(ExitReason.LATE_SESSION_DETERIORATION)) == "EXIT_ENGINE_EOD"
    assert f(decision(ExitReason.MFE_GIVEBACK)) == "EXIT_ENGINE_GIVEBACK"
    assert f(decision(ExitReason.EMERGENCY_RISK_EXIT)) == "SAFETY_FLATTEN"  # safety, not a strategy exit
    assert "SAFETY_FLATTEN" not in UNCENSORED_EXITS and "MANUAL" not in UNCENSORED_EXITS and "EXTERNAL" not in UNCENSORED_EXITS
    assert ENGINE_EXIT_REASONS <= UNCENSORED_EXITS


def test_stop_exit_slippage_is_measured_against_the_stop_in_force(tmp_path):
    """After a stop move the broker stop fires at the NEW level: slippage vs the initial stop would be wrong."""
    broker, stack = stack_with(tmp_path, breakeven_trigger_r_multiple=D("1"))
    stack.start()
    try:
        open_position(stack)
        broker.set_quote(25055.0, 25056.5)
        stack.manage_exits(now_of(stack))
        moved = broker.positions_get()[0].sl
        assert moved > 24950.0
        state = json.loads(stack._registry.get("intent-1").context)["exit_state"]
        assert Decimal(state["current_stop"]) == Decimal(str(moved))
        row = stack._registry.get("intent-1")
        ctx = json.loads(row.context)
        assert Decimal(ctx["exit_state"]["current_stop"]) != Decimal(row.stop)
    finally:
        stack.stop()


# -- fixed_1_5r golden ---------------------------------------------------------------------------------------------


def test_fixed_policy_orders_and_registry_are_unchanged_by_lane_e2(tmp_path):
    from tests.unit.demo.execution.stack_harness import FAST

    broker = build_broker()
    stack = make_stack(broker, tmp_path, config=FAST)
    try:
        stack.start()
        stack.submit(make_intent(), {"family": "fam-a", "atr": "20", "exit_plan": PLAN_TP1_TP2})
        req = broker.request_log[0]
        assert (req["sl"], req["tp"], req["volume"]) == (24950.0, 25150.0, 1.75)
        assert stack._registry.get("intent-1").target == "25150.0"
        broker.set_quote(25125.0, 25126.5)
        assert stack.manage_exits(now_of(stack)) == [] and stack.exit_log() == []
        assert stack.exit_policy == "fixed_1_5r"
    finally:
        stack.stop()


# -- producer --------------------------------------------------------------------------------------------------------


def _bars():
    from tests.unit.demo.test_structure import ZIGZAG, frame

    return frame(ZIGZAG)


def produce(**over):
    kw = dict(
        direction=1, entry_ref=108.0, stop=105.0, target=112.0, family="fam-a", market="GER40", atr=2.0,
        frame=_bars(), spread=0.2, tick_size=0.1, structure_levels=None, target_is_structural=False,
        cfg=em.ExitPlanConfig(), staged=True,
    )
    kw.update(over)
    return em.produce_exit_context(**kw)


def test_default_geometry_is_family_and_the_structure_geometry_is_logged_as_shadow():
    out = produce()
    assert out["source"] == "family" and out["structure_stop"] is None  # no change to the stop / sizing
    assert out["shadow"]["family"]["stop"] == "105.0" and out["shadow"]["structure"]["stop"] is not None
    plan = out["exit_plan"]
    assert plan["geometry_source"] == "family" and plan["stages"][0]["source"] == "R"  # honest: a fixed-R family target
    assert plan["stages"][0]["r_multiple"] == str(D("4.0") / D("3.0"))
    assert st.SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED in plan["markers"]
    assert plan["fractions"] == {"tp1": "0.5", "tp2": None, "runner": "0.5"}


def test_structure_opt_in_offers_the_chart_stop_and_chart_targets():
    cfg = em.ExitPlanConfig(structure_families=frozenset({"fam-a"}))
    out = produce(cfg=cfg)
    assert out["source"] == "structure" and out["structure_stop"] is not None
    plan = out["exit_plan"]
    assert all(s["source"].startswith("STRUCTURE:") for s in plan["stages"])
    prices = [D(s["target_price"]) for s in plan["stages"]]
    assert prices == sorted(prices) and all(p > D("108") for p in prices)
    assert out["structure_stop"] < 108.0
    other = produce(cfg=cfg, family="fam-b")  # a family that did NOT opt in keeps its own stop
    assert other["source"] == "family" and other["structure_stop"] is None


def test_family_supplied_structure_levels_take_priority_and_fractions_are_family_specific():
    cfg = em.ExitPlanConfig(family_fractions={"fam-a": (D("0.3"), D("0.3"))})
    out = produce(cfg=cfg, structure_levels=[{"price": 111.0, "id": "pdh"}, 110.0, 100.0, "junk"])
    stages = out["exit_plan"]["stages"]
    assert [s["target_price"] for s in stages] == ["110.0", "111.0"]  # nearest first, the level behind entry dropped
    assert [s["close_fraction"] for s in stages] == ["0.3", "0.3"]
    assert out["exit_plan"]["fractions"] == {"tp1": "0.3", "tp2": "0.3", "runner": "0.4"}
    assert em.parse_exit_plan(out["exit_plan"])[0].source == "STRUCTURE:level1"


def test_short_plan_is_mirrored_and_direction_integrity_is_asserted():
    plan = em.build_exit_plan(
        direction=-1, entry_ref=D("100"), stop=D("103"), fractions=(D("0.5"), D("0.25")), geometry=None,
        source="family", family_target=None, target_is_structural=False, structure_levels=[97, 95],
    )
    assert [s["target_price"] for s in plan["stages"]] == ["97", "95"]
    with pytest.raises(ValueError, match="direction integrity"):
        em.build_exit_plan(direction=1, entry_ref=D("100"), stop=D("101"), fractions=(D("0.5"),), geometry=None,
                           source="family", family_target=D("105"), target_is_structural=True)
    none = em.build_exit_plan(direction=1, entry_ref=D("100"), stop=D("98"), fractions=(D("0.5"),), geometry=None,
                              source="family", family_target=None, target_is_structural=False)
    assert none["stages"] == [] and st.NO_STRUCTURAL_TP1 in none["markers"]


def test_plan_config_validation():
    with pytest.raises(ValueError):
        em.ExitPlanConfig(geometry_source="bogus")
    with pytest.raises(ValueError):
        em.ExitPlanConfig(default_fractions=(D("0.7"), D("0.5")))
    assert em.default_staged_exit_policy().policy_id == "staged-e2-v1"
    assert em.ExitPlanConfig().fractions_for("anything") == em.DEFAULT_STAGE_FRACTIONS


def test_a_produced_plan_drives_the_staged_manager_end_to_end(tmp_path):
    """Plan from the producer (structure levels) -> submit -> TP1 partial -> runner stays, stop tighten-only."""
    broker, stack = stack_with(tmp_path, breakeven_after_first_stage=True)
    stack.start()
    try:
        out = produce(
            direction=1, entry_ref=25002.0, stop=24950.0, target=25150.0, atr=20.0, target_is_structural=True,
            structure_levels=[25060, 25120], frame=_bars(), cfg=em.ExitPlanConfig(family_fractions={"fam-a": (D("0.4"), D("0.3"))}),
        )
        stack.submit(make_intent(), {"family": "fam-a", "atr": "20", "exit_plan": out["exit_plan"], "exit_meta": out["exit_meta"]})
        ctx = json.loads(stack._registry.get("intent-1").context)
        assert ctx["exit_meta"]["fractions"] == {"tp1": "0.4", "tp2": "0.3", "runner": "0.3"}
        assert triple(stack, broker) == (D("1.75"),) * 3
        broker.set_quote(25060.5, 25062.0)
        stack.manage_exits(now_of(stack))
        assert broker.positions_get()[0].volume == 1.25
        wait_sync(stack)
        assert triple(stack, broker) == (D("1.25"),) * 3
        assert broker.positions_get()[0].sl >= 24950.0  # tighten-only (break-even after TP1)
        broker.set_quote(25120.5, 25122.0)
        stack.manage_exits(now_of(stack))
        wait_sync(stack)
        assert broker.positions_get()[0].volume == 0.75
        assert triple(stack, broker) == (D("0.75"),) * 3  # the runner stays, fully protected
    finally:
        stack.stop()
