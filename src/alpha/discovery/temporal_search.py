# ruff: noqa: E501
"""Operators + drivers of the V2 temporal search (design section 5).  Research only.

* ``mutate_structure`` / ``crossover``: structural operators.  Register dataflow is derived at compile
  time, so "only where dataflow allows" means: the child must pass ``validate()`` (which compiles it);
  bounded retries, on exhaustion the parent(s) come back unchanged.
* ``param_space`` / ``with_params``: numeric genes of a FIXED structure, grid values only (indices for
  within / variant / tol / expires, grid floats for stop/target/q).
* ``optimize_temporal_structure``: Optuna TPE over one structure; every trial goes through the evaluator
  ledger as a 'param' trial.
* ``evolve_temporal``: DEAP (mu + lambda) with quality-diversity: MAP-Elites archive keyed by niche,
  novelty bonus for rarely visited niches in the PARENT tournament, per-niche + per-family caps in the
  survivor selection, canonical-hash dedupe, twin-aware hall of fame, unique-evaluation budget.

Objective = Train-only fitness of the sealed ``TrainView``; this module has no Validation reference.
"""

from __future__ import annotations

import copy
import math
import random
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

import numpy as np
import optuna
from deap import base, creator, tools

from alpha.discovery.fitness import train_fitness
from alpha.discovery.search import lineage_family, make_lineage, parse_lineage
from alpha.discovery.temporal_archetypes import (
    ARCHETYPES,
    LONG_TARGET_LEVELS,
    WINDOWS,
    random_genome,
    random_stop,
    random_target,
)
from alpha.discovery.temporal_genome import (
    EXPIRES_GRID,
    MAX_STEPS,
    EventGene,
    EventPool,
    GenomeError,
    StepGene,
    StopGene,
    TargetGene,
    TemporalGenome,
    canonical_hash,
    canonicalize,
    guard_preset_ids,
    invalidate_preset_ids,
    is_valid,
    long_variants,
    parse_preset,
)
from alpha.discovery.temporal_niches import Elite, NicheArchive, NicheKey, niche_key
from alpha.events import schema as ev
from alpha.temporal import spec as sp

optuna.logging.set_verbosity(optuna.logging.WARNING)

ParamSpec = tuple[str, float, float, str, float]  # (name, low, high, "int" | "float", step)
MUTATION_RETRIES = 40
CROSSOVER_RETRIES = 20
MAX_TRIALS_PER_STRUCTURE = 200
TOURNAMENT_SIZE = 3
LINEAGE_CAP_FRACTION = 0.25
COLLISION_RETRIES = 6

# ---- option catalogues -----------------------------------------------------------------------
_STEP_NAMES = ("SWEEP_LOW", "SWING_LOW_CONF", "SWING_HIGH_CONF", "BOS_UP", "CHOCH_UP", "ZONE_EXIT",
               "TRENDLINE_TOUCH", "TRENDLINE_BREAK", "PATTERN_COMPLETE", "MOMENTUM_RESUME_UP",
               "TOUCH", "BREAK_UP", "BREAK_DN", "RECLAIM_UP", "RETEST_HOLD_UP", "TREND_UP")
_ANCHOR_NAMES = ("ZONE_ENTER", "SWEEP_LOW", "SWING_LOW_CONF", "SWING_HIGH_CONF", "PATTERN_COMPLETE",
                 "BOS_UP", "CHOCH_UP", "TRENDLINE_TOUCH", "MOMENTUM_RESUME_UP")


def _pick(rng: np.random.Generator, seq):
    return seq[int(rng.integers(len(seq)))]


@lru_cache(maxsize=32)
def _step_options(pool: EventPool) -> dict[str, tuple[EventGene, ...]]:
    out: dict[str, list[EventGene]] = {}
    for name in _STEP_NAMES:
        d = ev.get(name)
        for tf in ("M5", "M15", "H1"):
            for v in long_variants(name):
                if not pool.available(name, tf, v):
                    continue
                if d.kind == "state":
                    out.setdefault(name, []).extend(
                        EventGene(name, tf, v, op="HOLD", arg=a) for a in (3, 5, 8))
                elif d.bound_only and name in ("TOUCH", "BREAK_UP", "BREAK_DN", "RETEST_HOLD_UP"):
                    out.setdefault(name, []).extend(EventGene(name, tf, v, tol=t) for t in ev.TOL_GRID)
                else:
                    out.setdefault(name, []).append(EventGene(name, tf, v))
    for name in ("TOUCH", "BREAK_UP", "BREAK_DN", "RECLAIM_UP", "RETEST_HOLD_UP"):
        if name in out:  # bound clauses are evaluated on M5 bars only
            out[name] = [g for g in out[name] if g.tf == "M5"]
    return {k: tuple(v) for k, v in out.items() if v}


