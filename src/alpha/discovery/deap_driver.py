"""DEAP structural search over genomes.  Objective = ``train_fitness`` (Train ONLY).

Research only.  Same structural seal as the Optuna driver: this module never touches the
Validation partition; it only ever passes ``GenomeEval.train`` to ``train_fitness``.

Design (deliberately thin over DEAP, no custom GA framework):

* Individual = a small class carrying an immutable ``Genome`` and a ``deap.base.Fitness``
  (``creator``-registered ``FitnessMax``); DEAP ``toolbox.clone`` (deepcopy) works on it.
* Init: ``random_genome`` with lineage-stratified rejection sampling (uniform over the 15
  archetypes + HYBRID), deduplicated by canonical hash.
* Variation: ``search.crossover`` / ``search.mutate_structure`` (validate()-passing children).
* Parent selection: ``tools.selTournament`` (size 3).  Survivor selection: (mu + lambda),
  best-first with a per-family cap (``lineage_cap`` of the population, default 25%), so no
  single family can take over; ``tools.HallOfFame`` keeps the elite over the whole run.
* Diversity: the population never holds two individuals with the same canonical hash (children
  are re-mutated on collision, bounded retries, else dropped).
* Determinism: ``random`` and a numpy ``Generator`` both derive from ``seed``; n_jobs = 1;
  ties broken by canonical hash.
* Budget: ``max_evaluations`` bounds UNIQUE canonical structures evaluated by this run; the loop
  stops as soon as it would be exceeded.  Repeat genomes are memoised (not re-evaluated).
"""

from __future__ import annotations

import copy
import math
import random
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from deap import base, creator, tools

from alpha.discovery.archetypes import ARCHETYPES, random_genome
from alpha.discovery.catalog import FeaturePool
from alpha.discovery.compile import canonical_hash, canonicalize
from alpha.discovery.evaluate import GenomeEvaluator
from alpha.discovery.fitness import train_fitness
from alpha.discovery.genome import Genome, is_valid
from alpha.discovery.optuna_driver import Candidate
from alpha.discovery.search import crossover, lineage_family, mutate_structure

TOURNAMENT_SIZE = 3
LINEAGE_CAP_FRACTION = 0.25
COLLISION_RETRIES = 6
STRATA = (*sorted(ARCHETYPES), "HYBRID")

if not hasattr(creator, "AD1FitnessMax"):
    creator.create("AD1FitnessMax", base.Fitness, weights=(1.0,))


class Individual:
    """DEAP individual: an immutable Genome plus a ``fitness`` attribute."""

    __slots__ = ("chash", "fitness", "genome")

    def __init__(self, genome: Genome) -> None:
        self.genome = genome
        self.chash = canonical_hash(genome)
        self.fitness = creator.AD1FitnessMax()

    @property
    def family(self) -> str:
        return lineage_family(self.genome.lineage)

    def __deepcopy__(self, memo: dict) -> Individual:
        new = Individual.__new__(Individual)
        new.genome, new.chash = self.genome, self.chash  # immutable, safe to share
        new.fitness = copy.deepcopy(self.fitness, memo)
        return new


@dataclass
class GenerationStats:
    generation: int
    best: float
    median: float
    unique_hashes: int
    lineage_histogram: dict[str, int]
    evaluations_used: int
    cache_hit_rate: float | None
    cap_relaxed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


@dataclass
class DeapResult:
    hall_of_fame: list[Candidate]  # best distinct non-rejected genomes over the whole run
    population: list[Candidate]
    stats: list[GenerationStats]
    evaluations_used: int  # unique canonical structures evaluated by this run
    budget_exhausted: bool
    scored: dict[str, Candidate] = field(default_factory=dict)  # every non-rejected genome seen

    @property
    def hof_hashes(self) -> list[str]:
        return [c.genome_hash for c in self.hall_of_fame]


class _Scorer:
    """Memoised Train-only scoring with a unique-evaluation budget."""

    def __init__(self, evaluator: GenomeEvaluator, max_evaluations: int | None) -> None:
        self.evaluator = evaluator
        self.budget = max_evaluations
        self.memo: dict[str, tuple[float, Candidate | None]] = {}
        self.calls = 0
        self.hits0 = evaluator.ledger.cache_hits
        self.exhausted = False

    def known(self, chash: str) -> bool:
        return chash in self.memo

    def can_afford(self, chash: str) -> bool:
        if chash in self.memo:
            return True
        if self.budget is not None and len(self.memo) >= self.budget:
            self.exhausted = True
            return False
        return True

    def score(self, ind: Individual) -> bool:
        """Assign ``ind.fitness``; False when the budget forbids a new evaluation."""
        if not self.can_afford(ind.chash):
            return False
        if ind.chash not in self.memo:
            ev = self.evaluator.evaluate(ind.genome, kind="structural")
            self.calls += 1
            fit = train_fitness(ev.train, self.evaluator.min_trades)
            cand = None if ev.rejected else Candidate(canonicalize(ind.genome), ev.genome_hash,
                                                      fit, ev)
            self.memo[ind.chash] = (fit, cand)
        ind.fitness.values = (self.memo[ind.chash][0],)
        return True

    def candidate(self, ind: Individual) -> Candidate | None:
        return self.memo[ind.chash][1]

    def hit_rate(self) -> float | None:
        if not self.calls:
            return None
        return (self.evaluator.ledger.cache_hits - self.hits0) / self.calls


