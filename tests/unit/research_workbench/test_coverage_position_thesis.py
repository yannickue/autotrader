# ruff: noqa: E501
"""Coverage auditor position_thesis section: NOT_AVAILABLE, pending != missing, per-phase, no-promotion propagation."""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))
from demo_factories import T0, iso

from demo.store import DemoStore
from research_workbench import coverage as C
from research_workbench.thesis.contracts import Direction, PositionThesis
from research_workbench.thesis.position_thesis import open_position_thesis

FIELDS = C.POSITION_THESIS_FIELDS


def _summary(**over):
    base = {
        "open_positions_eligible": 4,
        "opposing_events_detected": 3,
        "opposing_events_future_path_complete": 3,
        "position_thesis_assessment_complete": 4,
        "hypothetical_exit_complete": 16,
        "pending": {"opposing_events": 0, "position_thesis_assessment": 0, "hypothetical_exit": 0},
        "unexpected_missing": 0,
        "late_events_ignored": 0,
    }
    base.update(over)
    return base


@pytest.fixture
def store(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    yield s
    with contextlib.suppress(Exception):
        s.close()


def _rep(tmp_path, store, pt_input, phase="DISCOVERY"):
    store.close()
    return C.coverage_for_path(
        tmp_path / "d.db", now=iso(T0), phase=phase, position_thesis_input=pt_input
    )


def _pt(i: int) -> PositionThesis:
    return open_position_thesis(f"p{i}", "EURUSD", Direction.LONG, 1000, None, "STRUCT_RETEST", ())


def test_absent_input_is_not_available_not_zero(tmp_path, store):
    sec = _rep(tmp_path, store, None)["sections"]["position_thesis"]
    assert sec["status"] == C.NOT_AVAILABLE and sec["no_promotion_claim"] is True
    assert all(v == C.NOT_AVAILABLE for v in sec["fields"].values()) and set(sec["fields"]) == set(
        FIELDS
    )


def test_complete_summary_is_green_and_no_claim(tmp_path, store):
    rep = _rep(tmp_path, store, {"DISCOVERY": _summary()})
    sec = rep["sections"]["position_thesis"]
    assert sec["status"] == C.GREEN and sec["no_promotion_claim"] is False
    assert sec["fields"]["position_thesis_assessment_complete"] == 4
    assert "position_thesis" not in rep["section_no_promotion"]


def test_pending_is_not_a_failure_but_blocks_promotion(tmp_path, store):
    s = _summary(
        position_thesis_assessment_complete=1,
        pending={"opposing_events": 2, "position_thesis_assessment": 3, "hypothetical_exit": 1},
    )
    rep = _rep(tmp_path, store, {"DISCOVERY": s})
    sec = rep["sections"]["position_thesis"]
    assert sec["status"] == C.GREEN  # pending is never a failure
    assert sec["no_promotion_claim"] is True and "position_thesis" in rep["section_no_promotion"]
    assert (
        rep["no_promotion_claim"] is True and rep["promotion_claim_allowed"] is False
    )  # any section claim blocks promotion


def test_unexpected_missing_influences_status(tmp_path, store):
    amber = _rep(
        tmp_path, store, {"DISCOVERY": _summary(open_positions_eligible=100, unexpected_missing=1)}
    )
    assert amber["sections"]["position_thesis"]["status"] == C.AMBER
    rep = _rep(tmp_path, store, {"DISCOVERY": _summary(unexpected_missing=8)})
    assert rep["sections"]["position_thesis"]["status"] == C.RED
    assert rep["status"] == C.RED and rep["no_promotion_claim"] is True
    assert "position_thesis" in rep["red_populations"]


def test_real_theses_without_variants_report_variant_fields_not_available(tmp_path, store):
    payload = {"position_theses": [_pt(1), _pt(2)]}
    sec = _rep(tmp_path, store, {"DISCOVERY": payload})["sections"]["position_thesis"]
    f = sec["fields"]
    assert f["open_positions_eligible"] == 2
    assert (
        f["unexpected_missing"] == C.NOT_AVAILABLE
        and f["hypothetical_exit_complete"] == C.NOT_AVAILABLE
    )
    assert sec["status"] == C.GREEN and sec["no_promotion_claim"] is True


def test_real_theses_with_empty_variants_are_unexpected_missing(tmp_path, store):
    payload = {"position_theses": [_pt(1), _pt(2)], "variants": {}}
    sec = _rep(tmp_path, store, {"DISCOVERY": payload})["sections"]["position_thesis"]
    assert sec["fields"]["unexpected_missing"] == 10 and sec["status"] == C.RED


def test_phases_are_never_pooled(tmp_path, store):
    both = {"DISCOVERY": _summary(), "FROZEN": _summary(unexpected_missing=8)}
    store.close()
    kw = {"now": iso(T0), "position_thesis_input": both}
    d = C.coverage_for_path(tmp_path / "d.db", phase="DISCOVERY", **kw)
    f = C.coverage_for_path(tmp_path / "d.db", phase="FROZEN", **kw)
    assert d["sections"]["position_thesis"]["status"] == C.GREEN
    assert f["sections"]["position_thesis"]["status"] == C.RED
    per = C.coverage_for_path(tmp_path / "d.db", **kw)
    assert per["phase"] == "PER_PHASE" and per["no_promotion_claim"] is True
    assert per["per_phase"]["DISCOVERY"]["sections"]["position_thesis"]["status"] == C.GREEN


def test_untagged_or_mismatched_phase_input_is_refused():
    assert (
        C.resolve_position_thesis_input(_summary(), "DISCOVERY")[0] is None
    )  # untagged flat payload
    assert (
        C.resolve_position_thesis_input({**_summary(), "phase": "FROZEN"}, "DISCOVERY")[0] is None
    )
    assert C.resolve_position_thesis_input({"FROZEN": _summary()}, "DISCOVERY")[0] is None
    assert C.resolve_position_thesis_input({"DISCOVERY": _summary()}, None)[0] is None
    assert (
        C.resolve_position_thesis_input({**_summary(), "phase": "DISCOVERY"}, "DISCOVERY")[0]
        is not None
    )


def test_json_path_and_invalid_payload(tmp_path, store):
    p = tmp_path / "pt.json"
    p.write_text(json.dumps({"DISCOVERY": _summary()}), "utf-8")
    assert _rep(tmp_path, store, str(p))["sections"]["position_thesis"]["status"] == C.GREEN
    assert (
        _rep(tmp_path, store, {"DISCOVERY": {"junk": 1}})["sections"]["position_thesis"]["status"]
        == C.RED
    )
    nope = _rep(tmp_path, store, str(tmp_path / "nope.json"))
    assert nope["sections"]["position_thesis"]["status"] == C.NOT_AVAILABLE


def test_top_level_claim_follows_any_section_and_markdown_lists_reasons(tmp_path, store):
    full = _rep(tmp_path, store, {"DISCOVERY": _summary()})
    assert (
        full["status"] == C.GREEN
        and full["promotion_claim_allowed"] is True
        and full["no_promotion_claim"] is False
    )
    assert "NO PROMOTION CLAIM: no" in C.render_markdown(full)
    absent = _rep(tmp_path, store, None)
    assert (
        absent["status"] == C.GREEN
        and absent["no_promotion_claim"] is True
        and absent["promotion_claim_allowed"] is False
    )
    md = C.render_markdown(absent)
    assert "NO PROMOTION CLAIM: YES" in md and "NOT_AVAILABLE (not zero)" in md
    assert any("NOT_AVAILABLE" in r for r in absent["no_promotion_reasons"])
    pend = _rep(
        tmp_path,
        store,
        {
            "DISCOVERY": _summary(
                pending={
                    "opposing_events": 1,
                    "position_thesis_assessment": 0,
                    "hypothetical_exit": 0,
                }
            )
        },
    )
    assert pend["no_promotion_claim"] is True and "pending item" in C.render_markdown(pend)


def test_late_events_ignored_is_surfaced():
    from research_workbench.thesis.contracts import Direction as D
    from research_workbench.thesis.contracts import OpposingEvent
    from research_workbench.thesis.position_thesis import observe, summary

    from .thesis._synth import mm

    pt = open_position_thesis("p9", "EURUSD", D.LONG, 0, None, "STRUCT_RETEST", ())
    late = OpposingEvent(
        1, D.SHORT, "ROUND_REJECT", True, True
    )  # stamped before T: not visible at T, ignored
    out = observe(pt, mm(3), [late])
    assert out.late_events_ignored == 1 and out.opposing_events == ()
    assert summary([out])["late_events_ignored"] == 1
    sec = C.assess_position_thesis(
        summary([out]) | {"unexpected_missing": 0}, C.CoverageThresholds()
    )
    assert any("after their own decision bar" in r for r in sec[2])


def test_older_summary_without_late_counter_never_allows_a_promotion_claim(tmp_path, store):
    old = {k: v for k, v in _summary().items() if k != "late_events_ignored"}
    rep = _rep(tmp_path, store, {"DISCOVERY": old})
    sec = rep["sections"]["position_thesis"]
    assert sec["fields"]["late_events_ignored"] == C.NOT_AVAILABLE and sec["status"] == C.GREEN
    assert sec["no_promotion_claim"] is True and any(
        "late_events_ignored" in r for r in sec["no_promotion_reasons"]
    )
    assert rep["promotion_claim_allowed"] is False and rep["no_promotion_claim"] is True
    ok = _rep(tmp_path, store, {"DISCOVERY": _summary()})
    assert ok["promotion_claim_allowed"] is True


def test_late_counter_counts_each_distinct_late_event_once_and_ignores_on_time_events():
    from research_workbench.thesis.contracts import Direction as D
    from research_workbench.thesis.contracts import OpposingEvent
    from research_workbench.thesis.position_thesis import observe, summary

    from .thesis._synth import mm

    sh = D.SHORT
    pt = open_position_thesis("p8", "EURUSD", D.LONG, 0, None, "STRUCT_RETEST", ())
    on_time = OpposingEvent(mm(1).decision_ts_ns, sh, "ROUND_REJECT", True, True)
    cum: list = []
    for i in (1, 2, 3, 4):  # callers legitimately pass the CUMULATIVE event list every bar
        if i == 1:
            cum.append(on_time)
        pt = observe(pt, mm(i), list(cum))
    assert pt.late_events_ignored == 0 and len(pt.opposing_events) == 1
    late = OpposingEvent(
        mm(2).decision_ts_ns, sh, "OTHER_REJECT", True, True
    )  # first supplied at bar 5, stamped bar 2
    for i in (5, 6, 7):
        pt = observe(pt, mm(i), [*cum, late])
    assert pt.late_events_ignored == 1 and summary([pt])["late_events_ignored"] == 1
    assert len(pt.opposing_events) == 1  # the causal rule is unchanged: a late event is never used
