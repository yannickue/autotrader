"""AD1D probe: sample random grammar genomes, compile, evaluate, light-screen (Train/Validation).

Research only.  Thresholds come from TRAIN quantiles; OOS bars are physically removed by
``ar2_fast.dev_frame``.  Positive counts are informational, never used to select anything.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402

from alpha.common.dataset import POINT, load_research_dataset  # noqa: E402
from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec  # noqa: E402
from alpha.discovery.archetypes import random_genome  # noqa: E402
from alpha.discovery.catalog import FeaturePool  # noqa: E402
from alpha.discovery.compile import (  # noqa: E402
    ThresholdResolver,
    TrialLedger,
    behavior_key,
    compile_genome,
)
from alpha.fast.screen import light_screen  # noqa: E402
from alpha.fast.spec import evaluate_spec  # noqa: E402
from alpha.fast.store import FeatureStore  # noqa: E402
from research.runners import ar2_fast  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/ad1_discovery.json"
DEFAULT_CACHE = REPO_ROOT / "data/feature_store/ad1_bench_gram"
DEFAULT_OUT = REPO_ROOT / "research/reports/alpha_discovery_v1/grammar_probe.json"
MIN_TRADES = 30


def run(n: int, seed: int, config: Path, cache_dir: Path) -> dict:
    cfg = json.loads(config.read_text(encoding="utf-8"))
    plan = ar2_fast._plan(cfg)
    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    dev = ar2_fast.dev_frame(ds.frame, plan)
    features = FeatureStore.load_or_build(dev, {"point_size": POINT}, cache_dir)
    market, dates = ar2_fast._market(features), ar2_fast._dates(features)
    resolver = ThresholdResolver.from_plan(features, plan, dates)
    pool = FeaturePool.from_features(features)
    cost = COST_SCENARIOS["BASE"]
    sizing, rules = SizingSpec(**cfg["sizing"]), SimRules(**cfg["rules"])

    # JIT warm-up excluded from timing
    warm = compile_genome(random_genome(np.random.default_rng(0), pool), resolver)
    cand = evaluate_spec(features, warm)
    if len(cand.decision_idx):
        light_screen(market, cand, cost, plan, dates=dates, sizing=sizing, rules=rules)

    rng = np.random.default_rng(seed)
    ledger = TrialLedger()
    behaviors: set[str] = set()
    behavior_dups = 0
    rows: dict[str, dict] = defaultdict(lambda: {"cands": [], "enough": 0, "n": 0})
    invalid_any = invalid_all = 0
    raw_invalid = raw_valid = 0
    pos = {"train": 0, "validation": 0, "both": 0}
    t0 = time.perf_counter()
    for _ in range(n):
        genome = random_genome(rng, pool)
        ledger.record(genome)
        spec = compile_genome(genome, resolver)
        key = behavior_key(spec)
        behavior_dups += key in behaviors
        behaviors.add(key)
        cand = evaluate_spec(features, spec)
        n_cand = len(cand.decision_idx)
        n_bad = evaluate_spec.invalid_stop_count
        raw_valid += n_cand
        raw_invalid += n_bad
        invalid_any += n_bad > 0
        invalid_all += n_cand == 0 and n_bad > 0
        row = rows[genome.lineage]
        row["n"] += 1
        row["cands"].append(n_cand)
        if not n_cand:
            continue
        res = light_screen(market, cand, cost, plan, dates=dates, sizing=sizing, rules=rules)
        row["enough"] += res.train.n_trades >= MIN_TRADES
        tr = res.train.n_trades > 0 and (res.train.expectancy_r or 0.0) > 0
        va = res.validation.n_trades > 0 and (res.validation.expectancy_r or 0.0) > 0
        pos["train"] += tr
        pos["validation"] += va
        pos["both"] += tr and va
    wall = time.perf_counter() - t0
    by_lineage = {
        name: {
            "count": r["n"],
            "median_candidates": statistics.median(r["cands"]),
            "frac_ge_30_train_trades": round(r["enough"] / r["n"], 3),
        }
        for name, r in sorted(rows.items())
    }
    return {
        "n": n, "seed": seed, "pool_size": len(pool.names), "wall_s": round(wall, 2),
        "ms_per_genome": round(1000 * wall / n, 2),
        "canonical_duplicate_rate": round(ledger.duplicate_rejects / n, 5),
        "behavior_duplicate_rate": round(behavior_dups / n, 5),
        "invalid_stop_genome_rate_any": round(invalid_any / n, 4),
        "invalid_stop_genome_rate_all_signals": round(invalid_all / n, 4),
        "invalid_stop_signal_share": round(raw_invalid / max(1, raw_invalid + raw_valid), 4),
        "positive_informational": pos,
        "by_lineage": by_lineage,
        "ledger": json.loads(ledger.to_json()) | {"seen": len(ledger.seen)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    report = run(args.n, args.seed, Path(args.config), Path(args.cache_dir))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"N={report['n']} pool={report['pool_size']} features; {report['wall_s']}s "
          f"= {report['ms_per_genome']} ms/genome")
    print(f"dup canonical {report['canonical_duplicate_rate']:.3%} behavior "
          f"{report['behavior_duplicate_rate']:.3%}; invalid-stop genomes "
          f"any {report['invalid_stop_genome_rate_any']:.1%} / all-signals "
          f"{report['invalid_stop_genome_rate_all_signals']:.1%}; invalid signal share "
          f"{report['invalid_stop_signal_share']:.1%}")
    print(f"positive (info) {report['positive_informational']}")
    print(f"{'lineage':24s}{'n':>5s}{'med_cand':>10s}{'>=30tr':>8s}")
    for name, r in report["by_lineage"].items():
        print(f"{name:24s}{r['count']:5d}{r['median_candidates']:10.0f}{r['frac_ge_30_train_trades']:8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
