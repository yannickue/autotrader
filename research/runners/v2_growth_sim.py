# ruff: noqa: E501
"""Capital-growth grid on an R stream (CSV/npz/parquet) or synthetic streams. Research only.

This scales a GIVEN stream; it does not create edge and guarantees nothing (see docs/V2_GROWTH.md).

    python research/runners/v2_growth_sim.py --synthetic --label synth
    python research/runners/v2_growth_sim.py --stream trades.csv --label my_stream
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from alpha.growth import (  # noqa: E402
    DrawdownThrottle,
    FixedFraction,
    FractionalKelly,
    RampAfterWins,
    SimConfig,
    load_stream,
    lot_sizing_for,
    required_fraction,
    simulate,
    summarize,
    synthetic_stream,
)
from alpha.growth.engine import analytic_block  # noqa: E402

DEFAULT_CONFIG = _ROOT / "research" / "configs" / "v2_growth.json"


def _schedules(cfg: dict, se: float) -> list:
    dyn = cfg["dynamic"]
    out: list = [FixedFraction(f) for f in cfg["fixed_fractions"]]
    out += [FractionalKelly(k, shrink_se=dyn["kelly_shrink_se"], se=se) for k in dyn["kelly_fractions"]]
    out.append(DrawdownThrottle(dyn["dd_throttle_base"], dyn["dd_throttle_threshold"]))
    out.append(RampAfterWins(**dyn["ramp"]))
    return out


def run_stream(stream, cfg: dict) -> dict:
    _mean, se = stream.mean_se()
    tpd = stream.n_trades / max(stream.total_days or len(set(stream.day.tolist())), 1)
    lots = lot_sizing_for(cfg["market"], leverage_cap=cfg["leverage_cap"]) if stream.stop_pts is not None else None
    policies = [p for p in cfg["policies"] if p == "ideal" or lots is not None]
    res: dict = {
        "label": stream.label, "n_trades": stream.n_trades, "trades_per_day": tpd,
        "analytic": analytic_block(stream, tuple(cfg["fixed_fractions"])),
        "required_fraction_to_target": {
            f"{t:g}": required_fraction(stream.r, trades_per_day=tpd, days=cfg["horizons"][-1],
                                        start=cfg["start_equity"], target=t)
            for t in cfg["targets"]
        },
        "runs": [],
    }
    for edge in cfg["edge_modes"]:
        for pol in policies:
            for sched in _schedules(cfg, se):
                sim = SimConfig(
                    scheme=cfg["scheme"], policy=pol, horizons=tuple(cfg["horizons"]),
                    n_paths=cfg["n_paths"], seed=cfg["seed"], start_equity=cfg["start_equity"],
                    targets=tuple(cfg["targets"]), edge_uncertainty=edge, lots=lots,
                    leverage_cap=cfg["leverage_cap"], ruin_frac=cfg["ruin_frac"],
                    block_len=cfg["block_len"], max_trades_per_day=cfg["max_trades_per_day"],
                )
                res["runs"].append(summarize(simulate(stream, sched, sim)))
    return res


def _md(all_res: list[dict], cfg: dict) -> str:
    h = str(cfg["horizons"][-1])
    big = f"{cfg['targets'][-1]:g}"
    L = [f"# Growth simulation ({cfg['start_equity']:g} EUR start, {h} trading days)", "",
         "NOT A FORECAST. Scales a given R stream; creates no edge; no target is guaranteed.", ""]
    for r in all_res:
        a = r["analytic"]
        L += [f"## {r['label']}  (n={r['n_trades']}, {r['trades_per_day']:.2f} trades/day)", "",
              f"mean R {a['mean_r']:+.3f}, SE {a['se_mean_r']:.3f}, Kelly f* {a['f_star']:.3%} "
              f"(full Kelly is too aggressive under estimation error)", ""]
        for run_edge in cfg["edge_modes"]:
            for pol in sorted({x["policy"] for x in r["runs"]}):
                L += [f"### policy={pol}, edge_uncertainty={run_edge}", "",
                      f"| schedule | median end | p5 | p95 | P({big}) | P(ruin) | P(DD>=50%) |",
                      "|---|---:|---:|---:|---:|---:|---:|"]
                for x in r["runs"]:
                    if x["policy"] != pol or x["edge_uncertainty"] != run_edge:
                        continue
                    hh = x["horizons"][h]
                    ec = hh["ending_capital"]
                    L.append(f"| {x['schedule']} | {ec['p50']:.0f} | {ec['p5']:.0f} | {ec['p95']:.0f} | "
                             f"{hh['p_reach'][big]:.3f} | {hh['p_ruin']:.3f} | {hh['p_dd_ge_50']:.3f} |")
                L.append("")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--stream", help="CSV/npz/parquet with r_multiple + day (+ risk_pts, entry_price)")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--label", default="run")
    ap.add_argument("--total-days", type=int, default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    streams = []
    if args.stream:
        streams.append(load_stream(args.stream, label=args.label, total_days=args.total_days))
    if args.synthetic:
        s = cfg["synthetic"]
        for m in s["means"]:
            streams.append(synthetic_stream(
                s["n_trades"], mean_r=m, win_rate=s["win_rate"], trades_per_day=s["trades_per_day"],
                stop_pts=s["stop_pts"], label=f"synthetic_mean{m:+.2f}R"))
    if not streams:
        ap.error("give --stream and/or --synthetic")
    out = Path(args.out) if args.out else _ROOT / "research" / "reports" / "v2_growth" / args.label
    out.mkdir(parents=True, exist_ok=True)
    all_res = [run_stream(s, cfg) for s in streams]
    (out / "growth_summary.json").write_text(json.dumps({"config": cfg, "results": all_res}, indent=1),
                                             encoding="utf-8")
    (out / "growth_summary.md").write_text(_md(all_res, cfg), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
