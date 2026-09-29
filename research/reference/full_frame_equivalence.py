"""Full Train+Validation frame equivalence gate: fast kernels vs the semantic strategies.

    <PY> research/reference/full_frame_equivalence.py [--workers N]

Generates candidates for every registered variant with the SLOW semantic strategy classes
(alpha.strategies.*, via the old runner's parallel Lab) and with the FAST numba kernels
(alpha.fast.kernels.* on the FeatureStore) on the complete dev frame (OOS bars physically absent)
and requires field-by-field, bit-exact equality. This covers DST days, holidays, month
boundaries and frame edges that the 12k golden slice cannot exercise. Prints EXIT=0 on success.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT / "src"), str(ROOT), str(ROOT / "research" / "runners")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

FIELDS = ("decision_idx", "direction", "stop", "target", "target_r", "exit_kind")


def main() -> int:
    import ar2_compare

    from alpha.common.dataset import POINT, load_research_dataset
    from alpha.common.frame import Frame
    from alpha.common.protocol import Partition, SplitPlan
    from alpha.fast.registry import discover
    from alpha.fast.sim import CandidateArrays
    from alpha.fast.store import FeatureStore

    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 6
    cfg = json.loads((ROOT / "research/configs/ar2_phase2.json").read_text(encoding="utf-8"))
    plan = SplitPlan(**{k: Partition(k, *v) for k, v in cfg["splits"].items()})
    dataset = load_research_dataset(ROOT / cfg["dataset_root"])
    df = ar2_compare.dev_frame(dataset.frame, plan)
    print(f"[equiv] dev frame: {len(df)} bars", flush=True)

    started = time.time()
    lab = ar2_compare.Lab(cfg, df, plan)
    variants = ar2_compare.discover_variants()
    slow = lab.candidates_many(variants, workers)
    print(f"[equiv] slow candidates: {time.time() - started:.1f}s", flush=True)

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="ffe") as cache:
        features = FeatureStore.load_or_build(df, {"point_size": POINT}, Path(cache))
    families = discover()
    frame = Frame.from_dataframe(df)
    print(f"[equiv] features: {time.time() - started:.1f}s", flush=True)

    failures = []
    total = 0
    for variant in variants:
        want = CandidateArrays.from_signal_candidates(frame, slow[variant["id"]])
        got = families[variant["strategy_id"]].generate(features, variant["params"])
        ok = all(np.array_equal(getattr(got, f), getattr(want, f), equal_nan=True) for f in FIELDS)
        total += len(want.decision_idx)
        print(
            f"[equiv] {variant['id']:38s} n={len(want.decision_idx):5d} "
            f"{'IDENTICAL' if ok else 'MISMATCH'}",
            flush=True,
        )
        if not ok:
            failures.append(variant["id"])
    print(f"[equiv] {len(variants)} variants, {total} candidates, mismatches: {failures or 'none'}")
    print(f"EXIT={1 if failures else 0}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
