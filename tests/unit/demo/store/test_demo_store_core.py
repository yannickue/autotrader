import sqlite3

import pytest
from demo_factories import (
    T0,
    drive_full_trade,
    iso,
    make_decision,
    make_exec,
    make_intent,
    make_label,
    make_outcome,
    make_risk,
    make_snapshot,
)

from demo.store import (
    DemoStore,
    DemoStoreError,
    DuplicateIntentError,
    IllegalTransition,
    ImmutableRecordError,
    LeakageError,
    MissingParentError,
    client_order_id_for,
)


@pytest.fixture
def store(tmp_path):
    s = DemoStore(tmp_path / "demo.db")
    yield s
    s.close()


def test_pragmas(store):
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_snapshot_insert_once_and_immutable(store):
    s = make_snapshot()
    assert store.record_snapshot(s) is True
    assert store.record_snapshot(s) is False
    changed = make_snapshot(entry=101.0)
    assert changed.opportunity_id == s.opportunity_id
    with pytest.raises(ImmutableRecordError):
        store.record_snapshot(changed)
    assert store.get_snapshot(s.opportunity_id) == s
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("UPDATE snapshots SET market='X'")
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("DELETE FROM snapshots")


def test_decision_requires_snapshot_and_is_immutable(store):
    s = make_snapshot()
    with pytest.raises(MissingParentError):
        store.record_decision(make_decision(s))
    store.record_snapshot(s)
    d = make_decision(s, accepted=False)
    assert store.record_decision(d) is True
    assert store.record_decision(d) is False
    with pytest.raises(ImmutableRecordError):
        store.record_decision(make_decision(s, accepted=True))
    assert store.get_decision(s.opportunity_id) == d


def test_seen_api_and_restart(tmp_path):
    p = tmp_path / "d.db"
    a = DemoStore(p)
    assert not a.seen("opp-x")
    assert a.mark_seen("opp-x") is True
    assert a.mark_seen("opp-x") is False
    s = make_snapshot()
    a.record_snapshot(s)
    a.close()
    b = DemoStore(p)
    assert b.seen("opp-x") and b.seen(s.opportunity_id)
    assert b.record_snapshot(s) is False
    b.close()


def test_one_intent_per_opportunity(store):
    s = make_snapshot()
    store.record_snapshot(s)
    with pytest.raises(MissingParentError):
        store.record_intent(make_intent(s))
    store.record_decision(make_decision(s, accepted=False))
    with pytest.raises(DemoStoreError):
        store.record_intent(make_intent(s))
    s2 = make_snapshot(i=1)
    store.record_snapshot(s2)
    store.record_decision(make_decision(s2))
    it = make_intent(s2)
    assert store.record_intent(it) is True
    assert store.record_intent(it) is False
    other = type(it)(**{**it.to_dict(), "intent_id": "int-other"})
    with pytest.raises(DuplicateIntentError):
        store.record_intent(other)
    assert len(store.list_intents()) == 1


def test_client_order_id_deterministic():
    assert client_order_id_for("a") == client_order_id_for("a") != client_order_id_for("b")
    assert len(client_order_id_for("int-1234")) <= 27


def test_lifecycle_state_machine(store):
    s = make_snapshot()
    it = drive_full_trade(store, s, upto="SENT")
    assert store.get_state(it.intent_id) == "SENT"
    assert store.transition(it.intent_id, "SENT") is False  # idempotent
    with pytest.raises(IllegalTransition):
        store.transition(it.intent_id, "PLANNED")  # backward
    with pytest.raises(IllegalTransition):
        store.transition(it.intent_id, "PROTECTED")  # skip
    with pytest.raises(IllegalTransition):
        store.transition(it.intent_id, "BOGUS")
    assert store.transition(it.intent_id, "FILLED") is True
    assert store.transition(it.intent_id, "PROTECTED") is True
    assert store.transition(it.intent_id, "CLOSED") is True
    with pytest.raises(IllegalTransition):
        store.transition(it.intent_id, "SENT")
    ev = [e["to_state"] for e in store.intent_events(it.intent_id)]
    assert ev == ["PLANNED", "RISK_APPROVED", "SENT", "FILLED", "PROTECTED", "CLOSED"]


def test_risk_rejected_and_send_failed_terminal(store):
    s = make_snapshot()
    store.record_snapshot(s)
    store.record_decision(make_decision(s))
    it = make_intent(s)
    store.record_intent(it)
    store.transition(it.intent_id, "RISK_REJECTED")
    with pytest.raises(IllegalTransition):
        store.transition(it.intent_id, "SENT")
    s2 = make_snapshot(i=1)
    it2 = drive_full_trade(store, s2, upto="RISK_APPROVED")
    store.transition(it2.intent_id, "SEND_FAILED")
    with pytest.raises(IllegalTransition):
        store.transition(it2.intent_id, "SENT")