def _random_step_event(rng: np.random.Generator, pool: EventPool) -> EventGene | None:
    opts = _step_options(pool)
    if not opts:
        return None
    return _pick(rng, opts[_pick(rng, sorted(opts))])


def _random_anchor_clause(rng: np.random.Generator, pool: EventPool) -> sp.Clause:
    r = rng.random()
    if r < 0.25:
        return sp.Clause("feature", _pick(rng, sorted(ev.FEATURE_MIRROR)), "M5",
                         cmp=_pick(rng, ("gt", "lt")), q=_pick(rng, (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)))
    if r < 0.5:
        tfs = [t for t in ("M15", "H1", "D1") if pool.available("TREND_UP", t)]
        if tfs:
            hold = _pick(rng, (0, 3, 5))
            return sp.Clause("state", "TREND_UP", _pick(rng, tfs), "HOLD" if hold else "IS", hold)
    for _ in range(20):
        name = _pick(rng, _ANCHOR_NAMES)
        opts = [(tf, v) for tf in ("M5", "M15", "H1") for v in long_variants(name)
                if pool.available(name, tf, v)]
        if opts:
            tf, v = _pick(rng, opts)
            return sp.Clause("event", name, tf, variant=v)
    return sp.Clause("feature", "range_ratio", "M5", cmp="lt", q=0.3)


def _bump_lineage(parent: TemporalGenome, other: TemporalGenome | None = None, *, mut: int = 0,
                  x: int = 0) -> str:
    fam, m, xo = parse_lineage(parent.lineage)
    if other is not None:
        _f, m2, x2 = parse_lineage(other.lineage)
        m, xo = max(m, m2), max(xo, x2)
    return make_lineage(fam, m + mut, xo + x)


# ---- mutation --------------------------------------------------------------------------------
def _new_step(rng, pool, ev_gene: EventGene) -> StepGene:
    guards, invs = guard_preset_ids(pool), invalidate_preset_ids(pool)
    return StepGene(ev_gene, int(_pick(rng, sp.WITHIN_GRID)),
                    _pick(rng, guards[1:]) if rng.random() < 0.3 else "none",
                    _pick(rng, invs[1:]) if rng.random() < 0.4 else "none")


def _op_insert(g, rng, pool):
    if len(g.steps) >= MAX_STEPS or (e := _random_step_event(rng, pool)) is None:
        return None
    i = int(rng.integers(0, len(g.steps) + 1))
    return replace(g, steps=(*g.steps[:i], _new_step(rng, pool, e), *g.steps[i:]))


def _op_delete(g, rng, pool):
    if len(g.steps) <= 1:
        return None
    i = int(rng.integers(len(g.steps)))
    return replace(g, steps=(*g.steps[:i], *g.steps[i + 1:]))


def _op_replace(g, rng, pool):
    if (e := _random_step_event(rng, pool)) is None:
        return None
    i = int(rng.integers(len(g.steps)))
    return replace(g, steps=(*g.steps[:i], replace(g.steps[i], event=e), *g.steps[i + 1:]))


def _op_swap(g, rng, pool):
    if len(g.steps) < 2:
        return None
    i = int(rng.integers(len(g.steps) - 1))
    s = list(g.steps)
    s[i], s[i + 1] = s[i + 1], s[i]
    return replace(g, steps=tuple(s))


def _op_within(g, rng, pool):
    i = int(rng.integers(len(g.steps)))
    cur = sp.WITHIN_GRID.index(g.steps[i].within)
    new = min(max(cur + _pick(rng, (-1, 1)), 0), len(sp.WITHIN_GRID) - 1) if rng.random() < 0.6 \
        else int(rng.integers(len(sp.WITHIN_GRID)))
    return replace(g, steps=(*g.steps[:i], replace(g.steps[i], within=sp.WITHIN_GRID[new]), *g.steps[i + 1:]))


def _op_preset(field_name: str, ids_fn):
    def op(g, rng, pool):
        i = int(rng.integers(len(g.steps)))
        ids = ids_fn(pool)
        new = "none" if rng.random() < 0.25 else _pick(rng, ids[1:])
        return replace(g, steps=(*g.steps[:i], replace(g.steps[i], **{field_name: new}), *g.steps[i + 1:]))
    return op


def _op_stop(g, rng, pool):
    if rng.random() < 0.5:
        return replace(g, stop=random_stop(rng, zone=True))
    s = g.stop
    return replace(g, stop=replace(
        s, buffer_atr=float(_pick(rng, (0.0, 0.05, 0.1, 0.25, 0.5))),
        max_risk_atr=float(_pick(rng, (1.5, 2.0, 3.0, 4.0, 6.0)))))


