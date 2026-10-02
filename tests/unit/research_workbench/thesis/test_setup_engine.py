# ruff: noqa: E501
"""Generic setup engine: legality, precedence, retroactivity, batch==incremental, mirror symmetry, no look-ahead, NOT_EVALUATED."""

from __future__ import annotations

from dataclasses import replace

import pytest

from research_workbench.thesis import setup_engine as eng
from research_workbench.thesis.contracts import (
    TERMINAL_SETUP_STATES,
    Alignment,
    EvidenceClass,
    MarketPhase,
    SetupState,
    legal_setup_transition,
)
from research_workbench.thesis.setup_engine import EvaluationStatus, SetupReplayer, advance, replay
from research_workbench.thesis.specs import CONTINUATION_RETEST, catalog

from ._synth import (
    GEO_LONG,
    LONG,
    LONG_TRIG,
    SCENARIOS,
    SHORT,
    mirror_event,
    mirror_geo,
    mirror_map,
    mm,
    ts,
)

SPEC = CONTINUATION_RETEST


def _states(results):
    return [None if r.thesis is None else r.thesis.state for r in results]


def _run(name, spec=SPEC, direction=LONG):
    maps, events, geo = SCENARIOS[name]()
    return maps, events, geo, replay(maps, spec, direction, events, geometry_by_ts=geo)


# ------------------------------------------------------------------------------------------------ progression + legality
def test_full_progression_states_and_timestamps():
    _, _, _, res = _run("full_progression")
    assert _states(res) == [
        SetupState.FORMING,
        SetupState.CONFIRMING,
        SetupState.ARMED,
        SetupState.TRIGGERED,
        SetupState.TRIGGERED,
    ]
    th = res[3].thesis
    assert [(t.ts_ns, t.src, t.dst) for t in th.history] == [
        (ts(0), SetupState.CANDIDATE, SetupState.FORMING),
        (ts(1), SetupState.FORMING, SetupState.LOCATION_REACHED),
        (ts(1), SetupState.LOCATION_REACHED, SetupState.CONFIRMING),
        (ts(2), SetupState.CONFIRMING, SetupState.ARMED),
        (ts(3), SetupState.ARMED, SetupState.TRIGGERED),
    ]
    assert th.created_at_ns == ts(0) and th.family_events == (LONG_TRIG,)
    assert (
        th.structural_stop == 99.0 and th.opposition_1 == 105.0 and th.entry_zone == (100.0, 101.0)
    )


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_transition_is_a_declared_edge(name):
    _, _, _, res = _run(name)
    for r in res:
        if r.evaluation is None:
            continue
        for t in r.evaluation.transitions:
            assert legal_setup_transition(t.src, t.dst)


def test_terminal_states_absorbing():
    _, _, _, res = _run("full_progression")
    assert res[3].thesis.state in TERMINAL_SETUP_STATES
    last = res[4]
    assert last.status is EvaluationStatus.TERMINAL
    assert last.evaluation.transitions == () and last.thesis == res[3].thesis
    # even a hostile bar cannot move a terminal thesis
    out = advance(res[3].thesis, mm(9, m15_structure="DOWN_SEQUENCE"), (), SPEC, LONG)
    assert out.status is EvaluationStatus.TERMINAL and out.thesis.state is SetupState.TRIGGERED


def test_skipping_an_edge_raises(monkeypatch):
    skip = dict(eng.SETUP_NEXT)
    skip[SetupState.CANDIDATE] = SetupState.LOCATION_REACHED
    monkeypatch.setattr(eng, "SETUP_NEXT", skip)
    monkeypatch.setitem(
        eng.EDGE_EVIDENCE,
        (SetupState.CANDIDATE, SetupState.LOCATION_REACHED),
        (EvidenceClass.CONTEXT,),
    )
    with pytest.raises(eng.IllegalSetupTransition):
        advance(None, mm(0), (), SPEC, LONG)


def test_non_monotone_ts_and_foreign_spec_raise():
    first = advance(None, mm(1), (), SPEC, LONG).thesis
    with pytest.raises(ValueError):
        advance(first, mm(0), (), SPEC, LONG)
    with pytest.raises(ValueError):
        advance(first, mm(2), (), replace(SPEC, expiry_bars=7), LONG)
    with pytest.raises(ValueError):
        advance(first, mm(2), (), SPEC, SHORT)


