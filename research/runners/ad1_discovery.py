"""AD1 discovery CAMPAIGN: random structures -> Optuna params -> DEAP structures -> candidate pool.

Research only.  Every search decision (top-K structure choice, Optuna objective, DEAP fitness,
pool ranking) uses TRAIN fitness exclusively.  This runner never reads a Validation number and
never touches OOS: the dev frame has its OOS bars removed and an explicit assertion checks that
the evaluator's bar dates contain no OOS-partition bar.  ``oos_touched: false`` is written only
after that assertion held.

Output: ``candidate_pool.json`` (interface to the later stages) and ``campaign_summary.json``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402

from alpha.common.dataset import POINT, load_research_dataset  # noqa: E402
from alpha.common.protocol import stable_hash  # noqa: E402
from alpha.discovery.archetypes import random_genome  # noqa: E402
from alpha.discovery.catalog import FeaturePool  # noqa: E402
from alpha.discovery.compile import TrialLedger, canonicalize  # noqa: E402
from alpha.discovery.deap_driver import evolve_structures, lineage_family  # noqa: E402
from alpha.discovery.disk import assert_free_space  # noqa: E402
from alpha.discovery.evaluate import (  # noqa: E402
    EVALUATOR_VERSION,
    GenomeEvaluator,
    _sha_arrays,
)
from alpha.discovery.fitness import train_fitness  # noqa: E402
from alpha.discovery.optuna_driver import Candidate, optimize_structure  # noqa: E402
from alpha.discovery.provenance import assert_oos_untouched  # noqa: E402
from alpha.fast.store import FeatureStore  # noqa: E402
from research.runners import ar2_fast  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/ad1_discovery.json"
DEFAULT_CACHE = REPO_ROOT / "data/feature_store/ad1_bench_gram"
POOL_LINEAGE_CAP = 0.15


def log(msg: str) -> None:
    print(msg, flush=True)


def _git_commit() -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
        return head + ("+dirty" if dirty else "")
    except Exception:
        return "UNKNOWN"


class Pool:
    """Best Train fitness per canonical hash, remembering the phase that produced it."""

    def __init__(self) -> None:
        self.best: dict[str, tuple[Candidate, str]] = {}

    def add(self, cand: Candidate, origin: str) -> None:
        cur = self.best.get(cand.genome_hash)
        if cur is None or cand.fitness > cur[0].fitness:
            self.best[cand.genome_hash] = (cand, origin)

    def ranked(self) -> list[tuple[Candidate, str]]:
        return sorted(self.best.values(), key=lambda t: (-t[0].fitness, t[0].genome_hash))


def merge_pool(entries: list[tuple[Candidate, str]], keep: int, cap_fraction: float
               ) -> list[tuple[Candidate, str]]:
    """Top ``keep`` by Train fitness with at most ``cap_fraction`` per lineage family; the
    remainder (if the cap leaves the pool short) is filled by the next best."""
    cap = max(1, int(np.ceil(cap_fraction * keep)))
    chosen: list[tuple[Candidate, str]] = []
    rest: list[tuple[Candidate, str]] = []
    per: Counter[str] = Counter()
    for item in entries:
        fam = lineage_family(item[0].genome.lineage)
        if len(chosen) < keep and per[fam] < cap:
            chosen.append(item)
            per[fam] += 1
        else:
            rest.append(item)
    chosen += rest[: max(0, keep - len(chosen))]
    return sorted(chosen, key=lambda t: (-t[0].fitness, t[0].genome_hash))


def run(args: argparse.Namespace, dev_override=None) -> dict:
    t_start = time.perf_counter()
    config = Path(args.config)
    cfg = json.loads(config.read_text(encoding="utf-8"))
    seed = args.seed if args.seed is not None else cfg["seed"]
    plan = ar2_fast._plan(cfg)
    if dev_override is not None:  # injected (e.g. synthetic null) dev frame; OOS already absent
        dev = dev_override
    else:
        ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
        dev = ar2_fast.dev_frame(ds.frame, plan)
    features = FeatureStore.load_or_build(dev, {"point_size": POINT}, Path(args.cache_dir))
    market, dates = ar2_fast._market(features), ar2_fast._dates(features)
    assert_oos_untouched(plan, dates)  # OOS never enters any frame the evaluator sees
    pool = FeaturePool.from_features(features)
    ledger = TrialLedger()
    evaluator = GenomeEvaluator(features, market, dates, plan, cfg, args.cache_dir, ledger,
                                min_train_trades=args.min_train_trades,
                                max_cache_mb=args.cache_max_mb)
    log(f"[setup] {len(dates)} dev bars, feature pool ready, seed={seed}, "
        f"max_unique_specs={args.max_unique_specs}")

    cands = Pool()
    phases: dict[str, dict] = {}

    def phase_stats(name: str, t0: float, u0: int, hits0: int, trials0: int, best: float | None,
                    n_new: int) -> None:
        evaluator.flush()  # persist buffered evaluation cache shard at phase end
        wall = time.perf_counter() - t0
        trials = ledger.total_trials - trials0
        phases[name] = {
            "wall_s": round(wall, 2), "evaluations": trials,
            "evals_per_s": round(trials / wall, 2) if wall else None,
            "cache_hit_rate": round((ledger.cache_hits - hits0) / trials, 4) if trials else None,
            "unique_specs_added": ledger.unique - u0, "valid_candidates_added": n_new,
            "best_train_fitness": None if best is None else round(best, 6),
        }
        log(f"[{name}] {wall:.1f}s, {trials} evals ({phases[name]['evals_per_s']}/s), "
            f"cache_hit_rate={phases[name]['cache_hit_rate']}, +{ledger.unique - u0} unique "
            f"specs (total {ledger.unique}), "
            f"best_train_fitness={phases[name]['best_train_fitness']}")

    def guard() -> bool:
        return ledger.unique >= args.max_unique_specs

    # ------------------------------------------------------------ (a) random structures
    t0, u0, h0, tr0 = time.perf_counter(), ledger.unique, ledger.cache_hits, ledger.total_trials
    rng = np.random.default_rng(seed)
    random_valid: list[Candidate] = []
    for i in range(args.random_structures):
        if guard():
            log(f"[random] budget guard hit at {i} genomes")
            break
        g = random_genome(rng, pool)
        ev = evaluator.evaluate(g, kind="structural", need_base=False)
        if not ev.rejected:
            fit = train_fitness(ev.train, evaluator.min_trades)
            c = Candidate(canonicalize(g), ev.genome_hash, fit, ev)
            random_valid.append(c)
            cands.add(c, "random")
        if (i + 1) % 100 == 0:
            log(f"[random] {i + 1}/{args.random_structures}, {len(random_valid)} pass Stage A")
    random_valid.sort(key=lambda c: (-c.fitness, c.genome_hash))
    phase_stats("random", t0, u0, h0, tr0, random_valid[0].fitness if random_valid else None,
                len(random_valid))

    # ------------------------------------------------------------ (b) Optuna over top-K
    t0, u0, h0, tr0 = time.perf_counter(), ledger.unique, ledger.cache_hits, ledger.total_trials
    top_structs = [c.genome for c in random_valid[: args.optuna_structures]]
    best_opt, n_opt = None, 0
    for i, g in enumerate(top_structs):
        if guard():
            log(f"[optuna] budget guard hit before structure {i}")
            break
        res = optimize_structure(evaluator, g, args.optuna_trials, seed + i)
        for c in res.top:
            cands.add(c, "optuna")
            n_opt += 1
            best_opt = c.fitness if best_opt is None else max(best_opt, c.fitness)
        if (i + 1) % 10 == 0 or i + 1 == len(top_structs):
            log(f"[optuna] {i + 1}/{len(top_structs)} structures, best so far {best_opt}")
    phase_stats("optuna", t0, u0, h0, tr0, best_opt, n_opt)

    # ------------------------------------------------------------ (c) DEAP structures
    t0, u0, h0, tr0 = time.perf_counter(), ledger.unique, ledger.cache_hits, ledger.total_trials
    best_deap, n_deap, deap_info = None, 0, {}
    if args.deap_gens >= 0 and args.deap_pop >= 2 and not guard():
        remaining = args.max_unique_specs - ledger.unique
        res = evolve_structures(
            evaluator, pool, args.deap_pop, args.deap_gens, seed, args.deap_cxpb, args.deap_mutpb,
            hof_size=max(args.deap_pop, 50), max_evaluations=remaining,
            progress=lambda s: log(
                f"[deap] gen {s.generation}: best={s.best:.4f} median={s.median:.4f} "
                f"unique={s.unique_hashes} evals={s.evaluations_used} "
                f"hit_rate={s.cache_hit_rate}"))
        for c in res.scored.values():
            cands.add(c, "deap")
            n_deap += 1
            best_deap = c.fitness if best_deap is None else max(best_deap, c.fitness)
        deap_info = {"budget_exhausted": res.budget_exhausted,
                     "unique_evaluations": res.evaluations_used,
                     "generations_run": len(res.stats) - 1,
                     "per_generation": [s.to_dict() for s in res.stats]}
    phase_stats("deap", t0, u0, h0, tr0, best_deap, n_deap)

    # ------------------------------------------------------------ (d) merge + write
    assert_oos_untouched(plan, dates)  # still holds after the whole campaign
    merged = merge_pool(cands.ranked(), args.pool_size, POOL_LINEAGE_CAP)
    ledger_json = json.loads(ledger.to_json())
    ledger_json.pop("seen")
    ledger_json.pop("behaviors", None)
    total_s = time.perf_counter() - t_start
    meta = {
        "campaign_version": "ad1-campaign-v1",
        "dataset_hash": _sha_arrays(features, ("ts_ns", "o", "h", "l", "c", "spread")),
        "feature_key": getattr(features, "metadata", {}).get("cache_key"),
        "config_hash": stable_hash(cfg), "config_path": str(config),
        "embargo_days": plan.embargo_days, "splits": plan.to_dict(),
        "evaluator_version": EVALUATOR_VERSION, "evaluator_fingerprint": evaluator._fp_static,
        "seed": seed, "git_commit": _git_commit(),
        "args": {k: v for k, v in vars(args).items() if k != "cache_dir"},
        "min_train_trades": evaluator.min_trades,
        "ledger": ledger_json,
        # trials of EARLIER campaigns on the same data (cumulative multiple-testing N)
        "prior_trials": args.prior_trials, "prior_unique_specs": args.prior_unique_specs,
        "phase_timings_s": {k: v["wall_s"] for k, v in phases.items()},
        "total_wall_s": round(total_s, 2), "pool_size": len(merged),
        "pool_lineage_cap_fraction": POOL_LINEAGE_CAP, "fitness": "train_fitness (Train only)",
        "oos_touched": False,  # written only after assert_oos_untouched() held (twice)
    }
    out_dir = Path(args.out_dir or
                   REPO_ROOT / f"research/reports/alpha_discovery_v1/campaign_{args.tag}")
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "candidates": [
        {"genome": c.genome.to_dict(), "canonical_hash": c.genome_hash,
         "train_fitness": round(c.fitness, 10), "origin": origin,
         "lineage": c.genome.lineage} for c, origin in merged]}
    (out_dir / "candidate_pool.json").write_text(
        json.dumps(payload, indent=1, sort_keys=True, allow_nan=False), encoding="utf-8")
    fam_hist = Counter(lineage_family(c.genome.lineage) for c, _ in merged)
    fit = [c.fitness for c, _ in merged]
    summary = {
        "phases": phases, "deap": deap_info, "total_wall_s": round(total_s, 2),
        "overall_evals_per_s": round(ledger.total_trials / total_s, 2),
        "overall_cache_hit_rate": round(ledger.cache_hits / max(1, ledger.total_trials), 4),
        "unique_specs_total": ledger.unique, "ledger": ledger_json,
        "pool": {"size": len(merged),
                 "origin_histogram": dict(Counter(o for _, o in merged)),
                 "lineage_family_histogram": dict(sorted(fam_hist.items())),
                 "train_fitness": {"max": round(max(fit), 4),
                                   "median": round(statistics.median(fit), 4),
                                   "min": round(min(fit), 4)} if fit else None},
        "oos_touched": False,
    }
    (out_dir / "campaign_summary.json").write_text(
        json.dumps(summary, indent=1, sort_keys=True, allow_nan=False), encoding="utf-8")
    log(f"[done] pool={len(merged)} -> {out_dir / 'candidate_pool.json'}; total {total_s:.1f}s")
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--seed", type=int, default=None, help="default: config seed")
    p.add_argument("--tag", default="run")
    p.add_argument("--out-dir", default=None)
    p.add_argument("--random-structures", type=int, default=600)
    p.add_argument("--optuna-structures", type=int, default=60)
    p.add_argument("--optuna-trials", type=int, default=40)
    p.add_argument("--deap-pop", type=int, default=200)
    p.add_argument("--deap-gens", type=int, default=20)
    p.add_argument("--deap-cxpb", type=float, default=0.6)
    p.add_argument("--deap-mutpb", type=float, default=0.4)
    p.add_argument("--pool-size", type=int, default=600)
    p.add_argument("--max-unique-specs", type=int, default=10000)
    p.add_argument("--min-train-trades", type=int, default=None,
                   help="Train minimum trades (Stage A + fitness); default: config "
                        "sample_rules.min_trades_flag (60). Recorded in meta + cache fingerprint")
    p.add_argument("--prior-trials", type=int, default=0,
                   help="trials of earlier campaigns on this data; stored in pool meta and added "
                        "to N by ad1_survivors")
    p.add_argument("--prior-unique-specs", type=int, default=0)
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    p.add_argument("--cache-max-mb", type=float, default=500.0)
    p.add_argument("--cleanup", action="store_true",
                   help="remove this run's cache directory after completion")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    assert_free_space(args.cache_dir)
    try:
        run(args)
    finally:
        if args.cleanup:
            shutil.rmtree(Path(args.cache_dir), ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