def _op_target(g, rng, pool):
    return replace(g, target=random_target(rng))


def _op_window(g, rng, pool):
    return replace(g, window=None if g.window is not None and rng.random() < 0.5 else _pick(rng, WINDOWS))


def _op_context(g, rng, pool):
    ctx = list(g.context)
    r = rng.random()
    if ctx and (r < 0.35 or len(ctx) >= 3):
        ctx.pop(int(rng.integers(len(ctx))))
        if rng.random() < 0.4:
            ctx.append(_random_context_clause(rng, pool))
    else:
        ctx.append(_random_context_clause(rng, pool))
    return replace(g, context=tuple(ctx))


def _random_context_clause(rng, pool) -> sp.Clause:
    if rng.random() < 0.5:
        tfs = [t for t in ("H1", "D1") if pool.available("TREND_UP", t)]
        if tfs:
            tf = _pick(rng, tfs)
            return sp.Clause("state", "TREND_UP", tf, "HOLD", _pick(rng, (3, 5))) if tf == "H1" \
                else sp.Clause("state", "TREND_UP", "D1")
    return sp.Clause("feature", _pick(rng, sorted(ev.FEATURE_MIRROR)), "M5", cmp=_pick(rng, ("gt", "lt")),
                     q=_pick(rng, (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)))


def _op_anchor(g, rng, pool):
    a = list(g.anchor)
    r = rng.random()
    if r < 0.4 and len(a) < 3:
        a.append(_random_anchor_clause(rng, pool))
    elif r < 0.65 and len(a) > 1:
        a.pop(int(rng.integers(len(a))))
    else:
        a[int(rng.integers(len(a)))] = _random_anchor_clause(rng, pool)
    return replace(g, anchor=tuple(a))


def _op_expires(g, rng, pool):
    cur = EXPIRES_GRID.index(g.expires_after) if g.expires_after in EXPIRES_GRID else 0
    new = min(max(cur + _pick(rng, (-2, -1, 1, 2)), 0), len(EXPIRES_GRID) - 1)
    return replace(g, expires_after=EXPIRES_GRID[new])


_OPS = (
    (_op_insert, 0.15), (_op_delete, 0.10), (_op_replace, 0.15), (_op_swap, 0.08),
    (_op_within, 0.10), (_op_preset("guard", guard_preset_ids), 0.09),
    (_op_preset("invalidate", invalidate_preset_ids), 0.09), (_op_stop, 0.08), (_op_target, 0.06),
    (_op_window, 0.03), (_op_context, 0.05), (_op_anchor, 0.10), (_op_expires, 0.03),
)
_OP_P = np.array([w for _, w in _OPS])
_OP_P = _OP_P / _OP_P.sum()


def mutate_structure(genome: TemporalGenome, rng: np.random.Generator,
                     pool: EventPool | None = None) -> TemporalGenome:
    """One structural mutation; canonical, validate()-passing, different from the parent.

    Bounded retries; on exhaustion the parent is returned unchanged.
    """
    pool = pool if pool is not None else EventPool.full()
    parent_hash = canonical_hash(genome)
    for _ in range(MUTATION_RETRIES):
        op = _OPS[int(rng.choice(len(_OPS), p=_OP_P))][0]
        try:
            child = op(genome, rng, pool)
            if child is None:
                continue
            child = canonicalize(replace(child, lineage=_bump_lineage(genome, mut=1)))
        except GenomeError:
            continue
        if canonical_hash(child) != parent_hash and is_valid(child):
            return child
    return genome


# ---- crossover -------------------------------------------------------------------------------
def _repair(c: TemporalGenome) -> TemporalGenome | None:
    """Cumulative fallbacks for a child that does not validate (register repair is implicit)."""
    try:
        c = canonicalize(c)
    except GenomeError:
        return None
    if is_valid(c):
        return c
    fixes = [
        lambda x: replace(x, stop=StopGene("register", "ext", x.stop.buffer_atr, 0.0, x.stop.max_risk_atr)),
        lambda x: replace(x, steps=tuple(replace(s, guard="none") for s in x.steps)),
        lambda x: replace(x, steps=tuple(replace(s, invalidate="none") for s in x.steps)),
        lambda x: replace(x, context=()),
        lambda x: replace(x, stop=StopGene("atr", "", 0.0, 2.0, 3.0), target=TargetGene("fixed_r", 2.0)),
    ]
    for fx in fixes:
        try:
            c = canonicalize(fx(c))
        except GenomeError:
            return None
        if is_valid(c):
            return c
    return None


