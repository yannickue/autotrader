"""AD1B: throughput benchmark of the light-screen path with the REAL fast kernels.

Deterministically perturbs the registered variants of all kernel families, times candidate
generation and light screening separately, applies Stage-A rejects and reports throughput,
memory, CPU utilisation and result-cache behaviour.  Research only; no OOS data is loaded.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402

from alpha.common.dataset import POINT, load_research_dataset  # noqa: E402
from alpha.common.protocol import SplitPlan, stable_hash  # noqa: E402
from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec  # noqa: E402
from alpha.fast.registry import FastFamily, discover  # noqa: E402
from alpha.fast.screen import (  # noqa: E402
    LightScreenResult,
    PartitionScreen,
    light_screen,
    reject_reason,
)
from alpha.fast.spec import Rule, StopSpec, StrategySpec, TargetSpec  # noqa: E402
from alpha.fast.store import FEATURE_SCHEMA_VERSION, FeatureSet, FeatureStore  # noqa: E402
from research.runners import ar2_fast  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/ad1_discovery.json"
DEFAULT_CACHE = REPO_ROOT / "data/feature_store/ad1_bench"
REPORT_DIR = REPO_ROOT / "research/reports/alpha_discovery_v1"
SCREEN_SIM_VERSION = "ad1-light-screen-v1"
MIN_TRADES = 30
TARGET_SECONDS = 300.0


# --------------------------------------------------------------------------- param generation
def canonical_hash(strategy_id: str, params: Any) -> str:
    return stable_hash({"strategy_id": strategy_id, "params": dataclasses.asdict(params)})


def _perturb(params: Any, rng: np.random.Generator) -> Any:
    """Bounded multiplicative/additive perturbation; may raise ValueError from validation."""
    changes: dict[str, Any] = {}
    for field in dataclasses.fields(params):
        value = getattr(params, field.name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, np.integer)):
            changes[field.name] = max(1, int(value) + int(rng.integers(-3, 4)))
        elif isinstance(value, (float, np.floating)):
            new = float(value) * float(rng.uniform(0.5, 1.5))
            if field.name == "target_r":
                new = min(4.0, max(1.0, new))
            changes[field.name] = round(new, 3)
    return dataclasses.replace(params, **changes)


def generate_param_sets(
    families: dict[str, FastFamily], n: int, seed: int
) -> tuple[list[tuple[str, Any, str]], dict[str, int]]:
    """Return n distinct (strategy_id, params, hash) round-robin over families + counters."""
    rng = np.random.default_rng(seed)
    ids = sorted(families)
    out: list[tuple[str, Any, str]] = []
    seen: set[str] = set()
    stats = {"duplicates_rejected": 0, "invalid_params_skipped": 0, "attempts": 0}
    max_attempts = max(50, n * 30)
    while len(out) < n and stats["attempts"] < max_attempts:
        turn = stats["attempts"]
        stats["attempts"] += 1
        sid = ids[turn % len(ids)]
        variants = families[sid].variants
        round_ = turn // len(ids)
        try:
            if round_ < len(variants):
                params = variants[round_]  # the existing variants first, unperturbed
            else:
                params = _perturb(variants[int(rng.integers(len(variants)))], rng)
        except ValueError:
            stats["invalid_params_skipped"] += 1
            continue
        key = canonical_hash(sid, params)
        if key in seen:
            stats["duplicates_rejected"] += 1
            continue
        seen.add(key)
        out.append((sid, params, key))
    return out, stats


def _spec_for(sid: str, params: Any) -> StrategySpec:
    return StrategySpec(
        strategy_id=sid,
        version=SCREEN_SIM_VERSION,
        direction="BOTH",
        entry_rules=(Rule("c", ">", threshold=0.0),),
        stop=StopSpec("atr_multiple", feature="m5_atr14", multiple=1.0),
        target=TargetSpec("fixed_r", r=float(getattr(params, "target_r", 2.0))),
        params=ar2_fast._plain(params),
    )


# --------------------------------------------------------------------------- result cache
def _screen_to_json(result: LightScreenResult) -> dict:
    return {
        "train": dataclasses.asdict(result.train),
        "validation": dataclasses.asdict(result.validation),
    }


def _screen_from_json(raw: dict) -> LightScreenResult:
    return LightScreenResult(PartitionScreen(**raw["train"]), PartitionScreen(**raw["validation"]))


class ScreenCache:
    """In-process dict backed by one JSON file per fingerprint on disk."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.memory: dict[str, dict] = {}
        self.hits = self.misses = self.disk_hits = 0

    def get(self, key: str) -> dict | None:
        if key in self.memory:
            self.hits += 1
            return self.memory[key]
        path = self.root / f"{key}.json"
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                value = None
            if value is not None:
                self.memory[key] = value
                self.hits += 1
                self.disk_hits += 1
                return value
        self.misses += 1
        return None

    def put(self, key: str, value: dict) -> None:
        self.memory[key] = value
        (self.root / f"{key}.json").write_text(
            json.dumps(value, sort_keys=True, default=str), encoding="utf-8"
        )

    def reset_counters(self) -> None:
        self.hits = self.misses = self.disk_hits = 0


