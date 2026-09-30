# ruff: noqa: E501
"""DEAP genetic programming over formula trees with Train-only fitness, novelty and a random baseline.

Research only.  This module sees ONLY what the caller hands it: a ``FactorScorer`` and ``EvalContext``
built on the Train-prefix ``FormulaData`` (see ``alpha.formula.prepare``).  It never imports a split,
partition or gate object.

* Typed DEAP primitive set: every (operator, parameter) pair is one primitive (``MA@20``, ``Quantile@48@0.8``,
  ...), leaves are terminals (``T:name`` array, ``K:v`` constant, ``B:0/1`` bool constant, ``L:ATR@n`` / ``L:TR``
  bar helpers).  Pointwise operators are registered several times so they are not drowned by the ~170
  windowed primitives.  Limits: DEAP height <= 4 (5 levels) and <= 15 nodes (``staticLimit``).
* Individuals are converted to ``alpha.formula.tree.Node``, canonicalised and hashed; a formula is scored at
  most once per canonical hash (memo).  ``max_evals`` bounds UNIQUE canonical formulas scored by one run.
* Fitness (``fitness.FactorScorer``): Train rank-IC vs ATR-scaled forward return, stability, complexity penalty.
* Novelty / quality-diversity: parents are selected on ``fit * (1 - novelty_w * max|corr| to the decorrelated
  hall of fame)``; a MAP-Elites archive keeps the best formula per niche (dominant operator family, best
  horizon, |corr| cluster) and a share of parents is drawn from it every generation.
* Determinism: ``random`` is seeded from ``seed`` for the run (global state restored afterwards); ties are
  broken by canonical hash; single process.
* ``random_baseline`` draws the same number of unique trees from the SAME primitive set with the same
  initialiser and no evolution.  ``null_fitness_distribution`` / ``run_null_frames`` are the V1-style null
  hooks (label circular shift, or any caller-supplied null frame such as the AD1 block-shuffled frames).
"""

from __future__ import annotations

import operator
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
from deap import base, creator, gp, tools

from alpha.formula import ops
from alpha.formula.data import FormulaData
from alpha.formula.fitness import FactorScore, FactorScorer, shifted_labels
from alpha.formula.tree import (
    MAX_DEPTH,
    MAX_NODES,
    EvalContext,
    FormulaError,
    Node,
    canonical_hash,
    canonicalize,
    complexity,
    dominant_family,
    validate,
)

DEAP_HEIGHT = MAX_DEPTH - 1
CORR_CLUSTER = 0.7
TOURNAMENT = 3
INIT_MIN_H, INIT_MAX_H = 1, 3

if not hasattr(creator, "FormulaFitnessMax"):
    creator.create("FormulaFitnessMax", base.Fitness, weights=(1.0,))
if not hasattr(creator, "FormulaTree"):
    creator.create("FormulaTree", gp.PrimitiveTree, fitness=creator.FormulaFitnessMax)


def _noop(*_a):  # DEAP needs a callable; evaluation goes through tree.EvalContext
    return None


# ------------------------------------------------------------------------------ primitive set
def build_pset(terminals: Sequence[str], *, consts: Sequence[float] = ops.CONSTS,
               pointwise_share: float = 0.45) -> gp.PrimitiveSetTyped:
    pset = gp.PrimitiveSetTyped("formula", [], float)
    tmap = {"f": float, "b": bool}
    windowed = [(s, p) for s in ops.OPS.values() if s.arity > 0 and s.windowed for p in s.param_grid()]
    pointwise = [s for s in ops.OPS.values() if s.arity > 0 and not s.windowed]
    weight = max(1, round(pointwise_share / (1 - pointwise_share) * len(windowed) / max(1, len(pointwise))))
    for spec, params in windowed:
        name = spec.name + "".join(f"@{v:g}" for v in params)
        pset.addPrimitive(_noop, [tmap[t] for t in spec.arg_types], tmap[spec.ret_type], name=name)
    for spec in pointwise:
        for params in spec.param_grid():
            for j in range(weight):
                name = spec.name + "".join(f"@{v:g}" for v in params) + f"#{j}"
                pset.addPrimitive(_noop, [tmap[t] for t in spec.arg_types], tmap[spec.ret_type], name=name)
    for t in terminals:
        pset.addTerminal(f"T:{t}", float)
    for v in consts:
        pset.addTerminal(f"K:{v:g}", float)
    pset.addTerminal("B:0", bool)
    pset.addTerminal("B:1", bool)
    for n in ops.WINDOWS:
        pset.addTerminal(f"L:ATR@{n}", float)
    pset.addTerminal("L:TR", float)
    return pset