def crossover(a: TemporalGenome, b: TemporalGenome, rng: np.random.Generator
              ) -> tuple[TemporalGenome, TemporalGenome]:
    """One-point crossover on the step lists (+ register repair); returns a validate()-passing pair.

    Child 1 keeps ``a``'s anchor/context/window/expires/stop/target (child 2 ``b``'s); with
    probability 0.3 the stop and target genes swap between the children.  Bounded retries, then the
    parents come back unchanged.
    """
    lineage = _bump_lineage(a, b, x=1)
    lineage_b = _bump_lineage(b, a, x=1)
    for _ in range(CROSSOVER_RETRIES):
        ca = int(rng.integers(0, len(a.steps) + 1))
        cb = int(rng.integers(0, len(b.steps) + 1))
        s1 = (*a.steps[:ca], *b.steps[cb:])[:MAX_STEPS]
        s2 = (*b.steps[:cb], *a.steps[ca:])[:MAX_STEPS]
        if not s1 or not s2:
            continue
        c1 = replace(a, steps=s1, lineage=lineage)
        c2 = replace(b, steps=s2, lineage=lineage_b)
        if rng.random() < 0.3:
            c1, c2 = (replace(c1, stop=b.stop, target=b.target), replace(c2, stop=a.stop, target=a.target))
        r1, r2 = _repair(c1), _repair(c2)
        if r1 is not None and r2 is not None:
            return r1, r2
    return a, b


# ---- Optuna parameter space ------------------------------------------------------------------
def _variants(name: str, pool: EventPool | None, tf: str) -> tuple[str, ...]:
    vs = long_variants(name)
    return tuple(v for v in vs if pool is None or pool.available(name, tf, v))


def param_space(genome: TemporalGenome, pool: EventPool | None = None) -> list[ParamSpec]:
    """Ordered numeric genes of the FIXED structure of ``genome`` (canonical order, stable names)."""
    g = canonicalize(genome)
    space: list[ParamSpec] = []
    for i, s in enumerate(g.steps):
        space.append((f"within{i}", 0, len(sp.WITHIN_GRID) - 1, "int", 1))
        vs = _variants(s.event.name, pool, s.event.tf)
        if len(vs) > 1:
            space.append((f"evvar{i}", 0, len(vs) - 1, "int", 1))
        if s.event.name in ("TOUCH", "BREAK_UP", "BREAK_DN", "RETEST_HOLD_UP"):
            space.append((f"tol{i}", 0, len(ev.TOL_GRID) - 1, "int", 1))
    space.append(("expires", 0, len(EXPIRES_GRID) - 1, "int", 1))
    st = g.stop
    if st.kind in ("register", "zone_edge", "swing"):
        space.append(("stop_buffer", *sp.BUFFER_RANGE[:2], "float", sp.BUFFER_RANGE[2]))
    if st.kind == "atr":
        space.append(("stop_mult", *sp.ATR_MULT_RANGE[:2], "float", sp.ATR_MULT_RANGE[2]))
    space.append(("stop_risk", *sp.MAX_RISK_RANGE[:2], "float", sp.MAX_RISK_RANGE[2]))
    if g.target.kind == "fixed_r":
        space.append(("target_r", *sp.R_RANGE[:2], "float", sp.R_RANGE[2]))
    else:
        space.append(("fallback_r", *sp.R_RANGE[:2], "float", sp.R_RANGE[2]))
        space.append(("min_space", *sp.MIN_SPACE_RANGE[:2], "float", sp.MIN_SPACE_RANGE[2]))
    q = (sp.Q_RANGE[0], sp.Q_RANGE[1], "float", sp.Q_RANGE[2])
    for j, c in enumerate(g.anchor):
        if c.kind == "feature":
            space.append((f"q_anchor{j}", *q))
    for j, c in enumerate(g.context):
        if c.kind == "feature":
            space.append((f"q_context{j}", *q))
    for i, s in enumerate(g.steps):
        if parse_preset(s.guard).kind == "feature":
            space.append((f"q_guard{i}", *q))
        if parse_preset(s.invalidate).kind == "feature":
            space.append((f"q_inv{i}", *q))
    return space


