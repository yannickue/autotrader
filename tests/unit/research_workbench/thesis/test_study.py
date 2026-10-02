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
from research_workbench.thesis.position_thesis import HypotheticalExit, Variant
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


def test_cached_run_hits_replays_registration_and_parent_key_misses(tmp_path, study_run):
    bars, maps, events, _, cfg, part, _ = study_run
    store = dag.ArtifactStore(tmp_path)
    kw = {"maps": maps, "geometry_fn": _geo, "geometry_version": "geo-const-v1", "partition": part}
    reg1 = ST.HypothesisRegistry("creg")
    a = S.run_study_cached(
        store, "exp", bars, events, parent_key="p", config=cfg, registry=reg1, **kw
    )
    assert not a.cached and a.multiplicity["n_hypotheses"] == reg1.n_hypotheses
    reg2 = ST.HypothesisRegistry("creg")
    b = S.run_study_cached(
        store, "exp", bars, events, parent_key="p", config=cfg, registry=reg2, **kw
    )
    assert b.cached and b.key == a.key and b.to_dict() == {**a.to_dict(), "cached": True}
    assert (
        reg2.n_hypotheses == reg1.n_hypotheses
    )  # a hit puts the stored hypotheses into the callers registry
    c = S.run_study_cached(
        store,
        "exp",
        bars,
        events,
        parent_key="p2",
        config=cfg,
        registry=ST.HypothesisRegistry("creg"),
        **kw,
    )
    assert not c.cached and c.key != a.key


# ------------------------------------------------------------------------------------------------ HIGH-2: the key covers ALL inputs
def _inputs(bars, events, maps, **over):
    kw = {
        "maps": maps,
        "geometry_fn": None,
        "geometry_version": None,
        "partition": None,
        "registry": ST.HypothesisRegistry("k"),
        "exit_variants": None,
    }
    kw.update(over)
    return S.study_inputs(bars, events, **kw)


def _key(inputs, parent="p", cfg=None):
    return S.thesis_study_key(parent, SPEC, cfg or S.StudyConfig(), inputs=inputs)


def _registry_with_one():
    reg = ST.HypothesisRegistry("k")
    reg.register("earlier|hypothesis")
    return reg


def _hx(entry_id, r, *, kind="VARIANT_EXIT", triggered=True, complete=True):
    return HypotheticalExit(
        entry_id, 1, kind, triggered, complete, r if complete else float("nan"), None, 0.0, 0.0, 1
    )


def test_every_changed_input_changes_the_key_with_the_same_parent_key():
    bars = _bars(300)
    maps = [mm(i, decision_ts_ns=int(bars.decision_ts_ns(i)), market="SYN") for i in range(300)]
    events = {int(bars.decision_ts_ns(5)): (LONG_TRIG,)}
    base = _key(_inputs(bars, events, maps))
    assert base == _key(_inputs(bars, dict(events), list(maps)))  # deterministic
    changed = {
        "events": _inputs(bars, {**events, int(bars.decision_ts_ns(9)): (LONG_TRIG,)}, maps),
        "events_name": _inputs(bars, {int(bars.decision_ts_ns(5)): ("OTHER",)}, maps),
        "maps": _inputs(
            bars, events, [*maps[:10], replace(maps[10], h1_context="DOWN"), *maps[11:]]
        ),
        "maps_replay_vs_given": _inputs(bars, events, None),
        "geometry_override": _inputs(bars, events, maps, geometry_fn=_geo, geometry_version="v1"),
        "partition": _inputs(bars, events, maps, partition=np.full(300, "TRAIN", dtype=object)),
        "registry_state": _inputs(bars, events, maps, registry=_registry_with_one()),
        "registry_name": _inputs(bars, events, maps, registry=ST.HypothesisRegistry("other")),
        "bars": _inputs(_bars(300, market="OTHER"), events, maps),
        "exit_variants": _inputs(
            bars, events, maps, exit_variants={Variant.CONTROL: (_hx("e1", 1.0),)}
        ),
    }
    keys = {n: _key(i) for n, i in changed.items()}
    assert all(k != base for k in keys.values()), keys
    assert len(set(keys.values())) == len(keys)
    assert (
        _key(_inputs(bars, events, maps, geometry_fn=_geo, geometry_version="v2"))
        != keys["geometry_override"]
    )  # the version token is keyed
    part2 = np.full(300, "TRAIN", dtype=object)
    part2[7] = "VALIDATION"
    assert _key(_inputs(bars, events, maps, partition=part2)) != keys["partition"]
    bars2 = _bars(300)
    bars2.c[3] += 0.5  # one price changed, same parent_key
    assert _key(_inputs(bars2, events, maps)) != base


