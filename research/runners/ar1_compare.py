"""AR1 Phase 1 comparison: Momentum vs Breakout vs Pullback on GER40 M5 (research only).

    <PY> research/runners/ar1_compare.py [CONFIG_JSON] [OUT_DIR]

Protocol (enforced by construction):
  1. DEVELOPMENT phase runs on a frame TRUNCATED at the validation end - OOS bars do not exist
     for it. The whole small grid is evaluated on TRAIN and VALIDATION at BASE costs.
  2. A pre-declared selection rule picks at most one representative per family; the
     pre-registered canonical configs (fixed in the config file before the grid ran) are always
     included. The frozen set is hashed.
  3. FINAL phase: the OOS gate records the frozen hash (a different set later is refused) and
     the frozen candidates are evaluated once on OOS with the full frame.
  4. Cost stress, regime/session breakdown, MFE/MAE and verdicts are produced per frozen
     candidate. Nothing is ever labelled production-ready.
"""

from __future__ import annotations

import json
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
from alpha.common.metrics import breakdown, compute_metrics, material_difference  # noqa: E402
from alpha.common.protocol import (  # noqa: E402
    OosGate,
    Partition,
    SplitPlan,
    run_record,
    stable_hash,
)
from alpha.common.regime import regime_labels  # noqa: E402
from alpha.common.sim import (  # noqa: E402
    COST_SCENARIOS,
    ExitSpec,
    SimRules,
    SizingSpec,
    simulate,
)
from alpha.registry import FAMILIES, variant_id  # noqa: E402

DEV_PARTS = ("train", "validation")


class Runner:
    def __init__(self, cfg: dict, frame: Frame, plan: SplitPlan, part_label: str) -> None:
        self.cfg, self.fr, self.plan = cfg, frame, plan
        self.sizing = SizingSpec(**cfg["sizing"])
        self.rules = SimRules(**cfg["rules"])
        self.regimes = regime_labels(frame)
        self._sig: dict[tuple, object] = {}
        self.runs = 0
        self.label = part_label

    def signals(self, family: str, params: dict):
        key = (family, json.dumps(params, sort_keys=True))
        if key not in self._sig:
            self._sig[key] = FAMILIES[family].signals(self.fr, params)
        return self._sig[key]

    def trades(self, family: str, params: dict, exit_spec: ExitSpec, scenario: str) -> pd.DataFrame:
        self.runs += 1
        tr, _ = simulate(
            self.fr, self.signals(family, params), exit_spec, COST_SCENARIOS[scenario],
            self.sizing, self.rules,
        )  # fmt: skip
        if len(tr):
            idx = tr["decision_idx"].to_numpy()
            tr["trend"] = self.regimes["trend"][idx]
            tr["vol"] = self.regimes["vol"][idx]
        return tr

    def part_days(self, part: Partition) -> tuple[np.ndarray, int]:
        fr = self.fr
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
        return compute_metrics(
            self.in_part(tr, part), trading_days=days, window_bars=window,
            sizing=self.sizing, seed=self.cfg["seed"],
        )  # fmt: skip


def exit_from(d: dict) -> ExitSpec:
    return ExitSpec(d["kind"], float(d["r"]))


def _neighbours(v: dict, others: list[dict]) -> list[dict]:
    out = []
    for o in others:
        if o["family"] != v["family"] or o["exit"] != v["exit"] or o is v:
            continue
        if o["params"].get("kind") != v["params"].get("kind"):
            continue
        diff = [k for k in v["params"] if v["params"][k] != o["params"].get(k)]
        if len(diff) == 1:
            out.append(o)
    return out