# ------------------------------------------------------------------------------------------------ precedence
def _full_bar(i, **kw):
    base = {
        "active_support_zone": (100.0, 101.0),
        "acceptance_state": {"LONG:L1": "RETEST_HELD"},
        "m5_structure": "UP_SEQUENCE",
    }
    base.update(kw)
    return mm(i, **base)


def test_multi_edge_chain_on_one_bar_single_timestamp():
    r = advance(None, _full_bar(0), (LONG_TRIG,), SPEC, LONG, geometry=GEO_LONG)
    ev = r.evaluation
    assert [t.dst for t in ev.transitions] == [
        SetupState.FORMING,
        SetupState.LOCATION_REACHED,
        SetupState.CONFIRMING,
        SetupState.ARMED,
        SetupState.TRIGGERED,
    ]
    assert {t.ts_ns for t in ev.transitions} == {ts(0)}


def test_invalidation_beats_expiry_and_progress():
    spec = replace(SPEC, expiry_bars=2)
    prev = advance(None, mm(0), (), spec, LONG).thesis
    # bar 2: expiry reached, forward edges would fire, AND structure breaks against -> INVALIDATED wins
    bar = _full_bar(2, m15_structure="DOWN_SEQUENCE")
    r = advance(prev, bar, (LONG_TRIG,), spec, LONG, geometry=GEO_LONG)
    assert [t.dst for t in r.evaluation.transitions] == [SetupState.INVALIDATED]
    assert r.evaluation.transitions[0].reason.startswith("invalidation:")


def test_expiry_beats_progress():
    spec = replace(SPEC, expiry_bars=2)
    prev = advance(None, mm(0), (), spec, LONG).thesis
    r = advance(prev, _full_bar(2), (LONG_TRIG,), spec, LONG, geometry=GEO_LONG)
    assert [t.dst for t in r.evaluation.transitions] == [SetupState.EXPIRED]


def test_expiry_scenario_stalls_then_expires():
    spec = replace(SPEC, expiry_bars=3)
    maps, ev, geo = SCENARIOS["stalls_then_expires"]()
    res = replay(maps, spec, LONG, ev, geometry_by_ts=geo)
    st = _states(res)
    assert (
        st[:3] == [SetupState.FORMING] * 3
        and st[3] is SetupState.EXPIRED
        and st[5] is SetupState.EXPIRED
    )
    assert res[3].evaluation.transitions[0].ts_ns == ts(3)


def test_invalidation_midway_and_acceptance_against():
    _, _, _, res = _run("invalidated_midway")
    assert (
        res[2].thesis.state is SetupState.INVALIDATED and res[3].status is EvaluationStatus.TERMINAL
    )
    _, _, _, res2 = _run("acceptance_invalidation")
    assert res2[2].thesis.state is SetupState.INVALIDATED
    assert "acceptance_against" in res2[2].evaluation.transitions[0].reason


def test_no_setup_has_no_thesis_and_no_silent_progress():
    _, _, _, res = _run("no_setup")
    assert all(r.status is EvaluationStatus.NO_SETUP and r.evaluation is None for r in res)


def test_creation_gate_blocked_by_active_invalidation():
    r = advance(None, mm(0, m15_structure="DOWN_SEQUENCE"), (), SPEC, LONG)
    assert r.status is EvaluationStatus.NO_SETUP


# ------------------------------------------------------------------------------------------------ retroactivity / look-ahead
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_prefix_transitions_never_change(name):
    maps, events, geo, full = _run(name)
    for k in range(1, len(maps) + 1):
        part = replay(maps[:k], SPEC, LONG, events, geometry_by_ts=geo)
        assert part == full[:k]  # identical results, incl. history and first_observed stamps


def test_first_observed_timestamps_frozen():
    _, _, _, res = _run("full_progression")
    first = {}
    for r in res:
        for c in r.thesis.conditions:
            key = (c.evidence_class, c.name)
            if key in first:
                assert c.first_observed_ns == first[key]  # never rewritten (or still unset)
            elif c.first_observed_ns is not None:
                first[key] = c.first_observed_ns
    assert first[(EvidenceClass.LOCATION, "at_active_zone")] == ts(1)
    # the zone is gone on bar 4 (observed False) but the stamp from bar 1 is kept
    last = {(c.evidence_class, c.name): c for c in res[3].thesis.conditions}
    assert last[(EvidenceClass.LOCATION, "at_active_zone")].first_observed_ns == ts(1)


