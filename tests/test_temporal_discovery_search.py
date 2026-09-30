# ruff: noqa: E501
"""Operators: validity of every generated / mutated / crossed genome, determinism, SHORT mirror, Optuna space."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from alpha.discovery import temporal_genome as tg
from alpha.discovery.temporal_archetypes import ARCHETYPES, random_genome
from alpha.discovery.temporal_compile import compile_temporal
from alpha.discovery.temporal_search import (
    crossover,
    current_values,
    mutate_structure,
    param_space,
    with_params,
)
from alpha.events import schema as ev
from alpha.temporal import spec as sp

POOL = tg.EventPool.full()


def test_5000_rounds_generated_mutated_crossed_are_valid_canonical():
    rng = np.random.default_rng(11)
    pop = [random_genome(rng, POOL) for _ in range(40)]
    changed = crossed = 0
    for i in range(5000):
        a, b = pop[int(rng.integers(len(pop)))], pop[int(rng.integers(len(pop)))]
        kind = i % 3
        if kind == 0:
            outs = [random_genome(rng, POOL)]
        elif kind == 1:
            c = mutate_structure(a, rng, POOL)
            changed += c != a
            outs = [c]
        else:
            c1, c2 = crossover(a, b, rng)
            crossed += (c1, c2) != (a, b)
            outs = [c1, c2]
        for g in outs:
            tg.validate(g)
            assert tg.canonicalize(g) == g
            compile_temporal(g, canonical=True)
            assert 1 <= len(g.steps) <= 5 and 1 <= len(g.anchor) <= 3
        pop[int(rng.integers(len(pop)))] = outs[-1]
    assert changed > 1200 and crossed > 900  # the operators really move (not silent parent returns)


def test_mutation_changes_canonical_hash_and_bumps_lineage():
    rng = np.random.default_rng(12)
    g = random_genome(rng, POOL, archetype="FAILED_BREAKOUT")
    seen = 0
    for _ in range(100):
        c = mutate_structure(g, rng, POOL)
        if c != g:
            assert tg.canonical_hash(c) != tg.canonical_hash(g)
            fam, mut, x = c.lineage.split(">")[0], int(c.lineage.split("MUT")[1].split(">")[0]), 0
            assert fam == "FAILED_BREAKOUT" and mut == 1 and x == 0
            seen += 1
    assert seen > 80


def test_crossover_lineage_and_one_point_structure():
    rng = np.random.default_rng(13)
    a = random_genome(rng, POOL, archetype="ZONE_SWEEP_RECLAIM_BOS_RETEST", direction="LONG")
    b = random_genome(rng, POOL, archetype="TREND_PULLBACK_RESUME", direction="LONG")
    outs = [crossover(a, b, rng) for _ in range(60)]
    assert any(c != (a, b) for c in outs)
    for c1, c2 in outs:
        assert (c1.lineage.split(">")[0] == "ZONE_SWEEP_RECLAIM_BOS_RETEST" and ">X1" in c1.lineage) or c1 == a
        assert (c2.lineage.split(">")[0] == "TREND_PULLBACK_RESUME" and ">X1" in c2.lineage) or c2 == b
        # steps of a child are drawn from the parents' steps (canonicalisation may rewrite within/guards only)
        pool_events = {s.event for s in (*a.steps, *b.steps)}
        assert all(s.event in pool_events for s in (*c1.steps, *c2.steps))


def test_determinism_per_seed():
    def run(seed):
        rng = np.random.default_rng(seed)
        out = []
        g = random_genome(rng, POOL)
        for _ in range(60):
            out.append(g.to_json())
            g = mutate_structure(g, rng, POOL)
            h = random_genome(rng, POOL)
            g, _ = crossover(g, h, rng)
        return out

    assert run(21) == run(21)
    assert run(21) != run(22)


def test_short_compiles_to_the_registry_mirror_of_long():
    rng = np.random.default_rng(14)
    n_neg = 0
    for _ in range(400):
        g = random_genome(rng, POOL, direction="LONG")
        long_spec = compile_temporal(g, canonical=True)
        short = compile_temporal(replace(g, direction="SHORT"), canonical=True)
        assert short.direction == "SHORT" and long_spec.direction == "LONG"
        strip = lambda x: {k: v for k, v in x.to_dict().items() if k not in ("strategy_id", "metadata")}  # noqa: E731
        assert strip(short) == strip(sp.mirror(long_spec))  # SHORT is exactly spec.mirror(LONG) (ids differ)
        assert strip(sp.mirror(short)) == strip(long_spec)  # involution
        for cl_l, cl_s in zip(long_spec.anchor, short.anchor, strict=True):
            if cl_l.kind == "feature":
                if ev.feature_mirror(cl_l.name) == "neg":
                    n_neg += 1
                    assert cl_s.cmp != cl_l.cmp and cl_s.q == cl_l.q and cl_s.neg and not cl_l.neg
                else:
                    assert cl_s == cl_l
            else:
                assert cl_s.name == ev.mirror_event(cl_l.name)
        assert short.stop.kind == long_spec.stop.kind
        if long_spec.target.kind == "next_structure":
            assert all(lv.endswith(("low", "pdl")) for lv in short.target.levels)
    assert n_neg >= 5


def test_param_space_grid_values_and_roundtrip():
    rng = np.random.default_rng(15)
    n_q = n_tol = 0
    for name in ARCHETYPES:
        for _ in range(15):
            g = random_genome(rng, POOL, archetype=name)
            space = param_space(g)
            cur = current_values(g)
            assert [s[0] for s in space] == list(cur)  # names stable & complete
            assert with_params(g, cur) == g  # current values reproduce the canonical genome
            names = [s[0] for s in space]
            assert len(set(names)) == len(names)
            for pname, lo, hi, kind, step in space:
                assert lo <= cur[pname] <= hi
                if kind == "float":
                    assert abs(cur[pname] - round(cur[pname] / step) * step) < 1e-6
                n_q += pname.startswith("q_")
                n_tol += pname.startswith("tol")
            # every corner of the space yields a valid canonical genome (or GenomeError, never a silent invalid)
            for pick in (0, 1):
                params = {p: (lo if pick == 0 else hi) for p, lo, hi, kind, _ in space}
                for p, _lo, _hi, kind, _s in space:
                    if kind == "int":
                        params[p] = int(params[p])
                try:
                    child = with_params(g, params)
                except tg.GenomeError:
                    continue
                tg.validate(child)
                assert tg.canonicalize(child) == child
    assert n_q > 0 and n_tol > 0


def test_with_params_rejects_unknown_and_snaps_to_grid():
    g = random_genome(np.random.default_rng(16), POOL, archetype="COMPRESSION_EXPANSION", direction="LONG")
    with pytest.raises(tg.GenomeError):
        with_params(g, {"nonsense": 1})
    space = dict((s[0], s) for s in param_space(g))
    qname = next(n for n in space if n.startswith("q_"))
    child = with_params(g, {qname: 0.5133})
    qs = [c.q for c in child.anchor if c.kind == "feature"]
    assert all(abs(q / 0.05 - round(q / 0.05)) < 1e-6 for q in qs)
