# ruff: noqa: E501
"""Lane N CLI: offline, read-only retrospective coverage analysis (hindsight diagnostics).

    uv run python scripts/coverage_analysis.py --data-root <main checkout>/data --days 60 \
        --db <scratch copy of demo.sqlite> --out docs/evidence

Reads: development bars via the existing ``load_dev_market_frame`` (the forward-holdout guard stays in force:
nothing after 2026-08-31 is ever opened), the frozen production spec, and -- optionally -- a COPY of the demo
database through the existing ``DemoStore`` readers. Writes only the report files under ``--out``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alpha.common.market_data import load_dev_market_frame  # noqa: E402
from alpha.families import leadlag  # noqa: E402
from alpha.families.data import build_leader_features  # noqa: E402
from alpha.families.spec import MarketCalendar  # noqa: E402
from coverage_analysis.moves import MoveParams  # noqa: E402
from coverage_analysis.r2 import funnel_summary, load_r2  # noqa: E402
from coverage_analysis.replay import DEFAULT_LEVELS, DEFAULT_TOLERANCE  # noqa: E402
from coverage_analysis.report import (  # noqa: E402
    analyse_market,
    meta_params,
    render_markdown,
    to_json,
)
from demo.opportunity.production_spec import load_production_spec  # noqa: E402
from markets.spec import CANONICALS, load_market_spec  # noqa: E402

WARMUP_DAYS = 30  # history before the evaluation window (ATR, D1 context, previous cash day)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default=None, help="directory containing markets/ (read-only)")
    ap.add_argument("--markets", nargs="*", default=list(CANONICALS))
    ap.add_argument("--days", type=int, default=60, help="evaluation window = last N calendar days of the DEV data")
    ap.add_argument("--n-atr", type=float, default=3.0)
    ap.add_argument("--m-bars", type=int, default=24)
    ap.add_argument("--max-adverse-atr", type=float, default=1.0)
    ap.add_argument("--swing-k", type=int, default=6)
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    ap.add_argument("--seed", type=int, default=20260930)
    ap.add_argument("--db", default=None, help="COPY of demo.sqlite (never the live file under artifacts/)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence"))
    ap.add_argument("--tag", default="lane_n_coverage")
    a = ap.parse_args(argv)

    prod = load_production_spec()
    mspecs = {m: load_market_spec(m) for m in CANONICALS}
    frames = {m: load_dev_market_frame(mspecs[m], data_root=a.data_root) for m in CANONICALS}
    p = MoveParams(n_atr=a.n_atr, m_bars=a.m_bars, max_adverse_atr=a.max_adverse_atr, swing_k=a.swing_k)
    r2 = load_r2(a.db) if a.db else []
    results = []
    span: list[str] = []
    for m in a.markets:
        fr = frames[m]
        last = pd.Timestamp(fr["ts"].iloc[-1])
        eval_from = last - pd.Timedelta(days=a.days)
        start = eval_from - pd.Timedelta(days=WARMUP_DAYS)
        cut = fr[pd.DatetimeIndex(fr["ts"]) >= start].reset_index(drop=True)
        cross = {}
        for ld in leadlag.PAIRS.get(m, ()):
            lf = frames[ld]
            lf = lf[pd.DatetimeIndex(lf["ts"]) >= start].reset_index(drop=True)
            cross[ld] = build_leader_features(cut, lf, MarketCalendar.from_market_spec(mspecs[ld]), ld)
        res = analyse_market(
            m, cut, mspecs[m], list(prod.specs_for(m)), eval_from=eval_from, cross=cross, params=p,
            tolerance=a.tolerance, r2_rows=r2, seed=a.seed,
        )
        results.append(res)
        span.append(f"{m} {eval_from.date()}..{last.date()}")
    overlap = sum(1 for r in r2 if any(r.market == res["market"] for res in results))
    meta = {
        "params": meta_params(p), "tolerance": a.tolerance, "seed": a.seed, "grid": list(DEFAULT_LEVELS),
        "data": "existing dev bars (load_dev_market_frame, holdout guard active: nothing after 2026-08-31); spans " + "; ".join(span),
        "r2": (f"{len(r2)} opportunities read via DemoStore.funnel_rows/counterfactual_rows/get_outcome from a copy; funnel summary {funnel_summary(a.db)}" if a.db else "not provided"),
        "sample": f"{sum(r['moves'] for r in results)} moves, {sum(r['replay_signals'] for r in results)} replayed-signal instances, {sum(r['near_miss_moves'] for r in results)} near_miss_moves (unit: moves), {sum(r['near_miss_control_instances'] for r in results)} near_miss_control_instances (unit: relaxed-condition instances); seed {a.seed}; R2 opportunities in the analysed markets: {overlap}",
    }
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.tag}.md").write_text(render_markdown(results, meta), encoding="utf-8")
    (out / f"{a.tag}.json").write_text(to_json(results, meta), encoding="utf-8")
    print(f"wrote {out / (a.tag + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
