"""AR2 Phase 2 multi-timeframe strategy lab (research only).

    <PY> research/runners/ar2_compare.py [CONFIG_JSON] [OUT_DIR]          # DEV + FREEZE
    <PY> research/runners/ar2_compare.py --oos [CONFIG_JSON] [OUT_DIR]    # OOS, frozen set only

Protocol (enforced by construction, same as AR1):
  1. DEV runs on a frame physically truncated at the validation end: OOS bars do not exist.
  2. FREEZE derives the candidate set from TRAIN + VALIDATION only, by the pre-registered rule
     in the config, and hashes it. The OOS subcommand is never run automatically.
  3. `--oos` refuses unless the frozen file's hash matches, binds the full experiment
     fingerprint into the OosGate and evaluates each frozen candidate once.
Strategies are discovered dynamically from alpha.strategies.* (packages exposing VARIANTS).
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import pkgutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from alpha.common.dataset import load_research_dataset  # noqa: E402
from alpha.common.frame import ENTRY_END_MIN, ENTRY_START_MIN, FLAT_MIN, Frame  # noqa: E402
from alpha.common.metrics import compute_metrics  # noqa: E402
from alpha.common.protocol import (  # noqa: E402
    OosGate,
    Partition,
    SplitPlan,
    run_record,
    stable_hash,
)
from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec  # noqa: E402
from alpha.signals import (  # noqa: E402
    CandidateStrategyBase,
    evaluate_candidates,
    generate_candidates,
    grouped_metrics,
    record_conflicts,
)
from alpha.strategies._labels import frame_labels  # noqa: E402
from alpha.timeframe import MtfView  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/ar2_phase2.json"
DEFAULT_OUT = REPO_ROOT / "research/reports/ar2_phase2"
PART_NAMES = ("train", "validation", "oos")
DIMENSIONS = ("DIRECTION", "TREND_STRENGTH", "VOLATILITY", "VOL_STATE")


# ------------------------------------------------------------------ discovery
def discover_variants(package: str = "alpha.strategies") -> list[dict]:
    """Find every strategy package exposing VARIANTS; no hard-coded list."""
    pkg = importlib.import_module(package)
    out: list[dict] = []
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if not info.ispkg:
            continue
        mod = importlib.import_module(f"{package}.{info.name}")
        variants = getattr(mod, "VARIANTS", None)
        if variants is None:
            continue
        classes = [
            obj
            for _, obj in inspect.getmembers(mod, inspect.isclass)
            if issubclass(obj, CandidateStrategyBase)
            and not inspect.isabstract(obj)
            and getattr(obj, "strategy_id", "")
            and obj.__module__.startswith(mod.__name__)  # defined here, not an imported base
        ]
        if len(classes) != 1:
            raise RuntimeError(f"{package}.{info.name} must export exactly one strategy class")
        cls = classes[0]
        for index, params in enumerate(variants):
            out.append(
                {
                    "package": info.name,
                    "cls": cls,
                    "params": params,
                    "strategy_id": cls.strategy_id,
                    "strategy_version": cls.strategy_version,
                    "variant": index,
                    "id": f"{cls.strategy_id}#{index}",
                }
            )
    return out


# ------------------------------------------------------------------ evaluation core
class Lab:
    def __init__(self, cfg: dict, df: pd.DataFrame, plan: SplitPlan) -> None:
        self.cfg, self.plan, self.df = cfg, plan, df
        self.frame = Frame.from_dataframe(df)
        self.sizing = SizingSpec(**cfg["sizing"])
        self.rules = SimRules(**cfg["rules"])
        self.runs = 0

    @property
    def view(self) -> MtfView:
        """The frame's causal MTF view, shared with the strategies (built once, on demand)."""
        return frame_labels(self.df)[0]

    def candidates(self, variant: dict) -> list:
        strategy = variant["cls"](self.df, variant["params"])
        return generate_candidates(strategy, self.view)

    def candidates_many(self, variants: list[dict], workers: int = 1) -> dict[str, list]:
        """Candidates per variant id. Variants are independent and deterministic, so they may be
        generated in parallel worker processes; the result is identical to the sequential run."""
        if workers <= 1 or len(variants) <= 1:
            return {v["id"]: self.candidates(v) for v in variants}
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(
            max_workers=min(workers, len(variants)), initializer=_worker_init, initargs=(self.df,)
        ) as pool:
            results = list(pool.map(_worker_generate, [v["id"] for v in variants]))
        return dict(results)

    def trades(self, candidates: list, scenario: str) -> pd.DataFrame:
        self.runs += 1
        return evaluate_candidates(
            self.frame, candidates, cost=COST_SCENARIOS[scenario],
            sizing=self.sizing, rules=self.rules,
        ).trades  # fmt: skip

    def part_days(self, part: Partition) -> tuple[np.ndarray, int]:
        fr = self.frame
        m = self.plan.mask(fr.date, part)
        in_entry = m & (fr.minute >= ENTRY_START_MIN) & (fr.minute < ENTRY_END_MIN)
        days = np.unique(fr.date[in_entry])
        window = int((m & (fr.minute >= ENTRY_START_MIN) & (fr.minute < FLAT_MIN)).sum())
        return days, window

    def in_part(self, tr: pd.DataFrame, part: Partition) -> pd.DataFrame:
        if not len(tr):
            return tr
        return tr[self.plan.mask(tr["date"].to_numpy(), part)]

    def metrics(self, tr: pd.DataFrame, part: Partition) -> dict:
        days, window = self.part_days(part)
        m = compute_metrics(
            self.in_part(tr, part), trading_days=days, window_bars=window,
            sizing=self.sizing, seed=self.cfg["seed"],
        )  # fmt: skip
        m["insufficient_sample"] = m["trades"] < self.cfg["sample_rules"]["min_trades_flag"]
        return m


