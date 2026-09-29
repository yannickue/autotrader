"""Render research/reports/ar1_phase1/report.md from the runner's JSON/CSV outputs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("research/reports/ar1_phase1")
KEYS = [
    "trades", "trades_per_day", "median_trades_per_day", "zero_trade_days", "net_pnl_eur",
    "expectancy_eur", "expectancy_r", "expectancy_r_ci95_trade_iid",
    "expectancy_r_ci95_day_clustered", "profit_factor", "win_rate", "avg_winner_eur",
    "avg_loser_eur", "payoff_ratio", "max_drawdown_eur", "max_drawdown_pct",
    "max_consecutive_losses", "worst_day_eur", "best_day_eur", "exposure_time_frac",
    "avg_holding_minutes", "avg_leverage", "max_leverage", "avg_entry_spread_pts",
    "est_execution_cost_eur", "cost_per_trade_eur",
]  # fmt: skip


def table(rows: list[dict], cols: list[str]) -> str:
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    return head + "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows)


def main() -> None:
    cands = json.loads((OUT / "candidates.json").read_text())
    rec = json.loads((OUT / "run_record.json").read_text())
    prov = json.loads((OUT / "dataset_provenance.json").read_text())
    grid = pd.read_csv(OUT / "grid_runs.csv")
    L: list[str] = [
        "# AR1 Phase 1 - GER40 research report (RESEARCH ONLY, nothing production-ready)\n"
    ]
    L.append(f"git: `{rec['git_commit']}`  fingerprint: `{rec['experiment_fingerprint'][:16]}`")
    L.append(f"seed {rec['seed']}, versions {rec['versions']}\n")
    L.append("## Dataset\n")
    L.append(
        f"{prov['n_bars']} M5 BID bars, {prov['first_bar_utc']} .. {prov['last_bar_utc']}, "
        f"ActivTrades DEMO Ger40. Bars per partition: {rec['bars']}. Splits: {rec['split_plan']}"
    )
    L.append("\nExcluded months (failed validation / no data): " + ", ".join(
        e["month"] for e in prov["excluded_months"]) + "\n")  # fmt: skip
    L.append(table(prov["months"], ["month", "rows", "validation_status", "warning_summary",
                                     "content_sha256"]))  # fmt: skip
    L.append("\nReviewed gaps admitted: " + json.dumps(prov["reviewed_gaps"]) + "\n")
    L.append("## Research runs\n")
    L.append(f"{json.dumps(rec['research_runs'])}\n")
    L.append("## Development grid (BASE costs, train / validation) - family summary\n")
    rows = []
    for fam, g in grid.groupby("family"):
        rows.append({
            "family": fam, "variants": len(g),
            "train exp_R>0": f"{(g.train_expectancy_r > 0).mean():.0%}",
            "val exp_R>0": f"{(g.validation_expectancy_r > 0).mean():.0%}",
            "both>0": f"{((g.train_expectancy_r > 0) & (g.validation_expectancy_r > 0)).mean():.0%}",  # noqa: E501
            "median train R": round(g.train_expectancy_r.median(), 3),
            "median val R": round(g.validation_expectancy_r.median(), 3),
            "max trades/day (train)": round(g.train_trades_per_day.max(), 2),
        })  # fmt: skip
    L.append(table(rows, list(rows[0])))
    hi = grid[grid.train_trades_per_day >= 2.0]
    L.append(
        f"\nTrade cadence: {len(hi)} of {len(grid)} variants reach >= 2 trades/day in train; "
        f"{int(((hi.train_expectancy_r > 0) & (hi.validation_expectancy_r > 0)).sum())} of them "
        "have positive expectancy in both train and validation. Full per-variant table: "
        "`grid_runs.csv` (parameter-stability map).\n"
    )
    for c in cands:
        L.append(f"## {c['role']} - {c['id']} exit {c['exit']}\n")
        L.append(f"Verdict: **{c['verdict']['verdict']}**. {c['selection_note']}")
        fails = [k for k, v in c["verdict"]["checks"].items() if not v]
        L.append(f"Failed checks: {fails or 'none'}. Pooled PF (train+val+oos): "
                 f"{c['pooled_profit_factor']}\n")  # fmt: skip
        L.append("### Metrics at BASE cost\n")
        cols = ["metric", "train", "validation", "oos"]
        b = c["cost"]["BASE"]
        L.append(table([{"metric": k, **{p: b[p].get(k) for p in ("train", "validation", "oos")}}
                        for k in KEYS], cols))  # fmt: skip
        L.append("\n### Cost stress (expectancy R / profit factor / net EUR)\n")
        rows = []
        for scen, parts in c["cost"].items():
            rows.append({"scenario": scen, **{
                p: f"{m.get('expectancy_r')} / {m.get('profit_factor')} / {m.get('net_pnl_eur')}"
                for p, m in parts.items()}})  # fmt: skip
        L.append(table(rows, ["scenario", "train", "validation", "oos"]))
        for label, key in (("development (train+validation)", "dev"), ("OOS", "oos")):
            bd = c[f"breakdowns_{key}"]
            if not bd:
                continue
            for name in ("trend", "vol", "session"):
                t = bd[name]
                L.append(f"\n### {name} breakdown - {label}\n")
                L.append(table(t["rows"], [name, "n", "expectancy_r", "se_r", "win_rate",
                                           "profit_factor", "net_pnl_eur"]))  # fmt: skip
                L.append(f"\nmaterial difference: `{json.dumps(t['material'])}`")
        L.append("\n### MFE/MAE\n")
        L.append(
            f"dev: `{json.dumps(c['mfe_mae_dev'])}`\n\noos: `{json.dumps(c['mfe_mae_oos'])}`\n"
        )
    (OUT / "report.md").write_text("\n".join(L), encoding="utf-8")
    print("wrote", OUT / "report.md")


if __name__ == "__main__":
    main()