def current_values(genome: TemporalGenome, pool: EventPool | None = None) -> dict[str, float]:
    g = canonicalize(genome)
    out: dict[str, float] = {}
    for name, *_ in param_space(g, pool):
        if name.startswith("within"):
            out[name] = sp.WITHIN_GRID.index(g.steps[int(name[6:])].within)
        elif name.startswith("evvar"):
            i = int(name[5:])
            out[name] = _variants(g.steps[i].event.name, pool, g.steps[i].event.tf).index(g.steps[i].event.variant)
        elif name.startswith("tol"):
            out[name] = ev.TOL_GRID.index(g.steps[int(name[3:])].event.tol)
        elif name == "expires":
            out[name] = EXPIRES_GRID.index(g.expires_after)
        elif name == "stop_buffer":
            out[name] = g.stop.buffer_atr
        elif name == "stop_mult":
            out[name] = g.stop.atr_mult
        elif name == "stop_risk":
            out[name] = g.stop.max_risk_atr
        elif name == "target_r":
            out[name] = g.target.r
        elif name == "fallback_r":
            out[name] = g.target.fallback_r
        elif name == "min_space":
            out[name] = g.target.min_space_r
        elif name.startswith("q_anchor"):
            out[name] = g.anchor[int(name[8:])].q
        elif name.startswith("q_context"):
            out[name] = g.context[int(name[9:])].q
        elif name.startswith("q_guard"):
            out[name] = parse_preset(g.steps[int(name[7:])].guard).q
        elif name.startswith("q_inv"):
            out[name] = parse_preset(g.steps[int(name[5:])].invalidate).q
    return out


def with_params(genome: TemporalGenome, params: dict[str, float],
                pool: EventPool | None = None) -> TemporalGenome:
    """Apply numeric genes to the canonical genome; result is canonical and validated (GenomeError)."""
    g = canonicalize(genome)
    steps = list(g.steps)
    anchor, context = list(g.anchor), list(g.context)
    stop, target, expires = g.stop, g.target, g.expires_after
    snap_q = lambda v: sp.snap(min(max(float(v), sp.Q_RANGE[0]), sp.Q_RANGE[1]), sp.Q_RANGE[2])  # noqa: E731

    def preset_q(pid: str, v: float) -> str:
        p = parse_preset(pid)
        return replace(p, q=snap_q(v)).pid()

    for name, v in params.items():
        if name.startswith("within"):
            i = int(name[6:])
            steps[i] = replace(steps[i], within=sp.WITHIN_GRID[int(v)])
        elif name.startswith("evvar"):
            i = int(name[5:])
            steps[i] = replace(steps[i], event=replace(
                steps[i].event, variant=_variants(steps[i].event.name, pool, steps[i].event.tf)[int(v)]))
        elif name.startswith("tol"):
            i = int(name[3:])
            steps[i] = replace(steps[i], event=replace(steps[i].event, tol=ev.TOL_GRID[int(v)]))
        elif name == "expires":
            expires = EXPIRES_GRID[int(v)]
        elif name == "stop_buffer":
            stop = replace(stop, buffer_atr=float(v))
        elif name == "stop_mult":
            stop = replace(stop, atr_mult=float(v))
        elif name == "stop_risk":
            stop = replace(stop, max_risk_atr=float(v))
        elif name == "target_r":
            target = replace(target, r=float(v))
        elif name == "fallback_r":
            target = replace(target, fallback_r=float(v))
        elif name == "min_space":
            target = replace(target, min_space_r=float(v))
        elif name.startswith("q_anchor"):
            j = int(name[8:])
            anchor[j] = replace(anchor[j], q=snap_q(v))
        elif name.startswith("q_context"):
            j = int(name[9:])
            context[j] = replace(context[j], q=snap_q(v))
        elif name.startswith("q_guard"):
            i = int(name[7:])
            steps[i] = replace(steps[i], guard=preset_q(steps[i].guard, v))
        elif name.startswith("q_inv"):
            i = int(name[5:])
            steps[i] = replace(steps[i], invalidate=preset_q(steps[i].invalidate, v))
        else:
            raise GenomeError(f"unknown parameter {name!r}")
    try:
        child = canonicalize(replace(g, steps=tuple(steps), anchor=tuple(anchor), context=tuple(context),
                                     stop=stop, target=target, expires_after=expires))
    except (IndexError, KeyError, ValueError) as exc:
        raise GenomeError(str(exc)) from None
    if not is_valid(child):
        raise GenomeError("with_params produced an invalid genome")
    return child


# ---- candidate record ------------------------------------------------------------------------
@dataclass
class TemporalCandidate:
    genome: TemporalGenome  # canonical
    genome_hash: str
    fitness: float
    evaluation: Any  # sealed evaluation record of the evaluator
    niche: NicheKey | None = None


# ---- Optuna ----------------------------------------------------------------------------------
@dataclass
class TemporalStudy:
    best_genome: TemporalGenome
    best_fitness: float
    n_trials: int
    n_pruned: int
    top: list[TemporalCandidate] = field(default_factory=list)


