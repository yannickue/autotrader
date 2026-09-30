"""One-process old/new evaluator timing harness used for discovery speed proofs."""

from __future__ import annotations

import argparse
import gc
import importlib
import json
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path


def _purge() -> None:
    for name in tuple(sys.modules):
        if name == "alpha" or name.startswith("alpha.") or name == "research" or name.startswith(
            "research."
        ):
            del sys.modules[name]
    importlib.invalidate_caches()
    gc.collect()


def _run(root: Path, dataset_root: Path, count: int, seed: int, lean: bool) -> dict:
    _purge()
    sys.path[:0] = [str(root / "src"), str(root)]
    try:
        import numpy as np

        from alpha.common.dataset import POINT, load_research_dataset
        from alpha.discovery.archetypes import random_genome
        from alpha.discovery.catalog import FeaturePool
        from alpha.discovery.compile import TrialLedger, canonical_hash
        from alpha.discovery.evaluate import GenomeEvaluator
        from alpha.fast.store import FeatureStore
        from research.runners import ar2_fast

        cfg = json.loads((root / "research/configs/ad1_discovery.json").read_text())
        plan = ar2_fast._plan(cfg)
        dev = ar2_fast.dev_frame(load_research_dataset(dataset_root).frame, plan)
        with tempfile.TemporaryDirectory(prefix="ad1-ab-") as td:
            features = FeatureStore.load_or_build(dev, {"point_size": POINT}, Path(td) / "features")
            evaluator = GenomeEvaluator(
                features, ar2_fast._market(features), ar2_fast._dates(features), plan, cfg,
                Path(td), TrialLedger(),
            )
            pool = FeaturePool.from_features(features)
            rng = np.random.default_rng(seed)
            genomes, seen = [], set()
            while len(genomes) < count:
                genome = random_genome(rng, pool)
                digest = canonical_hash(genome)
                if digest not in seen:
                    seen.add(digest)
                    genomes.append(genome)
            tracemalloc.start()
            times = []
            for _ in range(2):
                start = time.perf_counter()
                for genome in genomes:
                    if lean:
                        evaluator.evaluate(genome, need_base=False)
                    else:
                        evaluator.evaluate(genome)
                times.append(time.perf_counter() - start)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return {"cold_s": times[0], "warm_s": times[1], "peak_mb": peak / 1024**2,
                    "unique": len(genomes)}
    finally:
        del sys.path[:2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-root", type=Path, required=True)
    parser.add_argument("--new-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--count", type=int, default=800)
    parser.add_argument("--seed", type=int, default=20260930)
    args = parser.parse_args(argv)
    old = _run(args.old_root, args.dataset_root, args.count, args.seed, False)
    new = _run(args.new_root, args.dataset_root, args.count, args.seed, True)
    print(json.dumps({"old": old, "new": new,
                      "cold_speedup": old["cold_s"] / new["cold_s"],
                      "warm_speedup": old["warm_s"] / new["warm_s"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
