"""Lane I store additions: risk_detail + tca_records (new tables, contracts unchanged)."""

import sqlite3
from decimal import Decimal

import pytest
from demo_factories import make_decision, make_intent, make_snapshot

from demo.store import DemoStore, ImmutableRecordError, MissingParentError


@pytest.fixture
def store(tmp_path):
    s = DemoStore(tmp_path / "demo.db")
    yield s
    s.close()


def _intent(store, i=0):
    s = make_snapshot(i=i)
    store.record_snapshot(s)
    store.record_decision(make_decision(s))
    it = make_intent(s)
    store.record_intent(it)
    return it


def test_risk_detail_accepted_and_rejected_roundtrip(store):
    it = _intent(store)
    acc = {"decision": "TRADE", "quantity": Decimal("0.5"), "equity_risk_pct": Decimal("0.4"),
           "nested": {"cluster": "INDEX", "before": Decimal("1.0")}}
    rej = {"decision": "SKIP", "reject_code": "size_below_min", "gate_reject_class": "SAFETY",
           "violated_cap": "max_position_stop_risk_fraction"}
    assert store.record_risk_detail(it.intent_id, "ACCEPTED", acc) is True
    assert store.record_risk_detail(it.intent_id, "ACCEPTED", acc) is False  # idempotent
    assert store.record_risk_detail(it.intent_id, "REJECTED", rej) is True
    got = store.get_risk_detail(it.intent_id, "ACCEPTED")
    assert got["quantity"] == "0.5" and got["nested"]["cluster"] == "INDEX"
    rows = store.list_risk_details("DISCOVERY", "REJECTED")
    assert len(rows) == 1
    assert rows[0]["reject_code"] == "size_below_min" and rows[0]["gate_class"] == "SAFETY"
    assert rows[0]["opportunity_id"] == it.opportunity_id
    assert len(store.list_risk_details()) == 2
    assert store.list_risk_details("FROZEN") == []


def test_risk_detail_is_immutable_and_needs_intent(store):
    it = _intent(store)
    store.record_risk_detail(it.intent_id, "ACCEPTED", {"a": 1})
    with pytest.raises(ImmutableRecordError):
        store.record_risk_detail(it.intent_id, "ACCEPTED", {"a": 2})
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("UPDATE risk_detail SET json='{}'")
    with pytest.raises(MissingParentError):
        store.record_risk_detail("int-unknown", "ACCEPTED", {"a": 1})
    with pytest.raises(ValueError):
        store.record_risk_detail(it.intent_id, "MAYBE", {"a": 1})
    assert store.record_risk_detail(it.intent_id, "REJECTED", None) is False


def test_tca_roundtrip_immutable_and_backward_compatible_reopen(tmp_path):
    path = tmp_path / "demo.db"
    with DemoStore(path) as s:
        it = _intent(s)
        tca = {"slippage_vs_intended": Decimal("0.2"), "fill_vs_mid": Decimal("0.1"),
               "latency_total_ms": 12.5, "movement_to_cost": None}
        assert s.record_tca(it.intent_id, tca) is True
        assert s.record_tca(it.intent_id, tca) is False
        with pytest.raises(ImmutableRecordError):
            s.record_tca(it.intent_id, {"x": 1})
        assert s.record_tca(it.intent_id, {}) is False
    with DemoStore(path) as s:  # reopen: schema is CREATE IF NOT EXISTS, same version
        rows = s.list_tca("DISCOVERY")
        assert len(rows) == 1 and rows[0]["slippage_vs_intended"] == "0.2"
        assert s.get_tca(rows[0]["intent_id"])["latency_total_ms"] == 12.5
        assert rows[0]["stage"] == "ENTRY" and s.get_tca(rows[0]["intent_id"], "EXIT") is None
        assert s.record_tca(rows[0]["intent_id"], {"exit_slippage_vs_level": "0.1"}, "EXIT") is True
        assert len(s.list_tca()) == 2
        with pytest.raises(ValueError):
            s.record_tca(rows[0]["intent_id"], {"x": 1}, "MID")
