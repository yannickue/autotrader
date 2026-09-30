# ruff: noqa: E501
"""Report helpers: family aggregates, cross-market consistency, markdown rendering."""

from __future__ import annotations

import numpy as np
import pandas as pd

FAMILY_FILTERS = {
    "first30_cont": lambda t: t["family"] == "first30_cont",
    "first30_fade": lambda t: t["family"] == "first30_fade",
    "gap_go": lambda t: t["family"] == "gap_go",
    "gap_fade": lambda t: t["family"] == "gap_fade",
    "ovn_cont": lambda t: t["family"] == "ovn_cont",
    "ovn_rev": lambda t: t["family"] == "ovn_rev",
    "ovn_long": lambda t: (t["tag"] == "overnight") & (t["sigma"] == "long"),
    "ovn_short": lambda t: (t["tag"] == "overnight") & (t["sigma"] == "short"),
    "first30_long": lambda t: (t["tag"] == "first30") & (t["sigma"] == "long"),
    "first30_short": lambda t: (t["tag"] == "first30") & (t["sigma"] == "short"),
    "last60_long": lambda t: (t["tag"] == "last60") & (t["sigma"] == "long"),
    "last60_short": lambda t: (t["tag"] == "last60") & (t["sigma"] == "short"),
    "overlap_long": lambda t: (t["tag"] == "overlap") & (t["sigma"] == "long"),
    "overlap_short": lambda t: (t["tag"] == "overlap") & (t["sigma"] == "short"),
    "core_long": lambda t: (t["tag"] == "core") & (t["sigma"] == "long") & t["in_entry"],
    "core_short": lambda t: (t["tag"] == "core") & (t["sigma"] == "short") & t["in_entry"],
}


def family_stats(table: pd.DataFrame) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, f in FAMILY_FILTERS.items():
        sub = table[f(table) & np.isfinite(table["t"])]
        if not len(sub):
            continue
        out[name] = {
            "n_cells": len(sub), "mean_net_atr": float(sub["mean_net"].mean()),
            "mean_t": float(sub["t"].mean()), "best_t": float(sub["t"].max()),
            "share_net_positive": float((sub["mean_net"] > 0).mean()),
        }
    return out


def consistency_table(per_market: dict[str, dict[str, dict]]) -> dict:
    """family -> {market: stats, 'n_markets', 'n_positive_mean', 'same_sign', 'pooled_mean_t'}."""
    fams = sorted({f for m in per_market.values() for f in m})
    out = {}
    for f in fams:
        rows = {m: v[f] for m, v in per_market.items() if f in v}
        means = np.array([r["mean_net_atr"] for r in rows.values()])
        ts = np.array([r["mean_t"] for r in rows.values()])
        out[f] = {
            "markets": rows, "n_markets": len(rows), "n_positive_mean": int((means > 0).sum()),
            "same_sign": bool((means > 0).all() or (means < 0).all()),
            "mean_of_mean_t": float(ts.mean()),
        }
    return out


def _f(x, nd=2):
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{nd}f}"


def render_markdown(doc: dict) -> str:
    L: list[str] = []
    led = doc["ledger"]
    L.append("# V2 raw edge scan (structure-free, Train side only)\n")
    L.append(
        f"Trials: raw scan {led['rawscan_trials']:,} ({led['rawscan_unique']:,} unique); V1 header "
        f"{led['v1_cumulative_header_only']['trials']:,} / {led['v1_cumulative_header_only']['unique']:,}"
        " (header only, not added). Net = BASE costs, ATR units, one trade/day/cell, day-clustered t.\n"
    )
    L.append("| dataset | days | cells | best net t | q_BH | best cell | signflip p95/p99 | boot p95/p99 | "
             "shift p95 (labelled; obs) | q<0.05 | survive gate |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for lab, d in doc["datasets"].items():
        b = d["best_cell"]
        nb, ns = d["null_bootstrap"]["max_t"], d["null_day_shift_labelled"]["max_t"]
        nf = d["null_signflip"]["max_t"]
        L.append(
            f"| {lab}{'*' if d['calendar_provisional'] else ''} | {d['train_days']} | {d['n_cells']} "
            f"| {_f(b['t'])} | {_f(b['q_bh'], 3)} | {b['cell_id']} n={b['n']} mean={_f(b['mean_net'], 3)} "
            f"| {_f(nf['p95'])}/{_f(nf['p99'])} | {_f(nb['p95'])}/{_f(nb['p99'])} | {_f(ns['p95'])} (obs {_f(ns['observed'])}) "
            f"| {d['n_q_lt_0.05']} | {d['gate']['n_survive']}/{d['gate']['found']} |"
        )
    L.append("\n`*` provisional calendar. Bootstrap null = day-resampled, centred (all cells); "
             "day-shift null only speaks for labelled cells (DOW/conditioned).\n")
    pl = doc["pooled"]
    L.append(
        f"Pooled (primary datasets, max over markets): observed best net t {_f(pl['observed_max_t'])} "
        f"vs signflip null p50/p95/p99 {_f(pl['flip']['p50'])}/{_f(pl['flip']['p95'])}/{_f(pl['flip']['p99'])} "
        f"(bootstrap {_f(pl['boot']['p50'])}/{_f(pl['boot']['p95'])}/{_f(pl['boot']['p99'])}); "
        f"pooled BH survivors (q<0.05): {pl['n_q_lt_0.05']} of {pl['n_cells']:,}.\n"
    )
    L.append("## Top 5 cells per dataset (net t, Train)\n")
    for lab, d in doc["datasets"].items():
        L.append(f"**{lab}**: " + "; ".join(
            f"{c['cell_id']} t={_f(c['t'])} m={_f(c['mean_net'], 3)} comb={_f(c['mean_comb'], 3)} "
            f"shock={_f(c['mean_shock'], 3)} chunks+={c['chunks_same_sign']}/4" for c in d["top_cells"][:5]
        ) + "\n")
    L.append("## Cross-market consistency (mean net ATR / mean t over the family's cells)\n")
    marks = list(doc["consistency"]["_markets"])
    L.append("| family | " + " | ".join(marks) + " | #mkts>0 |")
    L.append("|---|" + "---|" * (len(marks) + 1))
    for f, v in doc["consistency"].items():
        if f == "_markets":
            continue
        cells = []
        for m in marks:
            r = v["markets"].get(m)
            cells.append("-" if r is None else f"{r['mean_net_atr']:+.3f}/{r['mean_t']:+.1f}")
        L.append(f"| {f} | " + " | ".join(cells) + f" | {v['n_positive_mean']}/{v['n_markets']} |")
    L.append("\n## Spread-spike vs sweep-like bars (Train bars)\n")
    L.append("| dataset | spike bars | sweep rate spike/non | obs/exp by slot | z (slot-stratified) |")
    L.append("|---|---|---|---|---|")
    for lab, d in doc["datasets"].items():
        s = d["spread_spike"]
        L.append(f"| {lab} | {s['n_spike_bars']} | {_f(s['sweep_rate_spike'], 3)}/{_f(s['sweep_rate_nonspike'], 3)} "
                 f"| {_f(s['observed_over_expected_by_slot'])} | {_f(s['z_slot_stratified'])} |")
    L.append("\n## Caveats\n")
    for c in doc["caveats"]:
        L.append(f"- {c}")
    return "\n".join(L) + "\n"
