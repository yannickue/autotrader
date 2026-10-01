# ruff: noqa: E501
"""Observer historical backfill CLI (OFFLINE / RESEARCH ONLY; never touches the live trader, artifacts/, MT5 or schedulers).

    uv run python scripts/observer_backfill.py --market GER40 [--market NAS100 ...] [--out DIR] [--limit N] [--seed S]
        [--phase2-root <dir with markets/manifest_BTCUSD.json>] [--data-root <dir with markets/>] [--force] [--jobs 1] [--step all|events|controls]

One market at a time (``--jobs`` default 1, max 2 = one market per worker process). Development bars only (the 2026-08-31 guard of the existing loaders,
re-asserted by the backfill). Output per market: ``<out>/<MARKET>/{table,events,features,labels}.parquet`` + ``manifest.json`` (events step) and
``controls{,_events,_features,_labels}.parquet`` + ``controls_manifest.json`` (controls step, re-runnable alone with ``--step controls``) + ``backfill.log``.
Idempotent per step: a complete step with an identical fingerprint is skipped.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_OUT = Path("C:/Users/yanni/AppData/Local/Temp/observer_backfill")
ACTIVE = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD", "BTCUSD", "BRENT")


def run_one(market: str, out: str, seed: int, limit: int | None, data_root: str | None, p2root: str | None, force: bool, steps: tuple[str, ...]) -> dict:
    import entry_exit_quality as X

    from coverage_analysis.observer_lab.backfill import run_market_backfill
    from markets.phase2 import load_phase2_spec
    from markets.spec import CANONICALS, PHASE2_CANONICALS, load_market_spec

    mi = X.build_market_inputs(market, 0, data_root, p2root)
    if mi is None:
        return {"market": market, "status": "NO_DATA", "note": "no Phase-2 data root with markets/manifest_<M>.json found (pass --phase2-root)"}
    if market in CANONICALS:
        ms = load_market_spec(market)
    elif market in PHASE2_CANONICALS:
        ms = load_phase2_spec(market)
    else:
        raise SystemExit(f"unknown market {market!r}")
    return run_market_backfill(mi, ms, out, seed=seed, limit=limit, force=force, steps=steps)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", action="append", default=None, help=f"one of {', '.join(ACTIVE)}; repeatable; default all (sequentially)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output root OUTSIDE git (default: %%LOCALAPPDATA%%\\Temp\\observer_backfill)")
    ap.add_argument("--limit", type=int, default=None, help="smoke runs: keep only the first N events (controls follow)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--phase2-root", default=None)
    ap.add_argument("--jobs", type=int, default=1, help="1 (default) or 2; one market per worker process")
    ap.add_argument("--force", action="store_true", help="rebuild even if a complete identical run exists")
    ap.add_argument("--step", choices=("all", "events", "controls"), default="all", help="controls = re-run ONLY the control step (needs a complete events step)")
    a = ap.parse_args(argv)
    if not 1 <= a.jobs <= 2:
        raise SystemExit("--jobs must be 1 or 2 (8 GB RAM)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    import entry_exit_quality as X

    p2root = X._find_phase2_root(a.phase2_root)
    p2 = None if p2root is None else str(p2root)
    steps = ("events", "controls") if a.step == "all" else (a.step,)
    markets = a.market or list(ACTIVE)
    for m in markets:
        if m not in ACTIVE:
            raise SystemExit(f"unknown market {m!r}")
    if a.jobs > 1 and len(markets) > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            res = [f.result() for f in [ex.submit(run_one, m, a.out, a.seed, a.limit, a.data_root, p2, a.force, steps) for m in markets]]
    else:
        res = [run_one(m, a.out, a.seed, a.limit, a.data_root, p2, a.force, steps) for m in markets]
    for r in res:
        if r.get("status") == "NO_DATA":
            print(f"{r['market']}: NO_DATA - {r['note']}")
            continue
        if "events" in r:
            ev = r["events"]
            print(f"{r['market']}: events step {r.get('status_this_call')} events={ev['n_events']} rows={r['rows']} runtime={r['runtime_s']}s peak_mb={r['peak_memory_mb']}")
        c = r.get("controls")
        if c:
            print(f"{r.get('market', '')}: controls step {c.get('status_this_call')} controls={c['n_controls']} match_rate={c['match_report']['match_rate']:.3f} runtime={c['runtime_s']}s peak_mb={c['peak_memory_mb']}")
    return 0 if all(r.get("status") != "NO_DATA" for r in res) else 2


if __name__ == "__main__":
    raise SystemExit(main())