def _parse_params(spec: ops.OpSpec, parts: list[str]) -> tuple:
    if spec.params == "n":
        return (int(parts[0]),)
    if spec.params == "nq":
        return (int(parts[0]), float(parts[1]))
    if spec.params == "k":
        return (float(parts[0]),)
    return ()


def to_node(ind: Sequence) -> Node:
    """DEAP prefix tree -> ``Node`` (not canonicalised)."""

    def build(i: int) -> tuple[Node, int]:
        x = ind[i]
        if isinstance(x, gp.Primitive):
            base_name = x.name.split("#")[0]
            opname, *ps = base_name.split("@")
            spec = ops.OPS[opname]
            kids, j = [], i + 1
            for _ in range(x.arity):
                k, j = build(j)
                kids.append(k)
            return Node(opname, _parse_params(spec, ps), tuple(kids)), j
        name = x.name
        kind, _, rest = name.partition(":")
        if kind == "T":
            return Node("T", (rest,)), i + 1
        if kind == "K":
            return Node("K", (float(rest),)), i + 1
        if kind == "B":
            return Node("B", (int(rest),)), i + 1
        if kind == "L":
            opname, *ps = rest.split("@")
            return Node(opname, _parse_params(ops.OPS[opname], ps), ()), i + 1
        raise FormulaError(f"unknown terminal {name}")

    node, end = build(0)
    if end != len(ind):
        raise FormulaError("malformed individual")
    return node


# ------------------------------------------------------------------------------ records
@dataclass
class FactorRecord:
    chash: str
    node: Node  # canonical
    cx: int
    family: str
    score: FactorScore
    sig: np.ndarray | None = field(default=None, repr=False)
    niche: tuple | None = None
    origin: str = ""

    @property
    def fitness(self) -> float:
        return self.score.fitness

    def to_dict(self) -> dict:
        return {"hash": self.chash, "expr": self.node.key, "complexity": self.cx, "family": self.family,
                "niche": None if self.niche is None else list(self.niche), "origin": self.origin,
                **self.score.to_dict()}


class _Hof:
    """Decorrelated hall of fame: keeps the fittest formulas whose signatures differ (|corr| < cluster)."""

    def __init__(self, size: int, thr: float = CORR_CLUSTER) -> None:
        self.size, self.thr = size, thr
        self.items: list[FactorRecord] = []

    def _mat(self) -> np.ndarray:
        return np.stack([r.sig for r in self.items]) if self.items else np.zeros((0, 1), np.float32)

    def max_corr(self, sig: np.ndarray, exclude: str | None = None) -> float:
        rows = [r.sig for r in self.items if r.chash != exclude]
        if not rows:
            return 0.0
        return float(np.abs(np.stack(rows) @ sig).max())

    def offer(self, rec: FactorRecord) -> None:
        if not rec.score.valid or rec.sig is None:
            return
        if any(r.chash == rec.chash for r in self.items):
            return
        if self.items:
            c = np.abs(self._mat() @ rec.sig)
            near = np.flatnonzero(c >= self.thr)
            if len(near):
                worst = max(near, key=lambda i: self.items[i].fitness)
                if rec.fitness <= self.items[worst].fitness:
                    return
                for i in sorted(near, reverse=True):  # replace every correlated (weaker) neighbour
                    if self.items[i].fitness < rec.fitness:
                        del self.items[i]
                    else:
                        return
        self.items.append(rec)
        self.items.sort(key=lambda r: (-r.fitness, r.chash))
        del self.items[self.size:]