def optimize_temporal_structure(evaluator: Any, genome: TemporalGenome, n_trials: int, seed: int,
                                *, pool: EventPool | None = None, top_n: int = 5) -> TemporalStudy:
    """TPE over the numeric genes of ONE structure; every trial is a 'param' trial in the ledger."""
    assert 1 <= n_trials <= MAX_TRIALS_PER_STRUCTURE, f"n_trials {n_trials} out of bounds"
    base_g = canonicalize(genome)
    space = param_space(base_g, pool)
    found: dict[str, TemporalCandidate] = {}
    before = evaluator.ledger.param_trials

    def objective(trial: optuna.Trial) -> float:
        params: dict[str, float] = {}
        for name, lo, hi, kind, step in space:
            params[name] = trial.suggest_int(name, int(lo), int(hi)) if kind == "int" \
                else trial.suggest_float(name, lo, hi, step=step)
        try:
            cand = with_params(base_g, params, pool)
        except GenomeError:
            raise optuna.TrialPruned() from None
        res = evaluator.evaluate(cand, kind="param", need_base=False)
        fit = train_fitness(res.train, evaluator.min_trades)
        if res.rejected:
            raise optuna.TrialPruned()
        found.setdefault(res.genome_hash, TemporalCandidate(canonicalize(cand), res.genome_hash, fit, res))
        return fit

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed),
                                pruner=optuna.pruners.NopPruner(), storage=None)
    start = current_values(base_g, pool)
    if len(start) == len(space):
        study.enqueue_trial({n: (int(start[n]) if k == "int" else float(start[n])) for n, _lo, _hi, k, _s in space})
    study.optimize(objective, n_trials=n_trials, n_jobs=1, gc_after_trial=False)
    assert evaluator.ledger.param_trials - before == n_trials or len(study.trials) == n_trials
    ranked = sorted(found.values(), key=lambda c: (-c.fitness, c.genome_hash))
    n_pruned = sum(t.state == optuna.trial.TrialState.PRUNED for t in study.trials)
    if ranked:
        return TemporalStudy(ranked[0].genome, ranked[0].fitness, n_trials, n_pruned, ranked[:top_n])
    return TemporalStudy(base_g, -1.0, n_trials, n_pruned, [])


# ---- DEAP quality-diversity evolution ---------------------------------------------------------
if not hasattr(creator, "TDFitnessMax"):
    creator.create("TDFitnessMax", base.Fitness, weights=(1.0,))


class TIndividual:
    """DEAP individual: immutable genome + true fitness + selection fitness (fitness + novelty)."""

    __slots__ = ("chash", "fitness", "genome", "sel_fitness")

    def __init__(self, genome: TemporalGenome) -> None:
        self.genome = genome
        self.chash = canonical_hash(genome)
        self.fitness = creator.TDFitnessMax()
        self.sel_fitness = creator.TDFitnessMax()

    @property
    def family(self) -> str:
        return lineage_family(self.genome.lineage)

    def __deepcopy__(self, memo: dict) -> TIndividual:
        new = TIndividual.__new__(TIndividual)
        new.genome, new.chash = self.genome, self.chash
        new.fitness = copy.deepcopy(self.fitness, memo)
        new.sel_fitness = copy.deepcopy(self.sel_fitness, memo)
        return new


@dataclass
class TGenerationStats:
    generation: int
    best: float
    median: float
    unique_hashes: int
    lineage_histogram: dict[str, int]
    evaluations_used: int
    cache_hit_rate: float | None
    niches: int
    twins_rejected: int
    cap_relaxed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


@dataclass
class TemporalDeapResult:
    hall_of_fame: list[TemporalCandidate]
    population: list[TemporalCandidate]
    stats: list[TGenerationStats]
    evaluations_used: int
    budget_exhausted: bool
    archive: NicheArchive
    scored: dict[str, TemporalCandidate] = field(default_factory=dict)

    @property
    def hof_hashes(self) -> list[str]:
        return [c.genome_hash for c in self.hall_of_fame]