def _peak_rss_mb() -> float | None:
    try:
        import psutil  # type: ignore[import-not-found]

        return round(psutil.Process(os.getpid()).memory_info().peak_wset / 1024**2, 1)
    except (ImportError, AttributeError):
        pass
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        ok = fn(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return round(counters.PeakWorkingSetSize / 1024**2, 1) if ok else None
    try:
        import resource

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except ImportError:
        return None


# --------------------------------------------------------------------------- the benchmark
def _fingerprint(
    dataset_hash: str, feature_key: str | None, sid: str, params: Any,
    plan: SplitPlan, cfg: dict, scenario: str, kernel_hashes: dict[str, str],
) -> str:
    return stable_hash(
        {
            "dataset_hash": dataset_hash,
            "feature_key": feature_key,
            "feature_schema": FEATURE_SCHEMA_VERSION,
            "strategy_id": sid,
            "params": ar2_fast._plain(params),
            "cost": ar2_fast._plain(COST_SCENARIOS[scenario]),
            "split": plan.to_dict(),  # includes embargo_days
            "sizing": cfg["sizing"],
            "rules": cfg["rules"],
            "simulator_version": SCREEN_SIM_VERSION,
            "source_hashes": kernel_hashes,
        }
    )


def run_batch(
    param_sets: list[tuple[str, Any, str]],
    families: dict[str, FastFamily],
    features: FeatureSet,
    market: Any,
    dates: np.ndarray,
    plan: SplitPlan,
    cfg: dict,
    cache: ScreenCache,
    *,
    dataset_hash: str,
    kernel_hashes: dict[str, str],
    scenario: str = "BASE",
) -> dict:
    cost = COST_SCENARIOS[scenario]
    sizing = SizingSpec(**cfg["sizing"])
    rules = SimRules(**cfg["rules"])
    min_trades = cfg.get("sample_rules", {}).get("min_trades_flag", MIN_TRADES)
    feature_key = features.metadata.get("cache_key")
    per_family: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n": 0, "generate_s": 0.0, "screen_s": 0.0, "candidates": 0, "cache_hits": 0}
    )
    rejects: dict[str, int] = defaultdict(int)
    zero_candidate_by_family: dict[str, int] = defaultdict(int)
    gen_s = screen_s = lookup_s = 0.0
    total_candidates = sims = 0
    positive = {"train": 0, "validation": 0, "both": 0}
    errors = 0
    cache.reset_counters()
    started = time.perf_counter()
    for sid, params, _key in param_sets:
        fam = per_family[sid]
        fam["n"] += 1
        t0 = time.perf_counter()
        key = _fingerprint(
            dataset_hash, feature_key, sid, params, plan, cfg, scenario, kernel_hashes
        )
        record = cache.get(key)
        lookup_s += time.perf_counter() - t0
        if record is None:
            try:
                t0 = time.perf_counter()
                candidates = families[sid].generate(features, params)
                dt = time.perf_counter() - t0
                gen_s += dt
                fam["generate_s"] += dt
                n_cand = len(candidates.decision_idx)
                reason = reject_reason(
                    _spec_for(sid, params), candidates, min_trades=1, market=market,
                    sizing=sizing, cost=cost,
                )
                result = None
                if n_cand:
                    t0 = time.perf_counter()
                    result = light_screen(market, candidates, cost, plan, dates=dates,
                                          sizing=sizing, rules=rules)
                    dt = time.perf_counter() - t0
                    screen_s += dt
                    fam["screen_s"] += dt
                    sims += 1
                record = {
                    "n_candidates": n_cand,
                    "reason": reason.value if reason else None,
                    "result": _screen_to_json(result) if result else None,
                }
            except (ValueError, IndexError):
                errors += 1
                record = {"n_candidates": 0, "reason": "generate_error", "result": None}
            cache.put(key, record)
        else:
            fam["cache_hits"] += 1
        n_cand = record["n_candidates"]
        total_candidates += n_cand
        fam["candidates"] += n_cand
        if n_cand == 0:
            zero_candidate_by_family[sid] += 1
        result = _screen_from_json(record["result"]) if record["result"] else None
        reason_value = record["reason"]
        # Stage A min_trades applies to Train trades, not raw candidate count.
        if reason_value is None and result is not None and result.train.n_trades < min_trades:
            reason_value = "too_few_trades"
        if reason_value:
            rejects[reason_value] += 1
        if result is not None:
            tr = (result.train.expectancy_r or 0.0) > 0 and result.train.n_trades > 0
            va = (result.validation.expectancy_r or 0.0) > 0 and result.validation.n_trades > 0
            positive["train"] += tr
            positive["validation"] += va
            positive["both"] += tr and va
    wall = time.perf_counter() - started
    lookups = cache.hits + cache.misses
    return {
        "n": len(param_sets),
        "wall_s": round(wall, 3),
        "generate_s": round(gen_s, 3),
        "screen_s": round(screen_s, 3),
        "cache_lookup_s": round(lookup_s, 3),
        "simulations_run": sims,
        "total_candidates": total_candidates,
        "candidates_per_s": round(total_candidates / wall, 1) if wall else None,
        "variants_per_s": round(len(param_sets) / wall, 2) if wall else None,
        "sims_per_s_excl_generation": round(sims / screen_s, 2) if screen_s else None,
        "cache_hits": cache.hits,
        "cache_misses": cache.misses,
        "cache_disk_hits": cache.disk_hits,
        "cache_hit_rate": round(cache.hits / lookups, 4) if lookups else None,
        "rejects": dict(sorted(rejects.items())),
        "generate_errors": errors,
        "zero_candidate_by_family": dict(sorted(zero_candidate_by_family.items())),
        "positive_informational": positive,
        "per_family": {
            sid: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()}
            for sid, row in sorted(per_family.items())
        },
    }


