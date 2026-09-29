"""AD1E probe: Train-only fitness + Optuna over the top structures of a random-genome sample.

Research only.  Search decisions (top-20 selection, Optuna objective, top-K pooling) use TRAIN
fitness exclusively.  The Validation numbers printed at the end are a labelled DIAGNOSTIC to
quantify whether Train fitness carries any out-of-sample signal; they select nothing.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402

from alpha.common.dataset import POINT, load_research_dataset  # noqa: E402
from alpha.discovery.archetypes import random_genome  # noqa: E402
from alpha.discovery.catalog import FeaturePool  # noqa: E402
from alpha.discovery.compile import TrialLedger  # noqa: E402
from alpha.discovery.evaluate import GenomeEval, GenomeEvaluator, validation_gate_view  # noqa: E402
from alpha.discovery.fitness import train_fitness  # noqa: E402
from alpha.discovery.optuna_driver import run_optuna_campaign  # noqa: E402
from alpha.fast.store import FeatureStore  # noqa: E402
from research.runners import ar2_fast  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/ad1_discovery.json"
DEFAULT_CACHE = REPO_ROOT / "data/feature_store/ad1_bench_gram"
DEFAULT_OUT = REPO_ROOT / "research/reports/alpha_discovery_v1/optuna_probe.json"


def _val_positive(ev: GenomeEval, cost: str) -> bool:
    side = getattr(validation_gate_view(ev), cost)
    return side.screen.n_trades > 0 and (side.screen.expectancy_r or 0.0) > 0


def _share(evals: list[GenomeEval]) -> dict:
    n = len(evals)
    return {
        "n": n,
        "validation_positive_adverse": round(sum(_val_positive(e, "adverse") for e in evals)
                                             / n, 4) if n else None,
        "validation_positive_base": round(sum(_val_positive(e, "base") for e in evals)
                                          / n, 4) if n else None,
        "train_positive_adverse": round(sum((e.train.adverse.screen.expectancy_r or 0) > 0
                                            for e in evals) / n, 4) if n else None,
    }


def _rank_corr(x: list[float], y: list[float]) -> float | None:
    if len(x) < 5:
        return None
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    if rx.std() == 0 or ry.std() == 0:
        return None
    return round(float(np.corrcoef(rx, ry)[0, 1]), 4)


def run(n_random: int, n_top: int, trials: int, seed: int, config: Path, cache_dir: Path) -> dict:
    cfg = json.loads(config.read_text(encoding="utf-8"))
    plan = ar2_fast._plan(cfg)
    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    dev = ar2_fast.dev_frame(ds.frame, plan)
    features = FeatureStore.load_or_build(dev, {"point_size": POINT}, cache_dir)
    market, dates = ar2_fast._market(features), ar2_fast._dates(features)
    pool = FeaturePool.from_features(features)
    ledger = TrialLedger()
    evaluator = GenomeEvaluator(features, market, dates, plan, cfg, cache_dir, ledger)

    rng = np.random.default_rng(seed)
    genomes = [random_genome(rng, pool) for _ in range(n_random)]
    t0 = time.perf_counter()
    evals = [evaluator.evaluate(g, kind="structural") for g in genomes]
    structural_s = time.perf_counter() - t0
    passers = [(e, g) for e, g in zip(evals, genomes, strict=True) if not e.rejected]
    passers.sort(key=lambda p: (-train_fitness(p[0].train), p[0].genome_hash))
    top_structures = [g for _, g in passers[:n_top]]

    hits0, sims0, trials0 = ledger.cache_hits, evaluator.sim_count, ledger.total_trials
    t0 = time.perf_counter()
    campaign = run_optuna_campaign(evaluator, top_structures, trials, seed)
    optuna_s = time.perf_counter() - t0
    n_optuna = ledger.total_trials - trials0
    optuna_hits = ledger.cache_hits - hits0

    best_fits = [s.best_fitness for s in campaign.studies]
    all_cands = campaign.candidates
    topk = all_cands[:n_top]
    topk_evals = [c.evaluation for c in topk]
    positive_topk = sum(train_fitness(e.train) > 0 for e in topk_evals)
    positive_adverse = sum((e.train.adverse.screen.expectancy_r or 0) > 0 for e in topk_evals)

    passer_evals = [e for e, _ in passers]
    corr = _rank_corr(
        [train_fitness(e.train) for e in passer_evals],
        [validation_gate_view(e).adverse.screen.expectancy_r or 0.0 for e in passer_evals])
    dist = lambda v: {  # noqa: E731
        "min": round(min(v), 4), "median": round(statistics.median(v), 4),
        "max": round(max(v), 4)} if v else None

    return {
        "seed": seed, "n_random": n_random, "n_top_structures": len(top_structures),
        "trials_per_structure": trials, "optuna_trials": n_optuna,
        "n_stage_a_passers": len(passers),
        "structural_wall_s": round(structural_s, 2),
        "optuna_wall_s": round(optuna_s, 2),
        "optuna_trials_per_s": round(n_optuna / optuna_s, 2) if optuna_s else None,
        "optuna_cache_hit_rate": round(optuna_hits / n_optuna, 4) if n_optuna else None,
        "optuna_simulations_pairs": evaluator.sim_count - sims0,
        "pruned_trials": sum(s.n_pruned for s in campaign.studies),
        "ledger": json.loads(ledger.to_json()) | {"seen": len(ledger.seen)},
        "best_train_fitness_per_structure": dist(best_fits),
        "top_structure_train_fitness_before_optuna": dist(
            [train_fitness(e.train) for e, _ in passers[:n_top]]),
        "pooled_distinct_candidates": len(all_cands),
        "topk": {
            "k": len(topk),
            "train_fitness_positive": int(positive_topk),
            "train_expectancy_positive_combined_adverse": int(positive_adverse),
            "train_fitness": [round(c.fitness, 4) for c in topk],
        },
        "VALIDATION_DIAGNOSTIC_NOT_USED_FOR_SELECTION": {
            "topk_by_train_fitness_after_optuna": _share(topk_evals),
            "random_baseline_all_200": _share(evals),
            "random_baseline_stage_a_passers": _share(passer_evals),
            "top20_structures_before_optuna": _share([e for e, _ in passers[:n_top]]),
            "spearman_train_fitness_vs_validation_adverse_expectancy_passers": corr,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-random", type=int, default=200)
    parser.add_argument("--n-top", type=int, default=20)
    parser.add_argument("--trials", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    report = run(args.n_random, args.n_top, args.trials, args.seed, Path(args.config),
                 Path(args.cache_dir))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"structural: {report['n_random']} genomes, {report['n_stage_a_passers']} pass Stage A, "
          f"{report['structural_wall_s']}s")
    print(f"optuna: {report['optuna_trials']} trials in {report['optuna_wall_s']}s = "
          f"{report['optuna_trials_per_s']} trials/s; cache hit rate "
          f"{report['optuna_cache_hit_rate']}; pruned {report['pruned_trials']}")
    print(f"ledger: {report['ledger']}")
    print(f"best train fitness per structure: {report['best_train_fitness_per_structure']}")
    print(f"top-K: {report['topk']['k']} pooled from {report['pooled_distinct_candidates']}; "
          f"fitness>0: {report['topk']['train_fitness_positive']}; "
          f"train adverse expectancy>0: "
          f"{report['topk']['train_expectancy_positive_combined_adverse']}")
    print("VALIDATION DIAGNOSTIC (not used for selection):")
    for k, v in report["VALIDATION_DIAGNOSTIC_NOT_USED_FOR_SELECTION"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