def test_unversioned_geometry_override_refuses_caching(tmp_path):
    bars = _bars(300)
    maps = [mm(i, decision_ts_ns=int(bars.decision_ts_ns(i)), market="SYN") for i in range(300)]
    with pytest.raises(ValueError, match="geometry_version"):
        S.run_study_cached(
            dag.ArtifactStore(tmp_path),
            "e",
            bars,
            {},
            parent_key="p",
            maps=maps,
            geometry_fn=_geo,
            partition=None,
        )


# ------------------------------------------------------------------------------------------------ HIGH-3: exit variants + registry scope
def _variants():
    ctl = (
        _hx("e1", 2.0, kind="TARGET", triggered=False),
        _hx("e2", -1.0, kind="STOP", triggered=False),
        _hx("e3", 0.0, kind="PENDING", triggered=False, complete=False),
    )
    mid = (_hx("e1", 0.5), _hx("e2", -0.5), _hx("e3", 0.0, complete=False))
    return {Variant.CONTROL: ctl, Variant.A: mid, Variant.B: mid, Variant.C: mid, Variant.D: ctl}


def _small_study(registry, **kw):
    bars = _bars(300)
    maps = [mm(i, decision_ts_ns=int(bars.decision_ts_ns(i)), market="SYN") for i in range(300)]
    return S.run_study(
        bars,
        {},
        config=S.StudyConfig(directions=(LONG,)),
        registry=registry,
        maps=maps,
        geometry_fn=_geo,
        partition=None,
        **kw,
    )


def test_exit_variants_are_registered_and_counted_in_n_hypotheses():
    reg = ST.HypothesisRegistry("ev")
    res = _small_study(reg, exit_variants=_variants())
    n_tested = 4 * len(S.EXIT_CONTRASTS)  # A-D x contrasts, each vs CONTROL
    assert res.multiplicity["n_exit_variant_hypotheses"] == n_tested
    assert res.multiplicity["n_hypotheses"] == reg.n_hypotheses == len(S.ARMS) + n_tested
    assert {r["variant"] for r in res.exit_variants} == {"A", "B", "C", "D"}
    a = next(r for r in res.exit_variants if r["variant"] == "A")
    assert (
        a["n_pairs"] == 3 and a["n_complete"] == 2 and a["n_pending_excluded"] == 1
    )  # pending is excluded, never a loss
    assert a["mean_r_delta_vs_control"] == pytest.approx(((0.5 - 2.0) + (-0.5 + 1.0)) / 2)
    assert a["whipsaw_rate"] == pytest.approx(0.5) and a["descriptive_only"] is True
    assert all(h in reg._family for r in res.exit_variants for h in r["hypotheses"])
    again = _small_study(
        reg, exit_variants=_variants()
    )  # a rerun is a NEW study (registry state is keyed): counted again, never reset
    assert again.key != res.key and reg.n_hypotheses == 2 * (len(S.ARMS) + n_tested)


def test_registry_scope_is_explicit_and_loud(tmp_path):
    none = _small_study(None)
    assert none.multiplicity_scope == "RUN_LOCAL_ONLY" == none.multiplicity["multiplicity_scope"]
    assert any("RUN_LOCAL_ONLY" in w for w in none.warnings) and any(
        "no registry supplied" in w for w in none.warnings
    )
    assert (
        any("RUN_LOCAL_ONLY" in r for r in none.no_promotion_reasons)
        and none.no_promotion_claim is True
    )
    mem = _small_study(ST.HypothesisRegistry("mem"))
    assert mem.multiplicity_scope == "RUN_LOCAL_ONLY"
    persistent = _small_study(ST.HypothesisRegistry("pers", path=tmp_path / "reg.json"))
    assert persistent.multiplicity_scope == "PERSISTENT" and not any(
        "RUN_LOCAL_ONLY" in r for r in persistent.no_promotion_reasons
    )
    assert persistent.no_promotion_claim is True  # still exploratory


