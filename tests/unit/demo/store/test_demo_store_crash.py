"""Kill-point simulations: close/reopen the db (and one real os._exit) at each lifecycle point."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from demo_factories import (
    drive_full_trade,
    make_decision,
    make_exec,
    make_intent,
    make_outcome,
    make_risk,
    make_snapshot,
)

from demo.store import DemoStore, DuplicateIntentError


def _reopen(store):
    p = store.path
    store.close()
    return DemoStore(p)


def _no_duplicates(store, opp_id):
    assert len(store.list_intents()) == 1
    it = make_intent(store.get_snapshot(opp_id))
    # a re-driven runner that builds a different intent id for the same opportunity is stopped
    with pytest.raises(DuplicateIntentError):
        store.record_intent(type(it)(**{**it.to_dict(), "intent_id": "int-dup"}))


def test_kill_before_risk(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    snap = make_snapshot()
    s.record_snapshot(snap)
    s.record_decision(make_decision(snap))
    it = make_intent(snap)
    s.record_intent(it)
    s = _reopen(s)
    assert s.get_state(it.intent_id) == "PLANNED"
    assert s.recover_open_intents() == []  # nothing sent
    assert [i["intent_id"] for i in s.unfinished_intents()] == [it.intent_id]
    assert s.record_intent(it) is False  # replay is a no-op
    _no_duplicates(s, snap.opportunity_id)
    s.close()


def test_kill_before_send(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    snap = make_snapshot()
    it = drive_full_trade(s, snap, upto="RISK_APPROVED")
    s = _reopen(s)
    assert s.get_state(it.intent_id) == "RISK_APPROVED"
    assert s.recover_open_intents() == []
    assert s.record_risk(it.intent_id, make_risk()) is False
    assert s.transition(it.intent_id, "RISK_APPROVED") is False
    _no_duplicates(s, snap.opportunity_id)
    s.close()


@pytest.mark.parametrize("upto", ["SENT", "FILLED", "PROTECTED"])
def test_kill_with_possible_broker_exposure(tmp_path, upto):
    s = DemoStore(tmp_path / "d.db")
    snap = make_snapshot()
    it = drive_full_trade(s, snap, upto=upto)
    s = _reopen(s)
    open_ = s.recover_open_intents()
    assert [i["intent_id"] for i in open_] == [it.intent_id]
    assert open_[0]["state"] == upto and open_[0]["client_order_id"]
    # replaying the whole pipeline after restart creates nothing new
    assert s.record_snapshot(snap) is False
    assert s.record_decision(make_decision(snap)) is False
    assert s.record_intent(it) is False
    assert s.transition(it.intent_id, upto) is False
    assert s.seen(snap.opportunity_id)
    _no_duplicates(s, snap.opportunity_id)
    s.close()


def test_kill_during_exit_then_restart_completes(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    snap = make_snapshot()
    it = drive_full_trade(s, snap, upto="PROTECTED")
    s.transition(it.intent_id, "CLOSED")  # closed at broker, crash before outcome written
    s = _reopen(s)
    assert s.recover_open_intents() == []
    assert s.closed_without_outcome() == [it.intent_id]
    s.record_execution(it.intent_id, make_exec(fees=-2.0))
    assert s.record_outcome(it.intent_id, make_outcome(1.0)) is True
    s = _reopen(s)
    assert s.closed_without_outcome() == []
    assert s.count_trades() == 1
    assert s.record_outcome(it.intent_id, make_outcome(1.0)) is False
    s.close()


def test_restart_after_close_is_clean(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    snap = make_snapshot()
    drive_full_trade(s, snap)
    s = _reopen(s)
    assert s.recover_open_intents() == [] and s.unfinished_intents() == []
    assert len(s.learning_records()) == 1
    s.close()


def test_real_process_kill_after_send(tmp_path):
    db = tmp_path / "kill.db"
    src = Path(__file__).resolve().parents[4] / "src"
    tests_demo = Path(__file__).resolve().parents[1]
    code = textwrap.dedent(f"""
        import os, sys
        sys.path[:0] = [{str(src)!r}, {str(tests_demo)!r}]
        from demo.store import DemoStore
        from demo_factories import drive_full_trade, make_snapshot
        s = DemoStore({str(db)!r})
        drive_full_trade(s, make_snapshot(), upto="SENT")
        os._exit(9)   # hard kill: no close(), no atexit, no flush
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 9, r.stderr
    s = DemoStore(db)
    open_ = s.recover_open_intents()
    assert len(open_) == 1 and open_[0]["state"] == "SENT"
    snap = make_snapshot()
    assert s.record_snapshot(snap) is False
    assert len(s.list_intents()) == 1
    s.close()
