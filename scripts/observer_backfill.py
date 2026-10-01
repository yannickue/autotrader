# ruff: noqa: E501
"""Observer historical backfill CLI (OFFLINE / RESEARCH ONLY; never touches the live trader, artifacts/, MT5 or schedulers).

    uv run python scripts/observer_backfill.py --market GER40 [--market NAS100 ...] [--out DIR] [--limit N] [--seed S]
        [--phase2-root <dir with markets/manifest_BTCUSD.json>] [--data-root <dir with markets/>] [--force] [--jobs 1] [--step all|events|controls]

``--jobs`` (default 1, max 3 = one market / segment per worker process; the effective count is also capped by free memory).
``--segmented`` (control method 3): the run is cut into market x stage segments (events, controls3_a, controls3_b) that run as independent processes (dependency: controls need
their market's events), each committed by an atomic segment manifest with an ARTIFACT_ID fingerprint (data, feature code, control code, config, label version) = CACHE HIT / resume
after an abort / explicit invalidation reasons; status + heartbeat in ``<out>/_status.json``; results are bit-identical to the serial run (docs/RESEARCH_SPEED.md).
One market at a time otherwise (legacy path). Development bars only (the 2026-08-31 guard of the existing loaders,
re-asserted by the backfill). Output per market: ``<out>/<MARKET>/{table,events,features,labels}.parquet`` + ``manifest.json`` (events step) and
``controls{,_events,_features,_labels}.parquet`` + ``controls_manifest.json`` (controls step, re-runnable alone with ``--step controls``; default ``--control-method 3`` writes ``<MARKET>/controls3/`` (+ ``controls3_b/``) and leaves the controls-2 files untouched) + ``backfill.log``.
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


def _init_worker(src: str) -> None:
    """Pool initializer: scripts/ is first on sys.path in a spawned child (the script body re-runs there) and scripts/coverage_analysis.py would shadow the src
    package of the same name when the task function is unpickled; put src in front before the first task arrives."""
    sys.path.insert(0, src)


def run_one(market: str, out: str, seed: int, limit: int | None, data_root: str | None, p2root: str | None, force: bool, steps: tuple[str, ...], exclusion_bars: int, control_method: str = "3", n_controls: int = 1, with_b: bool = True) -> dict:
    import entry_exit_quality as X

    from coverage_analysis.observer_lab.backfill import run_market_backfill
    from coverage_analysis.observer_lab.controls import MatchSpec
    from coverage_analysis.observer_lab.controls_sametime import SameTimeSpec
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
    return run_market_backfill(mi, ms, out, seed=seed, limit=limit, force=force, steps=steps, match_spec=MatchSpec(exclusion_bars=exclusion_bars), control_method=control_method, sametime_spec=SameTimeSpec(n_controls=n_controls), with_controls_b=with_b)


def run_segmented(a: argparse.Namespace, markets: list[str], steps: tuple[str, ...], p2: str | None) -> int:
    """Segment-level run (see module docstring); returns 0 when every segment is built / cache hit, 2 on NO_DATA, 3 on a failed segment."""
    from pathlib import Path as _P

    from coverage_analysis.observer_lab.backfill_segments import (
        plan_segments,
        run_segment,
        segment_id,
    )
    from coverage_analysis.observer_lab.controls_sametime import SameTimeSpec
    from research_speed.runlock import EXIT_LOCKED, RunLock, RunLockError
    from research_speed.scheduler import Task

    if a.control_method != "3":
        raise SystemExit("--segmented supports --control-method 3 only")
    with_b = not a.no_controls_b
    plan = plan_segments(markets, steps, with_b)
    st = SameTimeSpec(n_controls=a.n_controls)
    tasks = [Task(segment_id(m, s), run_segment, {"market": m, "stage": s, "out": a.out, "seed": a.seed, "limit": a.limit, "data_root": a.data_root, "p2root": p2, "force": a.force, "adopt": a.adopt_existing,
                                                "with_b": with_b, "sametime_spec": st}, deps) for m, s, deps in plan]
    out = _P(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        with RunLock(out / "_run.lock"):
            return _run_segmented_locked(a, markets, steps, tasks, out)
    except RunLockError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return EXIT_LOCKED


def _run_segmented_locked(a: argparse.Namespace, markets: list[str], steps: tuple[str, ...], tasks: list, out: Path) -> int:
    import time

    from research_speed.parallel import describe
    from research_speed.progress import StatusFile
    from research_speed.scheduler import run_dag

    jobs = describe(a.jobs, len(tasks))
    print(f"segmented run: {len(tasks)} segments, jobs={jobs}", flush=True)
    t0 = time.monotonic()
    with StatusFile(out / "_status.json", time.strftime("%Y%m%dT%H%M%S"), [t.id for t in tasks], meta={"jobs": jobs, "markets": markets, "steps": list(steps), "limit": a.limit, "seed": a.seed}) as status:
        results, errors = run_dag(tasks, a.jobs, status, initializer=_init_worker, initargs=(str(ROOT / "src"),))
    for t in tasks:
        r = results.get(t.id)
        if r is None:
            print(f"{t.id}: FAILED {errors.get(t.id, '').strip().splitlines()[-1] if errors.get(t.id) else '?'}")
        elif r.get("status") == "NO_DATA":
            print(f"{t.id}: NO_DATA - {r['note']}")
        else:
            print(f"{t.id}: {r['status']} ({r.get('reason')}) wall={r['wall_s']}s artifact={r['artifact_id'][:12]}" + (f" peak_mb={r['peak_memory_mb']}" if r.get("peak_memory_mb") else ""))
    print(f"total wall {time.monotonic() - t0:.1f}s")
    if errors:
        return 3
    return 2 if any(r.get("status") == "NO_DATA" for r in results.values()) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", action="append", default=None, help=f"one of {', '.join(ACTIVE)}; repeatable; default all (sequentially)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output root OUTSIDE git (default: %%LOCALAPPDATA%%\\Temp\\observer_backfill)")
    ap.add_argument("--limit", type=int, default=None, help="smoke runs: keep only the first N events (controls follow)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--phase2-root", default=None)
    ap.add_argument("--jobs", type=int, default=1, help="1 (default) .. 3; one market (legacy) or one market x stage segment (--segmented) per worker process")
    ap.add_argument("--reserve-mb", type=float, default=None, help="memory (MB) that must stay free for the rest of the machine when sizing the worker count (default 1500; env RESEARCH_SPEED_RESERVE_MB)")
    ap.add_argument("--segmented", action="store_true", help="checkpointed market x stage segments with fingerprint cache + status file (control method 3 only)")
    ap.add_argument("--adopt-existing", action="store_true", help="--segmented: commit segment manifests for COMPLETE legacy outputs without recomputing (code identity NOT verified; recorded as such)")
    ap.add_argument("--force", action="store_true", help="rebuild even if a complete identical run exists")
    ap.add_argument("--exclusion-bars", type=int, default=48, help="control exclusion radius around every opportunity (default 48 = the label horizon; smaller values = sensitivity runs only)")
    ap.add_argument("--control-method", choices=("2", "3"), default="3", help="3 (default) = observer-controls-3 (same Berlin clock time, other day; files under <MARKET>/controls3[_b]/ + blocking balance gate); 2 = legacy exclusion-radius controls")
    ap.add_argument("--n-controls", type=int, default=1, help="controls-3: controls per event (default 1)")
    ap.add_argument("--no-controls-b", action="store_true", help="controls-3: skip the disjoint A/A set B")
    ap.add_argument("--step", choices=("all", "events", "controls"), default="all", help="controls = re-run ONLY the control step (needs a complete events step)")
    a = ap.parse_args(argv)
    from research_speed.parallel import MAX_WORKERS, clamp_jobs, harden_process, managed_pool

    if a.reserve_mb is not None:
        import os

        os.environ["RESEARCH_SPEED_RESERVE_MB"] = str(a.reserve_mb)

    if not 1 <= a.jobs <= MAX_WORKERS:
        raise SystemExit(f"--jobs must be 1..{MAX_WORKERS} (8 GB RAM)")
    a.jobs = harden_process(a.jobs)  # FIRST: low priority, 1 BLAS thread, fail-closed memory, jobs=1 next to the live trader, workers die with this process
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    import entry_exit_quality as X

    p2root = X._find_phase2_root(a.phase2_root)
    p2 = None if p2root is None else str(p2root)
    steps = ("events", "controls") if a.step == "all" else (a.step,)
    markets = a.market or list(ACTIVE)
    for m in markets:
        if m not in ACTIVE:
            raise SystemExit(f"unknown market {m!r}")
    if a.segmented:
        return run_segmented(a, markets, steps, p2)
    if a.jobs > 1 and len(markets) > 1:
        with managed_pool(clamp_jobs(a.jobs, len(markets)), _init_worker, (str(ROOT / "src"),)) as ex:
            res = [f.result() for f in [ex.submit(run_one, m, a.out, a.seed, a.limit, a.data_root, p2, a.force, steps, a.exclusion_bars, a.control_method, a.n_controls, not a.no_controls_b) for m in markets]]
    else:
        res = [run_one(m, a.out, a.seed, a.limit, a.data_root, p2, a.force, steps, a.exclusion_bars, a.control_method, a.n_controls, not a.no_controls_b) for m in markets]
    for r in res:
        if r.get("status") == "NO_DATA":
            print(f"{r['market']}: NO_DATA - {r['note']}")
            continue
        if "events" in r:
            ev = r["events"]
            print(f"{r['market']}: events step {r.get('status_this_call')} events={ev['n_events']} rows={r['rows']} runtime={r['runtime_s']}s peak_mb={r['peak_memory_mb']}")
        c = r.get("controls")
        if c:
            print(f"{r.get('market', '')}: controls step {c.get('status_this_call')} controls={c['n_controls']} match_rate={c['match_report']['match_rate']:.3f} runtime={c['runtime_s']}s peak_mb={c['peak_memory_mb']}" + (f" market_status={c['market_status']}" if "market_status" in c else ""))
    return 0 if all(r.get("status") != "NO_DATA" for r in res) else 2


if __name__ == "__main__":
    raise SystemExit(main())
