# ruff: noqa: E501
"""V2 FAMILY probe (research only): parametric intraday edge families on the TRAIN side of fold 0, per market.

For each requested market (default GER40 [ar1 series], NAS100, SPX500, XAUUSD, EURUSD) and family (ORB, GAP,
OVERNIGHT, VOLREV, ROUND, LEADLAG, EOD) the bounded parameter grid (<= 400 specs) is evaluated through the SAME
sealed pipeline as the other V2 probes: ``simulate_fast`` with the market's ``SimWindow`` / ``market_costs``
sizing and costs, V1 ``train_fitness`` on a V1 ``TrainView`` (COMBINED_ADVERSE), cumulative trial ledger.

SEALING.  The dev frame is loaded (bars after 2026-08-31 refused), the purged fold layout is computed, and the
frame is CUT to the Train side of fold 0 before any feature is built; only that slice (and, for LEADLAG, leader
bars up to the same timestamp) lives in memory afterwards.  This module never imports the sealed later-fold module
and never reads a fold test mask; the tests scan its source for that.

Reports (``research/reports/v2_families/``): per market ``<market>/<family>.json`` (all spec rows, BH q-values,
day-shifted-label null of the best-of-grid statistic, summary), ``<market>/market.json`` (drift baselines, data
and calendar facts), ``summary.json`` / ``summary.md`` and ``ledger.json`` (cumulative trials incl. the V1 header).

    python research/runners/v2_family_probe.py --markets GER40 --max-specs 150 --n-shifts 12 --out-root <dir>
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from alpha.common.market_data import assert_no_forward_holdout, load_dev_market_frame  # noqa: E402
from alpha.common.protocol import stable_hash  # noqa: E402
from alpha.discovery.folds import (  # noqa: E402
    SearchSplitPlan,
    berlin_dates_from_ts_ns,
    fold_report,
    make_folds,
)
from alpha.families import evaluate as ev  # noqa: E402
from alpha.families import registry as reg  # noqa: E402
from alpha.families.data import FamilyData, build_family_data, build_leader_features  # noqa: E402
from alpha.families.leadlag import PAIRS  # noqa: E402
from alpha.families.spec import MarketCalendar  # noqa: E402
from markets.spec import load_market_spec  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/v2_families.json"
SUMMARY_VERSION = "v2-families-summary-v1"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def clean(x: Any) -> Any:
    """JSON-safe: numpy scalars -> python, NaN/inf -> None."""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, np.ndarray):
        return clean(x.tolist())
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def _q(values, qs=(0.25, 0.5, 0.75)) -> dict[str, float] | None:
    a = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if len(a) == 0:
        return None
    return {f"p{int(q * 100):02d}": round(float(np.quantile(a, q)), 5) for q in qs} | {"max": round(float(a.max()), 5), "n": len(a)}


# --------------------------------------------------------------------------- Train-only data
def train_slice(frame: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, dict]:
    """Cut a development frame to the TRAIN side of fold 0 (purged, embargo-aware); returns (Train frame, fold layout).

    The layout (dates / day counts only) is reported for reproducibility; the Train slice is the only data that
    survives this function."""
    assert_no_forward_holdout(frame)
    ts_ns = pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8
    dates = berlin_dates_from_ts_ns(ts_ns)
    f = cfg["folds"]
    folds = make_folds(dates, f["n_folds"], f["embargo_days"], f["purge_bars"], initial_train_frac=f["initial_train_frac"],
                       min_test_days=f["min_test_days"])
    plan = SearchSplitPlan.from_folds(dates, folds)
    train = plan.mask(dates, plan.train)
    idx = np.flatnonzero(train)
    stop = int(idx[-1]) + 1
    if int(train[:stop].sum()) != len(idx):
        raise RuntimeError("Train side is not a bar prefix")
    layout = fold_report(dates, folds, f["embargo_days"])
    return frame.iloc[:stop].reset_index(drop=True), layout


def load_market_train(market: str, cfg: dict) -> dict[str, Any]:
    """Real data: Train-only FamilyData (+ leader features), market spec, sim context."""
    spec = load_market_spec(market)
    source = cfg["ger40_source"] if market == "GER40" else "v2"
    frame = load_dev_market_frame(spec, source=source)
    n_dev = len(frame)
    train_frame, layout = train_slice(frame, cfg)
    del frame  # nothing after the Train side is kept
    cal = MarketCalendar.from_market_spec(spec)
    last_ts = train_frame["ts"].iloc[-1]
    cross = {}
    for leader in PAIRS.get(market, ()):
        lspec = load_market_spec(leader)
        lframe = load_dev_market_frame(lspec, source="v2")
        lframe = lframe.loc[lframe["ts"] <= last_ts].reset_index(drop=True)  # never later than the last Train bar
        if len(lframe):
            cross[leader] = build_leader_features(train_frame, lframe, MarketCalendar.from_market_spec(lspec), leader)
    data = build_family_data(train_frame, cal, name=market, point_size=spec.point_size, tick_size=spec.tick_size,
                             asset_class=spec.asset_class, cross=cross)
    s = cfg["sizing"]
    ctx = ev.SimContext.for_market(data, spec, research_equity_eur=s["research_equity_eur"], risk_fraction=s["risk_fraction"],
                                   max_trades_per_day=cfg["rules"]["max_trades_per_day"])
    return {"spec": spec, "data": data, "ctx": ctx, "layout": layout, "n_dev_bars": n_dev, "source": source,
            "leaders": sorted(cross), "calendar_status": spec.calendar.status}


# --------------------------------------------------------------------------- per family
def run_family(market: str, family: str, data: FamilyData, ctx: ev.SimContext, cfg: dict, ledger, *, max_specs: int,
               n_shifts: int, seed: int, skip_null: bool = False) -> dict[str, Any]:
    t0 = time.perf_counter()
    min_trades = cfg["min_train_trades"]
    specs = reg.grid_for(family, market, max_specs)
    if not specs:
        return {"family": family, "market": market, "skipped": "no spec grid for this market (no overlapping-session leader)"}
    if family == "LEADLAG" and not data.cross:
        return {"family": family, "market": market, "skipped": "no leader data for this market"}
    status = {"new": 0, "duplicate": 0, "invalid": 0}
    for s in specs:
        status[ledger.record(ev.TrialKey(s, market), "structural")] += 1
    results, cands = ev.evaluate_grid(data, ctx, specs, min_trades=min_trades)
    t_eval = time.perf_counter() - t0
    null: dict[str, Any] = {"skipped": True}
    if not skip_null:
        t1 = time.perf_counter()
        null = ev.null_calibration(data, ctx, specs, cands, results, n_shifts=n_shifts, seed=seed, min_trades=min_trades)
        null["seconds"] = round(time.perf_counter() - t1, 1)
    days = ev.n_days_of(data)
    active = [r for r in results if r.n_trades >= min_trades]
    by_fit = sorted(results, key=lambda r: (-r.fitness, r.spec_hash))
    by_exp = sorted(active, key=lambda r: (-(r.exp_adv if r.exp_adv is not None else -9.0), r.spec_hash))
    qs = [r.q_bh for r in active if r.q_bh is not None]
    tpd = [r.trades_per_day for r in active]
    best = by_fit[0] if by_fit else None
    summary = {
        "family": family, "market": market, "train_days": days, "n_specs": len(specs), "n_zero_candidates": sum(r.n_candidates == 0 for r in results),
        "n_with_min_trades": len(active), "min_trades": min_trades, "trades_per_day_active_specs": _q(tpd),
        "cand_per_day_all_specs": _q([r.n_candidates / days for r in results]),
        "exp_r_adverse_active_specs": _q([r.exp_adv for r in active]), "exp_r_base_active_specs": _q([r.exp_base for r in active]),
        "share_exp_r_adverse_gt0_active": round(float(np.mean([r.exp_adv > 0 for r in active])), 4) if active else None,
        "spread_cost_r_active_specs": _q([r.spread_cost_r_adv for r in active]), "cost_burden_r_active_specs": _q([r.cost_burden_adv for r in active]),
        "n_bh_q_lt_0.10": sum(1 for q in qs if q < 0.10), "min_q_bh": round(min(qs), 5) if qs else None,
        "best_by_fitness": None if best is None else best.row(),
        "best_by_expectancy_adverse": None if not by_exp else by_exp[0].row(),
        "null": {k: v for k, v in null.items() if k != "shifts_days"},
        "ledger_status_this_grid": status, "seconds": {"eval": round(t_eval, 1), "total": round(time.perf_counter() - t0, 1)},
    }
    return {"summary": summary, "top_by_fitness": [r.row() for r in by_fit[: cfg["top_k"]]], "rows": [r.row() for r in results],
            "null_full": null}


def market_report(market: str, loaded: dict, cfg: dict, seed: int) -> dict[str, Any]:
    data, ctx = loaded["data"], loaded["ctx"]
    b = cfg["baselines"]
    base = ev.drift_baselines(data, ctx, seed, draws=b["draws"], stop_atr=b["stop_atr_mult"], target_r=b["target_r"],
                              decisions_per_day=b["decisions_per_day"])
    sp = loaded["spec"]
    return {
        "market": market, "source": loaded["source"], "calendar_status": loaded["calendar_status"], "leaders": loaded["leaders"],
        "dev_bars_loaded": loaded["n_dev_bars"], "train_bars": len(data), "train_days": ev.n_days_of(data),
        "train_first_utc": str(pd.Timestamp(int(data.ts_ns[0]), tz="UTC")), "train_last_utc": str(pd.Timestamp(int(data.ts_ns[-1]), tz="UTC")),
        "calendar": {"tz": data.cal.tz, "cash": [data.cal.cash_open_min, data.cal.cash_close_min], "entry": [data.cal.entry_start_min, data.cal.entry_end_min], "flat": data.cal.flat_min},
        "round_steps_minor_major": list(data.round_steps), "asset_class": sp.asset_class,
        "sizing": {"equity": ctx.sizing.equity_eur, "risk_fraction": ctx.sizing.risk_fraction, "min_risk": ctx.sizing.min_risk_pts,
                   "max_risk": ctx.sizing.max_risk_pts, "lot": [ctx.sizing.min_lot, ctx.sizing.lot_step], "leverage": ctx.sizing.max_leverage},
        "costs": {n: {"spread_mult": c.spread_mult, "slippage": c.slippage_pts, "penetration": c.target_penetration_pts} for n, c in ctx.costs.items()},
        "max_entry_spread": ctx.rules.max_entry_spread_pts, "drift_baselines_train": base, "folds": loaded["layout"],
        "scope": "TRAIN side of fold 0 only; no Validation/fold-test number anywhere",
    }


def render_markdown(summary: dict) -> str:
    lines = ["# V2 family probe (Train side of fold 0 only)", "",
             f"cumulative trials (incl. {summary['cumulative']['prior_label']}): {summary['cumulative']['cumulative_trials']}, "
             f"unique {summary['cumulative']['cumulative_unique_specs']} (this run: {summary['cumulative']['this_run_trials']} spec evaluations; "
             f"{summary['cumulative']['null_evaluations']} shifted-null evaluations are diagnostics, not trials)", "",
             "| market | family | specs | tr/day med | best fitness spec: n, E[R] adv, t | best E[R] adv (n>=min) | null pct fit / exp | p_best fit | min q_BH |",
             "|---|---|---|---|---|---|---|---|---|"]
    for m, fams in summary["markets"].items():
        for f, s in fams.items():
            if "skipped" in s:
                lines.append(f"| {m} | {f} | - | - | {s['skipped']} | | | | |")
                continue
            b, e, n = s["best_by_fitness"], s["best_by_expectancy_adverse"], s["null"]
            tpd = (s["trades_per_day_active_specs"] or {}).get("p50")
            lines.append(
                f"| {m} | {f} | {s['n_specs']} | {tpd} | {b and b['n_trades']}, {b and b['exp_r_adverse']}, {b and b['t_day_clustered']} | "
                f"{e and e['exp_r_adverse']} | {(n.get('fitness') or {}).get('pct')} / {(n.get('expectancy_r_adverse') or {}).get('pct')} | "
                f"{(n.get('fitness') or {}).get('p_best')} | {s['min_q_bh']} |")
    return "\n".join(lines) + "\n"


def run(cfg: dict, markets: list[str], families: list[str], out_root: Path, *, max_specs: int, n_shifts: int, seed: int,
        skip_null: bool = False, resume: bool = False, loader=load_market_train) -> dict[str, Any]:
    out_root.mkdir(parents=True, exist_ok=True)
    ledger_path = out_root / "ledger.json"
    carried = {"trials": 0, "unique_specs": 0}
    ledger = ev.make_ledger()
    if resume and ledger_path.exists():
        ledger = ev.load_ledger(json.dumps(json.loads(ledger_path.read_text(encoding="utf-8"))["ledger"]))
        carried = {"trials": ledger.total_trials, "unique_specs": ledger.unique}
    prior = dict(cfg["prior"])
    t_all = time.perf_counter()
    out: dict[str, dict] = {}
    null_evals = 0
    for m in markets:
        t0 = time.perf_counter()
        loaded = loader(m, cfg)
        data, ctx = loaded["data"], loaded["ctx"]
        log(f"[{m}] Train view: {len(data)} bars / {ev.n_days_of(data)} days (calendar {loaded['calendar_status']}); leaders {loaded['leaders']}")
        mdir = out_root / m
        mdir.mkdir(parents=True, exist_ok=True)
        rep = market_report(m, loaded, cfg, seed)
        (mdir / "market.json").write_text(json.dumps(clean(rep), indent=1, sort_keys=True, allow_nan=False), encoding="utf-8")
        out[m] = {}
        for fam in families:
            res = run_family(m, fam, data, ctx, cfg, ledger, max_specs=max_specs, n_shifts=n_shifts, seed=seed, skip_null=skip_null)
            if "skipped" in res:
                out[m][fam] = res
                log(f"[{m}/{fam}] skipped: {res['skipped']}")
                continue
            s = res["summary"]
            null_evals += int(s["null"].get("null_evaluations", 0) or 0)
            (mdir / f"{fam}.json").write_text(json.dumps(clean(res), indent=0, sort_keys=True, allow_nan=False), encoding="utf-8")
            out[m][fam] = s
            b = s["best_by_fitness"]
            log(f"[{m}/{fam}] specs={s['n_specs']} active={s['n_with_min_trades']} best fit={b and b['fitness']} E[R]adv={b and b['exp_r_adverse']} t={b and b['t_day_clustered']} "
                f"null pct={(s['null'].get('fitness') or {}).get('pct')} ({s['seconds']['total']}s)")
        log(f"[{m}] done in {time.perf_counter() - t0:.0f}s")
        del loaded, data, ctx
    cum = {"prior_label": prior["label"], "prior_trials": prior["trials"], "prior_unique_specs": prior["unique_specs"],
           "this_run_trials": ledger.total_trials - carried["trials"], "carried_in_trials": carried["trials"],
           "cumulative_trials": prior["trials"] + ledger.total_trials, "cumulative_unique_specs": prior["unique_specs"] + ledger.unique,
           "family_ledger_trials": ledger.total_trials, "family_ledger_unique": ledger.unique,
           "duplicate_rejects": ledger.duplicate_rejects, "invalid_rejects": ledger.invalid_rejects, "null_evaluations": null_evals}
    summary = {"summary_version": SUMMARY_VERSION, "config_hash": stable_hash(cfg), "seed": seed, "max_specs": max_specs, "n_shifts": n_shifts,
               "scope": "TRAIN side of fold 0 only; the sealed later-fold module is not imported or called", "markets": out, "cumulative": cum,
               "runtime_s": round(time.perf_counter() - t_all, 1)}
    ledger_path.write_text(json.dumps({"header": {"v1_cumulative": prior, "config_hash": stable_hash(cfg)}, "ledger": json.loads(ledger.to_json())},
                                      sort_keys=True), encoding="utf-8")
    (out_root / "summary.json").write_text(json.dumps(clean(summary), indent=1, sort_keys=True, allow_nan=False), encoding="utf-8")
    (out_root / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    log(f"wrote {out_root} (cumulative trials {cum['cumulative_trials']}, unique {cum['cumulative_unique_specs']}) in {summary['runtime_s']}s")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--markets", nargs="*", default=None)
    ap.add_argument("--families", nargs="*", default=None)
    ap.add_argument("--max-specs", type=int, default=None, help="specs per family and market (default config, <= 400)")
    ap.add_argument("--n-shifts", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out-root", default=None)
    ap.add_argument("--skip-null", action="store_true")
    ap.add_argument("--resume", action="store_true", help="carry the existing ledger.json counts")
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    max_specs = min(400, a.max_specs if a.max_specs is not None else cfg["max_specs_per_family_market"])
    run(cfg, a.markets or cfg["markets"], a.families or cfg["families"], Path(a.out_root or REPO_ROOT / cfg["out_root"]),
        max_specs=max_specs, n_shifts=a.n_shifts if a.n_shifts is not None else cfg["null"]["n_shifts"],
        seed=a.seed if a.seed is not None else cfg["seed"], skip_null=a.skip_null, resume=a.resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