def select_representatives(grid: list[dict], rule: dict) -> dict[str, dict | None]:
    picked: dict[str, dict | None] = {}
    for fam in FAMILIES:
        fam_runs = [g for g in grid if g["family"] == fam]
        best, best_score = None, -np.inf
        for v in fam_runs:
            t, va = v["train"], v["validation"]
            if (
                t["trades"] < rule["min_train_trades"]
                or va["trades"] < rule["min_validation_trades"]
            ):
                continue
            te, ve = t.get("expectancy_r"), va.get("expectancy_r")
            if te is None or ve is None or te <= 0 or ve <= 0:
                continue
            nb = _neighbours(v, fam_runs)
            if not nb:
                continue
            med_t = float(np.median([n["train"].get("expectancy_r") or 0.0 for n in nb]))
            med_v = float(np.median([n["validation"].get("expectancy_r") or 0.0 for n in nb]))
            if med_t <= rule["neighbour_median_expectancy_r_gt"]:
                continue
            if med_v <= rule["neighbour_median_expectancy_r_gt"]:
                continue
            score = min(te, ve)
            if score > best_score:
                best, best_score = v, score
        picked[fam] = best
    return picked


def development_phase(cfg: dict, dev: Runner) -> tuple[list[dict], dict]:
    grid: list[dict] = []
    parts = {"train": dev.plan.train, "validation": dev.plan.validation}
    for fam, mod in FAMILIES.items():
        for params in mod.GRID:
            for ex in cfg["exits"]:
                tr = dev.trades(fam, params, exit_from(ex), "BASE")
                rec = {"family": fam, "params": params, "exit": ex, "id": variant_id(fam, params)}
                for pname, part in parts.items():
                    rec[pname] = dev.metrics(tr, part)
                grid.append(rec)
    picked = select_representatives(grid, cfg["selection_rule"])
    return grid, picked


def grid_frame(grid: list[dict]) -> pd.DataFrame:
    rows = []
    for g in grid:
        row = {
            "family": g["family"],
            "variant": g["id"],
            "exit": f"{g['exit']['kind']}{g['exit']['r']:g}",
        }
        for part in DEV_PARTS:
            m = g[part]
            for k in ("trades", "expectancy_r", "profit_factor", "win_rate", "net_pnl_eur",
                      "max_drawdown_pct", "trades_per_day"):  # fmt: skip
                row[f"{part}_{k}"] = m.get(k)
        rows.append(row)
    return pd.DataFrame(rows)


def evaluate_candidate(runner: Runner, fam: str, params: dict, ex: dict, parts: dict) -> dict:
    """Per-partition metrics at every cost scenario, plus BASE trades for breakdowns."""
    out: dict = {
        "family": fam,
        "params": params,
        "exit": ex,
        "id": variant_id(fam, params),
        "cost": {},
    }
    base_trades = None
    for scen in COST_SCENARIOS:
        tr = runner.trades(fam, params, exit_from(ex), scen)
        if scen == "BASE":
            base_trades = tr
        out["cost"][scen] = {pn: runner.metrics(tr, p) for pn, p in parts.items()}
    out["_base_trades"] = base_trades
    return out


def mfe_mae_summary(tr: pd.DataFrame) -> dict:
    if not len(tr):
        return {}
    win = tr[tr["pnl_eur"] > 0]
    los = tr[tr["pnl_eur"] <= 0]
    return {
        "p_mfe_ge_1r": round(float((tr["mfe_r"] >= 1).mean()), 4),
        "p_mfe_ge_2r": round(float((tr["mfe_r"] >= 2).mean()), 4),
        "p_mfe_ge_3r": round(float((tr["mfe_r"] >= 3).mean()), 4),
        "winners_mae_r_median": round(float(win["mae_r"].median()), 4) if len(win) else None,
        "winners_mae_r_q90": round(float(win["mae_r"].quantile(0.9)), 4) if len(win) else None,
        "losers_mfe_r_median": round(float(los["mfe_r"].median()), 4) if len(los) else None,
        "losers_reaching_1r_before_stop": round(float((los["mfe_r"] >= 1).mean()), 4)
        if len(los)
        else None,
        "median_bars_to_mfe": float(tr["bars_to_mfe"].median()),
        "note": "MAE/MFE on executable side incl. costs at fill; input for later ExitPolicy work",
    }