def warmup(families: dict[str, FastFamily], features: FeatureSet, market: Any, dates: np.ndarray,
           plan: SplitPlan, cfg: dict) -> float:
    """JIT-compile every family's kernel once (excluded from the timed batches)."""
    started = time.perf_counter()
    cost = COST_SCENARIOS["BASE"]
    for fam in families.values():
        cand = fam.generate(features, fam.variants[0])
        if len(cand.decision_idx):
            light_screen(market, cand, cost, plan, dates=dates, sizing=SizingSpec(**cfg["sizing"]),
                         rules=SimRules(**cfg["rules"]))
    return time.perf_counter() - started


def profile_slowest(families: dict[str, FastFamily], param_sets: list, features: FeatureSet,
                    market: Any, dates: np.ndarray, plan: SplitPlan, cfg: dict,
                    per_family: dict) -> dict:
    import cProfile
    import io
    import pstats

    slowest = max(per_family, key=lambda s: per_family[s]["generate_s"] + per_family[s]["screen_s"])
    subset = [p for p in param_sets if p[0] == slowest][:20]
    cost = COST_SCENARIOS["BASE"]
    sizing, rules = SizingSpec(**cfg["sizing"]), SimRules(**cfg["rules"])
    prof = cProfile.Profile()
    prof.enable()
    for sid, params, _ in subset:
        cand = families[sid].generate(features, params)
        if len(cand.decision_idx):
            light_screen(market, cand, cost, plan, dates=dates, sizing=sizing, rules=rules)
    prof.disable()
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(8)
    return {"family": slowest, "n_profiled": len(subset), "top": buf.getvalue()[-2500:]}