_WORKER: dict = {}


def _worker_init(df: pd.DataFrame) -> None:
    _WORKER["df"] = df


def _worker_generate(variant_id: str) -> tuple[str, list]:
    variant = next(v for v in discover_variants() if v["id"] == variant_id)
    df = _WORKER["df"]
    view, _, _ = frame_labels(df)  # view + labels built once per worker, then cached
    strategy = variant["cls"](df, variant["params"])
    return variant_id, generate_candidates(strategy, view)


def parts_of(plan: SplitPlan, names: tuple[str, ...]) -> dict[str, Partition]:
    lookup = {"train": plan.train, "validation": plan.validation, "oos": plan.oos}
    return {n: lookup[n] for n in names}


def restrict(candidates: list, restriction: dict | None) -> list:
    if not restriction:
        return candidates
    dim, values = restriction["dimension"], set(restriction["values"])
    return [c for c in candidates if c.h1_regime[dim] in values]


def pf(tr: pd.DataFrame) -> float | None:
    if not len(tr):
        return None
    p = tr["pnl_eur"].to_numpy(float)
    loss = -p[p < 0].sum()
    return float(p[p > 0].sum() / loss) if loss > 0 else None


def expectancy_without_top_winners(tr: pd.DataFrame, n: int) -> float | None:
    if len(tr) <= n:
        return None
    r = np.sort(tr["r_multiple"].to_numpy(float))
    return float(r[:-n].mean())


def dev_frame(df: pd.DataFrame, plan: SplitPlan) -> pd.DataFrame:
    """Physically drop every bar after the validation end: OOS does not exist in development."""
    local = pd.DatetimeIndex(df["ts"]).tz_convert("Europe/Berlin").normalize().tz_localize(None)
    keep = local <= pd.Timestamp(plan.validation.end)
    return df.loc[keep].reset_index(drop=True)


# ------------------------------------------------------------------ dev phase
def development_phase(
    cfg: dict, lab: Lab, variants: list[dict], workers: int = 1
) -> tuple[list[dict], dict]:
    parts = parts_of(lab.plan, ("train", "validation"))
    records: list[dict] = []
    matrix_rows: list[pd.DataFrame] = []
    session_rows: list[pd.DataFrame] = []
    all_candidates: list = []
    generated = lab.candidates_many(variants, workers)
    for variant in variants:
        cands = generated[variant["id"]]
        all_candidates.extend(cands)
        tr = lab.trades(cands, "BASE")
        rec = {k: variant[k] for k in ("id", "strategy_id", "strategy_version", "variant")}
        rec["params"] = dict(variant["params"].__dict__)
        rec["n_candidates"] = len(cands)
        for name, part in parts.items():
            rec[name] = lab.metrics(tr, part)
            sub = lab.in_part(tr, part)
            days, _ = lab.part_days(part)
            gm = grouped_metrics(
                sub, trading_days=days, minimum_trades=cfg["sample_rules"]["min_trades_flag"]
            ) if len(sub) else pd.DataFrame()  # fmt: skip
            if len(gm):
                matrix_rows.append(gm.assign(variant_id=variant["id"], partition=name))
            if len(sub):
                sess = (
                    sub.groupby("session_phase")["r_multiple"]
                    .agg(n_trades="size", expectancy_r="mean")
                    .reset_index()
                    .assign(variant_id=variant["id"], partition=name)
                )
                session_rows.append(sess)
        rec["_trades"] = tr
        rec["_cands"] = cands
        records.append(rec)
    conflicts = record_conflicts(all_candidates)
    extras = {
        "matrix": pd.concat(matrix_rows, ignore_index=True) if matrix_rows else pd.DataFrame(),
        "session": pd.concat(session_rows, ignore_index=True) if session_rows else pd.DataFrame(),
        "conflicts": conflicts,
    }
    return records, extras


