# ruff: noqa: E501
"""TemporalGenome: canonical fixed point, hashing, JSON, validity, derived dataflow, SHORT mirror."""

from __future__ import annotations

import json
import random
from dataclasses import replace

import numpy as np
import pytest

from alpha.discovery import temporal_genome as tg
from alpha.discovery.temporal_archetypes import ARCHETYPES, available_archetypes, random_genome
from alpha.discovery.temporal_compile import behavior_key, build_long_spec, compile_temporal
from alpha.discovery.temporal_search import _new_step, _random_anchor_clause, _random_step_event
from alpha.events import schema as ev
from alpha.temporal import spec as sp

POOL = tg.EventPool.full()


def _wild(rng: np.random.Generator) -> tg.TemporalGenome:
    """Arbitrary (possibly invalid, off-grid, unsorted, duplicated) genome for the fixed-point property."""
    n = int(rng.integers(1, 6))
    steps = []
    for _ in range(n):
        e = _random_step_event(rng, POOL)
        s = _new_step(rng, POOL, e)
        steps.append(replace(s, within=int(rng.integers(1, 49)),
                             guard=str(rng.choice([s.guard, "none", "S:TREND_UP:H1", "F:atr_pct:gt:0.333"]))))
    anchor = tuple(_random_anchor_clause(rng, POOL) for _ in range(int(rng.integers(1, 4))))
    anchor = anchor + anchor[:1]  # duplicate on purpose
    stop = tg.StopGene(str(rng.choice(tg.STOP_KINDS)),
                       str(rng.choice(tg.PICKS)), float(rng.uniform(-0.2, 1.4)), float(rng.uniform(0, 5)),
                       float(rng.uniform(0, 8)), str(rng.choice(("M5", "M15"))))
    target = tg.TargetGene(str(rng.choice(("fixed_r", "next_structure"))), float(rng.uniform(0, 5)),
                           ("h1_swing_high", "pdh", "pdh"), float(rng.uniform(0, 5)), float(rng.uniform(0, 4)))
    return tg.TemporalGenome(
        direction=str(rng.choice(("LONG", "SHORT"))), anchor=anchor, steps=tuple(steps),
        context=(), expires_after=int(rng.integers(1, 120)), stop=stop, target=target,
        window=(int(rng.integers(0, 1500)), int(rng.integers(0, 1500))) if rng.random() < 0.5 else None,
    )


def test_canonicalization_fixed_point_random_and_wild():
    rng = np.random.default_rng(1)
    for i in range(3000):
        g = random_genome(rng, POOL) if i % 2 == 0 else _wild(rng)
        c = tg.canonicalize(g)
        assert tg.canonicalize(c) == c, g
        assert tg.canonical_hash(c) == tg.canonical_hash(g)
        assert c.expires_after in tg.EXPIRES_GRID and all(s.within in tg.WITHIN_GRID for s in c.steps)


def test_permutations_and_noise_hash_equal():
    rng = np.random.default_rng(2)
    r = random.Random(2)
    for _ in range(400):
        g = random_genome(rng, POOL)
        h = tg.canonical_hash(g)
        anchor, context = list(g.anchor), list(g.context)
        r.shuffle(anchor)
        r.shuffle(context)
        noisy_stop = replace(g.stop, buffer_atr=g.stop.buffer_atr + 0.01, max_risk_atr=g.stop.max_risk_atr + 0.05)
        g2 = replace(g, anchor=tuple(anchor + anchor[:1]), context=tuple(context), stop=noisy_stop,
                     lineage="OTHER>MUT9")
        assert tg.canonical_hash(g2) == h


def test_hash_domain_separated_and_lineage_excluded():
    g = random_genome(np.random.default_rng(3), POOL)
    payload = tg.canonical_payload(g)
    assert payload["schema"] == "temporal-genome" and "lineage" not in payload
    assert tg.CANON_DOMAIN == b"temporal-v1:"
    assert tg.canonical_hash(g) != tg.canonical_hash(replace(g, target=replace(g.target, r=g.target.r + 0.5, kind="fixed_r")))
    assert tg.canonical_hash(replace(g, lineage="X")) == tg.canonical_hash(g)


