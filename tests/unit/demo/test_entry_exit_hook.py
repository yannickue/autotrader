# ruff: noqa: E501
"""Lane X forward/live hook: entry-vs-exit fields on closed trades + labelled counterfactuals, report section, migration."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import timedelta

import pytest
from demo_factories import T0, drive_full_trade, iso, make_decision, make_label, make_snapshot

from coverage_analysis.entry_exit import r2_summary
from coverage_analysis.r2 import load_r2
from demo.contracts import CounterfactualLabel, OutcomeRecord
from demo.labeling import Bar, PathPoint, label_one, path_analytics, realised_entry_exit
from demo.report import build_report, render_markdown
from demo.store import DemoStore


@pytest.fixture
def store(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    yield s
    s.close()


def _pts(*rows):
    return [PathPoint(iso(T0 + timedelta(minutes=5 * (i + 1))), hi, lo) for i, (hi, lo) in enumerate(rows)]


# ---- extended outcome analytics -----------------------------------------------------------------------
def test_path_analytics_has_the_extended_mfe_levels():
    a = path_analytics(direction=1, entry_price=100.0, initial_stop=99.0, entry_ts=iso(T0), exit_price=101.0, exit_ts=iso(T0 + timedelta(minutes=30)),
                       path=_pts((100.3, 99.9), (100.8, 100.2), (101.6, 100.9), (102.1, 101.0)))
    assert a["time_to_0.25R_s"] == 300.0 and a["time_to_0.75R_s"] == 600.0 and a["time_to_1R_s"] == 900.0
    assert a["time_to_1.5R_s"] == 900.0 and a["time_to_2R_s"] == 1200.0
    none = path_analytics(direction=1, entry_price=100.0, initial_stop=99.0, entry_ts=iso(T0), exit_price=99.0, exit_ts=iso(T0 + timedelta(minutes=10)), path=_pts((100.1, 99.5)))
    assert none["time_to_0.75R_s"] is None and none["time_to_1.5R_s"] is None and none["time_to_2R_s"] is None


def test_realised_trade_mfe_then_stop_is_useful_entry_with_exit_giveback():
    ee = realised_entry_exit(
        direction=1, entry_price=100.0, initial_stop=99.0, entry_ts=iso(T0), exit_price=99.0, exit_ts=iso(T0 + timedelta(minutes=20)),
        path=_pts((100.3, 99.9), (100.6, 100.2), (100.5, 99.0)), final_gross_r=-1.0, tp1=100.5, tp2=101.0,
    )
    assert ee["mfe_r"] == pytest.approx(0.6) and ee["mae_r"] == pytest.approx(1.0)
    assert ee["entry_exit_label"] == "POTENTIAL_USEFUL_ENTRY_EXIT_GIVEBACK" and ee["capture_label"] == "POOR_PROFIT_CAPTURE"
    assert ee["tp1_reached"] is True and ee["tp2_reached"] is False and ee["time_to_tp1_s"] == 600.0
    assert ee["mfe_before_mae"] is True and ee["eeq_version"].startswith("eeq-")
    assert ee["time_to_0.5R_s"] == 600.0 and ee["time_to_0.75R_s"] is None


def test_realised_trade_straight_to_the_stop_is_entry_failure():
    ee = realised_entry_exit(
        direction=-1, entry_price=100.0, initial_stop=101.0, entry_ts=iso(T0), exit_price=101.0, exit_ts=iso(T0 + timedelta(minutes=10)),
        path=_pts((100.4, 100.0), (101.0, 100.2)), final_gross_r=-1.0,
    )
    assert ee["entry_label"] == "ENTRY_FAILURE" and ee["capture_ratio"] is None and ee["mfe_before_mae"] is False


def test_realised_entry_exit_never_raises_on_bad_input():
    assert realised_entry_exit(direction=1, entry_price=100.0, initial_stop=100.0, entry_ts=iso(T0), exit_price=99.0, exit_ts=iso(T0), path=[], final_gross_r=0.0) is None


# ---- counterfactual labels ------------------------------------------------------------------------------
def _bars(rows, spread=0.0):
    return [Bar(iso(T0 + timedelta(minutes=5 * i)), o, h, lo, c, spread) for i, (o, h, lo, c) in enumerate(rows)]


def test_counterfactual_label_carries_entry_exit_fields_consistent_with_the_hypothetical_result():
    snap = make_snapshot(entry=100.0, risk=1.0, target_r=1.5, signal_ts=T0)
    bars = _bars([(100, 100.4, 99.8, 100.3), (100.3, 100.7, 100.2, 100.6), (100.6, 100.7, 98.9, 99.0)])
    label, res = label_one(snap, bars, labelled_utc=iso(T0 + timedelta(hours=3)))
    ee = label.entry_exit
    assert ee is not None and ee["mfe_r"] == pytest.approx(res.mfe_r) == pytest.approx(0.7)
    assert ee["mae_r"] == pytest.approx(res.mae_r)
    assert ee["entry_exit_label"] == "POTENTIAL_USEFUL_ENTRY_EXIT_GIVEBACK" and res.r == pytest.approx(-1.0)
    assert ee["time_to_0.5R_s"] == 300.0 and ee["path_exit_kind"] == "STOP"
    short = make_snapshot(entry=100.0, risk=1.0, target_r=1.5, direction=-1, signal_ts=T0)
    sb = _bars([(100, 100.2, 99.0, 99.3), (99.3, 99.4, 98.4, 98.5)], spread=0.1)
    lab2, res2 = label_one(short, sb, labelled_utc=iso(T0 + timedelta(hours=3)))
    assert res2.exit_kind == "TARGET" and lab2.entry_exit["mfe_r"] == pytest.approx(res2.mfe_r) and lab2.entry_exit["path_exit_kind"] == "TARGET"
    assert lab2.entry_exit["entry_exit_label"] == "GOOD_ENTRY_GOOD_CAPTURE"


def test_label_roundtrips_through_the_immutable_store_and_old_readers_ignore_the_field(store):
    snap = make_snapshot(entry=100.0, risk=1.0, signal_ts=T0)
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, False))
    label, _ = label_one(snap, _bars([(100, 100.4, 99.8, 100.3), (100.3, 100.7, 98.9, 99.0)]), labelled_utc=iso(T0 + timedelta(hours=3)))
    assert store.record_counterfactual(label) is True
    back = store.get_counterfactual(snap.opportunity_id)
    assert back.entry_exit["eeq_version"] == label.entry_exit["eeq_version"]
    assert store.record_counterfactual(label) is False  # identical re-insert: no-op
    raw = json.loads(store._one("SELECT json FROM counterfactuals")["json"])
    assert raw["hypothetical_r"] == pytest.approx(-1.0)
    assert CounterfactualLabel.from_dict({k: v for k, v in raw.items() if k != "entry_exit"}).entry_exit is None


# ---- report section -------------------------------------------------------------------------------------
def _closed_trade(store, i, *, gross_r, mfe, mae, direction=1):
    snap = make_snapshot(i=i, direction=direction)
    it = drive_full_trade(store, snap, upto="PROTECTED")
    store.transition(it.intent_id, "CLOSED")
    store.record_outcome(it.intent_id, OutcomeRecord(
        gross_r=gross_r, net_r=gross_r - 0.05, pnl_eur=gross_r * 100, mfe_r=mfe, mae_r=mae, time_to_mfe_s=300.0, time_to_mae_s=600.0,
        holding_s=900.0, exit_reason="STOP", closed_utc=iso(T0 + timedelta(hours=i + 1)),
    ))
    return it, snap


def test_report_section_separates_entry_and_capture_and_flags_small_n(store):
    _closed_trade(store, 0, gross_r=-1.0, mfe=0.6, mae=1.0)  # potential useful entry, lost: EXIT_GIVEBACK
    _closed_trade(store, 1, gross_r=-1.0, mfe=0.0, mae=1.0)  # entry failure
    _closed_trade(store, 2, gross_r=1.5, mfe=2.0, mae=0.3)  # good entry, good capture
    snap = make_snapshot(i=10)
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, False))
    store.record_counterfactual(make_label(snap, r=-1.0))  # mfe 0.5 / mae 1.0 / r -1
    rep = build_report(store)
    eeq = rep["entry_exit_quality"]
    real = eeq["real_trades"]["overall"]
    assert real["n"] == 3 and real["label_counts"]["POTENTIAL_USEFUL_ENTRY_EXIT_GIVEBACK"] == 1
    assert real["label_counts"]["ENTRY_FAILURE"] == 1 and real["label_counts"]["GOOD_ENTRY_GOOD_CAPTURE"] == 1
    assert real["verdict"] == "INCONCLUSIVE-n" and "n too small" in real["flag"]
    assert eeq["counterfactuals"]["overall"]["n"] == 1
    assert eeq["analysis_version"].startswith("eeq-") and eeq["label_spec_version"].startswith("eeq-labels")
    md = render_markdown(rep)
    assert "## Entry quality vs exit quality" in md and "low n" in md
    json.dumps(rep, default=str)  # the JSON report serialises


def test_report_section_uses_stored_path_fields_when_present_and_falls_back_for_legacy_rows(store):
    it, _ = _closed_trade(store, 0, gross_r=-1.0, mfe=0.7, mae=1.0)
    ee = realised_entry_exit(direction=1, entry_price=100.0, initial_stop=98.5, entry_ts=iso(T0), exit_price=98.5, exit_ts=iso(T0 + timedelta(minutes=20)),
                             path=_pts((100.4, 99.9), (101.05, 100.2), (100.5, 98.5)), final_gross_r=-1.0, tp1=101.0)
    store.record_outcome_extra(it.intent_id, {"entry_exit": ee, "final_gross_r": -1.0})
    _closed_trade(store, 1, gross_r=1.0, mfe=1.2, mae=0.1)  # legacy: no outcome_extra at all
    real = build_report(store)["entry_exit_quality"]["real_trades"]
    assert real["overall"]["n"] == 2 and real["n_with_path_fields"] == 1
    assert real["overall"]["tp1_reach_share"] == 1.0  # from the stored flag of the one row that has it


# ---- migration on a DB copy ----------------------------------------------------------------------------
def test_legacy_db_copy_migrates_additively(tmp_path):
    src = tmp_path / "legacy.db"
    s = DemoStore(src)
    snap = make_snapshot(i=0)
    s.record_snapshot(snap)
    s.record_decision(make_decision(snap, False))
    legacy = {k: v for k, v in make_label(snap, r=-1.0).to_dict().items() if k != "entry_exit"}  # pre-Lane-X JSON
    with s._tx() as c:
        c.execute("INSERT INTO counterfactuals(opportunity_id,phase,labelled_utc,json) VALUES(?,?,?,?)", (snap.opportunity_id, snap.phase, legacy["labelled_utc"], json.dumps(legacy, sort_keys=True)))
    _closed_trade(s, 5, gross_r=-1.0, mfe=0.6, mae=1.0)
    s.close()
    copy = tmp_path / "scratch" / "copy.db"
    copy.parent.mkdir()
    shutil.copy(src, copy)
    with sqlite3.connect(copy) as raw:
        before = sorted(r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    with DemoStore(copy) as cs:
        lab = cs.get_counterfactual(snap.opportunity_id)
        assert lab.entry_exit is None  # legacy label: None, not an error
        assert cs.record_counterfactual(make_label(snap, r=-1.0)) is False  # relabelling the same values is still a no-op
        rep = build_report(cs)
        assert rep["entry_exit_quality"]["counterfactuals"]["overall"]["n"] == 1  # classified from the stored MFE/MAE/R
        assert rep["entry_exit_quality"]["real_trades"]["overall"]["n"] == 1
    with sqlite3.connect(copy) as raw:
        after = sorted(r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    assert before == after  # no new table / no schema migration
    summary = r2_summary(str(copy))
    assert summary["n_rows"] == 2 and "REAL_TRADE" in " ".join(summary["cells"]) and "COUNTERFACTUAL" in " ".join(summary["cells"])
    assert len(load_r2(copy)) >= 1
    with pytest.raises(ValueError):
        r2_summary(str(tmp_path / "artifacts" / "demo.sqlite"))  # the live artifacts/ location is refused
