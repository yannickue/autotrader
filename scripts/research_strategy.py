# ruff: noqa: E501
"""Canonical research CLI (OFFLINE ONLY; never imported by the live trader).

    python scripts/research_strategy.py plan    --demo-synthetic [--artifact-root DIR] [--json]
    python scripts/research_strategy.py fast    --spec exp.json [--jobs 2] [--entry-exit] [--allow-light-only]
    python scripts/research_strategy.py compare --spec exp.json            # FAST <-> Nautilus differential (HEAVY)
    python scripts/research_strategy.py report  --spec exp.json [--format md|json] [--out FILE]
    python scripts/research_strategy.py coverage --db COPY.db [--phase DISCOVERY|FROZEN] [--now ISO] [--json]

Experiment input: a small JSON file (schema in ``research_workbench.experiment``) or the built-in ``--demo-synthetic``.

Commands
* ``plan``   LIGHT, read-only: experiment id, markets, dataset, strategy hash, per-stage cache HIT/MISS (features / signals /
             simulation / metrics), expected segments, requested vs effective workers, RAM guard, LIGHT/HEAVY. It runs no
             simulation and writes nothing.
* ``fast``   features -> signals -> simulate_fast -> metrics -> REJECT_FAST | PROMOTE_TO_FIDELITY (cached per stage).
* ``compare`` FAST <-> Nautilus differential for markets that were promoted (needs ``research_workbench.differential``).
* ``report`` standard report (markdown or json) from the stored artifacts.
* ``coverage`` READ-ONLY coverage audit of a demo DB COPY (sqlite mode=ro; paths under artifacts/demo_100k are refused
             unless --allow-production-db-readonly). Exit 0 even when RED (the verdict is in the report).

Resource policy: ``fast`` on real data (csv/parquet) and ``compare`` are HEAVY. While a live trader (``demo_trader.py`` /
``supervisor.py``) is running they are REFUSED (exit 3); LIGHT work (synthetic data) is only allowed next to it with
``--allow-light-only``. ``RESEARCH_SPEED_ALLOW_WITH_LIVE=1`` is the existing explicit override. BLAS threads are forced to 1.

Exit codes: 0 ok | 1 market failed / BLOCKED sub-step | 2 bad input | 3 refused (live trader) | 4 not enough memory |
6 BLOCKED (required component unavailable).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT, EXIT_REFUSED, EXIT_BLOCKED = 0, 1, 2, 3, 6


def _live_trader_running() -> bool:
    """True when a live trader process exists (or the process table is unreadable: fail closed)."""
    from research_speed.parallel import live_trader_running

    try:
        return live_trader_running()
    except Exception:
        return True


def _harden(jobs: int) -> int:
    from research_speed.parallel import harden_process

    return harden_process(jobs)


def _load_experiment(args: argparse.Namespace):
    from research_workbench.experiment import demo_synthetic_experiment, experiment_from_dict

    if args.demo_synthetic:
        experiment = demo_synthetic_experiment()
    elif args.spec:
        experiment = experiment_from_dict(json.loads(Path(args.spec).read_text(encoding="utf-8")))
    else:
        raise SystemExit(EXIT_BAD_INPUT)
    if args.artifact_root:
        experiment = replace(experiment, artifact_root=args.artifact_root)
    if args.markets:
        wanted = tuple(m.strip() for m in args.markets.split(",") if m.strip())
        unknown = set(wanted) - set(experiment.markets)
        if unknown:
            print(f"unknown markets: {sorted(unknown)}", file=sys.stderr)
            raise SystemExit(EXIT_BAD_INPUT)
        experiment = replace(experiment, markets=wanted)
    return experiment


def _guard(experiment, command: str, allow_light_only: bool) -> int | None:
    """Refuse HEAVY work (and unflagged LIGHT work) while a live trader is running."""
    from research_speed.parallel import ALLOW_LIVE_ENV
    from research_workbench.fastrun import classify

    heavy = command == "compare" or classify(experiment) == "HEAVY"
    if os.environ.get(ALLOW_LIVE_ENV) == "1" or not _live_trader_running():
        return None
    if heavy:
        print(
            f"REFUSED: {command} is HEAVY and a live trader is running (never run research next to it).",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if not allow_light_only:
        print(
            "REFUSED: a live trader is running; LIGHT work needs --allow-light-only.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    return None


def _print(obj, as_json: bool) -> None:
    print(json.dumps(obj, indent=1, default=str, sort_keys=True) if as_json else _text(obj))


def _text(plan: dict) -> str:
    lines = [
        f"EXPERIMENT_ID {plan['experiment_id']}  STRATEGY {plan['strategy_id']}  SPEC_HASH {plan['spec_hash'][:16]}",
        f"DATASET {plan['dataset']}",
        f"CLASS {plan['classification']}  WORKERS requested={plan['workers']['requested']} effective={plan['workers']['effective']}  "
        f"RAM {plan['ram_guard']['available_mb']} MB ({plan['ram_guard']['status']})",
        "PARTITIONS READ " + ", ".join(f"{k}={v}" for k, v in plan["partitions_read"].items()),
    ]
    for market, m in plan["markets"].items():
        if "error" in m:
            lines.append(f"  {market}: ERROR {m['error']}")
            continue
        stages = " ".join(f"{s}={v['cache']}" for s, v in m["stages"].items())
        lines.append(f"  {market}: {m['n_bars']} bars  {stages}  expected={m['expected_segments']}")
    return "\n".join(lines)


def cmd_plan(args: argparse.Namespace) -> int:
    from research_workbench.fastrun import plan_experiment

    experiment = _load_experiment(args)
    plan = plan_experiment(experiment, jobs=args.jobs, gate=_gate(args))
    _print(plan, args.json)
    return EXIT_OK


def cmd_fast(args: argparse.Namespace) -> int:
    from research_workbench.fastrun import run_fast

    experiment = _load_experiment(args)
    refused = _guard(experiment, "fast", args.allow_light_only)
    if refused is not None:
        return refused
    jobs = _harden(args.jobs)
    result = run_fast(experiment, jobs=jobs, gate=_gate(args), entry_exit=args.entry_exit)
    if args.json:
        _print(result, True)
    else:
        for market, r in result["markets"].items():
            stages = " ".join(
                f"{s}={v['cache']}{'*' if v['computed'] else ''}"
                for s, v in r.get("stages", {}).items()
            )
            print(f"{market}: {r['status']} {r.get('reasons', r.get('error', ''))}  {stages}")
    return EXIT_FAILED if result["n_failed"] else EXIT_OK


def _gate(args: argparse.Namespace):
    from research_workbench.fastrun import FastGate

    return FastGate(
        min_train_trades=args.min_train_trades,
        min_validation_trades=args.min_validation_trades,
        require_positive_expectancy=not args.allow_negative_expectancy,
    )


def cmd_compare(args: argparse.Namespace) -> int:
    experiment = _load_experiment(args)
    refused = _guard(experiment, "compare", args.allow_light_only)
    if refused is not None:
        return refused
    try:
        from research_workbench import differential  # noqa: F401  (availability check)
    except ImportError as exc:
        print(
            f"BLOCKED: research_workbench.differential is not available ({exc}).", file=sys.stderr
        )
        return EXIT_BLOCKED
    from research_workbench import dag
    from research_workbench.compare import run_compare_market
    from research_workbench.fastrun import run_market

    store = dag.ArtifactStore(experiment.artifact_root)
    _harden(args.jobs)
    not_ok = False
    for market in experiment.markets:
        record = run_market(experiment, market, gate=_gate(args), store=store)
        if record["status"] != "PROMOTE_TO_FIDELITY":
            print(
                f"{market}: SKIPPED ({record['status']}; only PROMOTE_TO_FIDELITY markets are compared)"
            )
            continue
        try:
            result = run_compare_market(experiment, market, record, store)
        except Exception as exc:  # never a silent PASS
            print(f"{market}: BLOCKED compare failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            not_ok = True
            continue
        print(
            f"{market}: DIFFERENTIAL {result['status']} -> {result['promotion_status']}"
            f"{' (cached)' if result.get('cached') else ''} {result.get('blocked_reason') or ''}"
        )
        not_ok = not_ok or result["status"] != "PASS"
    return EXIT_FAILED if not_ok else EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    from research_workbench import dag
    from research_workbench.report import build_report, render_markdown

    experiment = _load_experiment(args)
    store = dag.ArtifactStore(experiment.artifact_root)
    records = {
        m: rec
        for m in experiment.markets
        if (rec := store.read_run_record(experiment.experiment_id, m)) is not None
    }
    if not records:
        print("no run record found for this experiment: run `fast` first", file=sys.stderr)
        return EXIT_BAD_INPUT
    coverage = None
    if args.coverage_db:
        from research_workbench import coverage as cov

        try:
            coverage = cov.coverage_for_path(args.coverage_db, phase=args.coverage_phase)
        except (cov.ProductionPathRefused, FileNotFoundError, ValueError) as exc:
            print(f"coverage: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT
    report = build_report(experiment, store, records, coverage=coverage)
    text = (
        json.dumps(report, indent=1, default=str, sort_keys=True)
        if args.format == "json"
        else render_markdown(report)
    )
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return EXIT_OK


def cmd_coverage(args: argparse.Namespace) -> int:
    from research_workbench import coverage

    try:
        report = coverage.coverage_for_path(
            args.db,
            allow_production_readonly=args.allow_production_db_readonly,
            phase=args.phase,
            now=args.now,
        )
    except coverage.ProductionPathRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (FileNotFoundError, ValueError) as exc:
        print(f"bad input: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    print(
        json.dumps(report, indent=1, default=str, sort_keys=True)
        if args.json
        else coverage.render_markdown(report)
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research_strategy", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (
        ("plan", cmd_plan),
        ("fast", cmd_fast),
        ("compare", cmd_compare),
        ("report", cmd_report),
    ):
        p = sub.add_parser(name)
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument("--spec", help="experiment JSON file")
        src.add_argument(
            "--demo-synthetic", action="store_true", help="built-in synthetic smoke experiment"
        )
        p.add_argument("--artifact-root", help="override the experiment's artifact_root")
        p.add_argument("--markets", help="comma separated subset of the experiment's markets")
        p.add_argument(
            "--jobs",
            type=int,
            default=1,
            help="requested workers (clamped by research_speed.parallel)",
        )
        p.add_argument("--json", action="store_true")
        p.add_argument(
            "--allow-light-only",
            action="store_true",
            help="permit LIGHT work next to a live trader",
        )
        p.add_argument("--min-train-trades", type=int, default=30, help="fast gate (METRICS key)")
        p.add_argument(
            "--min-validation-trades", type=int, default=10, help="fast gate (METRICS key)"
        )
        p.add_argument(
            "--allow-negative-expectancy",
            action="store_true",
            help="exploratory/test gate: no reject on negative expectancy (METRICS key)",
        )
        if name == "fast":
            p.add_argument(
                "--entry-exit",
                action="store_true",
                help="also run the ENTRY_EXIT diagnostic (research only)",
            )
        if name == "report":
            p.add_argument("--format", choices=("md", "json"), default="md")
            p.add_argument("--out")
            p.add_argument(
                "--coverage-db",
                help="also attach a read-only coverage section from this demo DB COPY",
            )
            p.add_argument(
                "--coverage-phase",
                choices=("DISCOVERY", "FROZEN"),
                help="phase of the coverage section (omitted: each phase reported separately, never pooled, no promotion claim)",
            )
        p.set_defaults(handler=handler)
    cov = sub.add_parser("coverage", help="read-only coverage audit of a demo DB copy")
    cov.add_argument(
        "--db", required=True, help="path to a COPY (or synthetic) demo DB; opened mode=ro"
    )
    cov.add_argument(
        "--phase",
        choices=("DISCOVERY", "FROZEN"),
        help="omitted: each phase is reported separately (never pooled) and no_promotion_claim is set",
    )
    cov.add_argument("--now", help="ISO UTC 'now' (default: the current time)")
    cov.add_argument("--json", action="store_true")
    cov.add_argument(
        "--allow-production-db-readonly",
        action="store_true",
        help="allow a path under artifacts/demo_100k (still opened mode=ro)",
    )
    cov.set_defaults(handler=cmd_coverage)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        print(
            "interrupted: no stage is marked COMPLETE unless its manifest was committed",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
