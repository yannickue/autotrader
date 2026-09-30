# ruff: noqa: E501
"""V2 FormulaAlpha probe (research only): GP-evolved formulas vs a random-formula baseline on GER40 TRAIN.

Pipeline (Train only; the search process never loads a bar of any later partition):
  dev frame (ar1 dataset, bars after the config ``dev_cut`` refused) -> FeatureStore -> FormulaData ->
  ``make_train_view`` (contiguous Train slice) -> DEAP GP (``max_evals`` unique formulas) and a random baseline
  of the same budget -> factor IC distributions, decorrelated counts, candidates/trades per day and Train
  light-screen expectancy_r of the top formulas (informational) -> label-shift null.

No Validation or OOS number is computed, logged or used for selection (the runner does not import the sealed
gate module).  Cumulative trial counts are carried in ``trial_ledger.json`` as in V1 (a re-run adds to them).
Output: ``research/reports/v2_formula/probe_summary.json`` (small), ``probe_screen_rows.json`` (per-formula
screen rows) and ``trial_ledger.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402

from alpha.common.dataset import POINT, load_research_dataset  # noqa: E402
from alpha.common.market_data import assert_no_forward_holdout  # noqa: E402
from alpha.fast.store import FeatureStore, _run_start  # noqa: E402
from alpha.formula import gp  # noqa: E402
from alpha.formula import signal as sg  # noqa: E402
from alpha.formula.data import build_formula_data  # noqa: E402
from alpha.formula.evaluate import load_ledger, save_ledger, train_screen  # noqa: E402
from alpha.formula.fitness import FactorScorer  # noqa: E402
from alpha.formula.prepare import make_train_view  # noqa: E402
from alpha.formula.tree import EvalContext  # noqa: E402
from research.runners import ar2_fast  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/v2_formula.json"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _git() -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
        return head + ("+dirty" if dirty else "")
    except Exception:
        return "UNKNOWN"


def _q(a, *qs: float) -> list[float | None]:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    return [round(float(x), 5) for x in np.quantile(a, qs)] if len(a) else [None] * len(qs)


def ic_distribution(res: gp.RunResult) -> dict:
    recs = [r for r in res.records.values() if r.score.valid]
    abs_ic = [abs(r.score.ic[r.score.best_h]) for r in recs]
    return {
        "n_scored_unique": len(res.records), "n_valid": len(recs),
        "invalid_reasons": _count([r.score.reason for r in res.records.values() if not r.score.valid]),
        "abs_ic_best_h_q05_q25_q50_q75_q95_max": _q(abs_ic, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0),
        "fitness_q05_q25_q50_q75_q95_max": _q([r.fitness for r in recs], 0.05, 0.25, 0.5, 0.75, 0.95, 1.0),
        "t_stat_abs_q50_q95_max": _q([abs(r.score.t_stat) for r in recs], 0.5, 0.95, 1.0),
        "complexity_median": float(np.median([r.cx for r in recs])) if recs else None,
        "best_h_counts": _count([r.score.best_h for r in recs]),
        "family_counts": _count([r.family for r in recs]),
        "n_clusters_abs_corr_ge_0.7": res.n_clusters,
        "n_niches_archive": len(res.archive),
        "hof_size": len(res.hof),
    }


def _count(xs) -> dict:
    out: dict[str, int] = {}
    for x in xs:
        out[str(x)] = out.get(str(x), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def screen_group(name: str, recs, view, ctx, cfg, ledger) -> dict:
    """Train light-screen of formulas at each q of the grid (param trials are recorded in the ledger)."""
    sc = cfg["signal"]
    rows = []
    for r in recs:
        f = ctx.evaluate(r.node)
        per_q = {}
        for q in sc["q_grid"]:
            ledger.record(r.node, "param")
            spec = sg.SignalSpec(sign=r.score.sign, q=q, stop_k=sc["stop_k"], target_r=sc["target_r"], cooldown=sc["cooldown"], session=sc["session"])
            res = train_screen(view, f, r.score, spec)
            per_q[str(q)] = None if res is None else {
                "cand_per_day": round(res.candidates_per_day, 3),
                "trades_per_day_adv": None if res.adverse.trades_per_day is None else round(res.adverse.trades_per_day, 3),
                "n_trades_adv": res.adverse.n_trades,
                "exp_r_base": None if res.base.expectancy_r is None else round(res.base.expectancy_r, 4),
                "exp_r_adv": None if res.adverse.expectancy_r is None else round(res.adverse.expectancy_r, 4),
                "pf_adv": None if res.adverse.profit_factor is None else round(res.adverse.profit_factor, 3),
            }
        rows.append({"hash": r.chash, "expr": r.node.key, "fitness": round(r.fitness, 5), "ic_best": round(r.score.ic[r.score.best_h], 5),
                     "best_h": r.score.best_h, "sign": r.score.sign, "t": round(r.score.t_stat, 2), "screen": per_q})

    def vals(key: str, q: str | None):
        out = []
        for row in rows:
            for k, v in row["screen"].items():
                if v is not None and v[key] is not None and (q is None or k == q):
                    out.append(v[key])
        return out

    dq = str(cfg["signal"]["default_q"])
    return {
        "group": name, "n_formulas": len(rows),
        "cand_per_day_q50_of_default_q": _q(vals("cand_per_day", dq), 0.5)[0],
        "trades_per_day_adv_q50_of_default_q": _q(vals("trades_per_day_adv", dq), 0.5)[0],
        "exp_r_adv_default_q_mean_median": [round(float(np.mean(vals("exp_r_adv", dq))), 4), _q(vals("exp_r_adv", dq), 0.5)[0]] if vals("exp_r_adv", dq) else None,
        "exp_r_base_default_q_mean_median": [round(float(np.mean(vals("exp_r_base", dq))), 4), _q(vals("exp_r_base", dq), 0.5)[0]] if vals("exp_r_base", dq) else None,
        "exp_r_adv_all_q_mean": round(float(np.mean(vals("exp_r_adv", None))), 4) if vals("exp_r_adv", None) else None,
        "share_exp_r_adv_gt0_default_q": round(float(np.mean([v > 0 for v in vals("exp_r_adv", dq)])), 3) if vals("exp_r_adv", dq) else None,
        "rows": rows,
    }


def run(args: argparse.Namespace) -> dict:
    t0 = time.perf_counter()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    seed = args.seed if args.seed is not None else cfg["seed"]
    sr = cfg["search"]
    max_evals = args.max_evals if args.max_evals is not None else sr["max_evals"]
    out_dir = Path(args.out_dir or REPO_ROOT / cfg["report_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir or REPO_ROOT / cfg["cache_dir"])
    plan = ar2_fast._plan(cfg)
    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    dev = ar2_fast.dev_frame(ds.frame, plan)  # bars after the plan's last development day are physically removed
    assert_no_forward_holdout(dev)  # and nothing after 2026-08-31 may exist
    features = FeatureStore.load_or_build(dev, {"point_size": POINT}, cache_dir)
    market, dates = ar2_fast._market(features), ar2_fast._dates(features)
    rs = _run_start(np.asarray(features["berlin_day_id"], dtype=np.int64), np.asarray(features["contig"], dtype=bool)).astype(np.int64)
    fd = build_formula_data(features["o"], features["h"], features["l"], features["c"], features["m5_atr14"], rs,
                            features["berlin_minute"], features)
    assert str(dates.max()) <= cfg["dev_cut"]
    view = make_train_view(fd, market, dates, plan)
    del fd, features, dev  # the search below only ever holds the Train slice
    log(f"dev bars {len(dates)}; Train view {len(view.data)} bars / {view.n_days} days; terminals {len(view.data.terminals)}")

    scorer = FactorScorer(view.data, tuple(sr["horizons"]), n_chunks=sr["n_chunks"], complexity_penalty=sr["complexity_penalty"])
    ctx = EvalContext(view.data, 192.0)
    pset = gp.build_pset(list(view.data.terminals))
    ledger_path = out_dir / "trial_ledger.json"
    ledger = load_ledger(ledger_path)
    trials_before = ledger.total_trials
    timing: dict[str, float] = {}

    t = time.perf_counter()
    g = gp.evolve(scorer, ctx, pset, seed, max_evals=max_evals, pop_size=sr["pop_size"], hof_size=sr["hof_size"],
                  novelty_w=sr["novelty_w"], elite_share=sr["elite_share"], ledger=ledger, label="gp")
    timing["gp_s"] = round(time.perf_counter() - t, 1)
    log(f"GP done {timing['gp_s']}s: {g.evals} unique formulas, {g.generations} generations, best {g.ranked()[0].fitness:.4f}")
    t = time.perf_counter()
    r = gp.random_baseline(scorer, ctx, pset, seed + 1, max_evals=max_evals, hof_size=sr["hof_size"], ledger=ledger, label="random")
    timing["random_s"] = round(time.perf_counter() - t, 1)
    log(f"random done {timing['random_s']}s: {r.evals} unique formulas, best {r.ranked()[0].fitness:.4f}")

    k = args.top if args.top is not None else cfg["report"]["top_k"]
    rng = np.random.default_rng(seed + 2)
    r_valid = r.ranked()
    unselected = [r_valid[i] for i in sorted(rng.choice(len(r_valid), size=min(k, len(r_valid)), replace=False))]
    t = time.perf_counter()
    groups = {
        "gp_top": screen_group("gp_top_by_train_fitness", g.ranked()[:k], view, ctx, cfg, ledger),
        "gp_decorrelated_hof": screen_group("gp_decorrelated_hof", g.hof[:k], view, ctx, cfg, ledger),
        "random_top": screen_group("random_top_by_train_fitness", r_valid[:k], view, ctx, cfg, ledger),
        "random_decorrelated_hof": screen_group("random_decorrelated_hof", r.hof[:k], view, ctx, cfg, ledger),
        "random_unselected": screen_group("random_unselected_sample", unselected, view, ctx, cfg, ledger),
    }
    timing["screen_s"] = round(time.perf_counter() - t, 1)
    log(f"light screens done {timing['screen_s']}s")

    null: dict = {"skipped": True}
    if not args.skip_null:
        t = time.perf_counter()
        ns, sample = cfg["null"]["n_shifts"], min(cfg["null"]["sample_formulas"], len(r_valid))
        pick = [r_valid[i].node for i in sorted(rng.choice(len(r_valid), size=sample, replace=False))]
        dist = gp.null_fitness_distribution(scorer, ctx, pick, ns, seed + 3)
        best_per_shift = np.nanmax(dist, axis=1)
        top_null = gp.null_fitness_distribution(scorer, ctx, [x.node for x in g.ranked()[:k]], ns, seed + 4)
        gbest, rbest = g.ranked()[0].fitness, r_valid[0].fitness
        null = {
            "method": "labels circularly shifted by whole days (autocorrelation kept, factor relation destroyed)",
            "n_shifts": ns, "sample_formulas": sample,
            "null_best_of_sample_fitness_per_shift": [round(float(x), 5) for x in best_per_shift],
            "null_best_of_sample_mean_max": [round(float(best_per_shift.mean()), 5), round(float(best_per_shift.max()), 5)],
            "gp_best_fitness": round(gbest, 5), "random_best_fitness": round(rbest, 5),
            "note": "null best is over `sample_formulas` random formulas, the real bests are over the full budget: the null is optimistic for the real bests (conservative).",
            "gp_top_null_fitness_mean_per_formula": [round(float(x), 5) for x in np.nanmean(top_null, axis=0)],
            "gp_top_null_fitness_max_per_formula": [round(float(x), 5) for x in np.nanmax(top_null, axis=0)],
            "gp_top_real_fitness": [round(x.fitness, 5) for x in g.ranked()[:k]],
        }
        timing["null_s"] = round(time.perf_counter() - t, 1)
        log(f"null done {timing['null_s']}s")

    save_ledger(ledger, ledger_path)
    summary = {
        "version": "V2F-probe-1", "market": cfg["market"], "git": _git(),
        "config_sha": hashlib.sha256(Path(args.config).read_bytes()).hexdigest()[:16], "seed": seed,
        "data": {"dev_bars": len(dates), "last_dev_date": str(dates.max()), "dev_cut": cfg["dev_cut"], "train_bars": len(view.data),
                 "train_days": int(view.n_days), "train_first": str(view.dates.min()), "train_last": str(view.dates.max()),
                 "n_terminals": len(view.data.terminals), "terminals": list(view.data.terminals)},
        "validation_and_oos_numbers": "not computed, not logged, not used (search process holds the Train slice only)",
        "search": {**sr, "max_evals": max_evals},
        "gp": {**ic_distribution(g), "generations": g.generations, "gen_best_first_last": [round(g.gen_best[0], 5), round(g.gen_best[-1], 5)] if g.gen_best else None,
               "gen_best_series": [round(x, 4) for x in g.gen_best], "gen_median_series": [round(x, 4) for x in g.gen_median]},
        "random": ic_distribution(r),
        "decorrelated_counts_top100_abs_corr_lt_0.7": {"gp": gp.decorrelated_count(list(g.records.values()), top=cfg["report"]["decorrelated_top"]),
                                                        "random": gp.decorrelated_count(list(r.records.values()), top=cfg["report"]["decorrelated_top"])},
        "top_formulas_gp": [x.to_dict() for x in g.ranked()[:k]],
        "screens_train_only": {name: {kk: vv for kk, vv in grp.items() if kk != "rows"} for name, grp in groups.items()},
        "null": null,
        "ledger": {"total_trials_cumulative": ledger.total_trials, "this_run": ledger.total_trials - trials_before, "structural": ledger.structural_trials,
                   "param": ledger.param_trials, "unique_canonical": ledger.unique, "duplicate_rejects": ledger.duplicate_rejects,
                   "invalid_rejects": ledger.invalid_rejects,
                   "untracked_exploratory_trials": cfg.get("untracked_exploratory_trials")},
        "eval_cache": ctx.stats(),
        "timing_s": {**timing, "total": round(time.perf_counter() - t0, 1)},
    }
    (out_dir / "probe_screen_rows.json").write_text(
        json.dumps({name: grp["rows"] for name, grp in groups.items()}, indent=0, allow_nan=False), encoding="utf-8")
    out = out_dir / "probe_summary.json"
    out.write_text(json.dumps(summary, indent=1, sort_keys=False, allow_nan=False), encoding="utf-8")
    log(f"wrote {out} ({out.stat().st_size} bytes)")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--max-evals", type=int, default=None)
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--skip-null", action="store_true")
    run(ap.parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