class _Clusters:
    def __init__(self, thr: float = CORR_CLUSTER) -> None:
        self.reps: list[np.ndarray] = []
        self.thr = thr

    def assign(self, sig: np.ndarray) -> int:
        if self.reps:
            c = np.abs(np.stack(self.reps) @ sig)
            j = int(np.argmax(c))
            if c[j] >= self.thr:
                return j
        self.reps.append(sig)
        return len(self.reps) - 1


@dataclass
class RunResult:
    label: str
    seed: int
    records: dict[str, FactorRecord]
    hof: list[FactorRecord]
    archive: dict[tuple, FactorRecord]
    evals: int
    gen_best: list[float]
    gen_median: list[float]
    generations: int
    n_clusters: int

    def ranked(self) -> list[FactorRecord]:
        return sorted((r for r in self.records.values() if r.score.valid), key=lambda r: (-r.fitness, r.chash))

    def fitness_values(self) -> np.ndarray:
        return np.array([r.fitness for r in self.records.values() if r.score.valid])


# ------------------------------------------------------------------------------ scoring memo
class _Scoring:
    """Memoised scoring with a unique-formula budget; shared by GP and the random baseline."""

    def __init__(self, scorer: FactorScorer, ctx: EvalContext, max_evals: int, ledger, origin: str,
                 hof_size: int) -> None:
        self.scorer, self.ctx, self.budget, self.ledger, self.origin = scorer, ctx, max_evals, ledger, origin
        self.records: dict[str, FactorRecord] = {}
        self.hof = _Hof(hof_size)
        self.clusters = _Clusters()
        self.archive: dict[tuple, FactorRecord] = {}
        self.evals = 0

    @property
    def exhausted(self) -> bool:
        return self.evals >= self.budget

    def score_node(self, raw: Node) -> FactorRecord | None:
        """Record for ``raw`` (memoised); None if invalid or the budget is spent."""
        if self.ledger is not None:
            self.ledger.record(raw, "structural")  # cumulative trial accounting (counts every request)
        try:
            validate(raw)
            canon = canonicalize(raw)
            validate(canon)
        except FormulaError:
            return None
        h = canonical_hash(canon)
        rec = self.records.get(h)
        if rec is not None:
            return rec
        if self.exhausted:
            return None
        self.evals += 1
        f = self.ctx.evaluate(canon)
        cx = complexity(canon)
        sc = self.scorer.score_array(f, cx)
        rec = FactorRecord(h, canon, cx, dominant_family(canon), sc, origin=self.origin)
        if sc.valid:
            rec.sig = self.scorer.signature(f)
            cl = self.clusters.assign(rec.sig)
            rec.niche = (rec.family, sc.best_h, cl)
            cur = self.archive.get(rec.niche)
            if cur is None or (rec.fitness, rec.chash) > (cur.fitness, cur.chash):
                self.archive[rec.niche] = rec
            self.hof.offer(rec)
        self.records[h] = rec
        return rec

    def adjusted(self, rec: FactorRecord, novelty_w: float) -> float:
        if not rec.score.valid or rec.sig is None:
            return -1.0
        return rec.fitness * (1.0 - novelty_w * self.hof.max_corr(rec.sig, exclude=rec.chash))


def _result(label: str, seed: int, sc: _Scoring, gb: list[float], gm: list[float], gens: int) -> RunResult:
    return RunResult(label, seed, sc.records, list(sc.hof.items), dict(sc.archive), sc.evals, gb, gm, gens,
                     len(sc.clusters.reps))