def run(args: argparse.Namespace) -> dict:
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    plan = ar2_fast._plan(cfg)
    cache_dir = Path(args.cache_dir)
    wall_start = time.perf_counter()
    cpu_start = time.process_time()

    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    dev = ar2_fast.dev_frame(ds.frame, plan)

    feature_timing: dict[str, Any] = {"dev_bars": len(dev)}
    if not args.skip_cold:
        with tempfile.TemporaryDirectory(prefix="ad1fs_") as tmp:
            t0 = time.perf_counter()
            FeatureStore.load_or_build(dev, {"point_size": POINT}, Path(tmp))
            feature_timing["cold_build_s"] = round(time.perf_counter() - t0, 2)
    t0 = time.perf_counter()
    FeatureStore.load_or_build(dev, {"point_size": POINT}, cache_dir)  # ensure cache exists
    feature_timing["first_call_s"] = round(time.perf_counter() - t0, 2)
    t0 = time.perf_counter()
    features = FeatureStore.load_or_build(dev, {"point_size": POINT}, cache_dir)
    feature_timing["warm_load_s"] = round(time.perf_counter() - t0, 2)
    feature_timing["warm_cache_hit"] = bool(features.metadata.get("cache_hit"))

    market = ar2_fast._market(features)
    dates = ar2_fast._dates(features)
    dataset_hash = ar2_fast._feature_dataset_hash(features)
    kernel_hashes = ar2_fast._kernel_hashes()
    families = discover()

    param_sets, gen_stats = generate_param_sets(families, args.n, args.seed)
    param_hashes = [h for _, _, h in param_sets]
    n_unique_specs = len(set(param_hashes))

    results_dir = cache_dir / "screen_results"
    if not args.keep_results and results_dir.exists():
        for old in results_dir.glob("*.json"):
            old.unlink()
    cache = ScreenCache(results_dir)

    jit_s = warmup(families, features, market, dates, plan, cfg)
    batch_kwargs = dict(dataset_hash=dataset_hash, kernel_hashes=kernel_hashes)
    args_ = (param_sets, families, features, market, dates, plan, cfg, cache)
    first = run_batch(*args_, **batch_kwargs)
    second = run_batch(*args_, **batch_kwargs)

    profile = None
    if first["wall_s"] > TARGET_SECONDS:
        profile = profile_slowest(families, param_sets, features, market, dates, plan, cfg,
                                  first["per_family"])

    fam_total = {s: r["generate_s"] + r["screen_s"] for s, r in first["per_family"].items()}
    dominating = max(fam_total, key=fam_total.get) if fam_total else None
    wall = time.perf_counter() - wall_start
    cpu = time.process_time() - cpu_start
    report = {
        "version": "AD1B-benchmark-1",
        "n_requested": args.n,
        "seed": args.seed,
        "config": str(Path(args.config).relative_to(REPO_ROOT)).replace("\\", "/")
        if Path(args.config).is_absolute() else str(args.config),
        "embargo_days": plan.embargo_days,
        "features": feature_timing,
        "jit_warmup_s": round(jit_s, 2),
        "param_generation": {
            **gen_stats,
            "unique_specs": n_unique_specs,
            "total_trials": len(param_sets),
            "families": len(families),
            "first_hashes_sha": stable_hash(param_hashes),
        },
        "first_pass": first,
        "second_pass_warm_cache": second,
        "dominating_family": dominating,
        "target_seconds": TARGET_SECONDS,
        "target_met": first["wall_s"] <= TARGET_SECONDS,
        "process_wall_s": round(wall, 2),
        "process_cpu_s": round(cpu, 2),
        "cpu_utilisation": round(cpu / wall, 3) if wall else None,
        "peak_rss_mb": _peak_rss_mb(),
        "profile": profile,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"benchmark_{args.n}.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return report


def summarize(report: dict) -> str:
    f, s = report["first_pass"], report["second_pass_warm_cache"]
    pg = report["param_generation"]
    return "\n".join(
        [
            f"AD1B benchmark N={report['n_requested']} seed={report['seed']} "
            f"bars={report['features']['dev_bars']:,}",
            f"features: cold {report['features'].get('cold_build_s')}s warm "
            f"{report['features']['warm_load_s']}s | JIT warmup {report['jit_warmup_s']}s",
            f"pass1 wall {f['wall_s']}s = generate {f['generate_s']}s + screen {f['screen_s']}s "
            f"(+lookup {f['cache_lookup_s']}s)",
            f"throughput: {f['variants_per_s']} variants/s, {f['candidates_per_s']} cand/s, "
            f"{f['sims_per_s_excl_generation']} sims/s (screen only)",
            f"pass2 (warm cache) wall {s['wall_s']}s hit rate {s['cache_hit_rate']} "
            f"(pass1 {f['cache_hit_rate']})",
            f"peak RSS {report['peak_rss_mb']} MB | CPU util {report['cpu_utilisation']}",
            f"trials: {pg['total_trials']} total / {pg['unique_specs']} unique; dup rejected "
            f"{pg['duplicates_rejected']}, invalid skipped {pg['invalid_params_skipped']}",
            f"rejects: {f['rejects']} | generate errors {f['generate_errors']}",
            f"positive (info): {f['positive_informational']} | "
            f"dominating: {report['dominating_family']}",
            f"<=5 min target met: {report['target_met']} -> research/reports/alpha_discovery_v1/"
            f"benchmark_{report['n_requested']}.json",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--skip-cold", action="store_true", help="skip the cold feature build")
    parser.add_argument("--keep-results", action="store_true",
                        help="keep on-disk screen results from earlier invocations")
    args = parser.parse_args(argv)
    print(summarize(run(args)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
