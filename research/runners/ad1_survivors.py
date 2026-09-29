"""AD1 post-search stages C-E, selection-aware statistics and the survivors report.

Research only.  Consumes a CANDIDATE POOL json written by a search driver::

    {"meta": {..., "ledger": {total_trials, unique_specs, param_trials, structural_trials,
                              duplicate_rejects, invalid_rejects, cache_hits},
              "oos_touched": false},
     "candidates": [{"genome": Genome.to_dict(), "canonical_hash", "train_fitness", "origin",
                     "lineage"}]}

and writes ``survivors.json`` + ``report.md``.  This runner NEVER reads or evaluates the OOS
partition: the store is built from ``dev_frame`` (OOS bars physically removed), the pool must
assert ``oos_touched == false`` and ``validation_gate_view`` is used only inside
``alpha.discovery.stages``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from alpha.common.dataset import POINT, load_research_dataset  # noqa: E402
from alpha.discovery import selection as sel  # noqa: E402
from alpha.discovery import stages as st  # noqa: E402
from alpha.discovery.compile import TrialLedger  # noqa: E402
from alpha.discovery.describe import describe_genome  # noqa: E402
from alpha.discovery.evaluate import GenomeEvaluator  # noqa: E402
from alpha.fast.store import FeatureStore  # noqa: E402
from research.runners import ar2_fast  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/ad1_discovery.json"
DEFAULT_CACHE = REPO_ROOT / "data/feature_store/ad1_bench_gram"
MAX_FINALISTS = 10
OVERLAP_THRESHOLD = 0.6
VERDICT_PREFIX = "ROBUST POSITIVE TRAIN+VALIDATION EDGE FOUND"


# --------------------------------------------------------------------------- weaknesses
def derive_weaknesses(c: st.CandidateResult, train_adverse_exp: float | None) -> list[str]:
    """Auto-derived, human-readable warnings for one candidate."""
    w: list[str] = []
    for label, stage in (("C", c.stage_c), ("D", c.stage_d), ("E", c.stage_e)):
        if stage is not None:
            w += [f"stage {label} FAIL: {r}" for r in stage.reasons]
    sc, sd, s = c.stage_c, c.stage_d, c.selection
    if (sc and sc.expectancy_adverse is not None and train_adverse_exp
            and sc.expectancy_adverse < 0.25 * train_adverse_exp):
        w.append(f"Validation adverse expectancy {sc.expectancy_adverse:.3f} R is < 25% of "
                 f"Train {train_adverse_exp:.3f} R (decay / overfit signature)")
    if sc and sc.n_trades < 50:
        w.append(f"thin Validation sample ({sc.n_trades} trades)")
    if sd:
        adv = sd.by_cost[st.ADVERSE_COST]
        if adv.n_trades < 100:
            w.append(f"small pooled sample ({adv.n_trades} trades)")
        base = sd.by_cost[st.BASE_COST].expectancy_r
        if sd.r_lost_base_to_adverse and base and sd.r_lost_base_to_adverse > 0.5 * base > 0:
            w.append(f"cost sensitive: BASE->COMBINED loses {sd.r_lost_base_to_adverse:.3f} R "
                     f"(> 50% of BASE expectancy)")
    if c.genome is not None:
        from alpha.discovery.genome import complexity
        if complexity(c.genome) >= 6:
            w.append(f"high complexity ({complexity(c.genome)})")
    if s:
        if not s.exceeds_null:
            w.append(f"pooled t {s.pooled_t} does not exceed null bound {s.null_bound_t:.2f} "
                     f"(N={s.n_total_trials})")
        if s.validation_bonferroni_p is not None and s.validation_bonferroni_p > 0.05:
            w.append(f"Validation Bonferroni p = {s.validation_bonferroni_p:.3f} > 0.05 "
                     f"(N_eff={s.n_unique_specs})")
        if s.deflated_sharpe_prob is not None and s.deflated_sharpe_prob < 0.95:
            w.append(f"deflated-Sharpe probability {s.deflated_sharpe_prob:.3f} < 0.95")
    return w


# --------------------------------------------------------------------------- serialisation
def _finalist_record(c: st.CandidateResult, ev: GenomeEvaluator, cluster_size: int,
                     role: str) -> dict[str, Any]:
    train = st.peek_evaluation(ev, c.genome).train
    train_adv = train.adverse.screen.expectancy_r
    return {
        "role": role, "canonical_hash": c.canonical_hash, "cluster_size": cluster_size,
        "origin": c.origin, "lineage": c.lineage, "train_fitness": c.train_fitness,
        "furthest_stage": c.furthest_stage, "passed_all": c.passed_all,
        "genome": c.genome.to_dict(), "spec": describe_genome(c.genome, ev.resolver),
        "train": {"base": st.to_plain(train.base.screen),
                  "adverse": st.to_plain(train.adverse.screen),
                  "adverse_se_r": train.adverse.se_r,
                  "chunk_expectancy_adverse": list(train.adverse.chunk_expectancy)},
        "stage_c": st.to_plain(c.stage_c), "stage_d": st.to_plain(c.stage_d),
        "stage_e": st.to_plain(c.stage_e), "selection": st.to_plain(c.selection),
        "weaknesses": derive_weaknesses(c, train_adv),
    }


def _near_misses(res: st.PipelineResult, k: int = 5) -> list[st.CandidateResult]:
    order = {"E": 3, "D": 2, "C": 1, "A": 0}
    pool = [c for c in res.candidates if c.stage_c is not None and c.stage_c.passed]
    pool.sort(key=lambda c: (-order[c.furthest_stage],
                             -(c.selection.pooled_t if c.selection and c.selection.pooled_t
                               is not None else -1e9), c.canonical_hash))
    return pool[:k]


# --------------------------------------------------------------------------- report
def _fmt(x: Any, nd: int = 3) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _screen_row(name: str, d: dict[str, Any]) -> str:
    return (f"| {name} | {_fmt(d.get('n_trades'))} | {_fmt(d.get('expectancy_r'))} | "
            f"{_fmt(d.get('profit_factor'))} | {_fmt(d.get('top_3_positive_r_share'))} | "
            f"{_fmt(d.get('max_drawdown_r'), 1)} | {_fmt(d.get('max_loss_streak'))} |")


def _glance_table(finalists: list[dict[str, Any]]) -> list[str]:
    """Compact 'finalists at a glance' table (also used as the headline numbers)."""
    from alpha.discovery.search import lineage_family

    out = ["## Finalists at a glance", "",
           "| # | hash | origin | lineage family | Train n | Train E[R] | Val n | Val E[R] | "
           "Val t | Val Bonf. p | pooled t | null bound | trades/day | timing ok |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, f in enumerate(finalists, 1):
        tr, sc, s = f["train"]["adverse"], f["stage_c"] or {}, f["selection"] or {}
        sd = f["stage_d"]
        tpd = sd["by_cost"][st.ADVERSE_COST]["trades_per_day"] if sd else None
        se_ = f["stage_e"]
        timing_ok = "-" if not se_ else ("yes" if se_.get("timing_ok") else "NO")
        out.append(
            f"| {i} | `{f['canonical_hash'][:12]}` | {f['origin']} | "
            f"{lineage_family(f['lineage'] or 'HYBRID')} | {_fmt(tr.get('n_trades'))} | "
            f"{_fmt(tr.get('expectancy_r'))} | {_fmt(sc.get('n_trades'))} | "
            f"{_fmt(sc.get('expectancy_adverse'))} | {_fmt(sc.get('t_adverse'), 2)} | "
            f"{_fmt(s.get('validation_bonferroni_p'))} | {_fmt(s.get('pooled_t'), 2)} | "
            f"{_fmt(s.get('null_bound_t'), 2)} | {_fmt(tpd, 2)} | {timing_ok} |")
    return [*out, ""]


def render_report(summary: dict[str, Any], finalists: list[dict[str, Any]]) -> str:
    m, counts = summary["meta_ledger"], summary["counts"]
    acc = summary["accounting"]
    out = ["# AD1 survivors report (Train + Validation only)", "",
           "OOS TOUCHED: NO", "",
           f"**{VERDICT_PREFIX}: {summary['verdict']}**", "",
           *(_glance_table(finalists) if finalists else []),
           f"Verdict rule: {sel.VERDICT_RULE}", "",
           "## Search accounting (from pool meta.ledger)", "",
           "| total trials | unique specs | param | structural | duplicate rejects | invalid "
           "rejects | cache hits |", "|---|---|---|---|---|---|---|",
           f"| {m.get('total_trials')} | {acc['campaign_unique_specs']} | "
           f"{m.get('param_trials')} | "
           f"{m.get('structural_trials')} | {m.get('duplicate_rejects')} | "
           f"{m.get('invalid_rejects')} | {m.get('cache_hits')} |", "",
           f"Selection null bound: E[max t] ~ sqrt(2 ln N) = {summary['null_bound_t']:.2f} for "
           f"N={acc['n_total_trials']} (rough extreme-value bound, NOT a proof); "
           f"Bonferroni N_eff = {acc['n_unique_specs']} unique canonical specs.", "",
           f"Cumulative N: this campaign {acc['campaign_total_trials']} trials / "
           f"{acc['campaign_unique_specs']} unique specs + prior campaigns "
           f"{acc['prior_trials']} trials / {acc['prior_unique_specs']} unique specs "
           f"(`--prior-trials`, `--prior-unique-specs`) = {acc['n_total_trials']} / "
           f"{acc['n_unique_specs']}.", "",
           f"Stage-E neighbour evaluations added {summary['neighbor_param_trials']} param trials "
           "(pipeline ledger delta, not part of the search N).", "",
           "## Survivors per stage", "",
           "| stage | count |", "|---|---|",
           f"| pool (distinct) | {counts['pool']} |",
           f"| Stage A pass (Train screen) | {counts['stage_a']} |",
           f"| Train-positive (COMBINED_ADVERSE) | {counts['train_positive']} |",
           f"| Validation-positive (COMBINED_ADVERSE) | {counts['validation_positive']} |",
           f"| Train- AND Validation-positive | {counts['train_and_validation_positive']} |",
           f"| Stage C (validation gate) | {counts['C']} |",
           f"| Stage D (cost stress, pooled) | {counts['D']} |",
           f"| Stage E (stability) | {counts['E']} |",
           f"| overlap clusters (Jaccard >= {summary['overlap_threshold']}) | "
           f"{len(summary['clusters'])} |", ""]
    if summary["clusters"]:
        sizes = ", ".join(str(c["size"]) for c in summary["clusters"])
        out += [f"Cluster sizes (best-fitness representative first): {sizes}", ""]
    if not finalists:
        out += ["No finalists: no candidate passed all stages.", ""]
    for i, f in enumerate(finalists, 1):
        spec, tr, sc, sd, se, s = (f["spec"], f["train"], f["stage_c"], f["stage_d"],
                                   f["stage_e"], f["selection"])
        out += [f"## Finalist {i} ({f['role']}) `{f['canonical_hash'][:12]}`", "",
                f"origin {f['origin']} / lineage {f['lineage']} / cluster size "
                f"{f['cluster_size']} / furthest stage {f['furthest_stage']} / train fitness "
                f"{_fmt(f['train_fitness'])}", "",
                f"- direction: {spec['direction']}", "- H1 logic:"]
        out += [f"  - {x}" for x in spec["h1_logic"]] + ["- M15 logic:"]
        out += [f"  - {x}" for x in spec["m15_logic"]] + ["- M5 trigger:"]
        out += [f"  - {x}" for x in spec["m5_trigger"]]
        out += [f"- stop: {spec['stop']}", f"- target: {spec['target']}",
                f"- window: {spec['time_window']}", "",
                "| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |",
                "|---|---|---|---|---|---|---|",
                _screen_row("Train BASE", tr["base"]),
                _screen_row("Train COMBINED_ADVERSE", tr["adverse"])]
        if sc:
            out += [f"| Validation BASE | {sc['n_trades']} | {_fmt(sc['expectancy_base'])} | "
                    f"{_fmt(sc['profit_factor_base'])} | {_fmt(sc['top3_share_base'])} | - | - |",
                    f"| Validation COMBINED_ADVERSE | {sc['n_trades']} | "
                    f"{_fmt(sc['expectancy_adverse'])} | - | - | - | - |"]
        if sd:
            for cost, p in sd["by_cost"].items():
                out.append(f"| Pooled {cost} | {p['n_trades']} | {_fmt(p['expectancy_r'])} | "
                           f"{_fmt(p['profit_factor'])} | {_fmt(p['top3_share'])} | "
                           f"{_fmt(p['max_dd_r'], 1)} | {p['max_loss_streak']} |")
            out += ["", f"Cost stress: lower bound (adverse) {_fmt(sd['lower_bound_adverse'])} R; "
                    f"cost burden {_fmt(sd['cost_burden_adverse_r'])} R/trade; R lost BASE->"
                    f"COMBINED {_fmt(sd['r_lost_base_to_adverse'])}"]
        if se:
            h = se["neighborhood"]
            out += ["", f"Neighbours: n={h['n_neighbors']} (skipped {h['n_skipped']}), positive "
                    f"{_fmt(h['frac_positive'], 2)}, worst {_fmt(h['worst_expectancy_r'])} R, "
                    f"median {_fmt(h['median_expectancy_r'])} R", "",
                    "Regime / session / month (pooled, COMBINED_ADVERSE):", "",
                    "| dimension | bucket | trades | sum R | mean R |", "|---|---|---|---|---|"]
            for dim, table in se["regime"]["tables"].items():
                for bucket, v in table.items():
                    out.append(f"| {dim} | {bucket} | {v['n']} | {_fmt(v['sum_r'], 2)} | "
                               f"{_fmt(v['mean_r'])} |")
            tm = se.get("timing")
            if tm:
                thr = summary["thresholds"]
                req = ", ".join(f"k={k}" for k in thr["e_timing_required"])
                out += ["", "Timing robustness (pooled COMBINED_ADVERSE, decisions delayed by k M5 "
                        f"bars; required > 0: {req} and jitter):", "",
                        "| variant | trades | E[R] | t |", "|---|---|---|---|",
                        f"| undelayed | - | {_fmt(tm['undelayed_expectancy'])} | - |"]
                variants = {**tm["by_delay"],
                            f"jitter U{{0..{thr['e_timing_jitter_max']}}}": tm["jitter"]}
                for name, row in variants.items():
                    out.append(f"| {name} | {row['n_trades']} | {_fmt(row['expectancy_r'])} | "
                               f"{_fmt(row['t_stat'], 2)} |")
                out.append(f"timing_ok = {tm['passed']}")
            cc = se["concentration"]
            out += ["", f"Concentration: top-3 share {_fmt(cc['top3_share'])}, maxDD "
                    f"{_fmt(cc['max_dd_r'], 1)} R, loss streak {cc['max_loss_streak']}, "
                    f"trades/day {_fmt(cc['trades_per_day'])}, zero-trade-day fraction "
                    f"{_fmt(cc['zero_trade_day_frac'])}"]
        if s:
            out += ["", "Selection-aware statistics: pooled day-clustered t "
                    f"{_fmt(s['pooled_t'], 2)} vs null bound {_fmt(s['null_bound_t'], 2)} -> "
                    f"{'EXCEEDS' if s['exceeds_null'] else 'does NOT exceed'} null; Validation t "
                    f"{_fmt(s['validation_t'], 2)}, Bonferroni p "
                    f"{_fmt(s['validation_bonferroni_p'])}; per-trade Sharpe "
                    f"{_fmt(s['sharpe_per_trade'])}, skew {_fmt(s['skew'], 2)}, kurtosis "
                    f"{_fmt(s['kurtosis'], 2)}, deflated-Sharpe probability "
                    f"{_fmt(s['deflated_sharpe_prob'])}"]
        out += ["", "Weaknesses:"] + ([f"- {x}" for x in f["weaknesses"]] or ["- none derived"])
        out.append("")
    out += ["---", "OOS TOUCHED: NO", f"**{VERDICT_PREFIX}: {summary['verdict']}**", ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- driver
def run(pool_path: Path, config: Path, out_dir: Path, cache_dir: Path,
        overlap: float = OVERLAP_THRESHOLD, prior_trials: int | None = None,
        prior_unique_specs: int | None = None, min_train_trades: int | None = None,
        c_min_trades: int | None = None) -> dict[str, Any]:
    cfg = json.loads(config.read_text(encoding="utf-8"))
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    if pool.get("meta", {}).get("oos_touched") is not False:
        raise SystemExit("refusing: pool meta must assert oos_touched == false")
    plan = ar2_fast._plan(cfg)
    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    dev = ar2_fast.dev_frame(ds.frame, plan)  # OOS bars physically removed
    features = FeatureStore.load_or_build(dev, {"point_size": POINT}, cache_dir)
    market, dates = ar2_fast._market(features), ar2_fast._dates(features)
    # Train minimum: CLI override, else the value the pool was searched with (its cache
    # fingerprint must match), else the config default
    if min_train_trades is None:
        min_train_trades = pool["meta"].get("min_train_trades")
    ev = GenomeEvaluator(features, market, dates, plan, cfg, cache_dir, TrialLedger(),
                         min_train_trades=min_train_trades)
    overrides = {} if c_min_trades is None else {"c_min_trades": int(c_min_trades)}
    pcfg = st.PipelineConfig.from_dict({"seed": cfg.get("seed", 20260930),
                                        **cfg.get("pipeline", {}), **overrides})

    res = st.run_pipeline(pool, ev, pcfg, prior_trials, prior_unique_specs)
    e_surv = res.survivors("E")
    clusters = sel.overlap_clusters(e_surv, ev, overlap, sim=res.sim)
    reps = [c.representative for c in clusters]
    verdict = sel.robust_verdict(reps)
    finalists = [_finalist_record(c.representative, ev, c.size, "cluster representative")
                 for c in clusters[:MAX_FINALISTS]]
    if not finalists:
        finalists = [_finalist_record(c, ev, 1, f"near-miss (stopped at stage "
                                      f"{c.furthest_stage}{'+' if c.passed_all else ''})")
                     for c in _near_misses(res)]
    meta_ledger = pool["meta"].get("ledger", {})
    n_total = res.accounting["n_total_trials"]
    summary = {
        "verdict": verdict, "verdict_rule": sel.VERDICT_RULE, "oos_touched": False,
        "counts": res.counts, "meta_ledger": meta_ledger, "accounting": res.accounting,
        "null_bound_t": sel.expected_max_null_t(n_total),
        "overlap_threshold": overlap,
        "clusters": [{"representative": c.representative.canonical_hash, "size": c.size,
                      "members": [m.canonical_hash for m in c.members]} for c in clusters],
        "neighbor_param_trials": res.ledger_after["param_trials"]
        - res.ledger_before["param_trials"],
        "pipeline_ledger_before": res.ledger_before, "pipeline_ledger_after": res.ledger_after,
        "thresholds": st.to_plain(pcfg),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "survivors.json").write_text(
        json.dumps({"summary": summary, "finalists": finalists}, indent=2, sort_keys=True,
                   allow_nan=False), encoding="utf-8")
    (out_dir / "report.md").write_text(render_report(summary, finalists), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--overlap", type=float, default=OVERLAP_THRESHOLD)
    parser.add_argument("--prior-trials", type=int, default=None,
                        help="trials of EARLIER campaigns on this data (default: meta, else 0)")
    parser.add_argument("--prior-unique-specs", type=int, default=None,
                        help="unique specs of earlier campaigns (default: pool meta, else 0)")
    parser.add_argument("--min-train-trades", type=int, default=None,
                        help="Train minimum (default: value recorded in pool meta, else config)")
    parser.add_argument("--c-min-trades", "--c-min-val-trades", dest="c_min_trades", type=int,
                        default=None, help="Stage C minimum Validation trades (default 25)")
    args = parser.parse_args(argv)
    summary = run(Path(args.pool), Path(args.config), Path(args.out_dir), Path(args.cache_dir),
                  args.overlap, args.prior_trials, args.prior_unique_specs,
                  args.min_train_trades, args.c_min_trades)
    print(f"counts: {summary['counts']}")
    print(f"accounting: {summary['accounting']}")
    print(f"clusters: {len(summary['clusters'])}  neighbour param trials: "
          f"{summary['neighbor_param_trials']}")
    print(f"{VERDICT_PREFIX}: {summary['verdict']}   OOS TOUCHED: NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