def test_json_roundtrip_hashable_frozen():
    rng = np.random.default_rng(4)
    for _ in range(200):
        g = random_genome(rng, POOL)
        back = tg.TemporalGenome.from_json(g.to_json())
        assert back == g and hash(back) == hash(g)
        json.loads(g.to_json())  # valid JSON, no NaN
        with pytest.raises(AttributeError):
            g.direction = "LONG"  # type: ignore[misc]


def test_generated_genomes_cover_archetypes_and_validate():
    rng = np.random.default_rng(5)
    assert set(available_archetypes(POOL)) == set(ARCHETYPES)
    seen = set()
    for name in ARCHETYPES:
        for _ in range(60):
            g = random_genome(rng, POOL, archetype=name)
            tg.validate(g)
            seen.add(g.lineage.split(">")[0])
            assert 1 <= len(g.steps) <= 5 and g.lineage.split(">")[0] == name
    assert seen == set(ARCHETYPES)


def test_validate_rejects_structural_violations():
    g = random_genome(np.random.default_rng(6), POOL, archetype="ZONE_SWEEP_RECLAIM_BOS_RETEST")
    bad = [
        replace(g, steps=()),
        replace(g, steps=g.steps * 2),  # > 5
        replace(g, steps=(replace(g.steps[0], within=7),)),
        replace(g, steps=(replace(g.steps[0], guard="F:nope:gt:0.5"),)),
        replace(g, expires_after=7),
        replace(g, anchor=()),
        replace(g, direction="FLAT"),
        replace(g, steps=(replace(g.steps[0], event=tg.EventGene("SWEEP_HIGH", "M5", "prior20")),)),  # SHORT-frame name
        replace(g, steps=(replace(g.steps[0], event=tg.EventGene("SWEEP_LOW", "M5", "")),)),  # variant missing
        replace(g, context=(sp.Clause("event", "BOS_UP", "M5"),)),  # context event must use BEFORE
    ]
    for b in bad:
        with pytest.raises(tg.GenomeError):
            tg.validate(b)
        assert not tg.is_valid(b)


def test_guard_equal_to_trigger_is_dropped_and_within_clamped():
    g = tg.TemporalGenome(
        "LONG", (sp.Clause("state", "TREND_UP", "H1"),),
        (tg.StepGene(tg.EventGene("TREND_UP", "M15", op="HOLD", arg=5), 48, "S:TREND_UP:M15", "none"),
         tg.StepGene(tg.EventGene("BOS_UP", "M5"), 12, "EB:BOS_UP:M5:5", "none")),
        expires_after=16, stop=tg.StopGene("atr", "", 0.0, 2.0, 3.0), target=tg.TargetGene("fixed_r", 2.0))
    c = tg.canonicalize(g)
    assert c.steps[0].guard == "none"  # implied by HOLD on the same state
    assert c.steps[0].within == 24 and c.steps[1].within == 12  # 48 >= expires_after 16 never binds -> smallest grid >= 16; 12 stays
    assert c.steps[1].guard == "EB:BOS_UP:M5:5"


def test_complexity_formula():
    g = tg.TemporalGenome(
        "LONG", (sp.Clause("state", "TREND_UP", "H1"), sp.Clause("feature", "atr_pct", "M5", cmp="gt", q=0.4)),
        (tg.StepGene(tg.EventGene("SWEEP_LOW", "M5", "prior20"), 5, "F:range_ratio:lt:0.30", "BD:t0:first"),
         tg.StepGene(tg.EventGene("BOS_UP", "M15"), 8)),
        context=(sp.Clause("state", "TREND_UP", "D1"),), expires_after=24,
        stop=tg.StopGene("register", "ext", 0.1, 0.0, 3.0), target=tg.TargetGene("fixed_r", 2.0))
    # states 3 + clauses (2 anchor + 1 ctx + 2 steps) + guards 1 + invalidates 1 + tf {M5,M15,H1,D1}=4 + fitted q 2
    assert tg.complexity(g) == 3 + 5 + 1 + 1 + 4 + 2
    assert tg.genome_tfs(g) == ("M5", "M15", "H1", "D1")


