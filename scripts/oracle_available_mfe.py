# ruff: noqa: E501
"""Lane X2 CLI: ORACLE available_mfe_r diagnostic (ORACLE_RETROSPECTIVE, NEVER_IN_LIVE_DECISION_PATH; offline, hindsight).

    uv run python scripts/oracle_available_mfe.py --days 0 --out docs/evidence [--data-root ...] [--phase2-root ...] [--jobs 4]

Reuses Lane X's entry reconstruction (``scripts/entry_exit_quality.build_market_inputs`` + ``build_entry_rows``) on the
same development bars (2026-08-31 holdout guard unchanged) and adds the stop-independent maximum favourable excursion up
to the live-operating-policy flat horizon.  Writes only ``<out>/oracle_available_mfe.{md,json}``.  No MT5, no
``artifacts/``, no orders.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coverage_analysis.entry_exit import build_entry_rows  # noqa: E402
from coverage_analysis.oracle_mfe import (  # noqa: E402
    aggregate_oracle,
    build_oracle_rows,
    oracle_meta,
    render_oracle_markdown,
    render_oracle_market_md,
    to_oracle_json,
)
from demo.opportunity.operating_policy import load_operating_policy  # noqa: E402
from markets.shadow import load_shadow_specs  # noqa: E402

# Lane X's script is loaded by path: ``scripts/coverage_analysis.py`` would shadow the package if scripts/ were on sys.path.
_spec = importlib.util.spec_from_file_location("_lane_x_entry_exit_quality", ROOT / "scripts" / "entry_exit_quality.py")
_lx = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _lx
_spec.loader.exec_module(_lx)
ACTIVE_MARKETS, _find_phase2_root, build_market_inputs = _lx.ACTIVE_MARKETS, _lx._find_phase2_root, _lx.build_market_inputs


def analyse_market(m: str, days: int, limit: int | None, data_root: str | None, p2root: str | None) -> dict:
    t0 = time.time()
    mi = build_market_inputs(m, days, data_root, p2root)
    if mi is None:
        return {"market": m, "note": f"{m}: NO bar data available (phase-2 root not found)", "entry": {"markdown": f"## {m}\n\nNO DATA (phase-2 root not found).\n", "result": {}, "excl": {}}}
    rows, excl = build_entry_rows(mi, load_operating_policy(), limit=limit)
    orows = build_oracle_rows(mi, rows)
    res = aggregate_oracle(orows)
    cut = mi.frame
    span = f"{pd.Timestamp(cut['ts'].iloc[0]).date()}..{pd.Timestamp(cut['ts'].iloc[-1]).date()} ({len(cut)} M5 bars; evaluated from {pd.Timestamp(mi.eval_from).date()})"
    note = f"Data: {span}; analysed entries n={len(orows)}."
    print(f"{m}: {len(orows)} entries in {time.time() - t0:.0f}s", flush=True)
    return {"market": m, "note": f"{m}: {span}; n_entries={len(orows)}", "entry": {"markdown": render_oracle_market_md(m, res, note, excl), "result": res, "excl": excl, "span": span, "n_entries": len(orows)}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--phase2-root", default=None)
    ap.add_argument("--markets", nargs="*", default=list(ACTIVE_MARKETS))
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence"))
    ap.add_argument("--tag", default="oracle_available_mfe")
    a = ap.parse_args(argv)
    p2root = _find_phase2_root(a.phase2_root)
    p2 = None if p2root is None else str(p2root)
    if a.jobs > 1 and len(a.markets) > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            done = [f.result() for f in [ex.submit(analyse_market, m, a.days, a.limit, a.data_root, p2) for m in a.markets]]
    else:
        done = [analyse_market(m, a.days, a.limit, a.data_root, p2) for m in a.markets]
    notes = [d["note"] for d in done]
    shadow = load_shadow_specs()
    notes.append(f"shadow markets: {len(shadow)} shadow specs exist but NO bar history exists in the repo / data junction / Lane F root for any of them (same finding as Lane X); nothing to analyse yet - the module accepts any MarketInputs frame once shadow bars are recorded.")
    caveats = [
        "ORACLE_RETROSPECTIVE: uses future bars by construction (hindsight); NEVER_IN_LIVE_DECISION_PATH; not tradable foresight; not a signal, filter or threshold source.",
        "No edge claim, no promotion. Large available MFE is also what a driftless path offers: compare with the zero-drift null column; label counts / enrichment on a single dev window are descriptive only.",
        "Development window only (holdout after 2026-08-31 Berlin date never opened); the core-market family thresholds are Train-fitted with fit_end 2026-06-30, so the evaluation window partly overlaps their fit window.",
        "BTCUSD M5 history has gaps (Oct-2025 and Mar-2026 DST-fold months); BTCUSD/BRENT have no frozen split and use discovery-placeholder STRUCT constants.",
        "INDEX-cluster markets (GER40/NAS100/SPX500) share market moves; overlapping signals are not independent (event clusters shown; STRUCT variants of one break are one observation).",
        "Fills at the next bar open (long: ask), spread-adjusted exit-side prices, no commission / slippage / swap; the intrabar order of high/low is unknown.",
        "Horizon = live operating-policy forced-flat instant for every family (the frozen specs carry no own thesis horizon); entries with an incomplete horizon (data gap) are counted in horizon_complete_share.",
        "Null reference: zero-drift Brownian reflection with causal pre-entry sigma; real prices have fat tails / volatility clustering, so it is a yardstick, not an exact law.",
        "Thresholds (levels, small/large opportunity) are predeclared constants (ORACLE_VERSION); nothing was tuned on these results.",
    ]
    result = {"meta": oracle_meta(notes, caveats), "markets": {d["market"]: d["entry"] for d in done}}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.tag}.md").write_text(render_oracle_markdown(result), encoding="utf-8")
    (out / f"{a.tag}.json").write_text(to_oracle_json(result), encoding="utf-8")
    print(f"wrote {out / (a.tag + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
