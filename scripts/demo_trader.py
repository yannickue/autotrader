# ruff: noqa: E501
"""ActivTrades DEMO-only runner entry point.

Modes are mutually exclusive.  Exit codes: 0 success; 2 bad arguments, missing explicit DEMO
authorization or MT5_ALLOW_ACCOUNT_LOGIN=1; 3 no heartbeat / NOT RUNNING (--status); 7 fail-closed
stop (after an orderly shutdown); 8 the real MT5 DEMO stack (demo.execution.live.Mt5DemoStack) is
not available in this checkout.

* ``--preflight``    read-only C7 preflight.
* ``--shadow``       full loop (bars -> opportunities -> decisions -> counterfactuals -> shadow learning),
                     ZERO orders (stack constructed in shadow mode, runner never creates intents).
* ``--demo-auto --confirm-demo-auto=I-AUTHORIZE-ACTIVTRADES-DEMO-TRADING-ONLY``
                     same loop, accepted opportunities are traded on the ActivTrades DEMO account.
* ``--status``       print the heartbeat + verdict (older than 90 s => NOT RUNNING).
* ``--analyze``      offline report (+ parquet export) over the recorded DEMO data; ``--phase`` filters.
* ``--restart-proof`` the proven C7 restart worker.
* ``--record-canary FILE`` import manual / canary / test trades (broker deal-history fields as JSON) into the
                     DemoStore as EXECUTION_CANARY (censored, excluded from alpha metrics). Never touches the broker.

This module never logs in and there is no live-account path.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

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
    modes.add_argument("--record-canary", type=Path, default=None, metavar="FILE")
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
    parser.add_argument("--markets", default=None, help="comma separated subset of markets")
    parser.add_argument(
        "--forced-flat-on-shutdown", action="store_true",
        help="flatten open positions on shutdown IF the stack offers a flatten call (default off; the "
             "current StackPort has none: positions stay protected by their broker-side stops)",
    )
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

    markets = tuple(m.strip() for m in args.markets.split(",")) if args.markets else None
    try:
        r = rn.build_live_runner(
            mode, phase=args.phase or "DISCOVERY", db_path=args.db, artifacts_dir=args.artifacts,
            markets=markets, learning=args.learning,
            forced_flat_on_shutdown=args.forced_flat_on_shutdown, account_phase=args.account_phase,
        )
    except rn.LiveStackRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except rn.LiveStackUnavailable as exc:
        print(f"UNAVAILABLE: {exc}", file=sys.stderr)
        return rn.EXIT_UNAVAILABLE
    try:
        return r.run(install_signals=True)
    finally:
        r.store.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.status:
        return _status(args.heartbeat or args.artifacts / "heartbeat.json")
    if args.analyze:
        return _analyze(args)
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
    if args.demo_auto and args.confirm_demo_auto != CONFIRMATION:
        print(
            "REFUSED: --demo-auto requires "
            f"--confirm-demo-auto={CONFIRMATION}",
            file=sys.stderr,
        )
        return 2
    return _run(args, "demo-auto" if args.demo_auto else "shadow")


if __name__ == "__main__":
    raise SystemExit(main())
