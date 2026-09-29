"""AD1 NULL CALIBRATION: run the full discovery pipeline on synthetic data without structure.

Research only.  Purpose: measure how many "finalists" the automated pipeline (search on Train-only
fitness -> Stage C validation gate -> D cost stress -> E stability -> overlap clusters ->
selection-aware statistics) produces when the data has no exploitable serial structure, so real
campaign results can be compared with the pipeline's false-positive rate.

NULL CONSTRUCTION (block-shuffled returns, session structure preserved)
  1. Take the real dev frame (OOS bars already physically removed).
  2. "Anchor" bars = the first bar of the frame, every bar whose gap to the previous bar is not
     exactly 5 minutes (overnight / weekend / holiday / data-hole gaps) and every bar that opens a
     new Berlin calendar day.  Anchor bars keep their own log return (the gap return) and their own
     OHLC shape at their original position -> day gaps, session opens and data holes are preserved.
  3. The remaining ("intraday") bars form contiguous runs between anchors.  Each run is cut into
     blocks of ``block_len`` (12 = 1 hour) consecutive bars; a run remainder shorter than 12 is a
     short block and stays in place.  Blocks never straddle a day boundary or a data gap.
  4. All FULL blocks (identical length, hence exchangeable) are permuted across the WHOLE frame with
     a numpy Generator seeded per null seed; each block carries its bars' log returns AND their
     shape offsets (o-c, h-c, l-c, in points).  Serial dependence beyond the block length,
     time-of-day / day-of-week structure of returns and any trend/mean-reversion across blocks are
     destroyed; within-block autocorrelation, volatility clustering inside a block and the marginal
     return / shape distribution are kept.
  5. Closes are rebuilt as c_0 * exp(cumsum(log returns)) (so the path is a bridge with the same
     start and end price), rounded to the price tick (0.01); o/h/l = new close + the carried
     offset.  timestamps, spread_pts and tick_volume stay at their ORIGINAL positions (same
     session structure, same spreads, same length) so FeatureStore and everything downstream run
     unchanged.  Offsets are additive in price points, so high >= max(o, c) and low <= min(o, c)
     hold by construction (rounding is monotone).
Caveat: time-of-day volatility seasonality is not preserved (blocks move across the day), so the
null has slightly different intraday variance placement than the real data; it is a "no
exploitable serial structure" null, not a full replica of the volatility profile.
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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

BLOCK_LEN = 12
BAR_NS = 300 * 10**9
NULL_SEEDS = (20270001, 20270002, 20270003)
PRIOR_TRIALS = 20199
PRIOR_UNIQUE_SPECS = 19871  # main1+main2 unique specs (same cumulative N as the real bound)
OUT_ROOT = REPO_ROOT / "research/reports/alpha_discovery_v1/null_calibration"
REAL = {  # from the real campaigns' survivors.json (main1 gates were older/looser)
    "main1 (old gates, as reported)": dict(both_pos=294, C=234, D=234, E=28, clusters=21,
                                           best_deap_train=0.77),
    "main2": dict(both_pos=314, C=15, D=15, E=1, clusters=1, best_deap_train=0.48,
                  val_t=1.13, pooled_t=1.84),
}


def _price_tick(x: np.ndarray) -> np.ndarray:
    return np.round(x, 2)  # tick = 0.01


def make_null_frame(dev: pd.DataFrame, seed: int, block_len: int = BLOCK_LEN) -> pd.DataFrame:
    """Block-shuffled synthetic null of ``dev`` (see module docstring)."""
    n = len(dev)
    ts = pd.DatetimeIndex(dev["ts"])
    ts_ns = ts.asi8.astype(np.int64)
    # asi8 unit may be us; normalise to ns for the gap test
    unit_ns = 1000 if ts.dtype == "datetime64[us, UTC]" else 1
    gap_ok = np.zeros(n, dtype=bool)
    gap_ok[1:] = (np.diff(ts_ns) * unit_ns) == BAR_NS
    berlin_day = ts.tz_convert("Europe/Berlin").normalize().tz_localize(None).asi8
    same_day = np.zeros(n, dtype=bool)
    same_day[1:] = berlin_day[1:] == berlin_day[:-1]
    intraday = gap_ok & same_day  # bar continues the previous bar's run
    o, h, lo, c = (dev[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    ret = np.zeros(n)
    ret[1:] = np.log(c[1:] / c[:-1])
    offs = np.stack([o - c, h - c, lo - c], axis=1)

    # blocks: consecutive intraday bars, cut every ``block_len`` bars inside a run
    blocks_full: list[np.ndarray] = []
    i = 0
    while i < n:
        if not intraday[i]:
            i += 1
            continue
        j = i
        while j < n and intraday[j]:
            j += 1
        for s in range(i, j, block_len):
            e = min(s + block_len, j)
            if e - s == block_len:
                blocks_full.append(np.arange(s, e))
        i = j
    if blocks_full:
        rng = np.random.default_rng(seed)
        perm = rng.permutation(len(blocks_full))
        new_ret, new_offs = ret.copy(), offs.copy()
        for dst, src in zip(blocks_full, (blocks_full[k] for k in perm), strict=True):
            new_ret[dst] = ret[src]
            new_offs[dst] = offs[src]
    else:
        new_ret, new_offs = ret, offs
    new_c = _price_tick(c[0] * np.exp(np.cumsum(new_ret)))
    out = dev.copy()
    out["close"] = new_c
    out["open"] = _price_tick(new_c + new_offs[:, 0])
    out["high"] = _price_tick(new_c + new_offs[:, 1])
    out["low"] = _price_tick(new_c + new_offs[:, 2])
    return out


# --------------------------------------------------------------------------- one null run
def run_one(seed: int, out_root: Path, config: Path) -> dict:
    from alpha.common.dataset import load_research_dataset
    from research.runners import ad1_discovery, ad1_survivors, ar2_fast

    cfg = json.loads(config.read_text(encoding="utf-8"))
    plan = ar2_fast._plan(cfg)
    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    real_dev = ar2_fast.dev_frame(ds.frame, plan)
    null_dev = make_null_frame(real_dev, seed)
    out_dir = out_root / f"seed_{seed}"
    cache_dir = REPO_ROOT / f"data/feature_store/ad1_null_{seed}"
    args = ad1_discovery.build_parser().parse_args([
        "--config", str(config), "--seed", str(seed), "--tag", f"null_{seed}",
        "--out-dir", str(out_dir), "--random-structures", "1200", "--optuna-structures", "80",
        "--optuna-trials", "40", "--deap-pop", "300", "--deap-gens", "25",
        "--max-unique-specs", "10000", "--pool-size", "800",
        "--prior-trials", str(PRIOR_TRIALS), "--prior-unique-specs", str(PRIOR_UNIQUE_SPECS),
        "--cache-dir", str(cache_dir)])
    t0 = time.perf_counter()
    disc = ad1_discovery.run(args, dev_override=null_dev)
    surv = ad1_survivors.run(out_dir / "candidate_pool.json", config, out_dir, cache_dir,
                             dev_override=null_dev)
    raw = json.loads((out_dir / "survivors.json").read_text(encoding="utf-8"))
    return _digest(seed, disc, surv, raw, time.perf_counter() - t0)


def _digest(seed: int, disc: dict, surv: dict, raw: dict, wall: float) -> dict:
    fins = raw["finalists"]
    passed = [f for f in fins if f.get("passed_all")]
    counts = surv["counts"]
    rec = {
        "seed": seed, "wall_s": round(wall, 1), "counts": counts,
        "clusters": len(surv["clusters"]), "verdict": surv["verdict"],
        "best_train_fitness": {
            "deap": disc["phases"]["deap"]["best_train_fitness"],
            "optuna": disc["phases"]["optuna"]["best_train_fitness"],
            "random": disc["phases"]["random"]["best_train_fitness"],
            "pool_max": (disc["pool"]["train_fitness"] or {}).get("max")},
        "finalists_passed_all": [
            {"hash": f["canonical_hash"][:12],
             "val_t": (f["stage_c"] or {}).get("t_adverse"),
             "pooled_t": (f["selection"] or {}).get("pooled_t"),
             "null_bound": (f["selection"] or {}).get("null_bound_t"),
             "exceeds_null": (f["selection"] or {}).get("exceeds_null")} for f in passed],
        "near_misses": [
            {"hash": f["canonical_hash"][:12], "stage": f["furthest_stage"],
             "val_t": (f["stage_c"] or {}).get("t_adverse"),
             "pooled_t": (f["selection"] or {}).get("pooled_t")}
            for f in fins if not f.get("passed_all")],
    }
    return rec


def aggregate(out_root: Path) -> dict:
    recs = []
    for seed in NULL_SEEDS:
        p = out_root / f"seed_{seed}" / "digest.json"
        if p.exists():
            recs.append(json.loads(p.read_text(encoding="utf-8")))
    keys = ["pool", "train_and_validation_positive", "validation_positive", "train_positive",
            "C", "D", "E"]
    agg = {"seeds": [r["seed"] for r in recs], "per_seed": recs,
           "mean_counts": {k: float(np.mean([r["counts"][k] for r in recs])) for k in keys}
           if recs else {},
           "mean_clusters": float(np.mean([r["clusters"] for r in recs])) if recs else None,
           "real": REAL}
    (out_root / "aggregate.json").write_text(json.dumps(agg, indent=1, sort_keys=True),
                                             encoding="utf-8")
    return agg


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--aggregate", action="store_true")
    p.add_argument("--out-root", default=str(OUT_ROOT))
    p.add_argument("--config", default=str(REPO_ROOT / "research/configs/ad1_discovery.json"))
    a = p.parse_args(argv)
    out_root = Path(a.out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    if a.aggregate:
        print(json.dumps(aggregate(out_root), indent=1, sort_keys=True))
        return 0
    seeds = [a.seed] if a.seed is not None else list(NULL_SEEDS)
    for s in seeds:
        rec = run_one(s, out_root, Path(a.config))
        (out_root / f"seed_{s}" / "digest.json").write_text(
            json.dumps(rec, indent=1, sort_keys=True, default=str), encoding="utf-8")
        print(f"[null {s}] {json.dumps(rec['counts'])} clusters={rec['clusters']} "
              f"deap_best={rec['best_train_fitness']['deap']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