class _Scorer:
    """Memoised Train-only scoring with a unique-evaluation budget and archive bookkeeping."""

    def __init__(self, evaluator: Any, max_evaluations: int | None, archive: NicheArchive) -> None:
        self.evaluator, self.budget, self.archive = evaluator, max_evaluations, archive
        self.memo: dict[str, tuple[float, TemporalCandidate | None]] = {}
        self.calls = 0
        self.hits0 = evaluator.ledger.cache_hits
        self.exhausted = False

    def can_afford(self, chash: str) -> bool:
        if chash in self.memo:
            return True
        if self.budget is not None and len(self.memo) >= self.budget:
            self.exhausted = True
            return False
        return True

    def score(self, ind: TIndividual) -> bool:
        if not self.can_afford(ind.chash):
            return False
        if ind.chash not in self.memo:
            res = self.evaluator.evaluate(ind.genome, kind="structural", need_base=False)
            self.calls += 1
            fit = train_fitness(res.train, self.evaluator.min_trades)
            cand = None
            if not res.rejected:
                canon = canonicalize(ind.genome)
                key = niche_key(canon, res.trades_per_day)
                cand = TemporalCandidate(canon, res.genome_hash, fit, res, key)
                self.archive.visit(key)
                self.archive.insert(Elite(key, res.genome_hash, fit, res.twin_hash, cand))
            self.memo[ind.chash] = (fit, cand)
        ind.fitness.values = (self.memo[ind.chash][0],)
        return True

    def candidate(self, ind: TIndividual) -> TemporalCandidate | None:
        return self.memo[ind.chash][1]

    def eligible(self, c: TemporalCandidate) -> bool:
        """Not a behavioural twin that lost its archive slot to a better twin."""
        th = c.evaluation.twin_hash
        owner = self.archive.twin_owner.get(th) if th else None
        return owner is None or owner == c.genome_hash

    def hit_rate(self) -> float | None:
        if not self.calls:
            return None
        return (self.evaluator.ledger.cache_hits - self.hits0) / self.calls


def _stratified_seed(rng, pool: EventPool, population: int, scorer: _Scorer, strata: Sequence[str],
                     tries: int = 60) -> list[TIndividual]:
    inds: list[TIndividual] = []
    seen: set[str] = set()
    slot = 0
    while len(inds) < population and slot < population * 4:
        name = strata[slot % len(strata)]
        slot += 1
        pick = None
        for _ in range(tries):
            g = random_genome(rng, pool, archetype=name)
            if canonical_hash(g) not in seen:
                pick = g
                break
        if pick is None:
            continue
        seen.add(canonical_hash(pick))
        ind = TIndividual(pick)
        if not scorer.score(ind):
            break
        inds.append(ind)
    return inds


def _select_survivors(cands: list[TIndividual], mu: int, fam_cap: int, niche_cap: int,
                      scorer: _Scorer) -> tuple[list[TIndividual], bool]:
    ranked = sorted(cands, key=lambda i: (-i.fitness.values[0], i.chash))
    chosen: list[TIndividual] = []
    per_fam: Counter[str] = Counter()
    per_niche: Counter[NicheKey | None] = Counter()
    rest: list[TIndividual] = []

    def niche(i: TIndividual) -> NicheKey | None:
        c = scorer.candidate(i)
        return None if c is None else c.niche

    for ind in ranked:
        n = niche(ind)
        ok = per_fam[ind.family] < fam_cap and (n is None or per_niche[n] < niche_cap)
        if len(chosen) < mu and ok:
            chosen.append(ind)
            per_fam[ind.family] += 1
            per_niche[n] += 1
        else:
            rest.append(ind)
    relaxed = False
    for ind in rest:
        if len(chosen) >= mu:
            break
        chosen.append(ind)
        relaxed = True
    return chosen, relaxed


def _update_hof(hof: tools.HallOfFame, inds: list[TIndividual], scorer: _Scorer) -> None:
    valid = [i for i in inds if (c := scorer.candidate(i)) is not None and scorer.eligible(c)]
    # One hall-of-fame slot per behavioural twin group: keep the best (ties: smaller hash) even when the
    # rival arrives in a later generation or neither twin owns an archive slot.
    pool = {i.chash: i for i in list(hof) + valid}
    best: dict[str, TIndividual] = {}
    for i in sorted(pool.values(), key=lambda i: (-i.fitness.values[0], i.chash)):
        c = scorer.candidate(i)
        twin = c.evaluation.twin_hash if c is not None else ""
        best.setdefault(twin or f"solo:{i.chash}", i)
    hof.clear()
    hof.update(sorted(best.values(), key=lambda i: (-i.fitness.values[0], i.chash)))


