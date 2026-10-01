"""Segmented test runner: fast / integration / safety / slow / full / changed.

Every test carries exactly one tier marker (fast | integration | slow; assigned by
tests/conftest.py from its path) and, independently, `safety` for risk / execution /
reconciliation / persistence invariant suites. The tiers partition the suite, so
`full` = fast + integration + slow run as separate processes (bounded memory, and the
MT5-adjacent tests never share a process with the xdist workers).

Usage (from the repo root):
    uv run python scripts/run_tests.py fast
    uv run python scripts/run_tests.py integration
    uv run python scripts/run_tests.py safety
    uv run python scripts/run_tests.py slow
    uv run python scripts/run_tests.py full            # segmented FULL; see budget note
    uv run python scripts/run_tests.py changed [--base REF] [PATH ...]
    uv run python scripts/run_tests.py <cmd> --dry-run # print the pytest commands only

Extra pytest arguments go after `--`, e.g. `run_tests.py fast -- -x -k exits`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Hard wall-clock guard per pytest invocation (seconds). Not a per-test timeout; it only
# stops a hung segment from blocking the machine (live runner shares it).
SEGMENT_TIMEOUT_S = {"fast": 600, "integration": 1200, "safety": 1500, "slow": 2400}

SEGMENTS: dict[str, list[str]] = {
    "fast": ["-m", "fast"],
    "integration": ["-m", "integration"],
    "safety": ["-m", "safety"],
    "slow": ["-m", "slow"],
}

# Changed-path matrix: (source prefix, pytest targets). Targets are paths or `-m` expressions
# (prefixed with "m:"). `safety` markers make reduce-only/idempotency/exposure coverage follow.
# Research-only paths intentionally map to no demo safety suite.
_ALPHA_TARGETS = [
    "tests/unit/alpha",
    "tests/unit/strategies",
    "tests/contracts",
    "tests/test_alpha_*.py",
]

# Paths whose change must also run the SAFETY overlay (`-m safety`: reduce-only, idempotency,
# stale-signal, exposure, leverage, reconciliation, persistence, EOD/flatten). docs/TEST_GATES.md
# requires it for risk/execution/reconciliation/persistence changes, and the impact matrix of the
# night master plan adds exits and autostart/supervisor/EOD. `changed` previously never selected it.
SAFETY_PREFIXES: tuple[str, ...] = (
    "src/exits/",
    "src/risk/",
    "src/execution/",
    "src/nautilus_mt5/",
    "src/adapters/",
    "src/persistence/",
    "src/demo/",
    "src/data/",
    "src/markets/",
    "src/instruments/",
    "src/margin/",
    # production-LIVE alpha modules (signal families etc.), see the matrix entries below
    "src/alpha/families/",
    "src/alpha/fast/",
    "src/alpha/common/",
    "src/alpha/session.py",
    "src/alpha/context",
    "src/alpha/timeframe",
    "src/alpha/regime",
    "src/alpha/signals/",
    "src/alpha/__init__.py",
    "scripts/autostart/",
    "scripts/demo_trader.py",
)

MATRIX: list[tuple[str, list[str]]] = [
    (
        "src/exits/",
        [
            "tests/unit/exits",
            "tests/unit/execution/test_reduce_only_fill_clamp.py",
            "tests/unit/execution/test_reduce_only_reconciliation_admission.py",
            "tests/contracts/test_risk_reduce_only_and_halt_contracts.py",
            "tests/integration",
        ],
    ),
    ("src/risk/", ["tests/unit/risk", "tests/property", "tests/contracts", "tests/unit/execution"]),
    (
        "src/execution/",
        [
            "tests/unit/execution",
            "tests/unit/persistence",
            "tests/unit/risk",
            "tests/contracts",
            "tests/chaos",
        ],
    ),
    (
        "src/nautilus_mt5/",
        [
            "tests/unit/nautilus_mt5",
            "tests/unit/execution",
            "tests/unit/persistence",
            "tests/unit/risk",
            "tests/contracts",
        ],
    ),
    ("src/adapters/", ["tests/unit/adapters", "tests/unit/execution", "tests/contracts"]),
    (
        "src/demo/",
        [
            "tests/unit/demo",
            "tests/unit/persistence",
            "tests/unit/risk",
            "tests/unit/execution",
            "tests/contracts",
        ],
    ),
    (
        "src/markets/",
        [
            "tests/unit/markets",
            "tests/unit/instruments",
            "tests/unit/risk",
            "tests/unit/demo/execution",
        ],
    ),
    (
        "src/instruments/",
        [
            "tests/unit/markets",
            "tests/unit/instruments",
            "tests/unit/risk",
            "tests/unit/demo/execution",
        ],
    ),
    (
        "src/persistence/",
        ["tests/unit/persistence", "tests/unit/execution", "tests/unit/demo/store"],
    ),
    ("src/strategies/", ["tests/unit/strategies", "tests/contracts", "tests/parity"]),
    # LIVE-REACHABLE alpha modules: the production runner imports them (signal families, the fast
    # simulator used by the opportunity engine/policy, market data/costs, session). They are NOT
    # research-only, so a change here must also pull the demo (opportunity/execution) tests.
    # First match wins -> these prefixes must stay above the generic "src/alpha/" entry.
    (
        "src/alpha/families/",
        [*_ALPHA_TARGETS, "tests/unit/demo"],
    ),
    (
        "src/alpha/fast/",
        [*_ALPHA_TARGETS, "tests/unit/demo"],
    ),
    (
        "src/alpha/common/",
        [*_ALPHA_TARGETS, "tests/unit/demo", "tests/unit/markets"],
    ),
    *[
        (prefix, [*_ALPHA_TARGETS, "tests/unit/demo"])
        for prefix in (
            "src/alpha/session.py",
            "src/alpha/context",
            "src/alpha/timeframe",
            "src/alpha/regime",
            "src/alpha/signals/",
            "src/alpha/__init__.py",
        )
    ],
    ("src/alpha/", _ALPHA_TARGETS),
    ("src/data/", ["tests/unit/data", "tests/events"]),
    ("src/temporal/", ["tests/temporal", "tests/events"]),
    ("src/events/", ["tests/events", "tests/temporal", "tests/replay"]),
    ("src/portfolio/", ["tests/unit/portfolio", "tests/unit/risk"]),
    ("src/margin/", ["tests/unit/margin", "tests/unit/risk"]),
    ("src/costs/", ["tests/unit/costs", "tests/unit/risk"]),
    ("src/health/", ["tests/unit/health"]),
    ("src/pipeline/", ["tests/unit/pipeline", "tests/integration"]),
    # research-only: no demo safety suite
    ("research/", ["tests/unit/research", "tests/temporal"]),
    ("scripts/", ["tests/unit/scripts"]),
    ("configs/", ["tests/unit/risk", "tests/unit/demo/execution"]),
    ("tests/", []),  # tests changed -> run the touched test files themselves (see below)
]


# Default xdist workers per segment (measured, docs/TEST_TIMING.md). 0 = serial. integration and
# safety stay serial by default (sqlite/subprocess/MT5-lock/heartbeat tests, stable ordering);
# `serial`-marked tests always share one worker via `--dist loadgroup` if workers are requested.
DEFAULT_WORKERS = {"fast": 2, "integration": 0, "safety": 0, "slow": 2}


def xdist_available() -> bool:
    try:
        import xdist  # noqa: F401
    except ImportError:
        return False
    return True


def pytest_cmd(extra: list[str], workers: int = 0) -> list[str]:
    cmd = [sys.executable, "-m", "pytest", "-q", "--durations=15"]
    if workers > 1 and xdist_available():
        cmd += ["-n", str(workers), "--dist", "loadgroup"]
    elif workers > 1:
        print("pytest-xdist not installed; running serially (uv sync to get it).", file=sys.stderr)
    return [*cmd, *extra]


def run(cmd: list[str], timeout: int, dry: bool) -> int:
    print("+", " ".join(cmd), flush=True)
    if dry:
        return 0
    start = time.monotonic()
    try:
        rc = subprocess.run(cmd, cwd=ROOT, timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT after {timeout}s (segment aborted, not a pass)", file=sys.stderr)
        return 124
    print(f"-- {time.monotonic() - start:.0f}s rc={rc}", flush=True)
    return rc


def _git_names(args: list[str]) -> list[str]:
    """`git diff --name-only ...`; a git failure must be loud (never an empty = 'no tests' plan)."""
    res = subprocess.run(
        ["git", "diff", "--name-only", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"git diff --name-only {' '.join(args)} failed (rc={res.returncode}): "
            f"{res.stderr.strip()[:300]}"
        )
    return res.stdout.split()


def changed_paths(base: str) -> list[str]:
    return sorted(set(_git_names([f"{base}...HEAD"]) + _git_names([])))


def plan_for(paths: list[str]) -> list[list[str]]:
    """Translate changed paths into pytest invocations (deduplicated, stable order)."""
    targets: list[str] = []
    marks: list[str] = []
    for p in paths:
        p = p.replace("\\", "/")
        if (
            p.startswith("tests/")
            and p.endswith(".py")
            and "/test_" in "/" + p.split("tests/", 1)[1]
        ):
            targets.append(p)
            continue
        if p.startswith(SAFETY_PREFIXES):
            marks.append("safety")
        for prefix, tgt in MATRIX:
            if p.startswith(prefix):
                for t in tgt:
                    (marks if t.startswith("m:") else targets).append(
                        t[2:] if t.startswith("m:") else t
                    )
                break
        else:
            # Unmapped path: fail OPEN to more tests (FAST tier), never to none; pure docs exempt.
            if not (p.startswith("docs/") or p.endswith(".md")):
                marks.append("fast")
    plan: list[list[str]] = []
    expanded: list[str] = []
    for t in targets:
        if "*" in t:
            expanded += sorted(x.relative_to(ROOT).as_posix() for x in ROOT.glob(t))
        else:
            expanded.append(t)
    uniq = list(dict.fromkeys(expanded))
    if uniq:
        plan.append(uniq)
    for m in dict.fromkeys(marks):
        plan.append(["-m", m])
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("cmd", choices=[*SEGMENTS, "full", "changed"])
    ap.add_argument("paths", nargs="*", help="changed paths (changed only; default: git diff)")
    ap.add_argument("--base", default="main", help="git base ref for `changed` (default: main)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=None, help="xdist workers (0/1 = serial)")
    args, rest = ap.parse_known_args()
    extra = [a for a in rest if a != "--"]

    rc = 0
    if args.cmd in SEGMENTS:
        w = DEFAULT_WORKERS[args.cmd] if args.workers is None else args.workers
        return run(
            pytest_cmd([*SEGMENTS[args.cmd], *extra], w), SEGMENT_TIMEOUT_S[args.cmd], args.dry_run
        )
    if args.cmd == "full":
        print(
            "FULL = fast + integration + slow segments (separate processes). "
            "Expected ~10-14 min (docs/TEST_TIMING.md); defer if > 15 min."
        )
        for seg in ("fast", "integration", "slow"):
            w = DEFAULT_WORKERS[seg] if args.workers is None else args.workers
            rc |= run(pytest_cmd([*SEGMENTS[seg], *extra], w), SEGMENT_TIMEOUT_S[seg], args.dry_run)
        return rc
    paths = args.paths or changed_paths(args.base)
    plan = plan_for(paths)
    if not plan:
        print(
            "No mapped test targets for the changed paths; "
            "nothing to run (research-only/docs changes)."
        )
        return 0
    for p in plan:
        rc |= run(pytest_cmd([*p, *extra]), SEGMENT_TIMEOUT_S["safety"], args.dry_run)
    return rc


if __name__ == "__main__":
    sys.exit(main())
