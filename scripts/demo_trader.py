"""ActivTrades DEMO-only runner entry point.

Modes are mutually exclusive.  Exit codes: 0 success; 2 bad arguments or
missing explicit DEMO authorization; 3 status file unavailable; 7 fail-closed
preflight/runtime stop; 8 intentionally unavailable until the cross-owner
multi-market NautilusRiskBridge contract is generalized.

This module never logs in.  The preflight/shadow/restart-proof modes reuse the
already proven attach-only C7 worker, which owns the terminal lock and its one
dedicated MT5 IPC thread for the worker lifetime.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT = REPO_ROOT / "artifacts" / "demo_trader" / "heartbeat.json"
CONFIRMATION = "I-AUTHORIZE-ACTIVTRADES-DEMO-TRADING-ONLY"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ActivTrades DEMO-only trader")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--shadow", action="store_true")
    modes.add_argument("--demo-auto", action="store_true")
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--restart-proof", action="store_true")
    parser.add_argument("--confirm-demo-auto")
    parser.add_argument("--heartbeat", type=Path, default=HEARTBEAT)
    return parser


def _proven_worker(*args: str) -> int:
    command = [sys.executable, str(REPO_ROOT / "scripts" / "c7_demo_vertical_slice.py"), *args]
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.status:
        if not args.heartbeat.is_file():
            print(
                json.dumps(
                    {"mode": "DEMO MODE", "process_alive": False, "error": "no heartbeat"}
                )
            )
            return 3
        print(args.heartbeat.read_text(encoding="utf-8"))
        return 0
    if args.preflight:
        # Read-only, but the proven script also performs safe order_check shape
        # validation; it contains no order_send call.
        command = [sys.executable, str(REPO_ROOT / "scripts" / "c7_preflight.py")]
        return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode
    if args.shadow:
        return _proven_worker("--dry-run")
    if args.restart_proof:
        return _proven_worker("--restart-proof")
    if args.confirm_demo_auto != CONFIRMATION:
        print(
            "REFUSED: --demo-auto requires "
            f"--confirm-demo-auto={CONFIRMATION}",
            file=sys.stderr,
        )
        return 2
    print(
        "REFUSED: multi-market DEMO auto-runner is unavailable until the Risk Agent "
        "generalizes NautilusRiskBridge beyond its hardcoded GER40 account state.",
        file=sys.stderr,
    )
    return 8


if __name__ == "__main__":
    raise SystemExit(main())
