# ruff: noqa: E501
"""V2 structure-free raw edge scan runner (research only).

  python research/runners/v2_raw_edge_scan.py --only NAS100        # one dataset (one heavy process)
  python research/runners/v2_raw_edge_scan.py --combine            # pooled stats + rawscan_report.md

Per dataset: dev frame (nothing after 2026-08-31) -> first purged fold split -> Train-side scan,
BH, both nulls, top-K pre-registration (hashed, written BEFORE the gate) -> the ONE persistence
gate call (fold TEST sides; output marked 'survival stage, not for selection').
Outputs: research/reports/v2_rawscan/<label>/{scan_summary.json,cells_train.csv.gz,
preregistered_cells.json,persistence_gate.json,null_raw.npz} and rawscan_report.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from alpha.common.market_data import DEV_END, load_dev_market_frame  # noqa: E402
from alpha.discovery.folds import berlin_dates_from_ts_ns, fold_report, make_folds  # noqa: E402
from alpha.rawscan import (  # noqa: E402
    TrialLedger,
    WindowSpec,
    cost_params_for,
    persistence_gate,
    preregister,
    scan_train,
    spread_spike_diagnostic,
)
from alpha.rawscan.report import consistency_table, family_stats, render_markdown  # noqa: E402
from alpha.rawscan.scan import prereg_hash  # noqa: E402
from alpha.rawscan.stats import bh_qvalues, percentiles  # noqa: E402
from markets.spec import load_market_spec  # noqa: E402

CONFIG = REPO_ROOT / "research/configs/v2_raw_scan.json"
OUT = REPO_ROOT / "research/reports/v2_rawscan"

CAVEATS = [
    "Train side of the FIRST purged fold split only (~40% of dev days); small n per DOW cell. Fold TEST sides are read only in persistence_gate (once).",
    "Provisional calendars (NAS100, SPX500, XAUUSD, EURUSD; marked *): entry window/flat minutes and the London/NY overlap are unverified.",
    "Costs: half recorded spread at entry+exit plus one slippage per fill (calibrated fraction of median spread, not an observed fill); commission 0 in BASE; COMBINED_ADVERSE adds 1 EUR/lot round turn; swap/financing not modelled (overnight family holds through the night).",
    "Prices are BID OHLC; ATR(14) is the causal M5 ATR at the entry open; stop variant uses a fixed 1.0 ATR stop with a bar-extreme touch rule.",
    "Day-shift null is exactly invariant for unconditional (all-days) cells, so it is reported for labelled cells only; the centred day bootstrap covers the whole grid. Cells overlap heavily (adjacent slots/horizons, DOW subsets): BH is applied under positive dependence and is a screening tool.",
    "p-values use a normal approximation to Student-t (no scipy); one-sided upper tail = 'positive net edge'.",
    "Gate selection is by net t (positive edge); this equals |t| among edge-direction cells because the mirrored direction is its own cell.",
]


def rss_mb() -> float | None:
    try:
        import psutil

        return psutil.Process().memory_info().peak_wset / 2**20
    except Exception:
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t),
                            ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.WinDLL("kernel32")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            ps = ctypes.WinDLL("psapi")
            ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
            return pmc.PeakWorkingSetSize / 2**20
        except Exception:
            return None


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def _row(r: pd.Series) -> dict:
    keys = ["cell_id", "family", "sigma", "hhmm", "hlabel", "dow", "n", "mean_net", "t", "p1",
            "q_bh", "hit", "payoff", "mfe_mae", "mean_R", "t_R", "mean_comb", "t_comb",
            "mean_shock", "t_shock", "chunks_same_sign", "chunk_min_mean", "in_entry"]
    return {k: r[k] for k in keys}


def run_dataset(ds: dict, cfg: dict, n_rep: int | None, skip_gate: bool) -> dict:
    t0 = time.perf_counter()
    label, market, source = ds["label"], ds["market"], ds["source"]
    spec = load_market_spec(market)
    frame = load_dev_market_frame(spec, None, "M5", source=source)
    dates = berlin_dates_from_ts_ns(pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8)
    f = cfg["folds"]
    folds = make_folds(dates, f["n_folds"], f["embargo_days"], f["purge_bars"],
                       initial_train_frac=f["initial_train_frac"], min_test_days=f["min_test_days"])
    train = frame.loc[folds[0].train_mask].reset_index(drop=True)
    win = WindowSpec.from_spec(spec)
    costs = cost_params_for(spec)
    sc = cfg["scan"]
    nrep = n_rep or sc["n_null_replicates"]
    res = scan_train(
        train, tz=spec.calendar.tz, point_size=spec.point_size, win=win, costs=costs,
        train_end=folds[0].train_end, n_rep=nrep, seed=cfg["seed"], min_n=sc["min_n_days"],
        min_n_dow=sc["min_n_days_dow"],
    )
    tab = res.table
    out_dir = OUT / label
    out_dir.mkdir(parents=True, exist_ok=True)
    tab.drop(columns=["tag"]).to_csv(out_dir / "cells_train.csv.gz", index=False, float_format="%.6g")
    np.savez_compressed(
        out_dir / "null_raw.npz", boot_max_t=res.null_boot_raw["max_t"],
        boot_max_abs_t=res.null_boot_raw["max_abs_t"], shift_max_t=res.null_shift_raw["max_t"],
        flip_max_t=res.null_flip_raw["max_t"], boot_max_t_alldays=res.null_boot_raw["max_t_alldays"],
    )
    ok = tab[np.isfinite(tab["t"])]
    best = ok.sort_values(["t", "cell_id"], ascending=[False, True]).iloc[0]
    top = ok.sort_values(["t", "cell_id"], ascending=[False, True]).head(15)
    fam_counts = {str(k): int(v) for k, v in tab.groupby("family").size().items()}
    cells = preregister(tab, sc["top_k_gate"])
    sha = prereg_hash(cells)
    (out_dir / "preregistered_cells.json").write_text(json.dumps(_clean({
        "note": "top-K Train cells frozen BEFORE the persistence gate", "sha256": sha,
        "cells": cells.to_dict("records")}), indent=1), encoding="utf-8")
    if skip_gate:
        gate = {"found": 0, "n_survive": 0, "skipped": True}
    else:
        gate = persistence_gate(
            cells, frame, folds, tz=spec.calendar.tz, point_size=spec.point_size, win=win,
            costs=costs, run_key=f"{label}:{sha[:12]}", out_path=out_dir / "persistence_gate.json",
            prereg_sha256=sha,
        )
        gate = {k: gate[k] for k in ("found", "n_survive", "k", "note")}
    spike = spread_spike_diagnostic(train, spec.calendar.tz, **{
        "window": cfg["spike"]["window_bars"], "q": cfg["spike"]["quantile"],
        "lookback": cfg["spike"]["lookback_bars"]})
    summary = {
        "label": label, "market": market, "source": source,
        "calendar_status": spec.calendar.status,
        "calendar_provisional": spec.calendar.status == "provisional",
        "train_days": res.grid.n_days, "train_first": str(res.grid.dates[0]),
        "train_last": str(res.grid.dates[-1]), "dev_end": DEV_END,
        "fold0": fold_report(dates, folds, f["embargo_days"])["folds"][0],
        "n_cells": len(tab), "n_cells_evaluated": len(ok),
        "duplicate_cells": res.duplicate_cells, "ledger_family_counts": fam_counts,
        "best_cell": _row(best), "top_cells": [_row(r) for _, r in top.iterrows()],
        "n_q_lt_0.05": int((tab["q_bh"] < 0.05).sum()), "n_q_lt_0.10": int((tab["q_bh"] < 0.10).sum()),
        "min_q_bh": float(np.nanmin(tab["q_bh"])),
        "n_t_gt2": int((ok["t"] > 2).sum()), "n_t_gt3": int((ok["t"] > 3).sum()),
        "best_by_family": {
            fam: _row(g.sort_values("t", ascending=False).iloc[0])
            for fam, g in ok.groupby("family")},
        "null_bootstrap": res.null_boot, "null_day_shift_labelled": res.null_shift,
        "null_signflip": res.null_flip,
        "families": family_stats(tab), "spread_spike": spike, "gate": gate,
        "preregistered_sha256": sha,
        "window_slots": {"entry_start": win.entry_start, "entry_end": win.entry_end,
                         "flat": win.flat, "cash_open": win.cash_open, "cash_close": win.cash_close,
                         "overlap": win.overlap},
        "cost_params": {k: vars(v) for k, v in costs.items()},
        "runtime_s": round(time.perf_counter() - t0, 1), "peak_rss_mb": rss_mb(),
    }
    (out_dir / "scan_summary.json").write_text(json.dumps(_clean(summary), indent=1), encoding="utf-8")
    print(f"[rawscan] {label} done in {summary['runtime_s']}s best {best['cell_id']} t={best['t']:.2f} "
          f"q={best['q_bh']:.3f} rss={summary['peak_rss_mb']}", flush=True)
    return summary


def combine(cfg: dict) -> None:
    docs: dict[str, dict] = {}
    for ds in cfg["datasets"]:
        p = OUT / ds["label"] / "scan_summary.json"
        if p.is_file():
            docs[ds["label"]] = json.loads(p.read_text(encoding="utf-8"))
    ledger = TrialLedger()
    for lab, d in docs.items():
        for fam, n in d["ledger_family_counts"].items():
            ledger.add(lab, fam, n, n)
        if d["duplicate_cells"]:
            ledger.add(lab, "horizon_duplicates", d["duplicate_cells"], 0)
    pooled_labels = [lab for lab in cfg["pooled_labels"] if lab in docs]
    raw = {lab: np.load(OUT / lab / "null_raw.npz") for lab in pooled_labels}
    n_rep = min(len(r["boot_max_t"]) for r in raw.values())
    boot = np.max([raw[lab]["boot_max_t"][:n_rep] for lab in pooled_labels], axis=0)
    flip = np.max([raw[lab]["flip_max_t"][:n_rep] for lab in pooled_labels], axis=0)
    p_all = np.concatenate([
        pd.read_csv(OUT / lab / "cells_train.csv.gz", usecols=["p1"])["p1"].to_numpy() for lab in pooled_labels])
    q_all = bh_qvalues(p_all)
    pooled = {
        "labels": pooled_labels, "n_cells": int(np.isfinite(p_all).sum()),
        "observed_max_t": max(docs[lab]["best_cell"]["t"] for lab in pooled_labels),
        "boot": percentiles(boot), "flip": percentiles(flip), "n_q_lt_0.05": int((q_all < 0.05).sum()),
        "min_q_bh": float(np.nanmin(q_all)),
    }
    ovn_rows = []
    for lab in pooled_labels:
        c = pd.read_csv(OUT / lab / "cells_train.csv.gz",
                        usecols=["cell_id", "family", "n", "mean_net", "t", "p1"])
        c = c[c["family"].isin(["overnight", "ovn_cont", "ovn_rev"])].copy()
        c.insert(0, "label", lab)
        ovn_rows.append(c)
    ovn = pd.concat(ovn_rows, ignore_index=True)
    ovn["q_bh_family"] = bh_qvalues(ovn["p1"].to_numpy())
    pooled["overnight_subfamily_bh"] = {
        "note": "post-hoc descriptive: overnight cells (long/short/cont/rev) x pooled markets, BH over "
                f"{len(ovn)} tests; NOT pre-registered and NOT in the persistence gate",
        "rows": ovn.round(4).to_dict("records"),
    }
    fam_per = {lab: docs[lab]["families"] for lab in docs}
    cons = consistency_table(fam_per)
    cons_out = {"_markets": list(docs)}
    cons_out.update(cons)
    doc = {
        "ledger": ledger.to_dict(), "datasets": docs, "pooled": pooled, "consistency": cons_out,
        "caveats": CAVEATS, "dev_end": DEV_END,
        "ger40_v2_vs_ar1": ger40_compare(),
    }
    (OUT / "rawscan_combined.json").write_text(json.dumps(_clean(doc), indent=1), encoding="utf-8")
    md = render_markdown(_clean(doc))
    cmp_ = doc["ger40_v2_vs_ar1"]
    if cmp_:
        md += f"\nGER40 v2 vs ar1 series: cell-t correlation {cmp_['t_corr']:.3f} over {cmp_['n_common']} cells; " \
              f"best cells v2 {cmp_['best_v2']} / ar1 {cmp_['best_ar1']}.\n"
    (OUT / "rawscan_report.md").write_text(md, encoding="utf-8")
    print(md)


def ger40_compare() -> dict | None:
    a, b = OUT / "GER40" / "cells_train.csv.gz", OUT / "GER40_ar1" / "cells_train.csv.gz"
    if not (a.is_file() and b.is_file()):
        return None
    x, y = pd.read_csv(a), pd.read_csv(b)
    m = x.merge(y, on="cell_id", suffixes=("_v2", "_ar1"))
    m = m[np.isfinite(m["t_v2"]) & np.isfinite(m["t_ar1"])]
    return {"n_common": len(m), "t_corr": float(np.corrcoef(m["t_v2"], m["t_ar1"])[0, 1]),
            "best_v2": x.loc[x["t"].idxmax(), "cell_id"], "best_ar1": y.loc[y["t"].idxmax(), "cell_id"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--only", nargs="*", default=None, help="dataset labels to run")
    ap.add_argument("--combine", action="store_true")
    ap.add_argument("--n-rep", type=int, default=None)
    ap.add_argument("--skip-gate", action="store_true")
    args = ap.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.combine:
        combine(cfg)
        return 0
    for ds in cfg["datasets"]:
        if args.only is None or ds["label"] in args.only:
            run_dataset(ds, cfg, args.n_rep, args.skip_gate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
