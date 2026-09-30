"""Optuna parameter search over FIXED-structure genomes, objective = ``train_fitness``.

Research only.  Uses the shared ``GenomeEvaluator`` (``kind='param'``) and never touches
Validation: this module imports neither ``ValidationView`` nor ``validation_gate_view``.

Determinism: ``TPESampler(seed=seed)`` + ``n_jobs=1`` + in-memory storage.  The first trial of
each study is the structure's own parameters (enqueued), so the incoming genome is always scored.
Trials whose evaluation is rejected (Stage A: zero candidates / too few Train trades / invalid
stops / invalid genome) report their graded fitness as an intermediate value and are pruned, which
TPE consumes as a (bad) observation while keeping them out of the candidate pool.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import optuna

from alpha.discovery.catalog import Q_GRID, TIME_GRID
from alpha.discovery.compile import canonicalize
from alpha.discovery.evaluate import GenomeEval, GenomeEvaluator
from alpha.discovery.fitness import train_fitness
from alpha.discovery.genome import Genome, GenomeError
from alpha.discovery.search import ParamSpec, _quantile_clauses, param_space, with_params

MAX_TRIALS_PER_STRUCTURE = 500
TOP_PER_STRUCTURE = 5

optuna.logging.set_verbosity(optuna.logging.WARNING)


@dataclass(frozen=True)
class Candidate:
    genome: Genome  # canonical
    genome_hash: str
    fitness: float
    evaluation: GenomeEval  # sealed record; Validation only via validation_gate_view


@dataclass
class StudyResult:
    best_genome: Genome
    best_fitness: float
    n_trials: int
    n_pruned: int
    top: list[Candidate]  # top distinct non-rejected genomes over ALL trials (<= 5)
    trial_hashes: list[str] = field(default_factory=list)  # evaluation order (reproducibility)

    def as_tuple(self) -> tuple[Genome, float, int]:
        return self.best_genome, self.best_fitness, self.n_trials


@dataclass
class CampaignResult:
    studies: list[StudyResult]
    candidates: list[Candidate]  # deduped by canonical hash across every trial, best first

    def top_k(self, k: int) -> list[Candidate]:
        return self.candidates[:k]

    def as_tuples(self) -> list[tuple[Genome, float, int]]:
        return [s.as_tuple() for s in self.studies]


def _aligned(lo: float, hi: float, step: float) -> bool:
    k = (hi - lo) / step
    return abs(k - round(k)) < 1e-6


def _suggest(trial: optuna.Trial, spec: ParamSpec) -> float:
    name, lo, hi, kind = spec
    if kind == "int":
        return trial.suggest_int(name, int(lo), int(hi), step=TIME_GRID)
    if name.startswith("q_") and _aligned(lo, hi, Q_GRID):
        return trial.suggest_float(name, lo, hi, step=Q_GRID)
    return trial.suggest_float(name, lo, hi)


def _current_values(genome: Genome, space: Sequence[ParamSpec]) -> dict[str, float]:
    values: dict[str, float] = {}
    for name, _group, _i, clause in _quantile_clauses(genome):
        values[name] = float(clause.q)
    if genome.stop.kind == "atr_multiple":
        values["stop_mult"] = float(genome.stop.multiple)
    elif genome.stop.kind == "session_level":
        values["stop_offset"] = float(genome.stop.offset)
    values["target_r"] = float(genome.target_r)
    if genome.time_window is not None:
        values["tw_start"], values["tw_end"] = map(float, genome.time_window)
    return {n: values[n] for n, *_ in space if n in values}


def _snap_enqueue(values: dict[str, float], space: Sequence[ParamSpec]) -> dict[str, float]:
    """Make the enqueued start point legal for stepped suggestions (grid-aligned, in range)."""
    out = {}
    for name, lo, hi, kind in space:
        v = min(max(values[name], lo), hi)
        if kind == "int":
            v = lo + round((v - lo) / TIME_GRID) * TIME_GRID
            out[name] = int(min(v, hi))
        elif name.startswith("q_") and _aligned(lo, hi, Q_GRID):
            v = lo + round((v - lo) / Q_GRID) * Q_GRID
            out[name] = float(min(v, hi))
        else:
            out[name] = float(v)
    return out


def optimize_structure(
    evaluator: GenomeEvaluator, genome: Genome, n_trials: int, seed: int,
    *, top_n: int = TOP_PER_STRUCTURE,
) -> StudyResult:
    """TPE search over the numeric genes of one structure; objective is ``train_fitness``."""
    assert 1 <= n_trials <= MAX_TRIALS_PER_STRUCTURE, f"n_trials {n_trials} out of bounds"
    base = canonicalize(genome)
    space = param_space(base)
    pool: dict[str, Candidate] = {}
    hashes: list[str] = []
    ledger_before = evaluator.ledger.param_trials

    def objective(trial: optuna.Trial) -> float:
        params = {spec[0]: _suggest(trial, spec) for spec in space}
        try:
            candidate = with_params(base, params)
        except GenomeError:
            trial.report(-1.0, step=0)
            raise optuna.TrialPruned() from None
        ev = evaluator.evaluate(candidate, kind="param", need_base=False)
        fit = train_fitness(ev.train, evaluator.min_trades)
        hashes.append(ev.genome_hash)
        if ev.rejected:
            trial.report(fit, step=0)
            raise optuna.TrialPruned()
        cand = pool.get(ev.genome_hash)
        if cand is None:
            pool[ev.genome_hash] = Candidate(canonicalize(candidate), ev.genome_hash, fit, ev)
        return fit

    study = optuna.create_study(
        direction="maximize", sampler=optuna.samplers.TPESampler(seed=seed),
        pruner=optuna.pruners.NopPruner(), storage=None,  # in-memory; pruning is explicit
    )
    start = _current_values(base, space)
    if len(start) == len(space):
        study.enqueue_trial(_snap_enqueue(start, space))
    study.optimize(objective, n_trials=n_trials, n_jobs=1, gc_after_trial=False)

    assert len(study.trials) == n_trials, "trial bound violated"
    assert evaluator.ledger.param_trials - ledger_before == n_trials, "ledger/trial mismatch"
    ranked = sorted(pool.values(), key=lambda c: (-c.fitness, c.genome_hash))
    n_pruned = sum(t.state == optuna.trial.TrialState.PRUNED for t in study.trials)
    if ranked:
        best = ranked[0]
        return StudyResult(best.genome, best.fitness, n_trials, n_pruned, ranked[:top_n], hashes)
    fallback = max(
        (t.intermediate_values.get(0, -1.0) for t in study.trials), default=-1.0)
    return StudyResult(base, float(fallback), n_trials, n_pruned, [], hashes)


def run_optuna_campaign(
    evaluator: GenomeEvaluator, structures: Sequence[Genome], trials_per_structure: int,
    seed: int, *, top_n: int = TOP_PER_STRUCTURE,
) -> CampaignResult:
    """One study per structure (seed ``seed + index``); pooled distinct candidates."""
    assert trials_per_structure >= 1
    studies = [
        optimize_structure(evaluator, g, trials_per_structure, seed + i, top_n=top_n)
        for i, g in enumerate(structures)
    ]
    merged: dict[str, Candidate] = {}
    for study in studies:
        for cand in study.top:
            if cand.genome_hash not in merged:
                merged[cand.genome_hash] = cand
    ranked = sorted(merged.values(), key=lambda c: (-c.fitness, c.genome_hash))
    return CampaignResult(studies, ranked)


__all__ = (
    "MAX_TRIALS_PER_STRUCTURE", "CampaignResult", "Candidate", "StudyResult",
    "optimize_structure", "run_optuna_campaign",
)
