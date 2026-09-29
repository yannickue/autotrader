"""AD1 catalog degeneracy audit: fraction of TRAIN bars on which each catalog clause is true.

Research only; Train partition only (OOS bars are removed by ``dev_frame`` and the mask is the
Train mask).  Prints the table of features with clauses outside [0.5 %, 95 %] selectivity.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from alpha.common.dataset import POINT, load_research_dataset  # noqa: E402
from alpha.discovery import audit  # noqa: E402
from alpha.fast.store import FeatureStore  # noqa: E402
from research.runners import ar2_fast  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/ad1_discovery.json"
DEFAULT_CACHE = REPO_ROOT / "data/feature_store/ad1_bench_gram"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--out", default=None, help="optional JSON output of all rows")
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    plan = ar2_fast._plan(cfg)
    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    dev = ar2_fast.dev_frame(ds.frame, plan)
    features = FeatureStore.load_or_build(dev, {"point_size": POINT}, Path(args.cache_dir))
    dates = ar2_fast._dates(features)
    rows = audit.degeneracy_rows(features, plan.mask(dates, plan.train))
    flags = audit.flagged(rows)
    print(f"{len(rows)} clauses audited; {len(flags)} features flagged")
    print(audit.render_table(flags))
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 1 if any(d["n_always"] for d in flags.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