def breakdown_tables(tr: pd.DataFrame) -> dict:
    if not len(tr):
        return {}
    out = {}
    for by in ("trend", "vol", "session", "side"):
        t = breakdown(tr, by)
        out[by] = {"rows": t.to_dict(orient="records"), "material": material_difference(t)}
    out["trend_x_vol"] = breakdown(tr.assign(tv=tr["trend"] + "/" + tr["vol"]), "tv").to_dict(
        orient="records"
    )
    tr = tr.assign(month=pd.to_datetime(tr["date"]).dt.strftime("%Y-%m"))
    out["month"] = breakdown(tr, "month").to_dict(orient="records")
    return out


def verdict(cand: dict, rule: dict) -> dict:
    checks = {}
    base = cand["cost"]["BASE"]
    total = sum(base[p]["trades"] for p in ("train", "validation", "oos"))
    checks["total_trades>=min"] = total >= rule["min_total_trades"]
    for p in rule["expectancy_r_gt_0_in"]:
        e = base[p].get("expectancy_r")
        checks[f"expectancy_r>0 in {p} (BASE)"] = e is not None and e > 0
    for scen in rule["expectancy_r_gt_0_under"]:
        for p in ("dev", "oos"):
            m = cand["cost"][scen]
            if p == "dev":
                t, v = m["train"], m["validation"]
                n = t["trades"] + v["trades"]
                e = (
                    (
                        (t.get("expectancy_r") or 0) * t["trades"]
                        + (v.get("expectancy_r") or 0) * v["trades"]
                    )
                    / n
                    if n
                    else None
                )
            else:
                e = m["oos"].get("expectancy_r")
            checks[f"expectancy_r>0 in {p} ({scen})"] = e is not None and e > 0
    pf_pool = cand.get("pooled_profit_factor")
    checks["pooled_profit_factor>min"] = (
        pf_pool is not None and pf_pool > rule["pooled_profit_factor_gt"]
    )
    dd = max((base[p].get("max_drawdown_pct") or 0.0) for p in ("train", "validation", "oos"))
    oos_pf = base["oos"].get("profit_factor")
    checks["oos_profit_factor>1"] = oos_pf is not None and oos_pf > 1.0
    checks["max_drawdown_pct<=limit"] = dd <= rule["max_drawdown_pct_le"]
    ok = all(checks.values())
    return {"checks": checks, "verdict": "RESEARCH_CANDIDATE" if ok else "REJECTED"}


def pooled_pf(tr: pd.DataFrame) -> float | None:
    if not len(tr):
        return None
    p = tr["pnl_eur"].to_numpy()
    loss = -p[p < 0].sum()
    return float(p[p > 0].sum() / loss) if loss > 0 else None