def test_poisoned_future_does_not_change_earlier_evaluations():
    maps, events, geo, full = _run("full_progression")
    poisoned = list(maps[:2]) + [
        mm(i, m15_structure="DOWN_SEQUENCE", h1_context=None, active_support_zone=None)
        for i in range(2, 9)
    ]
    pres = replay(poisoned, SPEC, LONG, events, geometry_by_ts=geo)
    assert pres[:2] == full[:2]


# ------------------------------------------------------------------------------------------------ batch == incremental
@pytest.mark.parametrize("name", list(SCENARIOS))
@pytest.mark.parametrize("direction", [LONG, SHORT])
def test_batch_equals_incremental(name, direction):
    maps, events, geo = SCENARIOS[name]()
    if direction is SHORT:
        maps = [mirror_map(m) for m in maps]
        events = {t: tuple(mirror_event(e) for e in es) for t, es in events.items()}
        geo = {t: mirror_geo(g) for t, g in geo.items()}
    spec = replace(SPEC, expiry_bars=3) if name == "stalls_then_expires" else SPEC
    batch = replay(maps, spec, direction, events, geometry_by_ts=geo)
    rp = SetupReplayer(spec, direction)
    inc = [
        rp.step(m, events.get(m.decision_ts_ns, ()), geometry=geo.get(m.decision_ts_ns))
        for m in maps
    ]
    assert tuple(inc) == batch


def test_alignment_is_an_input_not_a_gate():
    maps, events, geo = SCENARIOS["full_progression"]()
    a = replay(
        maps,
        SPEC,
        LONG,
        events,
        alignment_by_ts={m.decision_ts_ns: Alignment.OPPOSED for m in maps},
        geometry_by_ts=geo,
    )
    b = replay(maps, SPEC, LONG, events, geometry_by_ts=geo)
    assert _states(a) == _states(b)
    assert (
        a[-1].thesis.daily_thesis_alignment is Alignment.OPPOSED
        and b[-1].thesis.daily_thesis_alignment is Alignment.NEUTRAL
    )


# ------------------------------------------------------------------------------------------------ mirror symmetry
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_long_short_mirror_symmetry(name):
    maps, events, geo = SCENARIOS[name]()
    spec = replace(SPEC, expiry_bars=3) if name == "stalls_then_expires" else SPEC
    lres = replay(maps, spec, LONG, events, geometry_by_ts=geo)
    smaps = [mirror_map(m) for m in maps]
    sevents = {t: tuple(mirror_event(e) for e in es) for t, es in events.items()}
    sgeo = {t: mirror_geo(g) for t, g in geo.items()}
    sres = replay(smaps, spec, SHORT, sevents, geometry_by_ts=sgeo)
    assert _states(lres) == _states(sres)
    for a, b in zip(lres, sres, strict=True):
        assert a.status == b.status
        if a.evaluation is None:
            continue
        assert [(t.ts_ns, t.src, t.dst, t.reason) for t in a.evaluation.transitions] == [
            (t.ts_ns, t.src, t.dst, t.reason) for t in b.evaluation.transitions
        ]
        assert [
            (c.evidence_class, c.name, c.observed, c.first_observed_ns) for c in a.thesis.conditions
        ] == [
            (c.evidence_class, c.name, c.observed, c.first_observed_ns) for c in b.thesis.conditions
        ]


# ------------------------------------------------------------------------------------------------ NOT_EVALUATED, determinism, explainability
@pytest.mark.parametrize(
    "spec", [s for s in catalog() if not s.implemented], ids=lambda s: s.archetype
)
def test_unimplemented_archetypes_are_explicitly_not_evaluated(spec):
    r = advance(None, mm(0), (), spec, LONG)
    assert (
        r.status is EvaluationStatus.NOT_EVALUATED
        and r.evaluation is None
        and spec.archetype in r.note
    )
    res = replay([mm(0), mm(1)], spec, SHORT)
    assert all(x.status is EvaluationStatus.NOT_EVALUATED for x in res)


def test_determinism_and_thesis_id():
    maps, events, geo = SCENARIOS["full_progression"]()
    a = replay(maps, SPEC, LONG, events, geometry_by_ts=geo)
    b = replay(maps, SPEC, LONG, events, geometry_by_ts=geo)
    assert a == b and a[0].thesis.thesis_id == b[0].thesis.thesis_id
    assert len({r.thesis.thesis_id for r in a}) == 1  # id fixed at creation
    later = replay(maps[1:], SPEC, LONG, events, geometry_by_ts=geo)
    assert later[0].thesis.thesis_id != a[0].thesis.thesis_id  # different created_at
    other_dir = replay([mirror_map(m) for m in maps], SPEC, SHORT)
    assert other_dir[0].thesis is None or other_dir[0].thesis.thesis_id != a[0].thesis.thesis_id


