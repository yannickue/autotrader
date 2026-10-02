# ruff: noqa: E501
"""Thesis study adapter: replay events, DAG key semantics, causal prefix, controls/ablation/multiplicity, status ceiling (synthetic data).

Runtime ~25-30 s (> 10 s): register "tests/unit/research_workbench/thesis/test_study.py" in SLOW_FILES of tests/conftest.py
(this lane does not edit conftest; the integrator adds it).
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from coverage_analysis.observer_lab import stats as ST
from research_workbench import dag
from research_workbench.status import PromotionStatus
from research_workbench.thesis import study as S
from research_workbench.thesis.contracts import Direction, EvidenceClass, SetupState
from research_workbench.thesis.marketmap import MARKETMAP_VERSION
from research_workbench.thesis.setup_engine import validate_spec
from research_workbench.thesis.specs import CONTINUATION_RETEST

from ._obs_bars import bars_from_closes, curve
from ._synth import GEO_LONG, LONG_TRIG, SCENARIOS, mm

SPEC = CONTINUATION_RETEST
LONG = Direction.LONG
ZONE = (100.0, 101.0)


# ------------------------------------------------------------------------------------------------ replay
def _geo(_mm, _d):
    return GEO_LONG


def test_replay_finds_the_trigger_and_starts_a_new_lifecycle():
    maps, events, _ = SCENARIOS["full_progression"]()
    out = S.replay_setup_events(maps, events, SPEC, (LONG,), geometry_fn=_geo, min_gap_bars=1)
    assert [(e.idx, e.direction) for e in out.events] == [(3, LONG)]
    assert out.events[0].spec_hash == SPEC.spec_hash and out.n_maps == len(maps)
    assert any(state == SetupState.TRIGGERED.value for *_, state in out.trace)


def test_overlapping_triggers_are_dropped_and_counted():
    maps, events, _ = SCENARIOS["full_progression"]()
    bar_ts = {m.bar_index: m.decision_ts_ns for m in maps}
    more = [
        replace(m, bar_index=m.bar_index + 5, decision_ts_ns=m.decision_ts_ns + 5 * 300_000_000_000)
        for m in maps
    ]
    events2 = {**events, bar_ts[3] + 5 * 300_000_000_000: (LONG_TRIG,)}
    out = S.replay_setup_events(
        [*maps, *more], events2, SPEC, (LONG,), geometry_fn=_geo, min_gap_bars=48
    )
    assert len(out.events) == 1 and out.n_overlap_dropped == 1


def test_ablation_spec_removes_level_behaviour_and_changes_the_hash():
    abl = S.without_level_behaviour(SPEC)
    validate_spec(abl)
    assert EvidenceClass.LEVEL_BEHAVIOUR not in abl.requirements
    assert abl.spec_hash != SPEC.spec_hash and abl.archetype == SPEC.archetype
    maps, events, _ = SCENARIOS["full_progression"]()
    assert (
        len(
            S.replay_setup_events(
                maps, events, abl, (LONG,), geometry_fn=_geo, min_gap_bars=1
            ).events
        )
        == 1
    )


# ------------------------------------------------------------------------------------------------ DAG key
def test_key_is_deterministic_and_cacheable():
    k = S.thesis_study_key("parent", SPEC)
    assert k == S.thesis_study_key("parent", SPEC)
    assert not dag.code_hash(S.THESIS_STUDY_CODE).startswith(dag.UNCACHEABLE)


def test_key_changes_with_spec_hash_marketmap_version_main_thesis_and_parent(monkeypatch):
    base = S.thesis_study_key("parent", SPEC)
    assert (
        S.thesis_study_key("parent", replace(SPEC, expiry_bars=SPEC.expiry_bars + 1)) != base
    )  # spec_hash
    assert S.thesis_study_key("other", SPEC) != base  # data identity
    assert S.thesis_study_key("parent", SPEC, S.StudyConfig(seed=7)) != base  # config
    monkeypatch.setattr(S, "MARKETMAP_VERSION", MARKETMAP_VERSION + "-x")
    assert S.thesis_study_key("parent", SPEC) != base  # MARKETMAP_VERSION
    monkeypatch.undo()
    assert S.thesis_study_key("parent", SPEC) == base
    monkeypatch.setattr(S, "MAIN_THESIS_VERSION", "main-thesis-x")
    assert S.thesis_study_key("parent", SPEC) != base
    v = S.study_versions(SPEC)
    assert {
        "marketmap_version",
        "marketmap_definition_hash",
        "main_thesis_version",
        "spec_hash",
        "ablation_spec_hash",
    } <= set(v)


# ------------------------------------------------------------------------------------------------ causal prefix (real MarketMapReplay)
def _bars(n=500, per_day=None, market="SYN"):
    return bars_from_closes(
        curve("up", n, amp=2.5, period=70), seed=3, bars_per_day=per_day, market=market
    )


def test_prefix_causality_of_the_study_replay():
    bars = _bars(420)
    full = S.replay_setup_events(
        S.maps_from_bars(bars),
        {},
        SPEC,
        (LONG, Direction.SHORT),
        geometry_fn=S.geometry_from_bars(bars),
        min_gap_bars=1,
    )
    for k in (150, 260, 340):
        pre_bars = bars.prefix(k)
        pre = S.replay_setup_events(
            S.maps_from_bars(pre_bars),
            {},
            SPEC,
            (LONG, Direction.SHORT),
            geometry_fn=S.geometry_from_bars(pre_bars),
            min_gap_bars=1,
        )
        assert pre.trace == full.trace[: len(pre.trace)]
        assert [e for e in full.events if e.idx < k] == list(pre.events)


# ------------------------------------------------------------------------------------------------ end to end
def _synthetic_study(n_days=120, per_day=150, slot=20):
    bars = bars_from_closes(
        curve("flat", n_days * per_day, amp=2.0, period=45),
        seed=5,
        bars_per_day=per_day,
        market="SYN",
    )
    maps, events = [], {}

    def at(i, **kw):
        return mm(i, decision_ts_ns=int(bars.decision_ts_ns(i)), market="SYN", **kw)

    pattern: dict[int, tuple[int, str, bool]] = {}  # bar -> (step, kind, main-thesis-neutral)
    for d in range(
        0, n_days, 2
    ):  # events on every other day only: the odd days supply same-time-of-day controls
        kind, neutral = ("full" if d % 4 == 0 else "no_level"), d % 8 in (4, 6)
        for step in range(4):
            pattern[d * per_day + slot + step] = (step, kind, neutral)
    for i in range(len(bars)):
        if i not in pattern:
            maps.append(at(i))
            continue
        step, kind, neutral = pattern[i]
        ctx = (
            {"h1_context": "NEUTRAL", "m15_structure": "MIXED_TRANSITION"} if neutral else {}
        )  # setup context still compatible, main thesis not aligned
        own = (
            "UP_SEQUENCE" if kind == "full" else "DOWN_SEQUENCE"
        )  # retest_confirmed fails for the no_level kind
        if step == 0:
            maps.append(at(i, **ctx))
        elif step == 1:
            maps.append(
                at(i, active_support_zone=ZONE, acceptance_state={"LONG:L1": "ACCEPTED"}, **ctx)
            )
        elif step == 2:
            maps.append(
                at(
                    i,
                    active_support_zone=ZONE,
                    acceptance_state={"LONG:L1": "ACCEPTED"},
                    m5_structure=own,
                    **ctx,
                )
            )
        else:
            acc = "RETEST_HELD" if kind == "full" else "ACCEPTED"
            maps.append(
                at(
                    i,
                    active_support_zone=ZONE,
                    acceptance_state={"LONG:L1": acc},
                    m5_structure=own,
                    **ctx,
                )
            )
            events[int(bars.decision_ts_ns(i))] = (LONG_TRIG,)
    return bars, maps, events


@pytest.fixture(scope="module")
def study_run():
    bars, maps, events = _synthetic_study()
    reg = ST.HypothesisRegistry("study-test")
    cfg = S.StudyConfig(directions=(LONG,), bootstrap_B=2000)
    part = np.full(len(bars), "TRAIN", dtype=object)
    res = S.run_study(
        bars,
        events,
        config=cfg,
        registry=reg,
        maps=maps,
        geometry_fn=_geo,
        partition=part,
        parent_key="p",
    )
    return bars, maps, events, reg, cfg, part, res


def test_study_events_arms_controls_and_ablation(study_run):
    _, _, _, _, _, _, res = study_run
    ev = res.events
    assert (
        ev["full_spec"] > 0
        and ev["no_level_spec"] > ev["full_spec"]
        and ev["union"] == ev["no_level_spec"]
    )
    assert [a["arm"] for a in res.arms] == [a for a, _ in S.ARMS]
    assert all(a["descriptive_only"] for a in res.arms)
    assert res.arms[2]["n_events"] == ev["union"] and res.arms[0]["n_events"] == ev["full_spec"]
    assert (
        0 < res.arms[1]["n_events"] < res.arms[0]["n_events"]
        and 0 < res.arms[3]["n_events"] < res.arms[2]["n_events"]
    )  # alignment arm is not vacuous
    assert res.controls["n_controls"] > 0 and res.controls["control_method"].startswith(
        "observer-controls"
    )
    assert {r["group"] for r in res.ablation} == {"level", "thesis"} and all(
        r["status"] for r in res.ablation
    )


def test_multiplicity_is_registered_and_exposed(study_run):
    *_, reg, _, _, res = study_run
    m = res.multiplicity
    assert (
        m["n_hypotheses"]
        == reg.n_hypotheses
        == len(S.ARMS) + len(res.ablation)
        == m["n_arm_hypotheses"] + m["n_ablation_hypotheses"]
    )
    assert m["n_families"] >= 2


def test_rerun_on_the_same_registry_is_refused(study_run):
    bars, maps, events, reg, cfg, part, _ = study_run
    with pytest.raises(ST.HypothesisReuseError):
        S.run_study(
            bars,
            events,
            config=cfg,
            registry=reg,
            maps=maps,
            geometry_fn=_geo,
            partition=part,
            parent_key="p",
        )


def test_status_never_above_research_candidate(study_run):
    *_, res = study_run
    assert PromotionStatus(res.promotion_status) in (
        PromotionStatus.REJECT_FAST,
        S.RESEARCH_CEILING,
    )
    assert res.no_promotion_claim is True and res.research_only is True
    assert not any("LIVE" in m.name for m in PromotionStatus)
    assert S.RESEARCH_CEILING is PromotionStatus.PROMOTE_TO_FIDELITY


def test_no_events_is_reject_fast_with_arms_still_registered():
    bars = _bars(300)
    reg = ST.HypothesisRegistry("none")
    res = S.run_study(
        bars,
        {},
        config=S.StudyConfig(directions=(LONG,)),
        registry=reg,
        maps=[mm(i, decision_ts_ns=int(bars.decision_ts_ns(i)), market="SYN") for i in range(300)],
        geometry_fn=_geo,
        partition=None,
    )
    assert (
        res.promotion_status == str(PromotionStatus.REJECT_FAST)
        and res.arms == []
        and res.ablation == []
    )
    assert res.multiplicity["n_hypotheses"] == len(S.ARMS) == reg.n_hypotheses


def test_cached_run_hits_and_key_miss_recomputes(tmp_path, study_run):
    bars, maps, events, _, cfg, part, res = study_run
    store = dag.ArtifactStore(tmp_path)
    kw = {"maps": maps, "geometry_fn": _geo, "partition": part}
    a = S.run_study_cached(
        store,
        "exp",
        bars,
        events,
        parent_key="p",
        config=cfg,
        registry=ST.HypothesisRegistry("c1"),
        **kw,
    )
    assert not a.cached and a.key == res.key
    b = S.run_study_cached(
        store,
        "exp",
        bars,
        events,
        parent_key="p",
        config=cfg,
        registry=ST.HypothesisRegistry("c2"),
        **kw,
    )
    assert b.cached and b.to_dict() == {**a.to_dict(), "cached": True}
    c = S.run_study_cached(
        store,
        "exp",
        bars,
        events,
        parent_key="p2",
        config=cfg,
        registry=ST.HypothesisRegistry("c3"),
        **kw,
    )
    assert not c.cached and c.key != a.key