# ------------------------------------------------------------------ freeze rule
def _restriction_for(rec: dict, rule: dict) -> dict | None:
    """One regime restriction from TRAIN+VALIDATION only (best dimension by min expectancy)."""
    tr = rec["_trades"]
    if not len(tr):
        return None
    best: tuple[float, dict] | None = None
    for dim in DIMENSIONS:
        values = tr["h1_regime"].map(lambda d, dim=dim: d[dim])
        keep, scores = [], []
        for value in sorted(values.unique()):
            sub = tr[values == value]
            per = {}
            for name in ("train", "validation"):
                ss = sub[
                    sub["date"].between(
                        np.datetime64(rec["_bounds"][name][0]),
                        np.datetime64(rec["_bounds"][name][1]),
                    )
                ]
                per[name] = (len(ss), float(ss["r_multiple"].mean()) if len(ss) else -1.0)
            if (
                per["train"][0] >= rule["min_train_trades"]
                and per["validation"][0] >= rule["min_validation_trades"]
                and per["train"][1] > 0
                and per["validation"][1] > 0
            ):
                keep.append(value)
                scores.append(min(per["train"][1], per["validation"][1]))
        if keep:
            score = float(np.mean(scores))
            if best is None or score > best[0]:
                best = (score, {"dimension": dim, "values": keep})
    return best[1] if best else None


def _survives_stress(lab: Lab, cands: list, restriction: dict | None, rule: dict) -> bool:
    """Development-window (TRAIN+VALIDATION) expectancy stays positive under cost stress."""
    subset = restrict(cands, restriction)
    for scen in rule["expectancy_r_gt_0_under"]:
        tr = lab.trades(subset, scen)
        dev = pd.concat([lab.in_part(tr, lab.plan.train), lab.in_part(tr, lab.plan.validation)])
        if not len(dev) or float(dev["r_multiple"].mean()) <= 0:
            return False
    return True