def test_spec_hash_sensitivity_flows_into_thesis():
    changed = replace(SPEC, params={**SPEC.params, "x": 1})
    assert changed.spec_hash != SPEC.spec_hash
    t1 = advance(None, mm(0), (), SPEC, LONG).thesis
    t2 = advance(None, mm(0), (), changed, LONG).thesis
    assert t1.spec_hash == SPEC.spec_hash and t1.thesis_id != t2.thesis_id


def test_conditions_are_explainable_required_observed_missing():
    prev = advance(None, mm(0, active_support_zone=None), (), SPEC, LONG).thesis
    by = {(c.evidence_class, c.name): c for c in prev.conditions}
    assert by[(EvidenceClass.CONTEXT, "h1_not_against")].observed is True
    assert by[(EvidenceClass.LOCATION, "at_active_zone")].observed is False  # missing
    assert (
        by[(EvidenceClass.GEOMETRY, "structural_stop_known")].observed is None
    )  # not evaluable (no geometry)
    assert by[(EvidenceClass.LOCATION, "at_active_zone")].required is True
    assert by[(EvidenceClass.PARTICIPATION, "participation_not_low")].required is False
    classes = {c.evidence_class for c in prev.conditions}
    assert {
        EvidenceClass.CONTEXT,
        EvidenceClass.LOCATION,
        EvidenceClass.STRUCTURE,
        EvidenceClass.LEVEL_BEHAVIOUR,
        EvidenceClass.TRIGGER,
        EvidenceClass.GEOMETRY,
    } <= classes


def test_newly_observed_and_newly_missing():
    maps, events, geo = SCENARIOS["full_progression"]()
    res = replay(maps, SPEC, LONG, events, geometry_by_ts=geo)
    assert "LOCATION:at_active_zone" in res[1].evaluation.newly_observed
    assert "LOCATION:at_active_zone" not in res[2].evaluation.newly_observed
    # bar 3 -> 4 would drop the zone but 4 is absorbed; check a live loss instead
    prev = advance(None, mm(0, active_support_zone=(1.0, 2.0)), (), SPEC, LONG).thesis
    r = advance(prev, mm(1), (), SPEC, LONG)
    assert "LOCATION:at_active_zone" in r.evaluation.newly_missing


def test_unregistered_predicate_on_implemented_spec_raises():
    bad = replace(
        SPEC, requirements={**SPEC.requirements, EvidenceClass.CONTEXT: ("does_not_exist",)}
    )
    with pytest.raises(eng.UnknownPredicate):
        advance(None, mm(0), (), bad, LONG)


def test_geometry_required_for_arming_and_missing_geometry_blocks():
    bar = _full_bar(0)
    r = advance(None, bar, (LONG_TRIG,), SPEC, LONG)  # no geometry -> stops at CONFIRMING
    assert r.thesis.state is SetupState.CONFIRMING and r.thesis.structural_stop is None


def test_phase_gate_for_creation():
    assert (
        advance(None, mm(0, market_phase=MarketPhase.UNDEFINED), (), SPEC, LONG).status
        is EvaluationStatus.NO_SETUP
    )


def test_opposing_retest_held_invalidates_in_engine():
    prev = advance(None, mm(0), (), SPEC, LONG).thesis
    bar = _full_bar(1, acceptance_state={"LONG:L1": "ACCEPTED", "SHORT:L2": "RETEST_HELD"})
    r = advance(prev, bar, (LONG_TRIG,), SPEC, LONG, geometry=GEO_LONG)
    assert r.thesis.state is SetupState.INVALIDATED


def test_edge_without_predicates_fails_closed_and_spec_invalid():
    bad = replace(
        SPEC,
        requirements={
            k: v for k, v in SPEC.requirements.items() if k is not EvidenceClass.LOCATION
        },
    )
    with pytest.raises(ValueError, match="no evidence predicate"):
        eng.validate_spec(bad)
    with pytest.raises(ValueError, match="no evidence predicate"):
        advance(None, mm(0), (), bad, LONG)
    # the helper itself never treats an empty edge as satisfied
    assert eng._all_true({}, []) is False
    empty_trigger = replace(SPEC, requirements={**SPEC.requirements, EvidenceClass.TRIGGER: ()})
    with pytest.raises(ValueError, match="no evidence predicate"):
        eng.validate_spec(empty_trigger)