def main() -> int:
    cfg_path = (
        Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "research/configs/ar1_phase1.json"
    )
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else REPO_ROOT / "research/reports/ar1_phase1"
    out.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    plan = SplitPlan(**{k: Partition(k, *v) for k, v in cfg["splits"].items()})
    full = Frame.from_dataframe(ds.frame)
    # ---- DEVELOPMENT: OOS bars are physically absent -------------------------------------
    n_dev = int((full.date <= np.datetime64(plan.validation.end)).sum())
    dev = Runner(cfg, full.head(n_dev), plan, "dev")
    grid, picked = development_phase(cfg, dev)
    dev_runs = dev.runs
    gdf = grid_frame(grid)
    gdf.to_csv(out / "grid_runs.csv", index=False)

    # ---- FREEZE ---------------------------------------------------------------------------
    frozen: list[dict] = []
    canon = cfg["pre_registered_canonical"]
    for fam in FAMILIES:
        frozen.append(
            {"role": "CANONICAL", "family": fam, "params": canon[fam], "exit": canon["exit"]}
        )
        rep = picked[fam]
        if rep is not None and (rep["params"] != canon[fam] or rep["exit"] != canon["exit"]):
            frozen.append(
                {"role": "SELECTED", "family": fam, "params": rep["params"], "exit": rep["exit"]}
            )
    frozen_hash = stable_hash(frozen)
    (out / "frozen_candidates.json").write_text(
        json.dumps({"hash": frozen_hash, "set": frozen}, indent=1)
    )

    # ---- FINAL: OOS evaluated once for the frozen set --------------------------------------
    import hashlib

    src_files = sorted((REPO_ROOT / "src" / "alpha").rglob("*.py")) + [Path(__file__).resolve()]
    source_hashes = {
        str(f.relative_to(REPO_ROOT)).replace("\\", "/"): hashlib.sha256(
            f.read_bytes().replace(b"
", b"
")
        ).hexdigest()
        for f in src_files
    }
    components = {
        "frozen_hash": frozen_hash,
        "config_hash": stable_hash(cfg),
        "dataset_hashes": {m.month: m.content_sha256 for m in ds.months},
        "source_hashes": source_hashes,
    }
    fingerprint = stable_hash(components)
    OosGate(out / "oos_access_log.json").evaluate(
        fingerprint, f"AR1 frozen set {len(frozen)} candidates", components=components
    )
    final = Runner(cfg, full, plan, "final")
    parts = {"train": plan.train, "validation": plan.validation, "oos": plan.oos}
    cands = []
    for f in frozen:
        c = evaluate_candidate(final, f["family"], f["params"], f["exit"], parts)
        c["role"] = f["role"]
        n_var = sum(1 for g in grid if g["family"] == f["family"])
        c["selection_note"] = (
            "pre-registered canonical config, single test"
            if f["role"] == "CANONICAL"
            else f"best of {n_var} variants of this family on train/validation; its OOS is one "
            "confirmation, not a selection-corrected estimate"
        )
        base_tr = c.pop("_base_trades")
        dev_tr = pd.concat(
            [final.in_part(base_tr, plan.train), final.in_part(base_tr, plan.validation)]
        )
        oos_tr = final.in_part(base_tr, plan.oos)
        c["pooled_profit_factor"] = pooled_pf(pd.concat([dev_tr, oos_tr]))
        c["breakdowns_dev"] = breakdown_tables(dev_tr)
        c["breakdowns_oos"] = breakdown_tables(oos_tr)
        c["mfe_mae_dev"] = mfe_mae_summary(dev_tr)
        c["mfe_mae_oos"] = mfe_mae_summary(oos_tr)
        c["verdict"] = verdict(c, cfg["verdict_rule"])
        tag = f"{f['role']}_{f['family']}"
        base_tr.drop(columns=["entry_ts", "exit_ts"], errors="ignore").assign(
            entry_ts=base_tr["entry_ts"].astype(str), exit_ts=base_tr["exit_ts"].astype(str)
        ).to_csv(out / f"trades_{tag}.csv.gz", index=False)
        cands.append(c)

    record = run_record(
        repo=REPO_ROOT,
        config=cfg,
        dataset_provenance=ds.provenance(),
        seed=cfg["seed"],
        source_hashes=source_hashes,
        experiment_fingerprint=fingerprint,
    )
    record["research_runs"] = {
        "development_simulations": dev_runs,
        "final_simulations": final.runs,
        "grid_variants": len(grid),
        "signal_configs": sum(len(m.GRID) for m in FAMILIES.values()),
    }
    record["split_plan"] = plan.to_dict()
    record["bars"] = {
        "total": ds.n_bars,
        **{
            n: int(plan.mask(full.date, p).sum())
            for n, p in (("train", plan.train), ("validation", plan.validation), ("oos", plan.oos))
        },
    }
    (out / "run_record.json").write_text(json.dumps(record, indent=1, default=str))
    (out / "candidates.json").write_text(json.dumps(cands, indent=1, default=str))
    (out / "dataset_provenance.json").write_text(json.dumps(ds.provenance(), indent=1, default=str))
    print(json.dumps({c["id"] + "|" + c["role"]: c["verdict"]["verdict"] for c in cands}, indent=1))
    print("selected:", {k: (v["id"], v["exit"]) if v else None for k, v in picked.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
