# ruff: noqa: E501
"""Coverage auditor: classification, causes, denominator, status, trade/lab gaps, segmentation, read-only, CLI.

Synthetic DemoStores are built with the REAL store writers in tmp_path; the production DB is never opened.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))
from demo_factories import (
    T0,
    drive_full_trade,
    iso,
    make_decision,
    make_exec,
    make_intent,
    make_label,
    make_risk,
    make_snapshot,
)

from demo.store import DemoStore
from research_workbench import coverage as C

HORIZON_END = T0 + timedelta(hours=1)  # make_snapshot(i=0): signal T0, horizon 3600 s
GRACE = timedelta(hours=6)


def _reject(store, i=0, **kw):
    snap = make_snapshot(i=i * 24, **kw)  # one day apart so horizons never overlap
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, accepted=False))
    return snap


def _label(store, snap):
    store.record_counterfactual(
        make_label(snap), gate_code="min_space", gate_class="QUALITY", gate_codes=["min_space"]
    )


def _end(snap):
    return parse(snap.signal_ts_utc) + timedelta(seconds=snap.geometry.expected_horizon_s)


def parse(s):
    from demo.store import parse_utc

    return parse_utc(s)


def _report(tmp_path, store, now, **kw):
    store.close()
    return C.coverage_for_path(
        tmp_path / "d.db", now=iso(now), phase=kw.pop("phase", "DISCOVERY"), **kw
    )


@pytest.fixture
def store(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    yield s
    with contextlib.suppress(Exception):
        s.close()


def _opp(rep):
    return rep["sections"]["opportunities"]["metrics"]


def test_horizon_open_is_pending_not_missing(tmp_path, store):
    snap = _reject(store)
    rep = _report(tmp_path, store, T0 + timedelta(minutes=30))
    m = _opp(rep)
    assert m["total_opportunities"] == 1 and m["non_traded"] == 1
    assert m["non_traded_pending_horizon"] == 1 and m["horizon_open_by_cause"] == {
        C.PENDING_HORIZON: 1
    }
    assert m["non_traded_eligible"] == 0 and m["counterfactual_unexpected_missing"] == 0
    assert m["eligible_coverage_pct"] is None
    assert rep["status"] == C.GREEN and not rep["no_promotion_claim"]
    assert snap.opportunity_id


def test_elapsed_and_labelled_is_complete(tmp_path, store):
    snap = _reject(store)
    _label(store, snap)
    rep = _report(tmp_path, store, HORIZON_END + timedelta(minutes=1))
    m = _opp(rep)
    assert (
        m["counterfactual_labelled"] == 1
        and m["non_traded_eligible"] == 1
        and m["eligible_coverage_pct"] == 100.0
    )
    assert rep["status"] == C.GREEN


def test_elapsed_unexplained_missing_is_unexpected_and_red(tmp_path, store):
    snap = _reject(store)
    store.set_bar_pointer(
        snap.market, iso(HORIZON_END + timedelta(hours=1))
    )  # bars were observed past the horizon
    rep = _report(tmp_path, store, HORIZON_END + GRACE + timedelta(minutes=1))
    m = _opp(rep)
    assert m["counterfactual_unexpected_missing"] == 1 and m["unexplained_missing"] == 1
    assert m["unexpected_missing_by_cause"] == {C.UNKNOWN: 1}
    assert m["eligible_coverage_pct"] == 0.0
    assert rep["status"] == C.RED and rep["no_promotion_claim"] is True
    sample = rep["sections"]["opportunities"]["unexpected_missing_sample"]
    assert sample[0]["cause"] == C.UNKNOWN


def test_waiting_for_bars_is_explained_pending(tmp_path, store):
    snap = _reject(store)
    store.set_bar_pointer(
        snap.market, iso(HORIZON_END - timedelta(minutes=10))
    )  # pointer short of the horizon end
    rep = _report(tmp_path, store, HORIZON_END + timedelta(minutes=30))
    m = _opp(rep)
    assert m["counterfactual_pending"] == 1 and m["counterfactual_unexpected_missing"] == 0
    assert m["pending_by_cause"] == {C.WAITING_FOR_BARS: 1}
    assert rep["status"] == C.AMBER and not rep["no_promotion_claim"]


def test_waiting_for_bars_beyond_grace_becomes_unexpected(tmp_path, store):
    _reject(store)  # no pointer at all
    rep = _report(tmp_path, store, HORIZON_END + GRACE + timedelta(minutes=1))
    m = _opp(rep)
    assert m["counterfactual_unexpected_missing"] == 1
    assert m["unexpected_missing_by_cause"] == {C.WAITING_FOR_BARS: 1}
    assert m["unexplained_missing"] == 0  # explained (stale bars), still visible and bounded


def test_incomplete_bar_coverage_inside_grace(tmp_path, store):
    snap = _reject(store)
    store.set_bar_pointer(snap.market, iso(HORIZON_END + timedelta(minutes=5)))
    rep = _report(tmp_path, store, HORIZON_END + timedelta(minutes=30))
    assert _opp(rep)["pending_by_cause"] == {C.INCOMPLETE_BAR_COVERAGE: 1}
    assert rep["status"] == C.AMBER


def test_scan_error_in_window_is_bar_provider_error(tmp_path, store):
    snap = _reject(store)
    store.record_scan_error(snap.market, iso(T0 + timedelta(minutes=20)), "provider timeout")
    rep = _report(tmp_path, store, HORIZON_END + timedelta(minutes=30))
    assert _opp(rep)["unexpected_missing_by_cause"] == {C.BAR_PROVIDER_ERROR: 1}
    assert _opp(rep)["unexplained_missing"] == 0


def test_invalid_geometry_is_flagged(tmp_path, store):
    snap = make_snapshot(i=0)
    bad = replace(
        snap, geometry=replace(snap.geometry, stop=snap.geometry.intended_entry)
    )  # zero risk distance
    store.record_snapshot(bad)
    store.record_decision(make_decision(bad, accepted=False))
    rep = _report(tmp_path, store, HORIZON_END + timedelta(minutes=30))
    assert _opp(rep)["unexpected_missing_by_cause"] == {C.INVALID_GEOMETRY: 1}


def test_diagnostic_bars_provider_distinguishes_causes(tmp_path, store):
    _reject(store, 0)
    _reject(store, 1)
    store.close()

    def provider(market, start, end):
        if start.startswith("2026-10-02"):
            raise RuntimeError("boom")
        return []

    rep = C.coverage_for_path(
        tmp_path / "d.db",
        now=iso(T0 + timedelta(days=3)),
        bars_provider=provider,
        phase="DISCOVERY",
    )
    causes = _opp(rep)["unexpected_missing_by_cause"]
    assert causes == {C.WAITING_FOR_BARS: 1, C.BAR_PROVIDER_ERROR: 1}


def test_denominator_excludes_horizon_open(tmp_path, store):
    done = _reject(store, 0)
    _label(store, done)
    _reject(store, 1)  # signal at T0+24h: horizon still open at "now"
    rep = _report(tmp_path, store, T0 + timedelta(hours=24, minutes=10))
    m = _opp(rep)
    assert (
        m["non_traded"] == 2
        and m["non_traded_pending_horizon"] == 1
        and m["non_traded_eligible"] == 1
    )
    assert m["eligible_coverage_pct"] == 100.0  # the horizon-open one must not dilute it
    assert m["non_traded"] == m["non_traded_pending_horizon"] + m["non_traded_eligible"]
    assert (
        m["non_traded_eligible"]
        == m["counterfactual_labelled"]
        + m["counterfactual_pending"]
        + m["counterfactual_unexpected_missing"]
    )


def test_accepted_without_intent_young_is_not_eligible(tmp_path, store):
    snap = make_snapshot(i=0)
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, accepted=True))  # accepted, never an intent
    rep = _report(
        tmp_path, store, HORIZON_END + timedelta(seconds=2)
    )  # decided 5 s after T0, horizon long over, age > 600 s
    assert _opp(rep)["non_traded_eligible"] == 1
    young = C.coverage_for_path(
        tmp_path / "d.db", now=iso(T0 + timedelta(seconds=100)), phase="DISCOVERY"
    )
    assert _opp(young)["horizon_open_by_cause"] == {C.PENDING_INTENT_WINDOW: 1}


def test_status_green_amber_red_matrix(tmp_path):
    th = C.CoverageThresholds()
    base = {
        "traded_and_counterfactual": 0,
        "undecided": 0,
        "unexplained_missing": 0,
        "non_traded_eligible": 10,
        "counterfactual_pending": 0,
        "counterfactual_unexpected_missing": 0,
        "pending_by_cause": {},
        "unexpected_missing_by_cause": {},
    }
    assert C.assess_opportunities([], dict(base), th)[0] == C.GREEN
    assert (
        C.assess_opportunities(
            [], {**base, "counterfactual_pending": 2, "pending_by_cause": {"X": 2}}, th
        )[0]
        == C.AMBER
    )
    assert (
        C.assess_opportunities([], {**base, "counterfactual_pending": 8}, th)[0] == C.RED
    )  # explained but not bounded
    assert (
        C.assess_opportunities([], {**base, "unexplained_missing": 1}, th)[0] == C.RED
    )  # 10% unexplained
    assert (
        C.assess_opportunities(
            [], {**base, "non_traded_eligible": 100, "unexplained_missing": 1}, th
        )[0]
        == C.AMBER
    )  # isolated 1%


def test_systematic_segment_missingness_is_red(tmp_path, store):
    for i in range(5):  # one market fully missing (explained, stale bars past grace), another fine
        _reject(store, i, market="BAD")
    for i in range(5, 12):
        s = _reject(store, i, market="OK")
        _label(store, s)
    rep = _report(tmp_path, store, T0 + timedelta(days=40))
    assert rep["status"] == C.RED
    assert any("market=BAD" in r for r in rep["sections"]["opportunities"]["reasons"])


def test_segmentation_counts(tmp_path, store):
    a = _reject(store, 0, market="GER40", direction=1)
    b = _reject(store, 1, market="US30", direction=-1)
    _reject(store, 2, market="US30", direction=1)
    _label(store, a)
    _label(store, b)
    rep = _report(tmp_path, store, T0 + timedelta(days=10))
    seg = rep["sections"]["opportunities"]["segments"]
    assert (
        seg["market"]["US30"]["non_traded_eligible"] == 2
        and seg["market"]["US30"]["counterfactual_labelled"] == 1
    )
    assert seg["market"]["US30"]["eligible_coverage_pct"] == 50.0
    assert (
        seg["direction"]["LONG"]["total_opportunities"] == 2
        and seg["direction"]["SHORT"]["total_opportunities"] == 1
    )
    assert seg["family"]["orb"]["total_opportunities"] == 3
    assert seg["engine_reject_reason"]["min_space"]["counterfactual_labelled"] == 2
    assert seg["source"]["ENGINE_REJECTED"]["non_traded_eligible"] == 3
    assert seg["session"]["open"]["total_opportunities"] == 3
    assert set(seg) == set(C.SEGMENT_DIMS)
    assert len(seg["day"]) == 3


def test_stack_rejected_segment_and_traded(tmp_path, store):
    snap = make_snapshot(i=0)
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, accepted=True))
    from demo_factories import make_intent

    it = make_intent(snap)
    store.record_intent(it)
    store.transition(it.intent_id, "RISK_REJECTED")
    store.record_risk_detail(
        it.intent_id, "REJECTED", {"reject_code": "EXPOSURE_CAP", "gate_reject_class": "SAFETY"}
    )
    traded = make_snapshot(i=24)
    drive_full_trade(store, traded, closed=T0 + timedelta(days=1, hours=1))
    rep = _report(tmp_path, store, T0 + timedelta(days=5))
    seg = rep["sections"]["opportunities"]["segments"]
    assert seg["source"]["STACK_REJECTED"]["non_traded_eligible"] == 1
    assert seg["stack_reject_code"]["EXPOSURE_CAP"]["total_opportunities"] == 1
    assert seg["gate_class"]["SAFETY"]["total_opportunities"] == 1
    assert _opp(rep)["traded"] == 1 and _opp(rep)["total_opportunities"] == 2


def _closed_trades(store, n):
    its = []
    for k in range(n):
        its.append(
            drive_full_trade(
                store, make_snapshot(i=24 * (k + 10)), closed=T0 + timedelta(days=k + 10, hours=1)
            )
        )
    return its


def test_closed_trade_missing_analytics_is_a_visible_gap(tmp_path, store):
    full, partial, bare = _closed_trades(store, 3)
    for it in (full, partial):
        store.record_outcome_extra(
            it.intent_id,
            {"final_gross_r": 1.0, "entry_exit": {"x": 1}}
            if it is full
            else {"final_gross_r": 1.0, "entry_exit": None},
        )
    store.record_tca(full.intent_id, {"a": 1}, "ENTRY")
    store.record_tca(full.intent_id, {"a": 1}, "EXIT")
    store.merge_outcome_extra(full.intent_id, "shadow_exit_lab", {"policies": 1})
    rep = _report(tmp_path, store, T0 + timedelta(days=60))
    ta = rep["sections"]["trade_analytics"]
    m = ta["metrics"]
    assert m["closed_trades"] == 3 and m["outcome_complete"]["complete"] == 3
    assert m["outcome_extra_complete"] == {
        **m["outcome_extra_complete"],
        "complete": 2,
        "missing": 1,
    }
    assert m["entry_tca_complete"]["complete"] == 1 and m["exit_tca_complete"]["complete"] == 1
    assert m["entry_exit_diagnostic_complete"]["complete"] == 1
    assert ta["status"] == C.RED and rep["no_promotion_claim"]
    gap_ids = {g["intent_id"]: g["missing"] for g in ta["gap_sample"]}
    assert "outcome_extra_complete" in gap_ids[bare.intent_id] and full.intent_id not in gap_ids
    lab = rep["sections"]["shadow_exit_lab"]["metrics"]
    assert (
        lab["eligible"] == 2
        and lab["complete"] == 1
        and lab["unexpected_missing"] == 1
        and lab["failed"] == C.NOT_AVAILABLE
    )
    assert lab["missing_is_not_negative"] is True


def test_lab_pending_until_flat_deadline_and_never_run(tmp_path, store):
    one, two = _closed_trades(store, 2)
    for it in (one, two):
        store.record_outcome_extra(it.intent_id, {"final_gross_r": 1.0, "entry_exit": {"x": 1}})
    # signal day 10 -> fallback flat = signal + 12 h; check just before / after it
    sig = make_snapshot(i=240).signal_ts_utc
    before = parse(sig) + timedelta(hours=12) - timedelta(minutes=1)
    rep = _report(tmp_path, store, before)
    lab = rep["sections"]["shadow_exit_lab"]["metrics"]["closed_trades"]
    assert lab["pending"] >= 1 and lab["unexpected_missing"] == 0
    rep2 = C.coverage_for_path(
        tmp_path / "d.db", now=iso(T0 + timedelta(days=60)), phase="DISCOVERY"
    )
    tb = rep2["sections"]["shadow_exit_lab"]["metrics"]["closed_trades"]
    assert tb["complete"] == 0 and tb["unexpected_missing"] == 2 and tb["lab_absent"] is True
    assert (
        rep2["sections"]["shadow_exit_lab"]["status"] == C.RED
        and rep2["no_promotion_claim"] is True
    )


def test_non_strategy_trades_are_not_expected_to_carry_analytics(tmp_path, store):
    (it,) = _closed_trades(store, 1)
    store.record_trade_tag(it.intent_id, "EXECUTION_CANARY", False, source="runner")
    rep = _report(tmp_path, store, T0 + timedelta(days=60))
    m = rep["sections"]["trade_analytics"]["metrics"]
    assert m["closed_trades"] == 1 and m["closed_trades_expected_analytics"] == 0
    assert rep["sections"]["trade_analytics"]["status"] == C.GREEN


def test_epochs_never_pooled_silently(tmp_path, store):
    s1 = make_snapshot(i=0)
    s2 = make_snapshot(i=24)
    s1 = replace(s1, versions={"git_commit": "11c6aec9999", "config_hash": "cfgA"})
    s2 = replace(s2, versions={"git_commit": "deadbeef000", "config_hash": "cfgB"})
    for s in (s1, s2):
        store.record_snapshot(s)
        store.record_decision(make_decision(s, accepted=False))
    _label(store, s1)  # epoch 1 labelled, epoch 2 not (unexplained after grace)
    store.set_bar_pointer(s2.market, iso(T0 + timedelta(days=40)))
    rep = _report(tmp_path, store, T0 + timedelta(days=30))
    ep = rep["sections"]["epochs"]
    assert rep["mixed_epochs"] is True and rep["epoch_pooling_warning"]
    assert set(ep["epochs"]) == {"11c6aec9999", "deadbeef000"}
    assert ep["epochs"]["11c6aec9999"]["vs_boundary"] == "AT_BOUNDARY"
    assert ep["meta"]["schema_version"] == "demo-store-1"
    assert ep["meta"]["observer_definition_hash"].startswith(C.NOT_AVAILABLE)
    be = rep["sections"]["opportunities"]["by_epoch"]
    assert be["11c6aec9999"]["status"] == C.GREEN and be["deadbeef000"]["status"] == C.RED
    assert "epoch:deadbeef000/opportunities" in rep["red_populations"] and rep["no_promotion_claim"]


def test_position_thesis_placeholder_and_registry(tmp_path, store):
    rep = _report(tmp_path, store, T0)
    pt = rep["sections"]["position_thesis"]
    assert pt["status"] == C.NOT_IMPLEMENTED_YET
    assert tuple(pt["fields"]) == C.POSITION_THESIS_FIELDS
    assert set(C.POSITION_THESIS_FIELDS) >= {
        "open_positions_eligible",
        "hypothetical_exit_complete",
        "unexpected_missing",
    }
    assert rep["status"] == C.GREEN  # a not-implemented section never changes the verdict
    C.register_section("custom_probe", lambda ctx: {"status": C.RED, "reasons": ["RED: probe"]})
    try:
        s2 = C.open_readonly(tmp_path / "d.db")
        rep2 = C.build_coverage(s2, now=iso(T0), phase="DISCOVERY")
        s2.close()
        assert rep2["sections"]["custom_probe"]["status"] == C.RED and rep2["status"] == C.RED
    finally:
        C.SECTIONS.pop("custom_probe", None)


def test_open_is_read_only(tmp_path, store):
    snap = _reject(store)
    store.close()
    ro = C.open_readonly(tmp_path / "d.db")
    try:
        assert ro.get_snapshot(snap.opportunity_id) is not None  # the store's own read API works
        with pytest.raises(sqlite3.OperationalError):
            ro.record_snapshot(make_snapshot(i=99))
        with pytest.raises(sqlite3.OperationalError):
            ro._conn.execute("DELETE FROM seen")
        with pytest.raises(sqlite3.OperationalError):
            ro.set_meta("x", "y")
    finally:
        ro.close()
    reopened = DemoStore(tmp_path / "d.db")
    assert reopened.get_meta("x") is None
    reopened.close()


def test_production_path_refused_unless_allowed(tmp_path):
    prod = tmp_path / "artifacts" / "demo_100k"
    prod.mkdir(parents=True)
    s = DemoStore(prod / "x.db")  # synthetic stand-in placed under a production-looking path
    s.close()
    with pytest.raises(C.ProductionPathRefused):
        C.open_readonly(prod / "x.db")
    with pytest.raises(C.ProductionPathRefused):
        C.coverage_for_path(prod / "x.db", phase="DISCOVERY")
    ro = C.open_readonly(
        prod / "x.db", allow_production_readonly=True
    )  # explicit override: still mode=ro
    with pytest.raises(sqlite3.OperationalError):
        ro.set_meta("a", "b")
    ro.close()


@pytest.fixture(scope="module")
def cli():
    path = Path(__file__).resolve().parents[3] / "scripts" / "research_strategy.py"
    spec = importlib.util.spec_from_file_location("research_strategy_cov", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_coverage_json_markdown_and_refusal(tmp_path, cli, capsys):
    s = DemoStore(tmp_path / "d.db")
    snap = _reject(s)
    _label(s, snap)
    s.close()
    now = iso(T0 + timedelta(days=2))
    assert (
        cli.main(
            [
                "coverage",
                "--db",
                str(tmp_path / "d.db"),
                "--now",
                now,
                "--json",
                "--phase",
                "DISCOVERY",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert (
        data["sections"]["opportunities"]["metrics"]["eligible_coverage_pct"] == 100.0
        and data["phase"] == "DISCOVERY"
    )
    assert cli.main(["coverage", "--db", str(tmp_path / "d.db"), "--now", now]) == 0
    md = capsys.readouterr().out
    assert "Coverage audit" in md and "position_thesis" in md and "NOT_IMPLEMENTED_YET" in md
    prod = tmp_path / "artifacts" / "demo_100k"
    prod.mkdir(parents=True)
    (prod / "x.db").write_bytes(b"")
    assert cli.main(["coverage", "--db", str(prod / "x.db")]) == cli.EXIT_REFUSED
    assert cli.main(["coverage", "--db", str(tmp_path / "missing.db")]) == cli.EXIT_BAD_INPUT


def test_report_attaches_coverage_section(tmp_path, store):
    from research_workbench import dag
    from research_workbench.experiment import demo_synthetic_experiment
    from research_workbench.report import build_report, render_markdown

    snap = _reject(store)
    _label(store, snap)
    cov = _report(tmp_path, store, T0 + timedelta(days=2))
    exp = demo_synthetic_experiment()
    art = dag.ArtifactStore(str(tmp_path / "art"))
    plain = build_report(exp, art, {})
    assert "COVERAGE" not in plain
    rep = build_report(exp, art, {}, coverage=cov)
    assert rep["COVERAGE"]["status"] == C.GREEN
    assert "Coverage audit" in render_markdown(rep) and "Coverage audit" not in render_markdown(
        plain
    )


def test_closed_intent_without_outcome_row_is_visible_and_red(tmp_path, store):
    snap = make_snapshot(i=0)
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, accepted=True))
    it = make_intent(snap)
    store.record_intent(it)
    for st in ("RISK_APPROVED", "SENT", "FILLED", "PROTECTED", "CLOSED"):
        if st == "RISK_APPROVED":
            store.record_risk(it.intent_id, make_risk())
        if st == "FILLED":
            store.record_execution(it.intent_id, make_exec())
        store.transition(it.intent_id, st)  # crash window: CLOSED, no record_outcome
    rep = _report(tmp_path, store, T0 + timedelta(days=5))
    m = rep["sections"]["trade_analytics"]["metrics"]
    assert m["closed_trades"] == 1
    assert m["outcome_complete"]["complete"] == 0 and m["outcome_complete"]["missing"] == 1
    assert rep["sections"]["trade_analytics"]["status"] == C.RED and rep["no_promotion_claim"]
    gap = rep["sections"]["trade_analytics"]["gap_sample"][0]
    assert "outcome_complete" in gap["missing"]


def test_phases_are_never_pooled(tmp_path, store):
    _label(store, _reject(store, 0, phase="DISCOVERY"))
    _label(store, _reject(store, 1, phase="FROZEN"))
    store.close()
    both = C.coverage_for_path(tmp_path / "d.db", now=iso(T0 + timedelta(days=5)))
    assert both["phase"] == "PER_PHASE" and both["no_promotion_claim"] is True
    assert both["pooled_warning"]
    assert set(both["per_phase"]) == {"DISCOVERY", "FROZEN"}
    for ph, r in both["per_phase"].items():
        assert r["phase"] == ph
        assert r["sections"]["opportunities"]["metrics"]["total_opportunities"] == 1
    one = C.coverage_for_path(tmp_path / "d.db", now=iso(T0 + timedelta(days=5)), phase="FROZEN")
    assert one["sections"]["opportunities"]["metrics"]["total_opportunities"] == 1
    assert not one["no_promotion_claim"]
    assert "PER PHASE" in C.render_markdown(both)


def test_traded_state_takes_precedence_over_counterfactual(tmp_path, store):
    snap = make_snapshot(i=0)
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, accepted=True))  # accepted, no intent yet
    store.record_counterfactual(  # labelled after the 10 min window (as the labeller does) ...
        make_label(snap),
        source="ACCEPTED_NO_INTENT",
        gate_code="ACCEPTED_NO_INTENT",
        gate_class="OPERATIONAL",
        gate_codes=["ACCEPTED_NO_INTENT"],
    )
    it = make_intent(snap)  # ... then an intent + fill arrives late
    store.record_intent(it)
    store.record_risk(it.intent_id, make_risk())
    for st in ("RISK_APPROVED", "SENT", "FILLED"):
        if st == "FILLED":
            store.record_execution(it.intent_id, make_exec())
        store.transition(it.intent_id, st)
    other = _reject(store, 1)
    _label(store, other)
    rep = _report(tmp_path, store, T0 + timedelta(days=5))
    m = _opp(rep)
    assert m["traded"] == 1 and m["traded_and_counterfactual"] == 1
    assert m["counterfactual_labelled"] == 1 and m["non_traded_eligible"] == 1
    assert m["non_traded"] == m["non_traded_pending_horizon"] + m["non_traded_eligible"]
    assert m["non_traded_eligible"] == (
        m["counterfactual_labelled"]
        + m["counterfactual_pending"]
        + m["counterfactual_unexpected_missing"]
    )
    assert rep["sections"]["opportunities"]["status"] == C.RED and rep["no_promotion_claim"]


def _tree(path):
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in path.iterdir()}


def test_audit_leaves_the_db_directory_untouched(tmp_path, store):
    _label(store, _reject(store, 0))
    store.close()
    for sidecar in ("d.db-wal", "d.db-shm", "d.db-journal"):
        (tmp_path / sidecar).unlink(missing_ok=True)
    before = _tree(tmp_path)
    C.coverage_for_path(tmp_path / "d.db", now=iso(T0 + timedelta(days=3)), phase="DISCOVERY")
    C.coverage_for_path(tmp_path / "d.db", now=iso(T0 + timedelta(days=3)))
    assert _tree(tmp_path) == before
    assert not any(n.endswith(("-wal", "-shm", "-journal")) for n in _tree(tmp_path))


def test_not_available_is_never_rendered_or_summed_as_zero(tmp_path, store):
    one, two = _closed_trades(store, 2)
    for it in (one, two):
        store.record_outcome_extra(it.intent_id, {"final_gross_r": 1.0, "entry_exit": {"x": 1}})
    rep = _report(tmp_path, store, T0 + timedelta(days=60))
    lab = rep["sections"]["shadow_exit_lab"]["metrics"]
    assert lab["failed"] == C.NOT_AVAILABLE and lab["closed_trades"]["failed"] == C.NOT_AVAILABLE
    assert rep["sections"]["epochs"]["meta"]["observer_definition_hash"].startswith(C.NOT_AVAILABLE)
    assert _opp(rep)["label_error"].startswith(C.NOT_AVAILABLE)
    md = C.render_markdown(rep)
    assert "'failed': 'NOT_AVAILABLE'" in md and "'failed': 0" not in md


def test_cli_production_override_still_opens_read_only(tmp_path, cli, capsys):
    prod = tmp_path / "artifacts" / "demo_100k"
    prod.mkdir(parents=True)
    s = DemoStore(prod / "x.db")
    s.close()
    for sidecar in ("x.db-wal", "x.db-shm"):
        (prod / sidecar).unlink(missing_ok=True)
    before = _tree(prod)
    assert cli.main(["coverage", "--db", str(prod / "x.db")]) == cli.EXIT_REFUSED
    argv = [
        "coverage",
        "--db",
        str(prod / "x.db"),
        "--allow-production-db-readonly",
        "--phase",
        "FROZEN",
        "--json",
    ]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "FROZEN"
    assert _tree(prod) == before