def _section2_genome(direction: str = "LONG") -> tg.TemporalGenome:
    return tg.TemporalGenome(
        direction,
        (sp.Clause("state", "TREND_UP", "H1"), sp.Clause("event", "ZONE_ENTER", "M15", variant="swing_cluster")),
        (tg.StepGene(tg.EventGene("SWEEP_LOW", "M5", "prior20"), 8, "none", "BD:t0:first"),
         tg.StepGene(tg.EventGene("RECLAIM_UP", "M5", "k3"), 3, "none", "BD:t0:ext"),
         tg.StepGene(tg.EventGene("BOS_UP", "M5"), 5),
         tg.StepGene(tg.EventGene("RETEST_HOLD_UP", "M5"), 3)),
        expires_after=48, stop=tg.StopGene("register", "ext", 0.1, 0.0, 3.0),
        target=tg.TargetGene("next_structure", 0.0, ("h1_swing_high", "pdh", "session_high"), 2.0, 1.5))


def test_dataflow_is_derived_like_the_section2_example():
    spec = build_long_spec(_section2_genome())
    assert [(c.reg, c.source, c.of) for c in spec.anchor_capture] == [("R0", "lv", "ZONE_LO")]
    t = spec.states
    assert [(c.reg, c.source, c.of) for c in t[0].capture] == [("R1", "evx", "SWEEP_LOW"), ("R2", "evl", "SWEEP_LOW")]
    # T1 invalidates on R0 (first register), T2's RECLAIM reads a level (latest lvl = sweep level R2 ... never a future reg)
    assert t[0].invalidate[0].reg == "R0"  # 'first' register = zone low
    assert t[1].trigger.reg == "R2" and t[1].invalidate[0].reg == "R1"  # RECLAIM reads the swept level, invalidated below the wick
    assert t[2].capture[0] == sp.Capture("R3", "evl", "BOS_UP")
    assert t[3].trigger.reg == "R3"  # RETEST_HOLD reads the BOS level, never an earlier one
    assert spec.stop.kind == "register" and spec.stop.reg == "R1"  # latest 'ext' = sweep wick low
    sp.validate(spec)


def test_captured_regs_never_exceed_budget_and_all_reads_are_backed():
    rng = np.random.default_rng(7)
    for _ in range(600):
        g = random_genome(rng, POOL)
        s = build_long_spec(tg.canonicalize(g), canonical=True)
        caps = [c.reg for c in s.anchor_capture] + [c.reg for t in s.states for c in t.capture]
        assert len(caps) == len(set(caps)) <= sp.MAX_REG
        sp.validate(s)  # includes 'no read before capture'


def test_unavailable_events_shrink_the_pool():
    names = set()
    for d in ev.all_events():
        if d.has_arrays and "M5" in d.tfs:
            for v in d.variants():
                names.update(ev.array_names(d.name, "M5", v))
    pool = tg.EventPool.from_array_names(names, tfs=("M5",))
    assert not pool.available("TREND_UP", "H1") and pool.available("BOS_UP", "M5")
    rng = np.random.default_rng(8)
    for _ in range(80):
        g = random_genome(rng, pool)
        assert all(s.event.tf == "M5" for s in g.steps)
        assert all(c.tf == "M5" for c in (*g.anchor, *g.context))


def test_behavior_key_ignores_ids_and_collapses_equal_thresholds():
    g1 = tg.TemporalGenome(
        "LONG", (sp.Clause("state", "TREND_UP", "H1"), sp.Clause("feature", "atr_pct", "M5", cmp="gt", q=0.40)),
        (tg.StepGene(tg.EventGene("BOS_UP", "M5"), 8),), expires_after=24,
        stop=tg.StopGene("atr", "", 0.0, 2.0, 3.0), target=tg.TargetGene("fixed_r", 2.0))
    g2 = replace(g1, anchor=(g1.anchor[0], replace(g1.anchor[1], q=0.45)), lineage="Z>MUT3")
    assert tg.canonical_hash(g1) != tg.canonical_hash(g2)
    flat = lambda name, q: 1.5  # noqa: E731  (both quantiles resolve to the same threshold)
    steep = lambda name, q: q * 10  # noqa: E731
    k1, k2 = (behavior_key(compile_temporal(g, flat)) for g in (g1, g2))
    assert k1 == k2
    s1, s2 = (behavior_key(compile_temporal(g, steep)) for g in (g1, g2))
    assert s1 != s2 and s1 != k1


def test_unresolvable_threshold_fails_closed():
    g = random_genome(np.random.default_rng(9), POOL, archetype="COMPRESSION_EXPANSION")
    with pytest.raises(tg.GenomeError):
        compile_temporal(g, lambda n, q: float("nan"))
    with pytest.raises(tg.GenomeError):
        compile_temporal(g, lambda n, q: {}[(n, q)])