def _stratified_seed(
    rng: np.random.Generator, pool: FeaturePool, population: int, scorer: _Scorer,
    *, tries_per_slot: int = 300,
) -> list[Individual]:
    inds: list[Individual] = []
    seen: set[str] = set()
    slot = 0
    while len(inds) < population and slot < population * 4:
        target = STRATA[slot % len(STRATA)]
        slot += 1
        pick: Genome | None = None
        for _ in range(tries_per_slot):
            g = random_genome(rng, pool)
            if g.lineage == target and canonical_hash(g) not in seen:
                pick = g
                break
        if pick is None:  # stratum unreachable/exhausted: accept any new random genome
            for _ in range(tries_per_slot):
                g = random_genome(rng, pool)
                if canonical_hash(g) not in seen:
                    pick = g
                    break
        if pick is None:
            continue
        seen.add(canonical_hash(pick))
        ind = Individual(pick)
        if not scorer.score(ind):
            break
        inds.append(ind)
    return inds


def _select_survivors(
    candidates: list[Individual], mu: int, cap: int
) -> tuple[list[Individual], bool]:
    ranked = sorted(candidates, key=lambda i: (-i.fitness.values[0], i.chash))
    chosen: list[Individual] = []
    per_family: Counter[str] = Counter()
    rest: list[Individual] = []
    for ind in ranked:
        if len(chosen) < mu and per_family[ind.family] < cap:
            chosen.append(ind)
            per_family[ind.family] += 1
        else:
            rest.append(ind)
    relaxed = False
    for ind in rest:  # too few families to honour the cap: fill with the next best
        if len(chosen) >= mu:
            break
        chosen.append(ind)
        relaxed = True
    return chosen, relaxed


def _stats(gen: int, pop: list[Individual], scorer: _Scorer, relaxed: bool) -> GenerationStats:
    fits = [i.fitness.values[0] for i in pop]
    return GenerationStats(
        gen, max(fits) if fits else float("nan"),
        float(statistics.median(fits)) if fits else float("nan"),
        len({i.chash for i in pop}), dict(sorted(Counter(i.family for i in pop).items())),
        len(scorer.memo), scorer.hit_rate(), relaxed,
    )


def evolve_structures(
    evaluator: GenomeEvaluator, pool: FeaturePool, population: int, generations: int, seed: int,
    cxpb: float = 0.6, mutpb: float = 0.4, hof_size: int = 50,
    *, max_evaluations: int | None = None,
    lineage_cap: float = LINEAGE_CAP_FRACTION, progress: Any = None,
) -> DeapResult:
    """(mu + lambda) evolution of genome structures; see the module docstring."""
    assert population >= 2 and generations >= 0 and hof_size >= 1
    assert 0.0 < lineage_cap <= 1.0
    saved_state = random.getstate()
    random.seed(seed)
    try:
        rng = np.random.default_rng(seed)
        cap = max(1, math.ceil(lineage_cap * population))
        scorer = _Scorer(evaluator, max_evaluations)
        toolbox = base.Toolbox()
        toolbox.register("clone", copy.deepcopy)
        toolbox.register("select", tools.selTournament, tournsize=TOURNAMENT_SIZE)
        hof = tools.HallOfFame(hof_size, similar=lambda a, b: a.chash == b.chash)

        pop = _stratified_seed(rng, pool, population, scorer)
        pop, relaxed = _select_survivors(pop, population, cap)
        _update_hof(hof, pop, scorer)
        history = [_stats(0, pop, scorer, relaxed)]
        if progress:
            progress(history[-1])

        for gen in range(1, generations + 1):
            if scorer.exhausted or len(pop) < 2:
                break
            parents = [toolbox.clone(p) for p in toolbox.select(pop, len(pop))]
            taken = {p.chash for p in pop}
            offspring: list[Individual] = []
            for i in range(0, len(parents) - 1, 2):
                a, b = parents[i], parents[i + 1]
                if rng.random() < cxpb:
                    ga, gb = crossover(a.genome, b.genome, rng)
                else:
                    ga, gb = a.genome, b.genome
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
                    child = Individual(g)
                    if not scorer.score(child):
                        break
                    taken.add(h)
                    offspring.append(child)
                if scorer.exhausted:
                    break
            pop, relaxed = _select_survivors(pop + offspring, population, cap)
            _update_hof(hof, pop + offspring, scorer)
            history.append(_stats(gen, pop, scorer, relaxed))
            if progress:
                progress(history[-1])

        as_cand = [c for c in (scorer.candidate(i) for i in pop) if c is not None]
        scored = {h: c for h, (_, c) in scorer.memo.items() if c is not None}
        return DeapResult(
            [c for c in (scorer.candidate(i) for i in hof) if c is not None], as_cand, history,
            len(scorer.memo), scorer.exhausted, scored,
        )
    finally:
        random.setstate(saved_state)


def _update_hof(hof: tools.HallOfFame, inds: list[Individual], scorer: _Scorer) -> None:
    valid = [i for i in inds if scorer.candidate(i) is not None]
    hof.update(sorted(valid, key=lambda i: (-i.fitness.values[0], i.chash)))


__all__ = ("DeapResult", "GenerationStats", "Individual", "evolve_structures", "lineage_family")
