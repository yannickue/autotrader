# ruff: noqa: E501
"""V2 META-LABEL + CONFLUENCE runner (research only), market-parametric.

For one market it takes the probe candidate pool (top-N genomes of ``candidate_pool.json`` + extra passers
regenerated with the probe's random_genome machinery), builds trade-level records on the TRAIN side of
search fold 0 ONLY (COMBINED_ADVERSE), trains own NumPy models (logistic/ridge/GBDT) with purged rolling-origin
CV inside that Train side, evaluates them out-of-fold against two honest nulls, and analyses confluence
(agreement of independent strategy clusters) with a standard reference outcome.

SEALING: only the Train side of fold 0 is ever used.  No fold TEST mask, no sealed "validation" evaluator
view and nothing after 2026-08-31 is read (see ``alpha.metalabel.dataset``).

    python research/runners/v2_metalabel.py --markets GER40
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402

from alpha.discovery.temporal_archetypes import available_archetypes, random_genome  # noqa: E402
from alpha.discovery.temporal_evaluate import TemporalTrialLedger  # noqa: E402
from alpha.discovery.temporal_genome import (  # noqa: E402
    TemporalGenome,
    canonicalize,
)
from alpha.discovery.temporal_search import lineage_family, search_windows  # noqa: E402
from alpha.metalabel import confluence as cf  # noqa: E402
from alpha.metalabel import cv  # noqa: E402
from alpha.metalabel import dataset as ds  # noqa: E402
from alpha.metalabel import evaluate as ev  # noqa: E402
from alpha.metalabel import features as F  # noqa: E402
from research.runners import v2_probe as vp  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/v2_metalabel.json"
SUMMARY_VERSION = "v2-metalabel-summary-v1"
ADVERSE = vp.ADVERSE


def log(msg: str) -> None:
    print(msg, flush=True)


def _clean(o: Any, nd: int = 5) -> Any:
    if isinstance(o, dict):
        return {str(k): _clean(v, nd) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v, nd) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else round(float(o), nd)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


# --------------------------------------------------------------------------- pool
def load_pool(probe_dir: Path, market: str, top: int) -> list[tuple[str, TemporalGenome]]:
    raw = json.loads((probe_dir / market / "candidate_pool.json").read_text(encoding="utf-8"))
    return [(c["canonical_hash"], TemporalGenome.from_dict(c["genome"])) for c in raw["candidates"][:top]]


def build_pool(evaluator: vp.ProbeEvaluator, ctx: vp.ProbeContext, cfg: dict, probe_dir: Path, market: str
               ) -> tuple[list[tuple[str, str, str, TemporalGenome]], dict]:
    pc = cfg["pool"]
    entries: list[tuple[str, str, str, TemporalGenome]] = []
    seen: set[str] = set()
    stats: dict[str, Any] = {"top_requested": pc["top_from_probe"], "top_loaded": 0, "top_rejected_on_reeval": 0}
    for h, g in load_pool(probe_dir, market, pc["top_from_probe"]):
        stats["top_loaded"] += 1
        res = evaluator.evaluate(g, kind="structural", need_base=False)
        if res.rejected:
            stats["top_rejected_on_reeval"] += 1
            continue
        if res.genome_hash != h:
            raise RuntimeError("canonical hash of a probe pool genome changed: pool file is stale")
        seen.add(h)
        entries.append((h, lineage_family(g.lineage), "pool_top", g))
    rng = np.random.default_rng(cfg["seed"] + pc["extra_seed_offset"])
    names = available_archetypes(ctx.pool)
    windows = search_windows(ctx.window)
    attempts = i = 0
    n_extra = 0
    while n_extra < pc["extra_specs"] and attempts < pc["extra_max_attempts"]:
        g = random_genome(rng, ctx.pool, archetype=names[i % len(names)], windows=windows)
        i += 1
        attempts += 1
        res = evaluator.evaluate(g, kind="structural", need_base=False)
        if res.rejected or res.genome_hash in seen:
            continue
        seen.add(res.genome_hash)
        entries.append((res.genome_hash, lineage_family(g.lineage), "extra", canonicalize(g)))
        n_extra += 1
    stats.update(extra_requested=pc["extra_specs"], extra_added=n_extra, extra_attempts=attempts,
                 structure_evaluations=evaluator.ledger.total_trials, archetypes=list(names))
    return entries, stats


# --------------------------------------------------------------------------- models
def run_models(table: ds.TradeTable, cfg: dict, folds: list[cv.DayFold], seed: int, with_null: bool,
               null_override: int | None = None, keep_importance: bool = True) -> tuple[dict, dict]:
    out = table.outcomes()
    x = table.x
    results: dict[str, dict] = {}
    verdicts: dict[str, dict] = {}
    n_verdict = sum(1 for m in cfg["models"] if m["verdict"])
    for mc in cfg["models"]:
        spec = ev.ModelSpec(mc["name"], mc["kind"], mc["label"], mc.get("params", {}))
        t0 = time.perf_counter()
        res = ev.oof_predict(x, out, folds, table.day, table.exit_day, spec, seed=seed, keep_models=keep_importance)
        row = ev.summarize_oof(res, out, spec, table.day, seed=seed, n_boot=cfg["boot"])
        if keep_importance and mc["kind"] in ("logit", "gbdt") and mc["label"] == "y1":
            row["permutation_importance_oof"] = ev.permutation_importance(x, out, folds, res, table.names, seed=seed)
        row["fit_s"] = round(time.perf_counter() - t0, 2)
        log(f"[{mc['name']}] OOF AUC={row['auc_y1']:.4f} CI={row['auc_y1_ci95']} top-decile diff={row['top_decile']['diff']} "
            f"t={row['top_decile']['t']} ({row['fit_s']}s)")
        if with_null and mc["verdict"]:
            draws = null_override or (cfg["null"]["draws_gbdt"] if mc["kind"] == "gbdt" else cfg["null"]["draws"])
            nulls = {}
            for kind, blk in (("perm", cfg["null"]["block_days_perm"]), ("shift", cfg["null"]["block_days_shift"])):
                t1 = time.perf_counter()
                nd = ev.null_distribution(x, out, folds, table.day, table.exit_day, spec, kind, draws, seed + 7, blk)
                a_obs, t_obs = ev.oof_stats(res, out, spec)
                nulls[kind] = {
                    "draws": draws, "block_days": blk,
                    "auc": {"obs": a_obs, "null_mean": float(np.nanmean(nd["auc"])), "null_sd": float(np.nanstd(nd["auc"])),
                            "null_q95": float(np.nanquantile(nd["auc"], 0.95)), "p": ev.p_value(a_obs, nd["auc"])},
                    "top_decile_mean_r": {"obs": t_obs, "null_mean": float(np.nanmean(nd["top_r"])), "null_sd": float(np.nanstd(nd["top_r"])),
                                          "null_q95": float(np.nanquantile(nd["top_r"], 0.95)), "p": ev.p_value(t_obs, nd["top_r"])},
                }
                log(f"  null[{kind}] {mc['name']}: p_auc={nulls[kind]['auc']['p']:.4f} p_topR={nulls[kind]['top_decile_mean_r']['p']:.4f} ({time.perf_counter() - t1:.0f}s)")
            row["nulls"] = nulls
            verdicts[mc["name"]] = ev.verdict(
                row, {"auc": nulls["perm"]["auc"]["p"], "top_r": nulls["perm"]["top_decile_mean_r"]["p"]},
                {"auc": nulls["shift"]["auc"]["p"], "top_r": nulls["shift"]["top_decile_mean_r"]["p"]}, n_verdict)
            row["verdict"] = verdicts[mc["name"]]
        row.pop("models", None)
        results[mc["name"]] = row
    return results, verdicts


# --------------------------------------------------------------------------- run one market
def run_market(ctx: vp.ProbeContext, cfg: dict, probe_cfg: dict, out_dir: Path, args: argparse.Namespace | None = None) -> dict:
    t_all = time.perf_counter()
    timings: dict[str, float] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    market = ctx.market_name
    probe_dir = REPO_ROOT / cfg["probe_dir"]
    if args is not None and getattr(args, "n_extra", None) is not None:
        cfg["pool"]["extra_specs"] = args.n_extra
    if args is not None and getattr(args, "null_draws", None) is not None:
        cfg["null"]["draws"] = cfg["null"]["draws_gbdt"] = args.null_draws
    if args is not None and getattr(args, "confluence_null", None) is not None:
        cfg["confluence"]["n_null"] = args.confluence_null
    tm = ctx.plan.mask(ctx.dates, ctx.plan.train)
    n_cut = ds.train_cut(ctx.dates, tm)
    evaluator = vp.ProbeEvaluator(ctx.frame_provider, ctx.market, ctx.dates, ctx.plan, sizing=ctx.sizing, rules=ctx.rules,
                                  cost_scenarios=ctx.costs, min_train_trades=probe_cfg["min_train_trades"],
                                  ledger=TemporalTrialLedger(), cache_dir=None, data_fingerprint=ctx.data_fingerprint,
                                  window=ctx.window, events_cache_key=ctx.events_cache_key,
                                  features_cache_key=ctx.features_cache_key)
    t0 = time.perf_counter()
    entries, pool_stats = build_pool(evaluator, ctx, cfg, probe_dir, market)
    timings["pool_s"] = round(time.perf_counter() - t0, 1)
    log(f"[{market}] pool: {len(entries)} specs ({pool_stats['top_loaded']} top, {pool_stats['extra_added']} extra, "
        f"{pool_stats['extra_attempts']} attempts) {timings['pool_s']}s")
    t0 = time.perf_counter()
    frame = evaluator.frame
    pool = ds.candidates_for_specs(entries, frame, evaluator.resolver, tm)
    timings["candidates_s"] = round(time.perf_counter() - t0, 1)
    market_tr = ds.truncate_market(ctx.market, n_cut)
    dates_tr, tm_tr = ctx.dates[:n_cut], tm[:n_cut]
    inp = F.inputs_from_frame(frame, ctx.market.minute, ctx.dates, ctx.window.entry_start_min, ctx.window.entry_end_min)
    inp_tr = ds.truncate_inputs(inp, n_cut)
    cost = ctx.costs[ADVERSE]
    t0 = time.perf_counter()
    table = ds.build_trade_table(pool, market_tr, inp_tr, dates_tr, tm_tr, cost, ctx.sizing, ctx.rules, ctx.window)
    timings["table_s"] = round(time.perf_counter() - t0, 1)
    n_specs_with_trades = len(np.unique(table.spec))
    log(f"[{market}] table: {len(table)} trades, {n_specs_with_trades} specs, {table.n_days} train days, {table.x.shape[1]} features "
        f"({timings['table_s']}s)")
    folds = cv.rolling_origin_folds(table.day, table.exit_day, table.n_days, cfg["cv"]["n_folds"],
                                    cfg["cv"]["embargo_days"], cfg["cv"]["min_train_days"])
    n_oof = int(sum(len(f.test) for f in folds))
    # ---- models + nulls
    t0 = time.perf_counter()
    models, verdicts = run_models(table, cfg, folds, cfg["seed"], with_null=True)
    timings["models_s"] = round(time.perf_counter() - t0, 1)
    # ---- sub-analysis on extra (NOT profit-selected) specs only, point metrics
    sub: dict[str, Any] = {}
    mx = table.source == 1
    if mx.sum() > 2000 and len(np.unique(table.day[mx])) > 60:
        sel = np.flatnonzero(mx)
        sub_tab = ds.TradeTable(table.spec[sel], table.decision_idx[sel], table.entry_idx[sel], table.exit_idx[sel],
                                table.direction[sel], table.day[sel], table.exit_day[sel], table.r[sel], table.y1[sel],
                                table.y3[sel], table.mfe[sel], table.mae[sel], table.hold[sel], table.x[sel], table.names,
                                table.n_days, table.source[sel])
        try:
            sf = cv.rolling_origin_folds(sub_tab.day, sub_tab.exit_day, sub_tab.n_days, cfg["cv"]["n_folds"],
                                         cfg["cv"]["embargo_days"], cfg["cv"]["min_train_days"])
            sm, _ = run_models(sub_tab, {**cfg, "models": [m for m in cfg["models"] if m["name"] in ("logit_y1", "gbdt_y1")]},
                               sf, cfg["seed"], with_null=False, keep_importance=False)
            sub = {"n_trades": len(sub_tab), "models": {k: {kk: v[kk] for kk in ("auc_y1", "auc_y1_ci95", "top_decile", "n_oof")} for k, v in sm.items()}}
        except cv.CVError as exc:
            sub = {"error": str(exc)}
    # ---- confluence
    t0 = time.perf_counter()
    conf = run_confluence(ctx, cfg, pool, table, market_tr, dates_tr, tm_tr, cost, n_cut)
    timings["confluence_s"] = round(time.perf_counter() - t0, 1)
    overall = ev.overall_verdict(verdicts)
    # ---- ledger header (trials)
    probe_trials = 0
    ps = probe_dir / market / "probe_summary.json"
    if ps.exists():
        probe_trials = json.loads(ps.read_text(encoding="utf-8"))["counts"]["evaluations_total"]
    n_cfg = len(cfg["models"])
    ledger = {
        "v1_cumulative": cfg["prior"], "v2_probe_this_market_evaluations": probe_trials,
        "metalabel_structure_evaluations": evaluator.ledger.total_trials, "metalabel_model_configurations": n_cfg,
        "null_pipeline_refits_not_counted_as_trials": True,
        "cumulative_trials_incl_this_run": cfg["prior"]["trials"] + probe_trials + evaluator.ledger.total_trials + n_cfg,
        "selection_caveat": "pool = top-100 by Train fitness (profit-selected) + extra min-trade passers (not profit-selected); "
                            "specs, ~89 features, 4 model configurations and the verdict thresholds were all chosen on this Train side; "
                            "the nulls do not re-run spec selection, so p-values are optimistic by the spec-selection effect "
                            "(see 'unselected_specs_only' for the profit-unselected subset)",
    }
    base_rate = float(table.y1.mean())
    summary = {
        "summary_version": SUMMARY_VERSION, "market": market,
        "scope": "TRAIN side of search fold 0 only; purged rolling-origin CV inside it; no fold-test / validation / holdout data",
        "folds": ctx.fold_rep["folds"][0], "config_hash": vp.stable_hash(cfg),
        "dataset": {
            "n_trades": len(table), "n_specs_requested": len(entries), "n_specs_with_trades": n_specs_with_trades,
            "n_train_days": table.n_days, "n_features": table.x.shape[1], "n_oof_rows": n_oof,
            "trades_by_source": {"pool_top": int((table.source == 0).sum()), "extra": int((table.source == 1).sum())},
            "base_hit_rate_R_gt_0": base_rate, "mean_r_adverse": float(table.r.mean()),
            "target_before_stop_rate": float(table.y3.mean()),
            "unique_bars_directions": len(np.unique(table.decision_idx * 2 + (table.direction > 0))),
            "pool": pool_stats, "cv_folds": [{"fold": f.index, "train_rows": len(f.train), "test_rows": len(f.test),
                                            "test_days": [f.test_start, f.test_end], "embargo": f.embargo} for f in folds],
        },
        "features": table.names, "models": models, "unselected_specs_only": sub,
        "verdict_metalabel": overall, "verdict_per_model": verdicts, "ledger": ledger,
        "confluence_verdict": conf["verdict"], "runtime_s": {**timings, "total_s": round(time.perf_counter() - t_all, 1)},
        "peak_rss_mb": vp.peak_rss_mb(),
    }
    (out_dir / "metalabel_summary.json").write_text(json.dumps(_clean(summary), indent=1, sort_keys=True), encoding="utf-8")
    conf_out = {"summary_version": SUMMARY_VERSION, "market": market, "scope": summary["scope"], **conf}
    (out_dir / "confluence_summary.json").write_text(json.dumps(_clean(conf_out), indent=1, sort_keys=True), encoding="utf-8")
    (out_dir / "metalabel_report.md").write_text(render_markdown(_clean(summary), _clean(conf_out)), encoding="utf-8")
    log(f"[{market}] META-LABEL verdict: {overall} | CONFLUENCE: {conf['verdict']['verdict']} | total {summary['runtime_s']['total_s']}s "
        f"rss {summary['peak_rss_mb']} MB -> {out_dir}")
    return summary


def run_confluence(ctx: vp.ProbeContext, cfg: dict, pool: list[ds.SpecCandidates], table: ds.TradeTable,
                   market_tr, dates_tr: np.ndarray, tm_tr: np.ndarray, cost, n_cut: int) -> dict:
    cc = cfg["confluence"]
    train_days = np.unique(dates_tr[tm_tr])
    day_ord = np.where(tm_tr, np.searchsorted(train_days, dates_tr), -1)
    atr = np.asarray(ctx.atr[:n_cut], dtype=float)
    idx = np.flatnonzero(tm_tr)
    ref = {}
    for d in (1, -1):
        ref[d] = cf.reference_outcomes(market_tr, atr, idx, d, cost, ctx.sizing, ctx.rules, ctx.window,
                                       stop_atr=cc["stop_atr"], target_r=cc["target_r"])
    log(f"[confluence] reference outcomes: long {int(np.isfinite(ref[1]).sum())}, short {int(np.isfinite(ref[-1]).sum())} bars")
    row_bar = np.concatenate([p.cands.decision_idx for p in pool]).astype(np.int64)
    row_dir = np.concatenate([p.cands.direction for p in pool])
    row_spec = np.concatenate([np.full(len(p.cands.decision_idx), i) for i, p in enumerate(pool)])
    codes = [np.unique(p.cands.decision_idx.astype(np.int64) * 2 + (p.cands.direction > 0)) for p in pool]
    daily = np.zeros((len(pool), len(train_days)))
    if len(table):
        np.add.at(daily, (table.spec, table.day), table.r)
    fam = np.array([F.family_index(p.family) for p in pool])
    data = cf.ConfluenceData(row_bar, (row_dir > 0).astype(np.int64), row_spec, fam, codes, daily, ref[1], ref[-1],
                             day_ord, np.asarray(market_tr.minute, dtype=np.int64), tm_tr)
    res = cf.analyse(data, n_null=cc["n_null"], seed=cfg["seed"])
    res["reference_definition"] = {"entry": "next open", "stop_atr": cc["stop_atr"], "target_r": cc["target_r"],
                                   "cost": ADVERSE, "note": "independent of every strategy's own exits; overlapping bars simulated in passes"}
    return res


def render_markdown(s: dict, c: dict) -> str:
    d = s["dataset"]
    lines = [f"# V2 meta-label + confluence: {s['market']}", "", f"Scope: {s['scope']}.", "",
             f"- trades {d['n_trades']} from {d['n_specs_with_trades']} specs over {d['n_train_days']} Train days, {d['n_features']} features, "
             f"{d['n_oof_rows']} OOF rows (base hit-rate R>0 {d['base_hit_rate_R_gt_0']}, mean R {d['mean_r_adverse']})",
             f"- META-LABEL VERDICT: **{s['verdict_metalabel']}**  (per model: { {k: v['verdict'] for k, v in s['verdict_per_model'].items()} })",
             f"- CONFLUENCE VERDICT: **{c['verdict']['verdict']}** ({c['verdict']['reason']})", "",
             "## Models (out-of-fold, purged rolling-origin)", "",
             "| model | AUC [95% day-cluster CI] | top-decile mean R | all mean R | diff t | calib slope | Brier skill | p_perm(AUC/R) | p_shift(AUC/R) | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, m in s["models"].items():
        td = m["top_decile"]
        nul = m.get("nulls", {})
        pp = nul.get("perm", {})
        ps = nul.get("shift", {})
        lines.append(f"| {name} | {m['auc_y1']} [{m['auc_y1_ci95'][0]}, {m['auc_y1_ci95'][1]}] | {td.get('mean_sel')} | {m['mean_r_all']} | {td.get('t')} | "
                     f"{(m.get('calibration') or {}).get('slope')} | {m.get('brier_skill')} | "
                     f"{pp.get('auc', {}).get('p')}/{pp.get('top_decile_mean_r', {}).get('p')} | {ps.get('auc', {}).get('p')}/{ps.get('top_decile_mean_r', {}).get('p')} | "
                     f"{(m.get('verdict') or {}).get('verdict', '-')} |")
    for name in ("gbdt_y1", "logit_y1"):
        m = s["models"].get(name)
        if m and m.get("deciles"):
            lines += ["", f"### {name} decile lift (OOF; decile 10 = highest score)", "", "| decile | n | mean R | hit rate |", "|---|---|---|---|"]
            lines += [f"| {r['decile']} | {r['n']} | {r['mean_r']} | {r.get('hit_rate')} |" for r in m["deciles"]]
        if m and m.get("permutation_importance_oof"):
            lines += ["", f"### {name} top OOF permutation importances (AUC drop)", ""]
            lines += [f"- {r['feature']}: {r['auc_drop']}" for r in m["permutation_importance_oof"][:10]]
    lines += ["", "## Confluence", "", f"clusters: {c['clusters']}", "", "| k (independent clusters) | groups | mean R | hit rate | day-clustered t |", "|---|---|---|---|---|"]
    for k in ("1", "2", "3+"):
        b = c["by_clusters"][k]
        lines.append(f"| {k} | {b.get('n')} | {b.get('mean')} | {b.get('hit_rate')} | {b.get('t')} |")
    lines += ["", f"k>=2 vs k=1: {c['by_clusters']['ge2_vs_1']}", f"day-shift null: {c.get('null_day_shift')}",
              f"by families: {c['by_families']['ge2_vs_1']}", "", "## Ledger / caveats", "", f"{s['ledger']}"]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--markets", nargs="*", default=None)
    p.add_argument("--out-root", default=None)
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--keep-cache", action="store_true")
    p.add_argument("--n-extra", type=int, default=None, help="override pool.extra_specs")
    p.add_argument("--null-draws", type=int, default=None, help="override null draws (smoke runs)")
    p.add_argument("--confluence-null", type=int, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    probe_cfg = json.loads((REPO_ROOT / cfg["probe_config"]).read_text(encoding="utf-8"))
    cache_dir = Path(args.cache_dir or REPO_ROOT / cfg["cache_dir"])
    out_root = Path(args.out_root or REPO_ROOT / cfg["out_root"])
    vp.assert_free_space(cache_dir)
    try:
        for m in args.markets or cfg["markets"]:
            ctx = vp.build_real_context(m, probe_cfg, cache_dir)
            run_market(ctx, cfg, probe_cfg, out_root / m, args)
    finally:
        if not args.keep_cache:
            shutil.rmtree(cache_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