def _toolbox(pset: gp.PrimitiveSetTyped) -> base.Toolbox:
    tb = base.Toolbox()
    tb.register("expr_init", gp.genHalfAndHalf, pset=pset, min_=INIT_MIN_H, max_=INIT_MAX_H)
    tb.register("individual", tools.initIterate, creator.FormulaTree, tb.expr_init)
    tb.register("expr_mut", gp.genFull, min_=0, max_=2)
    tb.register("mate", gp.cxOnePoint)
    tb.register("mutate_u", gp.mutUniform, expr=tb.expr_mut, pset=pset)
    tb.register("mutate_r", gp.mutNodeReplacement, pset=pset)
    tb.register("mutate_s", gp.mutShrink)
    for name in ("mate", "mutate_u", "mutate_r", "mutate_s"):
        for key, lim in ((operator.attrgetter("height"), DEAP_HEIGHT), (len, MAX_NODES)):
            tb.decorate(name, gp.staticLimit(key=key, max_value=lim))
    return tb


def _with_seed(seed: int):
    class _Ctx:
        def __enter__(self):
            self.state = random.getstate()
            random.seed(seed)

        def __exit__(self, *_):
            random.setstate(self.state)

    return _Ctx()


# ------------------------------------------------------------------------------ GP
def evolve(
    scorer: FactorScorer, ctx: EvalContext, pset: gp.PrimitiveSetTyped, seed: int, *,
    max_evals: int = 800, pop_size: int = 50, cx_pb: float = 0.55, mut_pb: float = 0.4,
    hof_size: int = 20, novelty_w: float = 0.5, elite_share: float = 0.2, max_generations: int = 400,
    ledger=None, label: str = "gp",
) -> RunResult:
    """(mu + lambda) GP; stops when ``max_evals`` UNIQUE canonical formulas have been scored."""
    tb = _toolbox(pset)
    sc = _Scoring(scorer, ctx, max_evals, ledger, label, hof_size)
    inds: dict[str, object] = {}  # canonical hash -> DEAP individual (kept for elite re-use)
    gen_best: list[float] = []
    gen_median: list[float] = []

    def adjusted_sorted(pairs: list) -> tuple[list, dict[str, float]]:
        adj = {r.chash: sc.adjusted(r, novelty_w) for _, r in pairs}
        return sorted(pairs, key=lambda p: (-adj[p[1].chash], p[1].chash)), adj

    with _with_seed(seed):
        pop: list = []  # (individual, record)
        tries = 0
        while len(pop) < pop_size and not sc.exhausted and tries < pop_size * 50:
            tries += 1
            ind = tb.individual()
            rec = sc.score_node(to_node(ind))
            if rec is not None and rec.score.valid and all(r.chash != rec.chash for _, r in pop):
                inds[rec.chash] = ind
                pop.append((ind, rec))
        gens = 0
        stale = 0
        while pop and not sc.exhausted and gens < max_generations and stale < 15:
            gens += 1
            n0 = sc.evals
            _, adj = adjusted_sorted(pop)
            elites = sorted(sc.archive.values(), key=lambda r: r.chash)
            parents = []
            for _ in range(pop_size):
                if elites and random.random() < elite_share:
                    ind = inds.get(random.choice(elites).chash)
                    if ind is not None:
                        parents.append(ind)
                        continue
                cand = random.sample(pop, min(TOURNAMENT, len(pop)))
                parents.append(max(cand, key=lambda p: (adj[p[1].chash], p[1].chash))[0])
            offspring = [tb.clone(p) for p in parents]
            for i in range(0, len(offspring) - 1, 2):
                if random.random() < cx_pb:
                    offspring[i], offspring[i + 1] = tb.mate(offspring[i], offspring[i + 1])
            new_pop = list(pop)
            for ind in offspring:
                if random.random() < mut_pb:
                    (ind,) = random.choice((tb.mutate_u, tb.mutate_r, tb.mutate_s))(ind)
                rec = sc.score_node(to_node(ind))
                if rec is None or not rec.score.valid:
                    continue
                inds.setdefault(rec.chash, ind)
                new_pop.append((ind, rec))
            uniq: dict[str, tuple] = {}
            for pair in new_pop:
                uniq.setdefault(pair[1].chash, pair)
            ranked, _ = adjusted_sorted(list(uniq.values()))
            pop = ranked[:pop_size]
            fits = [r.fitness for _, r in pop]
            gen_best.append(max(fits))
            gen_median.append(float(np.median(fits)))
            stale = stale + 1 if sc.evals == n0 else 0
    return _result(label, seed, sc, gen_best, gen_median, gens)