def freeze_rule(cfg: dict, lab: Lab, records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Deterministic freeze from TRAIN + VALIDATION. Returns (frozen set, rejected strategies)."""
    rule = cfg["freeze_rule"]
    bounds = {n: (p.start, p.end) for n, p in parts_of(lab.plan, ("train", "validation")).items()}
    for rec in records:
        rec["_bounds"] = bounds
    by_strategy: dict[str, list[dict]] = {}
    for rec in records:
        by_strategy.setdefault(rec["strategy_id"], []).append(rec)

    def exp(rec: dict, part: str) -> float | None:
        return rec[part].get("expectancy_r")

    rejected = [
        {"strategy_id": sid, "reason": "REJECTED_EARLY: negative in TRAIN and VALIDATION"}
        for sid, recs in by_strategy.items()
        if all(
            (exp(r, "train") is None or exp(r, "train") <= 0)
            and (exp(r, "validation") is None or exp(r, "validation") <= 0)
            for r in recs
        )
    ]
    rejected_ids = {r["strategy_id"] for r in rejected}
    frozen: list[dict] = []
    for sid, recs in sorted(by_strategy.items()):
        if sid in rejected_ids:
            continue
        for rec in recs:
            t, v = rec["train"], rec["validation"]
            checks = {
                "train_trades": t["trades"] >= rule["min_train_trades"],
                "validation_trades": v["trades"] >= rule["min_validation_trades"],
                "positive_expectancy_both": (exp(rec, "train") or 0) > 0
                and (exp(rec, "validation") or 0) > 0,
                "profit_factor_both": (t.get("profit_factor") or 0)
                > rule["min_profit_factor_each_partition"]
                and (v.get("profit_factor") or 0) > rule["min_profit_factor_each_partition"],
                "drawdown": max(t.get("max_drawdown_pct") or 0.0, v.get("max_drawdown_pct") or 0.0)
                <= rule["max_drawdown_pct_le"],
            }
            dev_tr = pd.concat(
                [lab.in_part(rec["_trades"], lab.plan.train),
                 lab.in_part(rec["_trades"], lab.plan.validation)]
            )  # fmt: skip
            top = expectancy_without_top_winners(
                dev_tr, rule["expectancy_r_gt_0_after_removing_top_n_winners"]
            )
            checks["not_dependent_on_top_winners"] = top is not None and top > 0
            siblings = [r for r in recs if r is not rec]
            checks["siblings_positive"] = all(
                (
                    (
                        (exp(s, "train") or 0) * s["train"]["trades"]
                        + (exp(s, "validation") or 0) * s["validation"]["trades"]
                    )
                    / max(s["train"]["trades"] + s["validation"]["trades"], 1)
                )
                > rule["sibling_variant_pooled_expectancy_r_gt"]
                for s in siblings
            )
            restriction = _restriction_for(rec, rule["regime_restriction"])
            if all(checks.values()):
                checks["survives_cost_stress"] = _survives_stress(lab, rec["_cands"], None, rule)
            rec["freeze_checks"] = checks
            if all(checks.values()):
                frozen.append(
                    {
                        "role": "WHOLE_VARIANT",
                        "strategy_id": sid,
                        "strategy_version": rec["strategy_version"],
                        "variant": rec["variant"],
                        "params": rec["params"],
                        "restriction": None,
                    }
                )
            if (
                restriction is not None
                and t["trades"] > 0
                and _survives_stress(lab, rec["_cands"], restriction, rule)
            ):
                frozen.append(
                    {
                        "role": "REGIME_RESTRICTED",
                        "strategy_id": sid,
                        "strategy_version": rec["strategy_version"],
                        "variant": rec["variant"],
                        "params": rec["params"],
                        "restriction": restriction,
                    }
                )
    return frozen, rejected


# ------------------------------------------------------------------ OOS phase
def source_hashes() -> dict[str, str]:
    files = [*sorted((REPO_ROOT / "src" / "alpha").rglob("*.py")), Path(__file__).resolve()]
    return {
        str(f.relative_to(REPO_ROOT)).replace("\\", "/"): hashlib.sha256(
            f.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for f in files
    }


def experiment_components(cfg: dict, ds, frozen_hash: str) -> dict:
    from importlib import metadata

    from alpha.common.protocol import git_commit

    try:
        nautilus = metadata.version("nautilus_trader")
    except metadata.PackageNotFoundError:
        nautilus = "NOT_INSTALLED"
    return {
        "git_commit": git_commit(REPO_ROOT),
        "frozen_hash": frozen_hash,
        "config_hash": stable_hash(cfg),  # includes splits, cost model, exit baseline, timeframes
        "dataset_hashes": {m.month: m.content_sha256 for m in ds.months},
        "source_hashes": source_hashes(),  # strategy/regime/context definitions and versions
        "nautilus_version": nautilus,
    }


def run_oos(cfg: dict, out: Path) -> int:
    frozen_path = out / "frozen_candidates.json"
    if not frozen_path.exists():
        raise SystemExit("REFUSED: no frozen_candidates.json - run the DEV/FREEZE phase first")
    payload = json.loads(frozen_path.read_text(encoding="utf-8"))
    if stable_hash(payload["set"]) != payload["hash"]:
        raise SystemExit("REFUSED: frozen set hash mismatch (file was modified after freezing)")
    if not payload["set"]:
        raise SystemExit("Nothing frozen: no candidate met the pre-registered rule; OOS not run")
    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    plan = SplitPlan(**{k: Partition(k, *v) for k, v in cfg["splits"].items()})
    components = experiment_components(cfg, ds, payload["hash"])
    fingerprint = stable_hash(components)
    OosGate(out / "oos_access_log.json").evaluate(
        fingerprint, f"AR2 frozen set {len(payload['set'])} candidates", components=components
    )
    lab = Lab(cfg, ds.frame, plan)
    variants = {(v["strategy_id"], v["variant"]): v for v in discover_variants()}
    results = []
    for item in payload["set"]:
        variant = variants[(item["strategy_id"], item["variant"])]
        if variant["strategy_version"] != item["strategy_version"]:
            raise SystemExit(f"REFUSED: strategy version changed for {item['strategy_id']}")
        if dict(variant["params"].__dict__) != item["params"]:
            raise SystemExit(f"REFUSED: parameters changed for {item['strategy_id']}")
        cands = restrict(lab.candidates(variant), item["restriction"])
        cost = {}
        base_tr = None
        for scen in COST_SCENARIOS:
            tr = lab.trades(cands, scen)
            if scen == "BASE":
                base_tr = tr
            cost[scen] = {n: lab.metrics(tr, p) for n, p in parts_of(plan, PART_NAMES).items()}
        oos_tr = lab.in_part(base_tr, plan.oos)
        results.append(
            {
                **item,
                "cost": cost,
                "oos_pf": pf(oos_tr),
                "oos_expectancy_without_top2": expectancy_without_top_winners(oos_tr, 2),
                "note": "single OOS evaluation of a frozen candidate; never production-ready",
            }
        )
    (out / "oos_results.json").write_text(json.dumps(results, indent=1, default=str))
    record = run_record(
        repo=REPO_ROOT, config=cfg, dataset_provenance=ds.provenance(), seed=cfg["seed"],
        source_hashes=components["source_hashes"], experiment_fingerprint=fingerprint,
    )  # fmt: skip
    (out / "oos_run_record.json").write_text(json.dumps(record, indent=1, default=str))
    print(json.dumps({r["strategy_id"] + "|" + r["role"]: "evaluated" for r in results}, indent=1))
    return 0


# ------------------------------------------------------------------ main
def public(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def main() -> int:
    argv = sys.argv[1:]
    if "--workers" in argv:  # drop the option and its value from the positional arguments
        at = argv.index("--workers")
        del argv[at : at + 2]
    args = [a for a in argv if not a.startswith("--")]
    cfg_path = Path(args[0]) if args else DEFAULT_CONFIG
    out = Path(args[1]) if len(args) > 1 else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if "--oos" in sys.argv:
        return run_oos(cfg, out)

    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    plan = SplitPlan(**{k: Partition(k, *v) for k, v in cfg["splits"].items()})
    dev_df = dev_frame(ds.frame, plan)  # OOS bars physically absent
    n_dev = len(dev_df)
    lab = Lab(cfg, dev_df, plan)
    variants = discover_variants()
    workers = 1
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
    records, extras = development_phase(cfg, lab, variants, workers)
    frozen, rejected = freeze_rule(cfg, lab, records)
    frozen_hash = stable_hash(frozen)
    (out / "frozen_candidates.json").write_text(
        json.dumps({"hash": frozen_hash, "set": frozen, "rejected_early": rejected}, indent=1)
    )
    (out / "dev_results.json").write_text(
        json.dumps([public(r) for r in records], indent=1, default=str)
    )
    if len(extras["matrix"]):
        extras["matrix"].to_csv(out / "regime_context_matrix.csv", index=False)
    if len(extras["session"]):
        extras["session"].to_csv(out / "session_matrix.csv", index=False)
    conflicts = [
        {"kind": c.kind, "timestamp": str(c.timestamp), "strategies": list(c.strategy_ids)}
        for c in extras["conflicts"]
    ]
    (out / "conflicts.json").write_text(json.dumps(conflicts, indent=1))
    record = run_record(
        repo=REPO_ROOT, config=cfg, dataset_provenance=ds.provenance(), seed=cfg["seed"],
        source_hashes=source_hashes(), experiment_fingerprint=frozen_hash,
    )  # fmt: skip
    record["research_runs"] = {
        "variants": len(variants),
        "development_simulations": lab.runs,
        "strategies": len({v["strategy_id"] for v in variants}),
    }
    record["split_plan"] = plan.to_dict()
    record["dev_bars"] = n_dev
    (out / "dev_run_record.json").write_text(json.dumps(record, indent=1, default=str))
    summary = {
        "variants": len(variants),
        "frozen": len(frozen),
        "rejected_early": [r["strategy_id"] for r in rejected],
    }
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