def evolve_temporal(
    evaluator: Any, pool: EventPool | None, population: int, generations: int, seed: int,
    cxpb: float = 0.6, mutpb: float = 0.4, hof_size: int = 50, *, max_evaluations: int | None = None,
    lineage_cap: float = LINEAGE_CAP_FRACTION, niche_cap: int | None = None,
    novelty_weight: float = 0.05, elite_pb: float = 0.15, archive: NicheArchive | None = None,
    progress: Any = None,
) -> TemporalDeapResult:
    """(mu + lambda) evolution with quality-diversity (see the module docstring).

    ``evaluator.evaluate(genome, kind, need_base)`` must return a sealed record with ``.rejected``,
    ``.train`` (TrainView), ``.genome_hash``, ``.trades_per_day`` and ``.twin_hash``.
    """
    assert population >= 2 and generations >= 0 and hof_size >= 1 and 0.0 < lineage_cap <= 1.0
    pool = pool if pool is not None else EventPool.full()
    saved_state = random.getstate()
    random.seed(seed)
    try:
        rng = np.random.default_rng(seed)
        fam_cap = max(1, math.ceil(lineage_cap * population))
        n_cap = niche_cap if niche_cap is not None else max(2, math.ceil(0.2 * population))
        archive = archive if archive is not None else NicheArchive()
        scorer = _Scorer(evaluator, max_evaluations, archive)
        strata = tuple(n for n in ARCHETYPES if _fillable(n, pool))
        toolbox = base.Toolbox()
        toolbox.register("clone", copy.deepcopy)
        toolbox.register("select", tools.selTournament, tournsize=TOURNAMENT_SIZE, fit_attr="sel_fitness")
        hof = tools.HallOfFame(hof_size, similar=lambda a, b: a.chash == b.chash)

        def stats(gen: int, pop: list[TIndividual], relaxed: bool) -> TGenerationStats:
            fits = [i.fitness.values[0] for i in pop]
            return TGenerationStats(
                gen, max(fits) if fits else float("nan"),
                float(statistics.median(fits)) if fits else float("nan"),
                len({i.chash for i in pop}), dict(sorted(Counter(i.family for i in pop).items())),
                len(scorer.memo), scorer.hit_rate(), archive.n_niches, archive.twins_rejected, relaxed)

        pop = _stratified_seed(rng, pool, population, scorer, strata)
        pop, relaxed = _select_survivors(pop, population, fam_cap, n_cap, scorer)
        _update_hof(hof, pop, scorer)
        history = [stats(0, pop, relaxed)]
        if progress:
            progress(history[-1])

        for gen in range(1, generations + 1):
            if scorer.exhausted or len(pop) < 2:
                break
            for i in pop:  # selection fitness = true fitness + novelty bonus of its niche
                c = scorer.candidate(i)
                bonus = 0.0 if c is None or c.niche is None else archive.novelty(c.niche, novelty_weight)
                i.sel_fitness.values = (i.fitness.values[0] + bonus,)
            parents = [toolbox.clone(p) for p in toolbox.select(pop, len(pop))]
            elites = archive.elites()
            if elites:
                for k in range(len(parents)):
                    if rng.random() < elite_pb:
                        parents[k] = TIndividual(elites[int(rng.integers(len(elites)))].payload.genome)
            taken = {p.chash for p in pop}
            offspring: list[TIndividual] = []
            for i in range(0, len(parents) - 1, 2):
                a, b = parents[i], parents[i + 1]
                ga, gb = crossover(a.genome, b.genome, rng) if rng.random() < cxpb else (a.genome, b.genome)
                for g in (ga, gb):
                    if rng.random() < mutpb or canonical_hash(g) in taken:
                        g = mutate_structure(g, rng, pool)
                    for _ in range(COLLISION_RETRIES):
                        if canonical_hash(g) not in taken:
                            break
                        g = mutate_structure(g, rng, pool)
                    h = canonical_hash(g)
                    if h in taken or not is_valid(g):
                        continue
                    child = TIndividual(g)
                    if not scorer.score(child):
                        break
                    taken.add(h)
                    offspring.append(child)
                if scorer.exhausted:
                    break
            pop, relaxed = _select_survivors(pop + offspring, population, fam_cap, n_cap, scorer)
            _update_hof(hof, pop + offspring, scorer)
            history.append(stats(gen, pop, relaxed))
            if progress:
                progress(history[-1])

        as_cand = [c for c in (scorer.candidate(i) for i in pop) if c is not None]
        scored = {h: c for h, (_, c) in scorer.memo.items() if c is not None}
        hof_c = [c for c in (scorer.candidate(i) for i in hof) if c is not None and scorer.eligible(c)]
        return TemporalDeapResult(hof_c, as_cand, history, len(scorer.memo), scorer.exhausted, archive, scored)
    finally:
        random.setstate(saved_state)


def _fillable(name: str, pool: EventPool) -> bool:
    rng = np.random.default_rng(0)
    return any(ARCHETYPES[name](rng, pool) is not None for _ in range(12))


__all__ = (
    "LONG_TARGET_LEVELS", "MAX_TRIALS_PER_STRUCTURE", "ParamSpec", "TGenerationStats",
    "TIndividual", "TemporalCandidate", "TemporalDeapResult", "TemporalStudy", "crossover",
    "current_values", "evolve_temporal", "lineage_family", "mutate_structure",
    "optimize_temporal_structure", "param_space", "with_params",
)