def test_limitations_are_part_of_the_result_and_causal_controls_is_keyed():
    res = _small_study(ST.HypothesisRegistry("lim"))
    text = " | ".join(res.limitations)
    assert (
        "EXPLORATORY" in text
        and "OOS" in text
        and "BOTH sides" in text
        and "causal_controls" in text
    )
    assert res.to_dict()["limitations"] == list(S.LIMITATIONS)
    assert S.thesis_study_key("p", SPEC, S.StudyConfig(causal_controls=True)) != S.thesis_study_key(
        "p", SPEC, S.StudyConfig()
    )


def test_causal_control_ranks_run_end_to_end(study_run):
    bars, maps, events, _, cfg, part, _ = study_run
    res = S.run_study(
        bars,
        events,
        config=replace(cfg, causal_controls=True),
        registry=ST.HypothesisRegistry("cc"),
        maps=maps,
        geometry_fn=_geo,
        partition=part,
    )
    assert res.controls["n_controls"] > 0 and res.controls["control_method"].startswith(
        "observer-controls"
    )


def test_registry_identity_hashes_names_families_and_results_not_just_counts():
    bars = _bars(300)
    maps = [mm(i, decision_ts_ns=int(bars.decision_ts_ns(i)), market="SYN") for i in range(300)]
    events = {int(bars.decision_ts_ns(5)): (LONG_TRIG,)}

    def reg_with(name, fam="f", p=None):
        r = ST.HypothesisRegistry("same")
        r.register(name, fam)
        if p is not None:
            r.record(name, p)
        return r

    base = _key(_inputs(bars, events, maps, registry=reg_with("h|a")))
    assert base == _key(_inputs(bars, events, maps, registry=reg_with("h|a")))
    assert (
        _key(_inputs(bars, events, maps, registry=reg_with("h|b"))) != base
    )  # equal counts, other name
    assert (
        _key(_inputs(bars, events, maps, registry=reg_with("h|a", fam="g"))) != base
    )  # other family
    assert (
        _key(_inputs(bars, events, maps, registry=reg_with("h|a", p=0.2))) != base
    )  # recorded result


def test_cache_hit_into_an_incompatible_registry_is_a_miss(tmp_path):
    bars = _bars(300)
    maps = [mm(i, decision_ts_ns=int(bars.decision_ts_ns(i)), market="SYN") for i in range(300)]
    store = dag.ArtifactStore(tmp_path)
    kw = {"maps": maps, "partition": None, "config": S.StudyConfig(directions=(LONG,))}
    a = S.run_study_cached(
        store, "e", bars, {}, parent_key="p", registry=ST.HypothesisRegistry("r"), **kw
    )
    # the stored run claims a different starting state than the registry presented: not restorable
    key_dir = store.stage_dir(bars.market, S.THESIS_STUDY_STAGE, a.key)
    stored = (key_dir / "study.json").read_text("utf-8")
    assert a.registry_state_at_start and a.registry_state_at_start in stored
    reg = ST.HypothesisRegistry("r")
    b = S.run_study_cached(store, "e", bars, {}, parent_key="p", registry=reg, **kw)
    assert b.cached and reg.n_hypotheses == a.multiplicity["n_hypotheses"]
    res = S.StudyResult(**{**a.to_dict(), "registry_state_at_start": "other-state"})
    assert not S._registry_compatible(ST.HypothesisRegistry("r"), res, a.registry_state_at_start)
    clash = ST.HypothesisRegistry("r")
    clash.register(a.registry_entries[0][0], a.registry_entries[0][1])
    assert not S._registry_compatible(clash, a, a.registry_state_at_start)
