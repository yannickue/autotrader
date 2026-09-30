# ruff: noqa: E501
"""Lane I addendum: IN_DOUBT (M1), transient halt-new-exposure (H5), idle closed markets, trainer thread
(M7), learning default, forced-flat-on-shutdown."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from demo import runner as rn
from demo.execution.events import Accepted, Fill, PositionClosed, ProtectionConfirmed, Rejected
from demo.execution.stack_port import StackFailClosed
from demo.store import CANCELLED, CLOSED, PROTECTED, IllegalTransition
from demo.testing import T0, FakeClock, FakeStack, floor5, make_pair

D = Decimal


def _cycle(r):
    r.start()
    r.run_cycle()


# ============================================================ M1 IN_DOUBT
def _in_doubt_setup(env):
    snap, dec, intent = make_pair()
    iid = intent.intent_id
    env.stack.script[iid] = [
        Accepted(iid, D("1"), D("10000"), D("0.0015"), D("15"), D("2"), risk_detail={"decision": "TRADE"}),
        Rejected(iid, "order_outcome_unknown", {"decision": "SKIP", "reject_code": "order_outcome_unknown", "gate_reject_class": "SAFETY"}),
    ]
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    _cycle(r)
    return r, intent


def test_in_doubt_entry_is_not_cancelled_and_halts_new_exposure(env):
    r, intent = _in_doubt_setup(env)
    assert env.store.get_state(intent.intent_id) == "IN_DOUBT"
    assert not r.can_trade() and r.halt_reason.startswith("order_outcome_unknown")
    assert env.store.get_risk(intent.intent_id).approved  # sized and sent
    assert intent.intent_id in json.loads(r.cfg.heartbeat_path.read_text())["in_doubt_intents"]
    assert [x["intent_id"] for x in env.store.recover_open_intents()] == [intent.intent_id]


def test_late_fill_and_close_resolve_in_doubt_and_write_the_outcome(env):
    r, intent = _in_doubt_setup(env)
    iid = intent.intent_id
    fill = Fill(iid, D("100.05"), D("1"), D("0.1"), D("0.05"), D("-1.0"), D("0"), "ord-" + iid, "pos-" + iid)
    env.stack.pending += [fill, ProtectionConfirmed(iid, "pos-" + iid, D(str(intent.stop)), None)]
    env.clock.advance(minutes=1)
    r.run_cycle()
    assert env.store.get_state(iid) == PROTECTED  # IN_DOUBT -> SENT -> FILLED -> PROTECTED
    env.stack.pending.append(PositionClosed(
        iid, "pos-" + iid, "STOP", exit_price=D("98.5"), exit_quantity=D("1"),
        closed_utc=(env.clock() + timedelta(minutes=30)).isoformat(), commission=D("-0.5"), swap=D("0")))
    env.clock.advance(minutes=35)
    r.run_cycle()
    assert env.store.get_state(iid) == CLOSED
    assert env.store.get_outcome(iid) is not None
    states = [e["to_state"] for e in env.store.intent_events(iid)]
    assert states[:4] == ["PLANNED", "RISK_APPROVED", "SENT", "IN_DOUBT"] and states[-1] == "CLOSED"
    assert iid not in json.loads(r.cfg.heartbeat_path.read_text())["in_doubt_intents"]


def test_restart_keeps_in_doubt_when_broker_knows_it_else_cancels_for_manual_review(env):
    _r, intent = _in_doubt_setup(env)
    iid = intent.intent_id
    env.stack.positions[iid] = intent  # the stack still lists it
    r2 = env.build()
    r2.start()
    assert env.store.get_state(iid) == "IN_DOUBT"
    env.stack.positions.pop(iid)
    r3 = env.build()
    r3.start()
    assert env.store.get_state(iid) == CANCELLED
    assert any("in_doubt_unknown_to_broker" in w for w in r3._warnings)


def test_store_in_doubt_lifecycle_is_backward_compatible(env):
    snap, dec, intent = make_pair()
    env.store.record_snapshot(snap)
    env.store.record_decision(dec)
    env.store.record_intent(intent)
    for st in ("RISK_APPROVED", "SENT", "IN_DOUBT", "SENT", "IN_DOUBT", "FILLED", "PROTECTED", "CLOSED"):
        env.store.transition(intent.intent_id, st)
    with pytest.raises(IllegalTransition):
        env.store.transition(intent.intent_id, "IN_DOUBT")


# ============================================================ H5 transient conditions
def test_reconciliation_drop_halts_new_exposure_then_recovers_without_exit(env):
    r = env.build(transient_grace_s=300.0)
    _cycle(r)
    env.stack.set_account(reconciliation="RECONCILING")
    env.clock.advance(minutes=1)
    r.run_cycle()
    assert r.fail_reason is None and not r.can_trade()
    assert "reconciliation" in r._transient and env.stack.submits == [] and env.stack.halts == []  # not latched
    hb = json.loads(r.cfg.heartbeat_path.read_text())
    assert hb["halt_new_exposure"] is True and "reconciliation" in hb["transient_conditions"]
    assert not r._should_exit(env.clock())
    sleeps = [r._sleep_s() for _ in range(4)]
    assert sleeps == sorted(sleeps) and sleeps[-1] > sleeps[0] and max(sleeps) <= r.cfg.transient_backoff_max_s
    env.stack.set_account(reconciliation="RECONCILED")
    env.clock.advance(minutes=5)
    env.engine.push("GER40", make_pair(signal_ts=floor5(env.clock()), tag="t2"))
    r.run_cycle()
    assert r.can_trade() and not r._transient and len(env.stack.submits) == 1  # resumed by itself
    assert r._sleep_s() == r.cfg.poll_interval_s


@pytest.mark.parametrize(("change", "key"), [
    (dict(reconciliation="NOT_RECONCILED"), "reconciliation"),
    (dict(connected=False), "disconnected"),
    (dict(kill_switch=True), "stack_halt"),
])
def test_transient_condition_persisting_beyond_grace_exits_7(env, change, key):
    r = env.build(transient_grace_s=120.0)
    _cycle(r)
    env.stack.set_account(**change)
    env.clock.advance(minutes=1)
    r.run_cycle()
    assert r.fail_reason is None and key in r._transient
    env.clock.advance(minutes=3)
    r.run_cycle()
    assert r.fail_reason and "persisted" in r.fail_reason
    assert r.run(max_cycles=1) == 7


def test_stack_failclosed_transient_prefix_retries_others_fail_closed(env):
    r = env.build()
    r.start()

    def disconnect():
        raise StackFailClosed("broker_disconnect:copy_rates")

    env.stack.poll_events = disconnect
    r.run_cycle()
    assert r.fail_reason is None and not r.can_trade()
    env.stack.poll_events = lambda: []
    r.run_cycle()
    assert r.can_trade() and r.fail_reason is None

    def boom():
        raise StackFailClosed("unprotected_exposure")

    env.stack.poll_events = boom
    r.run_cycle()
    assert r.fail_reason and "unprotected_exposure" in r.fail_reason


def test_permanent_conditions_still_fail_closed_immediately(env):
    env.stack.set_account(is_demo=False)
    r = env.build(transient_grace_s=10_000.0)
    assert r.run(max_cycles=1) == 7


# ------------------------------------------------------------ closed markets are idle, not stale
def test_weekend_stale_feeds_are_idle_no_halt_no_exit(env):
    env.clock.now = datetime(2026, 10, 3, 14, 0, 10, tzinfo=UTC)  # Saturday
    env.stack.bar_source.frozen["GER40"] = env.clock() - timedelta(hours=40)
    env.stack.bar_source.frozen["NAS100"] = env.clock() - timedelta(hours=40)
    r = env.build(markets=("GER40", "NAS100"), all_stale_grace_s=0.0, all_stale_exit_s=0.0)
    r.start()
    for _ in range(3):
        env.clock.advance(minutes=30)
        r.run_cycle()
    assert r.fail_reason is None and r.can_trade()
    assert r._feed["GER40"]["stale"] and r._feed["GER40"]["idle_market_closed"]
    assert env.engine.calls == []  # stale/closed markets are not scanned


def test_stale_feed_of_a_market_that_should_be_open_halts_then_exits_after_grace(env):
    # Thu 09:00Z = 11:00 Berlin, inside the GER40 cash session
    env.stack.bar_source.frozen["GER40"] = env.clock() - timedelta(hours=2)
    r = env.build(all_stale_grace_s=600.0, all_stale_exit_s=3600.0)
    r.start()
    r.run_cycle()
    assert r.can_trade() and r.fail_reason is None  # inside the grace
    env.clock.advance(minutes=11)
    r.run_cycle()
    assert not r.can_trade() and r.fail_reason is None  # halted, still managing
    assert json.loads(r.cfg.heartbeat_path.read_text())["stale_halt_since"]
    env.clock.advance(minutes=60)
    env.stack.bar_source.frozen["GER40"] = env.clock() - timedelta(hours=3)
    r.run_cycle()
    assert r.fail_reason == "all_feeds_stale"


def test_stale_halt_clears_when_the_feed_recovers(env):
    env.stack.bar_source.frozen["GER40"] = env.clock() - timedelta(hours=2)
    r = env.build(all_stale_grace_s=0.0, all_stale_exit_s=3600.0)
    r.start()
    r.run_cycle()
    assert not r.can_trade()
    env.stack.bar_source.frozen.pop("GER40")
    env.clock.advance(minutes=5)
    r.run_cycle()
    assert r.can_trade() and r.fail_reason is None


# ------------------------------------------------------------------- M7 learning / trainer thread
def test_trainer_runs_off_thread_only_when_flat_and_never_blocks_the_cycle(env):
    gate = threading.Event()
    seen = {}

    class Slow:
        def update(self, store):
            seen["thread"] = threading.current_thread().name
            seen["store_is_own"] = store is not env.store
            gate.wait(5)
            return {"ok": 1}

    r = env.build(trainer=Slow(), trainer_every_s=60.0)
    _cycle(r)
    env.clock.advance(minutes=2)
    t0 = time.monotonic()
    r.run_cycle()  # starts the trainer, must not wait for it
    assert time.monotonic() - t0 < 2.0
    time.sleep(0.05)
    assert r.status(env.clock())["trainer_running"] is True
    gate.set()
    r.join_training(5)
    env.clock.advance(minutes=1)
    r.run_cycle()
    assert seen["thread"] == "demo-trainer" and seen["store_is_own"] and r._last_train_report == {"ok": 1}


def test_trainer_not_started_while_a_position_is_open(env):
    calls = []

    class T:
        def update(self, store):
            calls.append(1)

    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(trainer=T(), trainer_every_s=60.0)
    _cycle(r)
    assert env.store.get_state(intent.intent_id) == PROTECTED
    env.clock.advance(minutes=5)
    r.run_cycle()
    r.join_training(1)
    assert calls == []


def test_learning_defaults_off_in_demo_auto_on_in_shadow(tmp_path, monkeypatch):
    monkeypatch.delenv("MT5_ALLOW_ACCOUNT_LOGIN", raising=False)
    got = []

    def fake_load(model_dir, enabled):
        got.append(enabled)
        return None, None, "disabled" if enabled is False else "test"

    monkeypatch.setattr(rn, "load_learning", fake_load)

    def factory(**kw):
        return FakeStack(FakeClock(T0), markets=kw["markets"], shadow=kw["dry_run"])

    for mode, arg in (("demo-auto", None), ("shadow", None), ("demo-auto", True), ("shadow", False)):
        r = rn.build_live_runner(mode, artifacts_dir=tmp_path / f"{mode}{arg}", stack_factory=factory, learning=arg)
        r.store.close()
    assert got == [False, True, True, False]


# ------------------------------------------------------------------- forced flat on shutdown
def test_forced_flat_on_shutdown_is_off_by_default_and_documented_when_unsupported(env):
    r = env.build()
    _cycle(r)
    r.shutdown()
    assert not any("forced_flat" in w for w in r._warnings)
    r2 = env.build(forced_flat_on_shutdown=True)
    _cycle(r2)
    r2.shutdown()
    assert any("forced_flat_on_shutdown_unsupported" in w for w in r2._warnings)


def test_forced_flat_on_shutdown_uses_flatten_all_when_the_stack_offers_it(env):
    called = []
    env.stack.flatten_all = lambda why: called.append(why) or []
    r = env.build(forced_flat_on_shutdown=True)
    _cycle(r)
    r.shutdown()
    assert called == ["shutdown"]
