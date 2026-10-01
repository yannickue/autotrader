# ruff: noqa: E501
"""Lane X CLI: entry quality vs exit quality + SAME-entry exit-policy shadow comparison (offline, hindsight diagnostics).

    uv run python scripts/entry_exit_quality.py --days 0 --out docs/evidence \
        [--data-root <dir with markets/>] [--phase2-root <dir with markets/manifest_BTCUSD.json>] \
        [--db <scratch COPY of demo.sqlite>] [--markets GER40 ...] [--shadow-root <dir of <CANONICAL>.parquet>]

Reads ONLY development bars through the existing loaders (the 2026-08-31 holdout guard stays in force), the frozen
production spec (v1.2), the live operating policy and - optionally - a COPY of the demo DB through the existing readers.
Writes only ``<out>/entry_exit_quality.{md,json}`` and ``<out>/entry_exit_quality_<MARKET>.md``.  Never calls MT5,
never opens ``artifacts/``, never places orders.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alpha.common.market_data import DEV_END, dev_frame, load_dev_market_frame  # noqa: E402
from alpha.families import leadlag  # noqa: E402
from alpha.families.data import build_family_data, build_leader_features  # noqa: E402
from alpha.families.spec import MarketCalendar  # noqa: E402
from coverage_analysis.entry_exit import (  # noqa: E402
    MarketInputs,
    aggregate_market,
    build_entry_rows,
    market_cluster_rollup,
    meta_block,
    r2_summary,
    render_markdown,
    render_market_md,
    to_json,
)
from demo.execution.risk_policy import cluster_of  # noqa: E402
from demo.opportunity.operating_policy import load_operating_policy  # noqa: E402
from demo.opportunity.production_spec import load_production_spec_for  # noqa: E402
from markets.phase2 import load_phase2_spec  # noqa: E402
from markets.shadow import load_shadow_specs  # noqa: E402
from markets.spec import CANONICALS, PHASE2_CANONICALS, load_market_spec  # noqa: E402

ACTIVE_MARKETS = (*CANONICALS, *PHASE2_CANONICALS)  # GER40 NAS100 SPX500 XAUUSD EURUSD + BTCUSD BRENT
WARMUP_DAYS = 30
# Phase-2 offline calendars: FULL-DAY entry window (session windows are operating policy, not family logic) - as Lane F.
PHASE2_CALS = {
    "BTCUSD": MarketCalendar("UTC", 0, 1440, 0, 1440, 1440),
    "BRENT": MarketCalendar("Europe/London", 0, 1440, 0, 1440, 1440),
}


def _find_phase2_root(explicit: str | None) -> Path | None:
    cands = [Path(explicit)] if explicit else []
    cands += [ROOT / "lane_f_root", ROOT.parent / "f" / "lane_f_root", ROOT.parent.parent / "trader" / "lane_f_root"]
    for c in cands:
        if (c / "markets" / "manifest_BTCUSD.json").is_file():
            return c
    return None


def _cut(frame: pd.DataFrame, days: int) -> tuple[pd.DataFrame, pd.Timestamp]:
    last = pd.Timestamp(frame["ts"].iloc[-1])
    first = pd.Timestamp(frame["ts"].iloc[0])
    eval_from = (last - pd.Timedelta(days=days)) if days > 0 else (first + pd.Timedelta(days=WARMUP_DAYS))
    start = eval_from - pd.Timedelta(days=WARMUP_DAYS)
    cut = frame[pd.DatetimeIndex(frame["ts"]) >= start].reset_index(drop=True)
    return cut, eval_from


def load_market_inputs(m: str, days: int, data_root: str | None, p2root: str | None) -> tuple[MarketInputs | None, pd.DataFrame | None, pd.Timestamp | None, str]:
    """Development frame -> ``MarketInputs`` of one active market (shared with the Lane W shadow-exit-lab CLI).
    Returns (inputs | None, cut frame, eval_from, tz); inputs None = no bar data."""
    prod = load_production_spec_for(tuple(PHASE2_CANONICALS))
    if m in CANONICALS:
        mspecs = {k: load_market_spec(k) for k in CANONICALS}
        ms = mspecs[m]
        need = {m, *leadlag.PAIRS.get(m, ())}
        frames = {k: dev_frame(load_dev_market_frame(mspecs[k], data_root=data_root)) for k in need}
        cut, eval_from = _cut(frames[m], days)
        cal = MarketCalendar.from_market_spec(ms)
        cross = {}
        for ld in leadlag.PAIRS.get(m, ()):
            lf = frames[ld][pd.DatetimeIndex(frames[ld]["ts"]) >= cut["ts"].iloc[0]].reset_index(drop=True)
            cross[ld] = build_leader_features(cut, lf, MarketCalendar.from_market_spec(mspecs[ld]), ld)
        data = build_family_data(cut, cal, name=m, point_size=ms.point_size, tick_size=ms.tick_size, asset_class=ms.asset_class, cross=cross)
    elif m in PHASE2_CANONICALS:
        if p2root is None:
            return None, None, None, ""
        ms = load_phase2_spec(m)
        frame = dev_frame(load_dev_market_frame(ms, data_root=p2root))
        cut, eval_from = _cut(frame, days)
        cal = PHASE2_CALS[m]
        data = build_family_data(cut, cal, name=m, point_size=ms.point_size, tick_size=ms.tick_size, asset_class=ms.asset_class)
    else:
        raise SystemExit(f"unknown active market {m!r}")
    specs = list(prod.specs_for(m))
    return MarketInputs(market=m, data=data, frame=cut, specs=specs, tick_size=float(ms.tick_size), eval_from=eval_from, tz=cal.tz), cut, eval_from, cal.tz


def analyse_market(m: str, days: int, limit: int | None, data_root: str | None, p2root: str | None) -> dict:
    """One market end to end (module level + plain arguments so it can run in a worker process)."""
    t0 = time.time()
    op = load_operating_policy()
    mi, cut, eval_from, _tz = load_market_inputs(m, days, data_root, p2root)
    if mi is None:
        msg = f"## {m}" + chr(10) * 2 + "NO DATA: no Phase-2 data root (lane_f_root) found; pass --phase2-root." + chr(10)
        return {"market": m, "rows": [], "entry": {"markdown": msg, "result": {}, "excl": {}}, "note": f"{m}: NO bar data available (phase-2 root not found)"}
    specs = mi.specs
    rows, excl = build_entry_rows(mi, op, limit=limit)
    res = aggregate_market(rows)
    span = f"{pd.Timestamp(cut['ts'].iloc[0]).date()}..{pd.Timestamp(cut['ts'].iloc[-1]).date()} ({len(cut)} M5 bars; evaluated from {pd.Timestamp(eval_from).date()})"
    data_note = f"Data: {span}; {len(specs)} frozen family specs ({', '.join(sorted({s.family + ':' + str(getattr(s.spec, 'mode', '')) for s in specs}))}); analysed entries n={len(rows)}."
    print(f"{m}: {len(rows)} entries in {time.time() - t0:.0f}s", flush=True)
    return {
        "market": m, "rows": rows, "note": f"{m}: {span}; n_entries={len(rows)}",
        "entry": {"markdown": render_market_md(m, res, data_note, excl), "result": res, "excl": excl, "n_bars": len(cut), "span": span, "n_entries": len(rows)},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--phase2-root", default=None)
    ap.add_argument("--markets", nargs="*", default=list(ACTIVE_MARKETS))
    ap.add_argument("--days", type=int, default=0, help="evaluation window = last N days of the DEV data (0 = all data after a 30-day warm-up)")
    ap.add_argument("--limit", type=int, default=None, help="debug: cap the analysed entries per market")
    ap.add_argument("--jobs", type=int, default=1, help="parallel worker processes (one market each)")
    ap.add_argument("--db", default=None, help="COPY of demo.sqlite (never the live file under artifacts/)")
    ap.add_argument("--shadow-root", default=None, help="optional directory with <CANONICAL>.parquet frames of shadow markets")
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence"))
    ap.add_argument("--tag", default="entry_exit_quality")
    a = ap.parse_args(argv)

    p2root = _find_phase2_root(a.phase2_root)
    p2 = None if p2root is None else str(p2root)
    if a.jobs > 1 and len(a.markets) > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            futs = [ex.submit(analyse_market, m, a.days, a.limit, a.data_root, p2) for m in a.markets]
            done = [f.result() for f in futs]
    else:
        done = [analyse_market(m, a.days, a.limit, a.data_root, p2) for m in a.markets]
    notes: list[str] = []
    results: dict[str, dict] = {}
    all_rows: list[dict] = []
    for d in done:
        results[d["market"]] = d["entry"]
        notes.append(d["note"])
        all_rows.extend(d["rows"])
    # shadow markets: consume any frame that exists; document what is missing
    shadow = load_shadow_specs()
    have = []
    if a.shadow_root:
        for canon in sorted(shadow):
            p = Path(a.shadow_root) / f"{canon}.parquet"
            if p.is_file():
                have.append(canon)
    if have:
        notes.append(f"shadow frames provided for: {', '.join(have)} - NOT analysed by this run (no validated family applicability for them; see docs)")
    notes.append(f"shadow markets: {len(shadow)} shadow specs (configs/markets_shadow) - NO bar history exists in the repo / data junction / Lane F root for any of them; the analysis accepts any MarketInputs frame, so they can be added as soon as shadow bars are recorded (Lane U2 scanner). Family applicability: fit-free STRUCT only; other families need Train-fitted thresholds and valid session semantics -> NO_SETUP until then.")
    caveats = [
        "Hindsight diagnostics on a development window; entries of overlapping signals are not independent (see clusters); no edge claim, no promotion.",
        "GER40/NAS100/SPX500 are strongly correlated (INDEX cluster): their entries share the same market moves; see the market-cluster table.",
        "Fills at the next bar open (long: ask), stop-first bar semantics, spread-adjusted gross R, no commission / slippage / swap; intrabar order of high/low is unknown (conservative: adverse first).",
        "Structural TP1/TP2/trail come from the E2 fractal geometry on closed M5/M15 bars; absent levels -> NOT_APPLICABLE, never invented.",
        f"Development end {DEV_END} (Berlin date) is enforced by the existing loaders; BTCUSD/BRENT have no frozen split, their STRUCT constants are discovery placeholders and BTC M5 history lacks Oct-2025 and Mar-2026 (DST fold months).",
        "Family generators run on Train-fitted frozen thresholds (fit_end 2026-06-30 for the five core markets): the evaluation window overlaps that fit window for part of the data (in-sample for thresholds) - a further reason the numbers are descriptive only.",
        "The exit-policy parameters are predeclared constants (POLICY_SET_VERSION); no threshold was searched or tuned on these results.",
    ]
    result = {"meta": meta_block(notes, caveats), "markets": results, "market_clusters": market_cluster_rollup(all_rows, cluster_of)}
    if a.db:
        result["r2"] = r2_summary(a.db)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.tag}.md").write_text(render_markdown(result), encoding="utf-8")
    (out / f"{a.tag}.json").write_text(to_json(result), encoding="utf-8")
    for m, r in results.items():
        (out / f"{a.tag}_{m}.md").write_text(f"# Lane X - {m}\n\n`{result['meta']['analysis_version']}` hindsight diagnostics, no edge claim.\n\n" + r["markdown"], encoding="utf-8")
    print(f"wrote {out / (a.tag + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