# ------------------------------------------------------------------------------ random baseline
def random_baseline(
    scorer: FactorScorer, ctx: EvalContext, pset: gp.PrimitiveSetTyped, seed: int, *,
    max_evals: int = 800, hof_size: int = 20, ledger=None, label: str = "random", max_tries_factor: int = 60,
) -> RunResult:
    """Same primitive set, same initialiser, same unique-formula budget, no selection or variation."""
    tb = _toolbox(pset)
    sc = _Scoring(scorer, ctx, max_evals, ledger, label, hof_size)
    with _with_seed(seed):
        tries = 0
        while not sc.exhausted and tries < max_evals * max_tries_factor:
            tries += 1
            sc.score_node(to_node(tb.individual()))
    return _result(label, seed, sc, [], [], 0)


# ------------------------------------------------------------------------------ decorrelation
def decorrelated_count(records: Sequence[FactorRecord], top: int = 100, thr: float = CORR_CLUSTER) -> int:
    """Greedy count of mutually decorrelated (|corr| < thr) formulas among the ``top`` fittest."""
    chosen: list[np.ndarray] = []
    ranked = sorted((r for r in records if r.score.valid and r.sig is not None), key=lambda r: (-r.fitness, r.chash))
    for r in ranked[:top]:
        if not chosen or float(np.abs(np.stack(chosen) @ r.sig).max()) < thr:
            chosen.append(r.sig)
    return len(chosen)


# ------------------------------------------------------------------------------ null hooks
def null_fitness_distribution(
    scorer: FactorScorer, ctx: EvalContext, nodes: Sequence[Node], n_shifts: int, seed: int,
) -> np.ndarray:
    """(n_shifts, len(nodes)) fitness of each formula against circularly shifted labels (Train only)."""
    rng = np.random.default_rng(seed)
    shifts = [shifted_labels(scorer, rng) for _ in range(n_shifts)]
    arrays = [(ctx.evaluate(canonicalize(n)), complexity(canonicalize(n))) for n in nodes]
    out = np.full((n_shifts, len(nodes)), np.nan)
    for j, (f, cx) in enumerate(arrays):
        for s, lab in enumerate(shifts):
            out[s, j] = scorer.score_array(f, cx, labels=lab).fitness
    return out


def run_null_frames(
    make_null_data: Callable[[int], FormulaData], seeds: Sequence[int], *, max_evals: int = 200,
    pop_size: int = 30, terminals: Sequence[str] | None = None,
) -> list[dict]:
    """V1-style hook: GP and random baseline on each caller-supplied null frame (no exploitable structure).

    ``make_null_data(seed)`` returns a Train-prefix ``FormulaData`` (e.g. built from the AD1 block-shuffled
    null frame).  Returns per seed the best/median Train fitness of GP vs random -- what "beating random on
    real data" has to exceed on data known to contain no signal.
    """
    rows = []
    for s in seeds:
        d = make_null_data(s)
        scorer, ctx = FactorScorer(d), EvalContext(d, 64.0)
        pset = build_pset(list(terminals) if terminals is not None else list(d.terminals))
        g = evolve(scorer, ctx, pset, s, max_evals=max_evals, pop_size=pop_size, label="null_gp")
        r = random_baseline(scorer, ctx, pset, s + 10_000, max_evals=max_evals, label="null_random")
        rows.append({"seed": s, "gp_best": float(g.fitness_values().max()) if len(g.records) else None,
                     "random_best": float(r.fitness_values().max()) if len(r.records) else None})
    return rows


__all__ = (
    "CORR_CLUSTER", "FactorRecord", "RunResult", "build_pset", "decorrelated_count", "evolve",
    "null_fitness_distribution", "random_baseline", "run_null_frames", "to_node",
)
