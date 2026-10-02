# ruff: noqa: E501
"""Workbench benchmark on synthetic multi-market data (OFFLINE ONLY; stdlib timing around the library functions).

    python scripts/bench_workbench.py [--markets 7] [--days 40] [--jobs 1,2,3,4] [--out artifacts/research_workbench_bench]

Measures (every number is tagged MEASURED or NOT_MEASURED in the output):
1. COLD full pipeline per jobs value (empty artifact root => empty FeatureStore cache) with wall time + per-stage runtimes;
2. WARM re-run (all HIT) for the smallest and largest jobs value;
3. RESUME: one market's SIMULATION artifact deleted / corrupted, and one market failing, then re-run;
4. speed invariants vs a full cold run: metric-only, cost-only, entry-rule change;
5. determinism: sha256 of metrics.json per market identical between all jobs values;
6. peak working set of the main process (ctypes; jobs=1 runs everything in-process). Worker memory: NOT_MEASURED.

Noise caveat: record machine load context (free RAM, other python processes) in the output.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

STAGES = ("FEATURES", "SIGNALS", "SIMULATION", "METRICS")


RUNS_MARKER = ".bench_workbench_runs"
ALLOWED_OUT_ROOTS = (ROOT / "artifacts" / "research_workbench_bench",)


def resolve_out_dir(raw: str) -> Path:
    """``--out`` must resolve below artifacts/research_workbench_bench or the OS temp dir; anything else is rejected."""
    import tempfile

    out = Path(raw).resolve()
    roots = [r.resolve() for r in ALLOWED_OUT_ROOTS] + [Path(tempfile.gettempdir()).resolve()]
    if not any(out == r or r in out.parents for r in roots):
        raise SystemExit(f"--out {out} is not below {roots[0]} or the OS temp dir: refused")
    return out


def peak_working_set_mb() -> float | None:
    if sys.platform != "win32":
        return None
    try:

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]  # fmt: skip

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.K32GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(Counters),
            ctypes.c_ulong,
        ]
        ok = ctypes.windll.kernel32.K32GetProcessMemoryInfo(
            ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb
        )
        return round(counters.PeakWorkingSetSize / 1e6, 1) if ok else None
    except Exception:
        return None


def load_context() -> dict:
    from research_speed.parallel import available_memory_mb

    others = "NOT_MEASURED"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 8 | ForEach-Object { '{0} {1:N0}MB' -f $_.ProcessName, ($_.WorkingSet64/1MB) }"],
            capture_output=True, text=True, timeout=30,
        ).stdout.split("\n")  # fmt: skip
        others = [line.strip() for line in out if line.strip()]
    except Exception:
        pass
    return {
        "free_ram_mb": available_memory_mb(),
        "top_processes_by_working_set": others,
        "cpu_count": os.cpu_count(),
    }


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_experiment(root: Path, markets: int, days: int):
    from research_workbench.experiment import DatasetRef, demo_synthetic_experiment

    exp = demo_synthetic_experiment(str(root))
    names = tuple(f"M{i + 1}" for i in range(markets))
    return replace(
        exp,
        markets=names,
        dataset=DatasetRef(kind="synthetic", seed=100, days=days, start="2024-03-04"),
    )


def stage_table(result: dict) -> dict:
    out = {}
    for market, rec in result["markets"].items():
        if rec["status"] == "FAILED":
            out[market] = {"status": "FAILED", "error": rec.get("error")}
            continue
        out[market] = {
            "status": rec["status"],
            "stages": {
                s: {"cache": v["cache"], "computed": v["computed"], "runtime_s": None if v["runtime_s"] is None else round(v["runtime_s"], 3)}
                for s, v in rec["stages"].items()
            },
        }  # fmt: skip
    return out


def metrics_hashes(root: Path, result: dict) -> dict:
    from research_workbench import dag

    store = dag.ArtifactStore(root)
    return {
        m: sha(store.stage_dir(m, "METRICS", rec["keys"]["METRICS"]) / "metrics.json")
        for m, rec in result["markets"].items()
        if rec["status"] != "FAILED"
    }


def timed_run(exp, jobs, **kw):
    from research_workbench.fastrun import FastGate, run_fast

    gate = FastGate(min_train_trades=1, min_validation_trades=1, require_positive_expectancy=False)
    t0 = time.perf_counter()
    result = run_fast(exp, jobs=jobs, gate=gate, **kw)
    return result, round(time.perf_counter() - t0, 3)


def stage_sums(result: dict) -> dict:
    sums = {s: 0.0 for s in STAGES}
    for rec in result["markets"].values():
        for s, v in (rec.get("stages") or {}).items():
            if v.get("computed") and v.get("runtime_s"):
                sums[s] += v["runtime_s"]
    return {s: round(v, 3) for s, v in sums.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", type=int, default=7)
    parser.add_argument("--days", type=int, default=40)
    parser.add_argument("--jobs", default="1,2,3,4")
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "research_workbench_bench"))
    args = parser.parse_args()
    os.environ.setdefault(
        "RESEARCH_SPEED_RESERVE_MB", "100"
    )  # the user lifted the RAM limit for this job
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[var] = "1"
    from research_workbench import dag

    out_dir = resolve_out_dir(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "runs"
    if work.exists():
        if not (work / RUNS_MARKER).is_file():
            print(
                f"refusing to delete {work}: it has no {RUNS_MARKER} marker written by this script",
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / RUNS_MARKER).write_text("created by scripts/bench_workbench.py\n", encoding="utf-8")
    jobs_list = [int(x) for x in args.jobs.split(",")]
    report: dict = {
        "config": {"markets": args.markets, "days": args.days, "jobs": jobs_list,
                   "note": "markets differ by seed (crc32 of the name); a per-market base price is not supported by DatasetRef"},
        "context_start": load_context(),
    }  # fmt: skip

    # (1) COLD per jobs value (+ peak working set of the main process, tagged)
    cold: dict = {}
    hashes: dict = {}
    for jobs in jobs_list:
        root = work / f"cold_j{jobs}"
        exp = build_experiment(root, args.markets, args.days)
        result, wall = timed_run(exp, jobs)
        cold[jobs] = {
            "wall_s": wall, "jobs_effective": result["jobs_effective"], "stage_runtime_sum_s": stage_sums(result),
            "n_failed": result["n_failed"], "main_process_peak_ws_mb": peak_working_set_mb(),
            "markets": stage_table(result),
        }  # fmt: skip
        hashes[jobs] = metrics_hashes(root, result)
    report["cold"] = {str(j): v for j, v in cold.items()}
    base_jobs = jobs_list[0]
    report["determinism"] = {
        "reference_jobs": base_jobs,
        "per_jobs_identical_to_reference": {str(j): hashes[j] == hashes[base_jobs] for j in jobs_list},
        "status": "PASS" if all(hashes[j] == hashes[base_jobs] for j in jobs_list) else "FAIL",
        "metrics_sha256_reference": hashes[base_jobs],
    }  # fmt: skip

    # (2) WARM (all HIT) on the cold roots of the smallest and largest jobs value
    report["warm"] = {}
    for jobs in sorted({jobs_list[0], jobs_list[-1]}):
        exp = build_experiment(work / f"cold_j{jobs}", args.markets, args.days)
        result, wall = timed_run(exp, jobs)
        all_hit = all(
            v["cache"] == "HIT" and not v["computed"]
            for r in result["markets"].values()
            for v in r["stages"].values()
        )
        report["warm"][str(jobs)] = {"wall_s": wall, "all_hit": all_hit}

    # (3) RESUME on a copy of the jobs=1 cold root
    ref_root = work / f"cold_j{base_jobs}"
    report["resume"] = {}
    for tag, action in (("delete_simulation_M3", "delete"), ("corrupt_simulation_M4", "corrupt")):
        root = work / f"resume_{tag}"
        shutil.copytree(ref_root, root)
        exp = build_experiment(root, args.markets, args.days)
        market = "M3" if action == "delete" else "M4"
        rec = json.loads(
            (root / "experiments" / exp.experiment_id / f"{market}.json").read_text("utf-8")
        )
        sim_dir = dag.ArtifactStore(root).stage_dir(market, "SIMULATION", rec["keys"]["SIMULATION"])
        if action == "delete":
            shutil.rmtree(sim_dir)
        else:
            npz = sim_dir / "trades.npz"
            data = bytearray(npz.read_bytes())
            data[len(data) // 2] ^= 0xFF
            npz.write_bytes(bytes(data))
        # METRICS of that market must also be invalidated for the damaged stage to be needed
        shutil.rmtree(dag.ArtifactStore(root).stage_dir(market, "METRICS", rec["keys"]["METRICS"]))
        result, wall = timed_run(exp, 1)
        report["resume"][tag] = {"wall_s": wall, "markets": stage_table(result)}
    # failing market: M6 raises while building; others complete; then a clean re-run recomputes only M6
    root = work / "resume_fail_M6"
    exp = build_experiment(root, args.markets, args.days)
    from research_workbench import fastrun

    real_prepare = fastrun._prepare

    def failing_prepare(experiment, market, *a, **k):
        if market == "M6":
            raise RuntimeError("injected failure (benchmark)")
        return real_prepare(experiment, market, *a, **k)

    fastrun._prepare = failing_prepare
    try:
        failed_result, failed_wall = timed_run(exp, 1)
    finally:
        fastrun._prepare = real_prepare
    rerun, rerun_wall = timed_run(exp, 1)
    report["resume"]["fail_M6"] = {
        "first_run_wall_s": failed_wall, "first_run_failed_markets": [m for m, r in failed_result["markets"].items() if r["status"] == "FAILED"],
        "rerun_wall_s": rerun_wall, "rerun_markets": stage_table(rerun),
    }  # fmt: skip

    # (4) speed invariants on copies of the warm jobs=1 root (single-process, comparable to cold jobs=1)
    cold1 = cold[base_jobs]["wall_s"]
    report["invariants"] = {"cold_jobs1_wall_s": cold1}
    from alpha.common.sim import COST_SCENARIOS
    from research_workbench.fastrun import METRIC_VERSION

    variants = {
        "metric_only": lambda e: (e, {"metric_version": METRIC_VERSION + "-bench"}),
        "cost_only": lambda e: (replace(e, cost_model=COST_SCENARIOS["SPREAD_STRESS"]), {}),
        "entry_rule": lambda e: (
            replace(e, strategy_spec=replace(e.strategy_spec, entry_rules=(replace(e.strategy_spec.entry_rules[0], threshold=30.0),))),
            {},
        ),
    }  # fmt: skip
    from research_workbench.fastrun import FastGate, run_fast

    gate = FastGate(min_train_trades=1, min_validation_trades=1, require_positive_expectancy=False)
    for name, make in variants.items():
        root = work / f"inv_{name}"
        shutil.copytree(ref_root, root)
        exp, extra = make(build_experiment(root, args.markets, args.days))
        t0 = time.perf_counter()
        result = run_fast(exp, jobs=1, gate=gate, **extra)
        wall = round(time.perf_counter() - t0, 3)
        caches = {
            s: sorted({r["stages"][s]["cache"] for r in result["markets"].values()}) for s in STAGES
        }
        report["invariants"][name] = {
            "wall_s": wall,
            "speedup_vs_cold_jobs1": round(cold1 / wall, 2),
            "stage_cache": caches,
            "stage_runtime_sum_s": stage_sums(result),
        }

    report["context_end"] = load_context()
    report["worker_memory"] = (
        "NOT_MEASURED (psutil not installed; only the main-process peak working set is recorded)"
    )
    (out_dir / "bench.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    (out_dir / "bench.md").write_text(render(report), encoding="utf-8")
    print(render(report))
    return 0


def render(r: dict) -> str:
    lines = [
        f"# Workbench benchmark ({r['config']['markets']} synthetic markets, {r['config']['days']} days each)",
        "",
    ]
    lines += [
        f"Context at start: free RAM {r['context_start']['free_ram_mb']} MB, cpus {r['context_start']['cpu_count']}; top processes {r['context_start']['top_processes_by_working_set'][:4]}",
        "",
    ]
    lines += [
        "## Cold (empty artifact root + FeatureStore cache) [MEASURED]",
        "",
        "| jobs req/eff | wall s | features s | signals s | simulation s | metrics s | main peak WS MB |",
        "|---|---|---|---|---|---|---|",
    ]
    for j, v in r["cold"].items():
        s = v["stage_runtime_sum_s"]
        lines.append(
            f"| {j}/{v['jobs_effective']} | {v['wall_s']} | {s['FEATURES']} | {s['SIGNALS']} | {s['SIMULATION']} | {s['METRICS']} | {v['main_process_peak_ws_mb']} |"
        )
    lines += [
        "",
        f"Determinism (metrics.json sha256 identical across jobs): **{r['determinism']['status']}**",
        "",
        "## Warm re-run [MEASURED]",
        "",
    ]
    lines += [f"- jobs={j}: {v['wall_s']} s, all HIT: {v['all_hit']}" for j, v in r["warm"].items()]
    lines += ["", "## Resume [MEASURED]", ""]
    for tag in ("delete_simulation_M3", "corrupt_simulation_M4"):
        v = r["resume"][tag]
        recomputed = {
            m: [s for s, x in rec["stages"].items() if x["computed"]]
            for m, rec in v["markets"].items()
            if rec["status"] != "FAILED"
        }
        lines.append(
            f"- {tag}: {v['wall_s']} s; recomputed stages per market: { {m: s for m, s in recomputed.items() if s} }"
        )
    f = r["resume"]["fail_M6"]
    recomputed = {
        m: [s for s, x in rec["stages"].items() if x["computed"]]
        for m, rec in f["rerun_markets"].items()
    }
    lines.append(
        f"- fail_M6: first run {f['first_run_wall_s']} s, failed {f['first_run_failed_markets']}; clean re-run {f['rerun_wall_s']} s, recomputed: { {m: s for m, s in recomputed.items() if s} }"
    )
    lines += [
        "",
        "## Speed invariants vs cold jobs=1 [MEASURED]",
        "",
        "| variant | wall s | speedup | cache per stage |",
        "|---|---|---|---|",
    ]
    lines.append(f"| cold (reference) | {r['invariants']['cold_jobs1_wall_s']} | 1.0 | all MISS |")
    for name in ("metric_only", "cost_only", "entry_rule"):
        v = r["invariants"][name]
        lines.append(
            f"| {name} | {v['wall_s']} | {v['speedup_vs_cold_jobs1']}x | {v['stage_cache']} |"
        )
    lines += [
        "",
        f"Worker memory: {r['worker_memory']}",
        f"Context at end: free RAM {r['context_end']['free_ram_mb']} MB; top processes {r['context_end']['top_processes_by_working_set'][:4]}",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
