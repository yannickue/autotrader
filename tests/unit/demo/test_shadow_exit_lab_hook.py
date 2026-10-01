# ruff: noqa: E501
"""Lane W forward hooks: additive JSON on counterfactual labels, failure isolation, DB-copy migration, report section."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import timedelta

import pytest
from demo_factories import T0, iso, make_decision, make_label, make_snapshot

import demo.labeling as labeling
from demo.contracts import CounterfactualLabel
from demo.labeling import Bar, label_counterfactuals, label_one
from demo.report import build_report, render_markdown
from demo.shadow_exit_lab import LabStats, summarise_groups
from demo.store import DemoStore


@pytest.fixture
def store(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    yield s
    s.close()


def _bars(rows, spread=0.0):
    return [Bar(iso(T0 + timedelta(minutes=5 * i)), o, h, lo, c, spread) for i, (o, h, lo, c) in enumerate(rows)]


ROWS = [(100, 100.4, 99.8, 100.3), (100.3, 100.7, 100.2, 100.6), (100.6, 100.7, 98.9, 99.0), (99.0, 99.5, 98.5, 99.2)]


def test_flag_off_leaves_the_label_without_the_field_and_flag_on_adds_it():
    snap = make_snapshot(entry=100.0, risk=1.0, target_r=1.5, signal_ts=T0)
    off, _ = label_one(snap, _bars(ROWS), labelled_utc=iso(T0 + timedelta(hours=3)))
    assert off.shadow_exit_lab is None
    on, res = label_one(snap, _bars(ROWS), labelled_utc=iso(T0 + timedelta(hours=3)), shadow_exit_lab=True)
    lab = on.shadow_exit_lab
    assert lab["version"] == "swl-1" and lab["policy_set_version"] == "eeq-policies-2"
    assert lab["entry"]["entry_id"] == snap.opportunity_id and lab["entry"]["fill"] == 100.0 and lab["entry"]["stop"] == 99.0
    assert lab["entry"]["policy_entry_ids"] == [snap.opportunity_id]
    assert lab["live"]["profile"] == "fixed_1_5r" and lab["live"]["r"] == pytest.approx(res.r)
    fx = lab["policies"]["fixed_1_5r"]
    assert fx["applicable"] and fx["r"] == pytest.approx(res.r) == pytest.approx(-1.0)  # golden: the baseline equals the labeller
    assert lab["policies"]["TP1_only"]["applicable"] is False  # no structural levels recorded for counterfactuals
    assert on.entry_exit == off.entry_exit and on.hypothetical_r == off.hypothetical_r  # nothing else changes
    json.dumps(on.to_dict())


def test_evaluation_failure_does_not_affect_the_label(monkeypatch):
    import demo.shadow_exit_lab as lab_mod

    def boom(*a, **k):
        raise RuntimeError("policy blew up")

    monkeypatch.setattr(lab_mod, "evaluate_shadow", boom)
    snap = make_snapshot(entry=100.0, risk=1.0, target_r=1.5, signal_ts=T0)
    stats = LabStats()
    lbl, res = label_one(snap, _bars(ROWS), labelled_utc=iso(T0 + timedelta(hours=3)), shadow_exit_lab=True, shadow_stats=stats)
    assert lbl.shadow_exit_lab is None and stats.failed == 1 and "policy blew up" in (stats.last_error or "")
    assert lbl.hypothetical_r == pytest.approx(res.r) and lbl.entry_exit is not None
    monkeypatch.setattr(labeling, "counterfactual_shadow_lab", boom)  # even a failure outside the guard cannot be hidden by the flag-off path
    off, _ = label_one(snap, _bars(ROWS), labelled_utc=iso(T0 + timedelta(hours=3)))
    assert off.shadow_exit_lab is None


def test_label_counterfactuals_with_the_flag_persists_and_is_idempotent(store):
    snap = make_snapshot(entry=100.0, risk=1.0, target_r=1.5, signal_ts=T0, horizon_s=3600)
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, False))
    provider = lambda market, s, e: _bars(ROWS)  # noqa: E731
    now = iso(T0 + timedelta(hours=3))
    stats = LabStats()
    written = label_counterfactuals(store, provider, now, shadow_exit_lab=True, shadow_stats=stats)
    assert len(written) == 1 and stats.ok == 1 and stats.mean_ms is not None
    back = store.get_counterfactual(snap.opportunity_id)
    assert back.shadow_exit_lab["policies"]["fixed_1_5r"]["r"] == pytest.approx(-1.0)
    # relabelling identical bars with the flag OFF or ON is a no-op, never an immutability error
    off_label, _ = label_one(snap, _bars(ROWS), labelled_utc=now)
    assert store.record_counterfactual(off_label, source="ENGINE_REJECTED") is False
    on_label, _ = label_one(snap, _bars(ROWS), labelled_utc=now, shadow_exit_lab=True)
    assert store.record_counterfactual(on_label, source="ENGINE_REJECTED") is False
    # the lab is ADDITIVE json: an older reader that does not know the field still loads the label
    raw = json.loads(store._one("SELECT json FROM counterfactuals")["json"])
    assert CounterfactualLabel.from_dict({k: v for k, v in raw.items() if k != "shadow_exit_lab"}).shadow_exit_lab is None


def test_per_cycle_cap_defers_the_rest_instead_of_writing_labels_without_the_lab(store):
    for i in range(3):
        snap = make_snapshot(i=i, entry=100.0, risk=1.0, target_r=1.5, signal_ts=T0 + timedelta(hours=i), horizon_s=3600)
        store.record_snapshot(snap)
        store.record_decision(make_decision(snap, False))
    provider = lambda market, s, e: [Bar(s, *ROWS[0], 0.0), Bar(iso(T0 + timedelta(hours=9)), *ROWS[1], 0.0)]  # noqa: E731
    now = iso(T0 + timedelta(hours=12))
    first = label_counterfactuals(store, provider, now, shadow_exit_lab=True, shadow_cap=2)
    assert len(first) == 2 and all(lbl.shadow_exit_lab is not None for lbl in first)
    second = label_counterfactuals(store, provider, now, shadow_exit_lab=True, shadow_cap=2)
    assert len(second) == 1 and second[0].shadow_exit_lab is not None  # the deferred one is labelled WITH the lab
    assert all(store.get_counterfactual(s["opportunity_id"]).shadow_exit_lab for s in [{"opportunity_id": x.opportunity_id} for x in first + second])


def test_merge_outcome_extra_is_additive_and_insert_once(store):
    from demo_factories import drive_full_trade

    snap = make_snapshot(i=0)
    it = drive_full_trade(store, snap, upto="PROTECTED")
    assert store.merge_outcome_extra(it.intent_id, "shadow_exit_lab", {"x": 1}) is False  # no row yet: never creates one
    assert store.record_outcome_extra(it.intent_id, {"path_mfe_r": 1.0})
    assert store.merge_outcome_extra(it.intent_id, "shadow_exit_lab", {"x": 1}) is True
    assert store.merge_outcome_extra(it.intent_id, "shadow_exit_lab", {"x": 2}) is False  # insert-once
    assert store.get_outcome_extra(it.intent_id) == {"path_mfe_r": 1.0, "shadow_exit_lab": {"x": 1}}


def test_db_copy_migration_is_additive_no_schema_change(tmp_path):
    src = tmp_path / "legacy.db"
    s = DemoStore(src)
    snap = make_snapshot(i=0, entry=100.0, risk=1.0, target_r=1.5, signal_ts=T0)
    s.record_snapshot(snap)
    s.record_decision(make_decision(snap, False))
    legacy = {k: v for k, v in make_label(snap, r=-1.0).to_dict().items() if k not in ("entry_exit", "shadow_exit_lab")}  # pre-Lane-X/W JSON
    with s._tx() as c:
        c.execute("INSERT INTO counterfactuals(opportunity_id,phase,labelled_utc,json) VALUES(?,?,?,?)", (snap.opportunity_id, snap.phase, legacy["labelled_utc"], json.dumps(legacy, sort_keys=True)))
    s.close()
    copy = tmp_path / "scratch" / "copy.db"
    copy.parent.mkdir()
    shutil.copy(src, copy)
    with sqlite3.connect(copy) as raw:
        before = sorted(r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    with DemoStore(copy) as cs:
        assert cs.get_counterfactual(snap.opportunity_id).shadow_exit_lab is None  # legacy: None, not an error
        assert cs.record_counterfactual(make_label(snap, r=-1.0)) is False
        rep = build_report(cs)
        assert rep["shadow_exit_lab"]["counterfactuals"]["overall"]["n"] == 0  # legacy label has no lab: not counted
    with sqlite3.connect(copy) as raw:
        after = sorted(r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    assert before == after


# ---- report section ------------------------------------------------------------------------------------
def test_report_section_renders_with_small_n_flags_and_the_no_promotion_statement(store):
    for i in range(3):
        snap = make_snapshot(i=i, entry=100.0, risk=1.0, target_r=1.5, signal_ts=T0 + timedelta(hours=i))
        store.record_snapshot(snap)
        store.record_decision(make_decision(snap, False))
        bars = [Bar(iso(T0 + timedelta(hours=i, minutes=5 * k)), *r, 0.0) for k, r in enumerate(ROWS)]
        lbl, _ = label_one(snap, bars, labelled_utc=iso(T0 + timedelta(hours=9)), shadow_exit_lab=True)
        assert store.record_counterfactual(lbl, source="ENGINE_REJECTED")
    rep = build_report(store)
    sec = rep["shadow_exit_lab"]
    cf = sec["counterfactuals"]
    assert cf["overall"]["n"] == 3 and cf["overall"]["small_n"] is True and cf["overall"]["same_entry_assertion"] is True
    assert sec["statement"] == "hypotheses only; forward evidence decides promotion; no policy promoted from this table"
    assert set(cf["overall"]["policies"]) >= {"fixed_1_5r", "TP1_only", "failed_move_exit", "time_decay"}
    assert any(k.startswith("GER40|") for k in cf["groups"])
    md = render_markdown(rep)
    assert "## Shadow exit lab" in md and "n too small" in md and "no policy promoted from this table" in md
    assert "fixed_1_5r" in md and "TP1_plus_runner" in md
    json.dumps(rep, default=str)
    assert summarise_groups([])["overall"] == {"n": 0}
