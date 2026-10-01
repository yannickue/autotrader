# ruff: noqa: E501
"""ActivTrades DEMO-only runner entry point.

Modes are mutually exclusive.  Exit codes: 0 success; 2 bad arguments, missing explicit DEMO
authorization or MT5_ALLOW_ACCOUNT_LOGIN=1; 3 no heartbeat / NOT RUNNING (--status); 7 fail-closed
stop (after an orderly shutdown); 8 the real MT5 DEMO stack (demo.execution.live.Mt5DemoStack) is
not available in this checkout; 9 a second ``--shadow``/``--demo-auto`` on the same artifacts directory was refused
(single-instance lock ``<artifacts>/runner.lock``, PID + process creation time, stale locks are taken over).

* ``--preflight``    read-only C7 preflight.
* ``--shadow``       full loop (bars -> opportunities -> decisions -> counterfactuals -> shadow learning),
                     ZERO orders (stack constructed in shadow mode, runner never creates intents).
* ``--demo-auto --confirm-demo-auto=I-AUTHORIZE-ACTIVTRADES-DEMO-TRADING-ONLY``
                     same loop, accepted opportunities are traded on the ActivTrades DEMO account.
* ``--demo-auto --flatten-only``  Lane R EOD recovery: flatten own-magic exposure only, never opens anything, exit 0 when flat.
* ``--print-operating-policy``  per-market effective flat deadline + derived entry runway (read-only diagnostic).
* ``--status``       print the heartbeat + verdict (older than 90 s => NOT RUNNING).
* ``--analyze``      offline report (+ parquet export) over the recorded DEMO data; ``--phase`` filters.
* ``--restart-proof`` the proven C7 restart worker.
* ``--record-canary FILE`` import manual / canary / test trades (broker deal-history fields as JSON) into the
                     DemoStore as EXECUTION_CANARY (censored, excluded from alpha metrics). Never touches the broker.

This module never logs in and there is no live-account path.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(Path(__file__).resolve().parent / "autostart") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "autostart"))

import instance_lock  # noqa: E402  (scripts/autostart/instance_lock.py; pure stdlib)

ARTIFACTS = REPO_ROOT / "artifacts" / "demo_trader"
HEARTBEAT = ARTIFACTS / "heartbeat.json"
CONFIRMATION = "I-AUTHORIZE-ACTIVTRADES-DEMO-TRADING-ONLY"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ActivTrades DEMO-only trader")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--shadow", action="store_true")
    modes.add_argument("--demo-auto", action="store_true")
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--analyze", action="store_true")
    modes.add_argument("--restart-proof", action="store_true")
    modes.add_argument(
        "--print-operating-policy", action="store_true",
        help="Lane R: print the versioned live operating policy with the per-market EFFECTIVE flat deadline and the DERIVED entry "
             "runway (summer / winter, incl. Brent). Read-only: no broker, no store.",
    )
    modes.add_argument("--record-canary", type=Path, default=None, metavar="FILE")
    parser.add_argument(
        "--exit-policy", choices=("fixed_1_5r", "staged", "staged_profiles"), default="fixed_1_5r",
        help="fixed_1_5r (DEFAULT, unchanged): broker SL + one fixed-R TP. staged: the ExitEngine manages TP1/TP2/runner "
             "partials, tighten-only stop moves, structure trailing and engine exits (DEMO only). staged_profiles (Lane Y): per-intent "
             "exit profile by family thesis (CONTINUATION / REVERSION / FAILED_MOVE on the same engine; FIXED_1_5R-mapped "
             "families unchanged) - activate only after the real-broker canary",
    )
    parser.add_argument(
        "--geometry-source", choices=("family", "structure"), default="family",
        help="initial stop geometry: family (DEFAULT, unchanged) or structure (chart invalidation stop; per-family "
             "opt-in is the supported use). The structure geometry is always logged next to the family one (shadow)",
    )
    parser.add_argument(
        "--flatten-only", action="store_true",
        help="Lane R EOD RECOVERY (with --demo-auto): DEMO/account checks, broker connect, state recovery, reconciliation, then ONLY "
             "reduce-only flatten of own-magic positions (bounded backoff) until the broker is flat and reconciled -> exit 0 "
             "(stop_reason eod_recovery_flat_confirmed). Never scans, never submits an entry, never learns; usable at any time/day.",
    )
    parser.add_argument("--confirm-demo-auto")
    parser.add_argument("--heartbeat", type=Path, default=None)
    parser.add_argument("--artifacts", type=Path, default=ARTIFACTS)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--phase", choices=("DISCOVERY", "FROZEN"), default=None)
    parser.add_argument(
        "--account-phase", default=None,
        help="ALPHA_EXECUTION_DISCOVERY | SMALL_ACCOUNT_FEASIBILITY | custom (default: artifacts dir meta, else "
             "ALPHA_EXECUTION_DISCOVERY). A store bound to another phase/account is refused.",
    )
    parser.add_argument(
        "--markets", default=None,
        help="comma separated subset of markets (default: the five live markets + every Phase-2 market switched on in "
             "configs/markets_phase2/enablement.toml; a Phase-2 market that is not enabled there is refused)",
    )
    parser.add_argument(
        "--forced-flat-on-shutdown", action="store_true",
        help="flatten open positions on shutdown IF the stack offers a flatten call (default off; the "
             "current StackPort has none: positions stay protected by their broker-side stops)",
    )
    parser.add_argument(
        "--daily", action="store_true",
        help="operating-day mode: after the 22:00 Europe/Berlin deadline, once the broker is flat and reconciled, "
             "finalize the day and exit 0 (stop_reason eod_flat_shutdown); no entries outside Mon-Fri (Berlin). Default off.",
    )
    parser.add_argument(
        "--shadow-universe", nargs="?", const="all-ready", default=None, metavar="LIST|all-ready",
        help="Lane U2 (DEFAULT OFF): observe the shadow-only markets of configs/markets_shadow on closed M5 bars INSIDE this "
             "runner (read-only, bounded per cycle, records REJECTED SHADOW_UNIVERSE snapshots; never trades). LIST = comma "
             "separated canonicals, or all-ready (every shadow spec; specs whose quote was stale at the Lane-U scan are "
             "re-validated with a fresh quote at runtime)",
    )
    oow = parser.add_mutually_exclusive_group()
    oow.add_argument(
        "--out-of-window-shadow", dest="out_of_window_shadow", action="store_true", default=None,
        help="Lane U2: for the active markets record what the frozen families WOULD have signalled while the broker is "
             "tradable but the entry window is closed (REJECTED OUT_OF_WINDOW_SHADOW; never trades, no window widened)",
    )
    oow.add_argument("--no-out-of-window-shadow", dest="out_of_window_shadow", action="store_false")
    sel = parser.add_mutually_exclusive_group()
    sel.add_argument(
        "--shadow-exit-lab", dest="shadow_exit_lab", action="store_true", default=None,
        help="Lane W (DEFAULT OFF): at label time run 12 predeclared hypothetical exit policies on the SAME entries of closed "
             "trades and labelled counterfactuals and store them additively (outcome_extra / counterfactual JSON); never "
             "trades, never in the order path, exception-contained, bounded per cycle",
    )
    sel.add_argument("--no-shadow-exit-lab", dest="shadow_exit_lab", action="store_false")
    mso = parser.add_mutually_exclusive_group()
    mso.add_argument(
        "--market-observer", dest="market_observer", action="store_true", default=None,
        help="Market Structure Observer (DEFAULT OFF, OBSERVATION ONLY): after the decisions of a closed bar are final, record level / swing / balance / "
             "acceptance / participation features of every opportunity into the additive observer_records table; never changes an opportunity, decision, "
             "intent, stop, target, size, risk or execution; exception-contained, bounded per cycle (docs/OBSERVER.md). Forced off with --flatten-only",
    )
    mso.add_argument("--no-market-observer", dest="market_observer", action="store_false")
    learn = parser.add_mutually_exclusive_group()
    learn.add_argument(
        "--learning", dest="learning", action="store_true", default=None,
        help="opt in to shadow learning (default: ON for --shadow, OFF for --demo-auto)",
    )
    learn.add_argument("--no-learning", dest="learning", action="store_false")
    return parser


def _proven_worker(*args: str) -> int:
    command = [sys.executable, str(REPO_ROOT / "scripts" / "c7_demo_vertical_slice.py"), *args]
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def _status(heartbeat: Path) -> int:
    from demo.monitor import NOT_RUNNING, heartbeat_verdict

    res = heartbeat_verdict(heartbeat, datetime.now(UTC))
    if res["status"] is None:
        print(json.dumps({"mode": "DEMO MODE", "process_alive": False, "error": "no heartbeat",
                          "verdict": NOT_RUNNING}))
        return 3
    out = dict(res["status"])
    out.update(verdict=res["verdict"], heartbeat_age_s=res["age_s"], verdict_reason=res["reason"])
    print(json.dumps(out, sort_keys=True, default=str))
    return 0 if res["verdict"] != NOT_RUNNING else 3


def _analyze(args: argparse.Namespace) -> int:
    from demo.monitor import analyze
    from demo.store import DemoStore

    art: Path = args.artifacts
    db = args.db or art / "demo.sqlite"
    if not db.is_file():
        print(f"no DEMO store at {db}", file=sys.stderr)
        return 3
    with DemoStore(db) as store:
        summary = analyze(store, args.phase, art / "reports", art / "export")
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


def _record_canary(args: argparse.Namespace) -> int:
    from demo.external import import_file
    from demo.store import AccountMismatch, DemoStore

    art: Path = args.artifacts
    db = args.db or art / "demo.sqlite"
    if not db.is_file():
        print(f"no DEMO store at {db}", file=sys.stderr)
        return 3
    with DemoStore(db) as store:
        try:
            ids = import_file(store, str(args.record_canary))
        except AccountMismatch as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    print(json.dumps({"imported": ids}, indent=1))
    return 0


def _run(args: argparse.Namespace, mode: str) -> int:
    from demo import runner as rn
    from demo.execution.exit_manager import ExitPlanConfig

    markets = tuple(m.strip() for m in args.markets.split(",")) if args.markets else None
    from demo.opportunity.operating_policy import load_operating_policy

    operating = load_operating_policy()  # Lane P: versioned live operating policy (flatten deadline, sessions)
    try:
        r = rn.build_live_runner(
            mode, phase=args.phase or "DISCOVERY", db_path=args.db, artifacts_dir=args.artifacts,
            markets=markets, learning=args.learning,
            forced_flat_on_shutdown=args.forced_flat_on_shutdown, account_phase=args.account_phase,
            exit_policy=args.exit_policy,
            out_of_window_shadow=args.out_of_window_shadow, shadow_exit_lab=args.shadow_exit_lab,
            shadow_universe=args.shadow_universe,
            market_observer=bool(args.market_observer),
            exit_plan=None if args.geometry_source == "family" else ExitPlanConfig(geometry_source=args.geometry_source),
            operating_policy=operating, daily=args.daily, flatten_only=args.flatten_only,
        )
    except rn.LiveStackRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except rn.LiveStackUnavailable as exc:
        print(f"UNAVAILABLE: {exc}", file=sys.stderr)
        return rn.EXIT_UNAVAILABLE
    global _LANE_WEDGED
    try:
        return r.run(install_signals=True)
    finally:
        _LANE_WEDGED = bool(getattr(r, "lane_wedged", False))  # Lane V2: read before the store closes
        r.store.close()


_LANE_WEDGED = False


def _hard_exit(code: int) -> None:
    """Lane V2 (HIGH-2 gap A): terminate NOW.  A wedged MT5 C call blocks the lane thread forever and Python's interpreter exit
    joins that thread, so an orderly return would never leave the process: the heartbeat goes stale, the EOD recovery only alerts
    (exit 10) and the supervisor never restarts.  The final heartbeat is written and runner.lock released before this is called;
    broker-side SL/TP stay on the server (nothing is closed here)."""
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(code)


def _run_single_instance(args: argparse.Namespace, mode: str) -> int:
    """One runner per artifacts directory.  Refused BEFORE any MT5 attach / store open."""
    lock = instance_lock.InstanceLock(args.artifacts / "runner.lock",
                                      role=f"runner:{mode}{':flatten-only' if args.flatten_only else ''}")
    try:
        lock.acquire()
    except instance_lock.LockHeld as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return instance_lock.EXIT_ALREADY_RUNNING
    try:
        code = _run(args, mode)
    finally:
        lock.release()
    if _LANE_WEDGED:
        _hard_exit(code)  # Lane V2 (HIGH-2 gap A): only the wedged-lane path skips the orderly interpreter exit
    return code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.status:
        return _status(args.heartbeat or args.artifacts / "heartbeat.json")
    if args.analyze:
        return _analyze(args)
    if args.print_operating_policy:
        from demo.opportunity.operating_policy import load_operating_policy
        from demo.opportunity.policy_report import operating_policy_report

        print(json.dumps(operating_policy_report(load_operating_policy()), indent=1, sort_keys=True))
        return 0
    if args.record_canary is not None:
        return _record_canary(args)
    if os.environ.get("MT5_ALLOW_ACCOUNT_LOGIN") == "1":
        print("REFUSED: MT5_ALLOW_ACCOUNT_LOGIN=1 is not permitted; the DEMO trader only attaches.",
              file=sys.stderr)
        return 2
    if args.preflight:
        # Read-only, but the proven script also performs safe order_check shape
        # validation; it contains no order_send call.
        command = [sys.executable, str(REPO_ROOT / "scripts" / "c7_preflight.py")]
        return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode
    if args.restart_proof:
        return _proven_worker("--restart-proof")
    if args.flatten_only and not args.demo_auto:
        print("REFUSED: --flatten-only is a modifier of --demo-auto", file=sys.stderr)
        return 2
    if args.demo_auto and args.confirm_demo_auto != CONFIRMATION:
        print(
            "REFUSED: --demo-auto requires "
            f"--confirm-demo-auto={CONFIRMATION}",
            file=sys.stderr,
        )
        return 2
    return _run_single_instance(args, "demo-auto" if args.demo_auto else "shadow")


if __name__ == "__main__":
    raise SystemExit(main())
