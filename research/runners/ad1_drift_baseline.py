"""AD1 drift / random-entry baseline runner (research only; Train + Validation, never OOS).

Reads finalist genomes from one or more ``survivors.json`` (``--survivors``, repeatable) or from
a candidate pool (``--pool`` + ``--hashes`` prefixes) and writes, keyed by canonical hash::

    {hash: {"train": {cand_E, base_mean, base_sd, excess, p, ...},
            "validation": {...}, "pooled": {...},
            "drift_excess_p": <Validation p>, "drift_ref": {...}, "direction": ...}}

which the selection stage can consume through a ``--drift-baseline-json`` option.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from alpha.common.dataset import POINT, load_research_dataset  # noqa: E402
from alpha.discovery.baseline import DEFAULT_DRAWS, BaselineContext, drift_baseline  # noqa: E402
from alpha.discovery.compile import TrialLedger  # noqa: E402
from alpha.discovery.evaluate import GenomeEvaluator  # noqa: E402
from alpha.discovery.genome import Genome  # noqa: E402
from alpha.fast.store import FeatureStore  # noqa: E402
from research.runners import ar2_fast  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/ad1_discovery.json"


def load_genomes(survivors: list[str], pool: str | None, hashes: list[str]) -> dict[str, Genome]:
    """{hash-prefix-or-full: Genome}; later sources do not overwrite earlier ones."""
    out: dict[str, Genome] = {}
    for path in survivors:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for f in raw["finalists"]:
            out.setdefault(f["canonical_hash"], Genome.from_dict(f["genome"]))
    if pool:
        raw = json.loads(Path(pool).read_text(encoding="utf-8"))
        for c in raw["candidates"]:
            if any(c["canonical_hash"].startswith(h) for h in hashes):
                out.setdefault(c["canonical_hash"], Genome.from_dict(c["genome"]))
    return out


def build_context(config: Path, cache_dir: Path, dev_override=None,
                  min_train_trades: int | None = None) -> BaselineContext:
    cfg = json.loads(config.read_text(encoding="utf-8"))
    plan = ar2_fast._plan(cfg)
    if dev_override is not None:
        dev = dev_override
    else:
        ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
        dev = ar2_fast.dev_frame(ds.frame, plan)  # OOS bars physically removed
    features = FeatureStore.load_or_build(dev, {"point_size": POINT}, cache_dir)
    ev = GenomeEvaluator(features, ar2_fast._market(features), ar2_fast._dates(features), plan,
                         cfg, cache_dir, TrialLedger(), min_train_trades=min_train_trades)
    return BaselineContext(ev)


def run(genomes: dict[str, Genome], ctx: BaselineContext, draws: int, seed: int) -> dict:
    out = {}
    for src_hash, g in genomes.items():
        rec = drift_baseline(ctx, g, draws, seed)
        rec["source_hash"] = src_hash  # differs from canonical_hash for pre-fix campaigns
        out[rec["canonical_hash"]] = rec
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--survivors", action="append", default=[])
    p.add_argument("--pool", default=None)
    p.add_argument("--hashes", nargs="*", default=[])
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--draws", type=int, default=DEFAULT_DRAWS)
    p.add_argument("--seed", type=int, default=20260930)
    p.add_argument("--min-train-trades", type=int, default=None)
    a = p.parse_args(argv)
    genomes = load_genomes(a.survivors, a.pool, a.hashes)
    if not genomes:
        raise SystemExit("no genomes selected")
    ctx = build_context(Path(a.config), Path(a.cache_dir), None, a.min_train_trades)
    t0 = time.perf_counter()
    res = run(genomes, ctx, a.draws, a.seed)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1, sort_keys=True, allow_nan=False),
                           encoding="utf-8")
    for h, r in res.items():
        t, v, pl = r["train"], r["validation"], r["pooled"]
        print(f"{h[:12]} {r['direction']:5s} train E={t['cand_E']} ex={t['excess']} p={t['p']} | "
              f"val E={v['cand_E']} ex={v['excess']} p={v['p']} | pooled ex={pl['excess']} "
              f"p={pl['p']}", flush=True)
    print(f"done {len(res)} genomes in {time.perf_counter() - t0:.0f}s -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