def test_outcome_requires_closed_and_insert_once(store):
    s = make_snapshot()
    it = drive_full_trade(store, s, upto="PROTECTED")
    with pytest.raises(IllegalTransition):
        store.record_outcome(it.intent_id, make_outcome())
    store.transition(it.intent_id, "CLOSED")
    assert store.closed_without_outcome() == [it.intent_id]
    o = make_outcome(1.0)
    assert store.record_outcome(it.intent_id, o) is True
    assert store.record_outcome(it.intent_id, o) is False
    with pytest.raises(ImmutableRecordError):
        store.record_outcome(it.intent_id, make_outcome(2.0))
    assert store.closed_without_outcome() == []
    assert store.count_trades() == 1
    recs = store.learning_records()
    assert len(recs) == 1 and recs[0].outcome == o and recs[0].snapshot == s


def test_execution_append_only_latest_wins(store):
    s = make_snapshot()
    it = drive_full_trade(store, s, upto="FILLED")
    e2 = make_exec(fees=-1.5)
    assert store.record_execution(it.intent_id, e2) is True
    assert store.record_execution(it.intent_id, e2) is False
    assert store.get_execution(it.intent_id) == e2
    assert len(store.execution_history(it.intent_id)) == 2
    assert store.record_risk(it.intent_id, make_risk()) is False


def test_counterfactual_only_for_rejected_and_once(store):
    s = make_snapshot()
    store.record_snapshot(s)
    store.record_decision(make_decision(s, accepted=True))
    with pytest.raises(DemoStoreError):
        store.record_counterfactual(make_label(s))
    s2 = make_snapshot(i=1)
    store.record_snapshot(s2)
    store.record_decision(make_decision(s2, accepted=False))
    assert [d.opportunity_id for d, _ in store.rejected_unlabelled()] == [s2.opportunity_id]
    assert store.record_counterfactual(make_label(s2)) is True
    assert store.record_counterfactual(make_label(s2)) is False
    with pytest.raises(ImmutableRecordError):
        store.record_counterfactual(make_label(s2, r=3.0))
    assert store.rejected_unlabelled() == []


def test_shadow_prediction_rules(store):
    s = make_snapshot()
    store.record_snapshot(s)
    assert store.record_shadow_prediction(s.opportunity_id, "logit", {"p": 0.6}, iso(T0)) is True
    assert store.record_shadow_prediction(s.opportunity_id, "logit", {"p": 0.6}, iso(T0)) is False
    with pytest.raises(ImmutableRecordError):
        store.record_shadow_prediction(s.opportunity_id, "logit", {"p": 0.9}, iso(T0))
    dec = make_decision(s, accepted=False)
    late = iso(T0.replace(year=2027))
    store.record_decision(dec)
    with pytest.raises(LeakageError):
        store.record_shadow_prediction(s.opportunity_id, "gbdt", {"p": 0.1}, late)
    store.record_counterfactual(make_label(s))
    with pytest.raises(LeakageError):
        store.record_shadow_prediction(s.opportunity_id, "gbdt", {"p": 0.1}, iso(T0))
    assert len(store.list_shadow_predictions(s.opportunity_id)) == 1


def test_decision_rejects_prediction_later_than_decision(store):
    s = make_snapshot()
    store.record_snapshot(s)
    store.record_shadow_prediction(s.opportunity_id, "m", {"p": 1}, iso(T0.replace(year=2027)))
    with pytest.raises(LeakageError):
        store.record_decision(make_decision(s))


def test_phase_separation(store):
    a = make_snapshot(phase="DISCOVERY")
    b = make_snapshot(i=1, phase="FROZEN")
    for s in (a, b):
        drive_full_trade(store, s)
    assert [x.opportunity_id for x in store.list_snapshots(phase="FROZEN")] == [b.opportunity_id]
    assert store.count_trades("DISCOVERY") == 1 and store.count_trades("FROZEN") == 1
    assert len(store.list_intents(phase="FROZEN")) == 1
    assert len(store.list_decisions(phase="DISCOVERY")) == 1
    assert len(store.learning_records("FROZEN")) == 1
    with pytest.raises(ValueError):
        store.list_snapshots(phase="LIVE")
    for t in (
        "snapshots",
        "decisions",
        "intents",
        "intent_events",
        "risk_records",
        "execution_records",
        "outcomes",
        "shadow_predictions",
        "counterfactuals",
    ):
        cols = [r[1] for r in store._conn.execute(f"PRAGMA table_info({t})")]
        assert "phase" in cols, t


def test_meta_once(store):
    assert store.set_meta_once("k", "1") is True
    assert store.set_meta_once("k", "2") is False
    assert store.get_meta("k") == "1"
