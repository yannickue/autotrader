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
import shutil
import sys
import tempfile
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


NULL_KINDS = ("shuffle_drift", "shuffle_zerodrift", "sign_flip_day")
ZERODRIFT_SEEDS = tuple(range(20280001, 20280013))  # K = 12
SIGNFLIP_SEEDS = tuple(range(20290001, 20290005))  # 4
ZERODRIFT_ROOT = REPO_ROOT / "research/reports/alpha_discovery_v1/null_calibration_zerodrift"


def _rebuild(dev: pd.DataFrame, c0: float, new_ret: np.ndarray, new_offs: np.ndarray
             ) -> pd.DataFrame:
    new_c = _price_tick(c0 * np.exp(np.cumsum(new_ret)))
    out = dev.copy()
    out["close"] = new_c
    out["open"] = _price_tick(new_c + new_offs[:, 0])
    out["high"] = _price_tick(new_c + new_offs[:, 1])
    out["low"] = _price_tick(new_c + new_offs[:, 2])
    return out


def _bar_structure(dev: pd.DataFrame):
    ts = pd.DatetimeIndex(dev["ts"])
    n = len(dev)
    ts_ns = ts.asi8.astype(np.int64)
    unit_ns = 1000 if ts.dtype == "datetime64[us, UTC]" else 1
    gap_ok = np.zeros(n, dtype=bool)
    gap_ok[1:] = (np.diff(ts_ns) * unit_ns) == BAR_NS
    local = ts.tz_convert("Europe/Berlin")
    berlin_day = local.normalize().tz_localize(None).asi8
    same_day = np.zeros(n, dtype=bool)
    same_day[1:] = berlin_day[1:] == berlin_day[:-1]
    o, h, lo, c = (dev[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    ret = np.zeros(n)
    ret[1:] = np.log(c[1:] / c[:-1])
    offs = np.stack([o - c, h - c, lo - c], axis=1)
    return (gap_ok & same_day), berlin_day, np.asarray(local.hour), ret, offs, c


def _full_blocks(intraday: np.ndarray, block_len: int) -> list[np.ndarray]:
    n = len(intraday)
    blocks: list[np.ndarray] = []
    i = 0
    while i < n:
        if not intraday[i]:
            i += 1
            continue
        j = i
        while j < n and intraday[j]:
            j += 1
        for s in range(i, j, block_len):
            if min(s + block_len, j) - s == block_len:
                blocks.append(np.arange(s, min(s + block_len, j)))
        i = j
    return blocks


def _hour_constrained_perm(rng: np.random.Generator, seg: np.ndarray, hour: np.ndarray,
                           tol: int = 1) -> np.ndarray:
    """Permutation ``src[k]``: destination k receives item src[k] of the same segment whose
    Berlin hour is within +-tol of k's hour.  Greedy in random destination order; a destination
    without an admissible source takes the unused same-segment item with the closest hour."""
    m = len(seg)
    src = np.full(m, -1, dtype=np.int64)
    used = np.zeros(m, dtype=bool)
    for k in rng.permutation(m):
        ok = np.flatnonzero(~used & (seg == seg[k]) & (np.abs(hour - hour[k]) <= tol))
        if len(ok) == 0:
            ok = np.flatnonzero(~used & (seg == seg[k]))
            if len(ok) == 0:
                ok = np.flatnonzero(~used)
            gap = np.abs(hour[ok] - hour[k])
            ok = ok[gap == gap.min()]
        pick = ok[rng.integers(len(ok))]
        src[k] = pick
        used[pick] = True
    return src


def _zerodrift(dev: pd.DataFrame, seed: int, plan, block_len: int = BLOCK_LEN) -> pd.DataFrame:
    """Zero-drift, within-partition, within-time-of-day block shuffle.

    Every bar (except the first) belongs to a SEGMENT: Train (Berlin date < validation.start)
    or Validation-side (date >= validation.start, embargo days included).  The mean log return
    of each segment is subtracted first (exactly zero drift per segment).  Full 12-bar intraday
    blocks are then permuted only WITHIN their segment and only between blocks whose start
    Berlin hour differs by <= 1 h.  Anchor bars (day opens / gaps / holes) are permuted the
    same way among anchors of the same segment (+-1 h) with their OHLC shape offsets.
    """
    if plan is None:
        raise ValueError("shuffle_zerodrift requires the SplitPlan")
    intraday, _, hour, ret, offs, c = _bar_structure(dev)
    n = len(dev)
    day = (pd.DatetimeIndex(dev["ts"]).tz_convert("Europe/Berlin").normalize()
           .tz_localize(None).to_numpy().astype("datetime64[D]"))
    segment = (day >= np.datetime64(plan.validation.start)).astype(np.int64)
    ret = ret.copy()
    for sg in (0, 1):
        sel = (segment == sg) & (np.arange(n) > 0)
        if sel.any():
            ret[sel] -= ret[sel].mean()
    rng = np.random.default_rng(seed)
    new_ret, new_offs = ret.copy(), offs.copy()
    blocks = _full_blocks(intraday, block_len)
    if blocks:
        b_seg = np.array([segment[b[0]] for b in blocks])
        b_hour = np.array([hour[b[0]] for b in blocks])
        src = _hour_constrained_perm(rng, b_seg, b_hour)
        for dst, k in zip(blocks, src, strict=True):
            new_ret[dst] = ret[blocks[k]]
            new_offs[dst] = offs[blocks[k]]
    anchors = np.flatnonzero(~intraday & (np.arange(n) > 0))
    if len(anchors):
        src = _hour_constrained_perm(rng, segment[anchors], hour[anchors])
        new_ret[anchors] = ret[anchors[src]]
        new_offs[anchors] = offs[anchors[src]]
    return _rebuild(dev, c[0], new_ret, new_offs)


def _sign_flip_day(dev: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Per Berlin day multiply all log returns (incl. that day's opening-gap return) by a random
    +-1 sign; destroys drift / directional persistence across days, keeps intraday volatility
    structure.  Mirrored days swap the high/low offsets so OHLC stays consistent."""
    _, berlin_day, _, ret, offs, c = _bar_structure(dev)
    rng = np.random.default_rng(seed)
    _, inv = np.unique(berlin_day, return_inverse=True)
    sign = rng.choice(np.array([-1.0, 1.0]), size=int(inv.max()) + 1)[inv]
    new_ret = ret * sign
    new_offs = offs.copy()
    flip = sign < 0
    new_offs[flip, 0] = -offs[flip, 0]
    new_offs[flip, 1] = -offs[flip, 2]
    new_offs[flip, 2] = -offs[flip, 1]
    return _rebuild(dev, c[0], new_ret, new_offs)


def make_null_frame(dev: pd.DataFrame, seed: int, block_len: int = BLOCK_LEN,
                    kind: str = "shuffle_drift", plan=None) -> pd.DataFrame:
    """Synthetic null of ``dev``: shuffle_drift (module docstring, legacy default),
    shuffle_zerodrift or sign_flip_day."""
    if kind == "shuffle_zerodrift":
        return _zerodrift(dev, seed, plan, block_len)
    if kind == "sign_flip_day":
        return _sign_flip_day(dev, seed)
    if kind != "shuffle_drift":
        raise ValueError(f"unknown null kind {kind}")
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
def run_one(seed: int, out_root: Path, config: Path, kind: str = "shuffle_drift") -> dict:
    from alpha.common.dataset import load_research_dataset
    from research.runners import ad1_discovery, ad1_survivors, ar2_fast

    cfg = json.loads(config.read_text(encoding="utf-8"))
    plan = ar2_fast._plan(cfg)
    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    real_dev = ar2_fast.dev_frame(ds.frame, plan)
    null_dev = make_null_frame(real_dev, seed, kind=kind, plan=plan)
    out_dir = out_root / f"seed_{seed}"
    cache_dir = Path(tempfile.mkdtemp(prefix=f"ad1_null_{seed}_"))
    args = ad1_discovery.build_parser().parse_args([
        "--config", str(config), "--seed", str(seed), "--tag", f"null_{seed}",
        "--out-dir", str(out_dir), "--random-structures", "1200", "--optuna-structures", "80",
        "--optuna-trials", "40", "--deap-pop", "300", "--deap-gens", "25",
        "--max-unique-specs", "10000", "--pool-size", "800",
        "--prior-trials", str(PRIOR_TRIALS), "--prior-unique-specs", str(PRIOR_UNIQUE_SPECS),
        "--cache-dir", str(cache_dir)])
    t0 = time.perf_counter()
    try:
        disc = ad1_discovery.run(args, dev_override=null_dev)
        surv = ad1_survivors.run(out_dir / "candidate_pool.json", config, out_dir, cache_dir,
                                 dev_override=null_dev)
        raw = json.loads((out_dir / "survivors.json").read_text(encoding="utf-8"))
        rec = _digest(seed, disc, surv, raw, time.perf_counter() - t0)
        rec["null_kind"] = kind
        rec["finalists_dir"] = {f["canonical_hash"][:12]: f["genome"]["direction"]
                                for f in raw["finalists"]}
        return rec
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


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


REAL_POWERED = {
    "main2 (current gates)": dict(C=15, E=1, max_val_t=1.13, max_pooled_t=1.84),
    "main1-rerun (current gates)": dict(C=25, E=2, max_val_t=1.18, max_pooled_t=2.0),
}


def _q(x: list[float]) -> dict:
    a = np.asarray(x, dtype=float)
    if not len(a):
        return {}
    return {"mean": float(a.mean()), "sd": float(a.std(ddof=1)) if len(a) > 1 else 0.0,
            "min": float(a.min()), "q25": float(np.quantile(a, .25)),
            "median": float(np.median(a)), "q75": float(np.quantile(a, .75)),
            "q95": float(np.quantile(a, .95)), "max": float(a.max())}


def aggregate_powered(out_root: Path) -> dict:
    """Aggregate every seed digest under out_root, separately per null kind."""
    result: dict = {"real": REAL_POWERED}
    for kind in ("shuffle_zerodrift", "sign_flip_day"):
        recs = []
        for p in sorted(out_root.glob("seed_*/digest.json")):
            r = json.loads(p.read_text(encoding="utf-8"))
            if r.get("null_kind") == kind:
                recs.append(r)
        if not recs:
            continue
        per_seed, cs, es, mv, mp = [], [], [], [], []
        for r in recs:
            fin = r["finalists_passed_all"]
            vts = [f["val_t"] for f in fin if f["val_t"] is not None]
            pts = [f["pooled_t"] for f in fin if f["pooled_t"] is not None]
            dirs = r.get("finalists_dir", {})
            per_seed.append({
                "seed": r["seed"], "counts": r["counts"], "E_finalists": len(fin),
                "best_train_fitness": r["best_train_fitness"]["pool_max"],
                "finalist_val_t": vts, "finalist_pooled_t": pts,
                "long_short": {d: sum(1 for f in fin if dirs.get(f["hash"]) == d)
                               for d in ("LONG", "SHORT")}})
            cs.append(r["counts"]["C"])
            es.append(r["counts"]["E"])
            mv.append(max(vts) if vts else 0.0)  # 0.0 = no finalist (counted as no exceedance)
            mp.append(max(pts) if pts else 0.0)
        q95v, q95p = float(np.quantile(mv, .95)), float(np.quantile(mp, .95))
        result[kind] = {
            "n_seeds": len(recs), "per_seed": per_seed,
            "aggregate": {"C_survivors": _q(cs), "E_finalists": _q(es),
                          "max_finalist_val_t": _q(mv), "max_finalist_pooled_t": _q(mp),
                          "empirical_p95_max_val_t": q95v, "empirical_p95_max_pooled_t": q95p},
            "real_vs_null_p95": {
                k: {"max_val_t": v["max_val_t"], "exceeds_null_p95_val_t": v["max_val_t"] > q95v,
                    "max_pooled_t": v["max_pooled_t"],
                    "exceeds_null_p95_pooled_t": v["max_pooled_t"] > q95p}
                for k, v in REAL_POWERED.items()}}
    (out_root / "aggregate.json").write_text(json.dumps(result, indent=1, sort_keys=True),
                                             encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    from alpha.discovery.disk import assert_free_space, limit_workers_by_space

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--aggregate", action="store_true")
    p.add_argument("--null-kind", choices=NULL_KINDS, default="shuffle_drift")
    p.add_argument("--seeds", type=int, nargs="*", default=None,
                   help="explicit seed list (default: legacy 3 / zerodrift 12 / signflip 4)")
    p.add_argument("--out-root", default=None)
    p.add_argument("--config", default=str(REPO_ROOT / "research/configs/ad1_discovery.json"))
    a = p.parse_args(argv)
    out_root = Path(a.out_root) if a.out_root else (
        OUT_ROOT if a.null_kind == "shuffle_drift" else ZERODRIFT_ROOT)
    assert_free_space(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    if a.aggregate:
        agg = aggregate(out_root) if a.null_kind == "shuffle_drift" else aggregate_powered(out_root)
        print(json.dumps(agg, indent=1, sort_keys=True))
        return 0
    default = {"shuffle_drift": NULL_SEEDS, "shuffle_zerodrift": ZERODRIFT_SEEDS,
               "sign_flip_day": SIGNFLIP_SEEDS}[a.null_kind]
    seeds = a.seeds if a.seeds else ([a.seed] if a.seed is not None else list(default))
    limit_workers_by_space(1, out_root)
    for s in seeds:
        rec = run_one(s, out_root, Path(a.config), a.null_kind)
        (out_root / f"seed_{s}" / "digest.json").write_text(
            json.dumps(rec, indent=1, sort_keys=True, default=str), encoding="utf-8")
        print(f"[null {s}] {json.dumps(rec['counts'])} clusters={rec['clusters']} "
              f"deap_best={rec['best_train_fitness']['deap']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
