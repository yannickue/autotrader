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
    uv run python scripts/run_tests.py changed [--base REF] [PATH ...] [--result-cache]
    uv run python scripts/run_tests.py t0|t1|t2|t3   # named tiers (TEST_GATES.md); t1 == changed
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
# impact_tests / test_result_cache live next to this file. APPENDED (not prepended): scripts/
# holds modules (e.g. coverage_analysis.py) that must never shadow src/ packages of that name.
sys.path.append(str(Path(__file__).resolve().parent))

import impact_tests  # noqa: E402

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
    (
        "src/alpha/",
        ["tests/unit/alpha", "tests/unit/strategies", "tests/contracts", "tests/test_alpha_*.py"],
    ),
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


def changed_paths(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    out += subprocess.run(
        ["git", "diff", "--name-only"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.split()
    # new files that are not added yet must select tests too (never select less)
    out += subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    return sorted(set(out))


def impact_plan(paths: list[str]) -> impact_tests.Plan:
    """Impact selection (scripts/impact_tests.py): MATRIX + explicit additions, never narrower."""
    return impact_tests.impact(paths, MATRIX)


def plan_for(paths: list[str]) -> list[list[str]]:
    """Translate changed paths into pytest invocations (deduplicated, stable order)."""
    ip = impact_plan(paths)
    if ip.full:  # global tooling / config change: the segmented FULL suite (separate processes)
        return [["-m", "fast"], ["-m", "integration"], ["-m", "slow"]]
    targets: list[str] = list(ip.targets)
    marks: list[str] = list(ip.marks)
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


def _run_planned(plan: list[list[str]], extra: list[str], dry: bool, use_cache: bool) -> int:
    """Run the impact invocations. With ``use_cache`` an invocation that is an explicit, eligible
    file list may be served from the green-result cache (scripts/test_result_cache.py); never for
    -m selections."""
    rc = 0
    for p in plan:
        cand = None
        if use_cache:
            import test_result_cache as trc

            cand, why = trc.candidate(p, extra)
            if cand is None:
                print(f"result-cache: not eligible ({why})", flush=True)
            else:
                hit = trc.lookup(cand)
                if hit is not None:
                    print(
                        f"result-cache: CACHE HIT {cand.fingerprint[:12]} "
                        f"({len(cand.files)} files, green {hit['green_utc']}Z, "
                        f"was {hit['wall_s']}s) "
                        "- NOT re-run",
                        flush=True,
                    )
                    continue
                print(f"result-cache: MISS {cand.fingerprint[:12]}", flush=True)
        cmd = pytest_cmd([*p, *extra])
        t0 = time.monotonic()
        r = run(cmd, SEGMENT_TIMEOUT_S["safety"], dry)
        if r == 0 and cand is not None and not dry:
            import test_result_cache as trc

            trc.store(cand, cmd, time.monotonic() - t0)
        rc |= r
    return rc


def _tier_t0(paths: list[str], dry: bool) -> int:
    """T0 (< 60 s): ruff + compileall on changed python files + the touched test files."""
    py = [
        p
        for p in (x.replace("\\", "/") for x in paths)
        if p.endswith(".py") and (ROOT / p).is_file()
    ]
    tests = [p for p in py if p.startswith("tests/") and p.rsplit("/", 1)[-1].startswith("test_")]
    rc = 0
    if py:
        rc |= run([sys.executable, "-m", "ruff", "check", *py], 120, dry)
        rc |= run([sys.executable, "-m", "compileall", "-q", *py], 120, dry)
    else:
        print("T0: no changed python files (docs-only?) - lint/compile skipped")
    if tests:
        rc |= run(pytest_cmd(tests), 180, dry)
    print("T0 is the inner loop only; it does not replace T1 (`changed`) before a merge.")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("cmd", choices=[*SEGMENTS, "full", "changed", "t0", "t1", "t2", "t3"])
    ap.add_argument(
        "paths", nargs="*", help="changed paths (changed/t0/t1 only; default: git diff)"
    )
    ap.add_argument("--base", default="main", help="git base ref for `changed` (default: main)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=None, help="xdist workers (0/1 = serial)")
    ap.add_argument(
        "--result-cache",
        action="store_true",
        help="changed/t1 only: reuse a previous GREEN run of an unchanged, eligible fast-tier file "
        "set (default OFF; never for safety/integration/slow suites or -m selections)",
    )
    args, rest = ap.parse_known_args()
    extra = [a for a in rest if a != "--"]

    rc = 0
    if args.cmd in SEGMENTS:
        w = DEFAULT_WORKERS[args.cmd] if args.workers is None else args.workers
        return run(
            pytest_cmd([*SEGMENTS[args.cmd], *extra], w), SEGMENT_TIMEOUT_S[args.cmd], args.dry_run
        )
    if args.cmd in ("full", "t3"):
        print(
            "FULL = fast + integration + slow segments (separate processes). "
            "Expected ~10-14 min (docs/TEST_TIMING.md); defer if > 15 min."
        )
        for seg in ("fast", "integration", "slow"):
            w = DEFAULT_WORKERS[seg] if args.workers is None else args.workers
            rc |= run(pytest_cmd([*SEGMENTS[seg], *extra], w), SEGMENT_TIMEOUT_S[seg], args.dry_run)
        if args.cmd == "t3":
            w = DEFAULT_WORKERS["safety"] if args.workers is None else args.workers
            rc |= run(
                pytest_cmd([*SEGMENTS["safety"], *extra], w),
                SEGMENT_TIMEOUT_S["safety"],
                args.dry_run,
            )
            print(
                "T3 release: run the broker canary manually (scripts/e2_broker_canary.py) if "
                "execution / risk / exits changed. This tier never uses the result cache."
            )
        return rc
    if args.cmd == "t2":
        for seg in ("integration", "safety"):
            w = DEFAULT_WORKERS[seg] if args.workers is None else args.workers
            rc |= run(pytest_cmd([*SEGMENTS[seg], *extra], w), SEGMENT_TIMEOUT_S[seg], args.dry_run)
        return rc
    paths = args.paths or changed_paths(args.base)
    if args.cmd == "t0":
        return _tier_t0(paths, args.dry_run)
    ip = impact_plan(paths)
    for n in ip.notes:
        print(f"NOTE: {n}")
    if ip.widened:
        print(f"WIDENED (no specific rule; never select less): {', '.join(ip.widened)}")
    if ip.docs_only:
        print("Docs-only change: no pytest; running the doc checks (git diff --check).")
        return run(["git", "diff", "--check"], 60, args.dry_run)
    plan = plan_for(paths)
    if not plan:
        print(
            "No changed paths (nothing to select); use a named segment or `full` for a wider run."
        )
        return 0
    return _run_planned(plan, extra, args.dry_run, args.result_cache)


if __name__ == "__main__":
    sys.exit(main())
