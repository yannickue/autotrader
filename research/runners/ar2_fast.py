"""Fast array-native AR2 TRAIN/VALIDATION runner and separately gated OOS command."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import platform
import sys
import time
import traceback
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from alpha.common.dataset import POINT, load_research_dataset  # noqa: E402
from alpha.common.frame import ENTRY_END_MIN, ENTRY_START_MIN, FLAT_MIN  # noqa: E402
from alpha.common.protocol import (  # noqa: E402
    OosGate,
    Partition,
    SplitPlan,
    git_commit,
    stable_hash,
)
from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec  # noqa: E402
from alpha.fast.registry import FastFamily, discover  # noqa: E402
from alpha.fast.sim import (  # noqa: E402
    CandidateArrays,
    MarketArrays,
    TradeArrays,
    day_clustered_mean_ci,
    simulate_fast,
)
from alpha.fast.store import FEATURE_SCHEMA_VERSION, FeatureSet, FeatureStore  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/ar2_phase2.json"
DEFAULT_OUT = REPO_ROOT / "scratch/ar2_fast"
DEFAULT_CACHE = REPO_ROOT / "data/feature_store/ar2_fast"
EVALUATOR_VERSION = "ar2-fast-v1"
DIMENSIONS = ("DIRECTION", "TREND_STRENGTH", "VOLATILITY", "VOL_STATE")
CONTEXT_NAMES = (
    "TREND_CONTINUATION",
    "PULLBACK",
    "CONSOLIDATION",
    "COMPRESSION",
    "RANGE_EXTREME",
    "BREAKOUT_SETUP",
    "RETEST",
    "FAILED_BREAKOUT",
    "MOMENTUM_CONTINUATION",
    "REVERSAL_CONTEXT",
)


def progress(stage: str, message: str) -> None:
    print(f"[{stage}] {message}", flush=True)


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def source_hashes() -> dict[str, str]:
    files = [*sorted((REPO_ROOT / "src" / "alpha").rglob("*.py")), Path(__file__).resolve()]
    return {str(path.relative_to(REPO_ROOT)).replace("\\", "/"): _sha(path) for path in files}


def dev_frame(df: pd.DataFrame, plan: SplitPlan) -> pd.DataFrame:
    """Physically remove OOS bars before any feature or candidate construction."""
    local = pd.DatetimeIndex(df["ts"]).tz_convert("Europe/Berlin").normalize().tz_localize(None)
    return df.loc[local <= pd.Timestamp(plan.validation.end)].reset_index(drop=True)


def _plan(cfg: dict) -> SplitPlan:
    return SplitPlan(
        **{name: Partition(name, *bounds) for name, bounds in cfg["splits"].items()},
        embargo_days=cfg.get("embargo_days", 0),
    )


def _dates(features: FeatureSet) -> np.ndarray:
    utc = pd.to_datetime(features["ts_ns"], utc=True)
    return (
        utc.tz_convert("Europe/Berlin")
        .normalize()
        .tz_localize(None)
        .to_numpy()
        .astype("datetime64[D]")
    )


def _market(features: FeatureSet) -> MarketArrays:
    contig = np.asarray(features["contig"], dtype=bool)
    return MarketArrays(
        features["o"],
        features["h"],
        features["l"],
        features["c"],
        features["spread"],
        features["berlin_minute"],
        features["berlin_day_id"],
        np.r_[contig[1:], False],
    )


def _subset(trades: TradeArrays, mask: np.ndarray) -> TradeArrays:
    values = []
    for name in trades.__dataclass_fields__:
        value = getattr(trades, name)
        values.append(value if name == "skip_counts" else value[mask])
    return TradeArrays(*values)


def _partition_mask(
    trades: TradeArrays, dates: np.ndarray, plan: SplitPlan, part: Partition
) -> np.ndarray:
    if not len(trades):
        return np.zeros(0, dtype=bool)
    entry_dates = dates[trades.entry_idx]
    return plan.mask(entry_dates, part)


def _finite(value: float) -> float | None:
    value = float(value)
    return round(value, 6) if math.isfinite(value) else None


def complete_metrics(
    trades: TradeArrays,
    mask: np.ndarray,
    *,
    trading_days: np.ndarray,
    window_bars: int,
    equity_eur: float,
    min_trades: int,
    seed: int,
) -> dict[str, Any]:
    """Full AR2 metric set computed directly from TradeArrays."""
    tr = _subset(trades, np.asarray(mask, dtype=bool))
    n = len(tr)
    n_days = len(np.unique(trading_days))
    result: dict[str, Any] = {
        "trades": n,
        "trading_days": n_days,
        "trades_per_day": _finite(n / n_days) if n_days else None,
        "median_trades_per_day": 0.0 if n_days else None,
        "zero_trade_days": n_days,
        "net_pnl_eur": 0.0,
        "insufficient_sample": n < min_trades,
    }
    if not n:
        return result
    r = tr.r_multiple.astype(float)
    pnl = tr.net_pnl_eur.astype(float)
    wins, losses = r[r > 0], r[r < 0]
    pnl_wins, pnl_losses = pnl[pnl > 0], pnl[pnl < 0]
    order = np.argsort(tr.entry_idx, kind="stable")
    cumulative_r = np.r_[0.0, np.cumsum(r[order])]
    dd_r = np.maximum.accumulate(cumulative_r) - cumulative_r
    equity = equity_eur + np.r_[0.0, np.cumsum(pnl[order])]
    dd_eur = np.maximum.accumulate(equity) - equity
    dd_pct = np.divide(
        dd_eur,
        np.maximum.accumulate(equity),
        out=np.zeros_like(dd_eur),
        where=np.maximum.accumulate(equity) != 0,
    )
    unique_days, counts = np.unique(tr.entry_day, return_counts=True)
    count_map = dict(zip(unique_days.tolist(), counts.tolist(), strict=True))
    per_day = np.asarray(
        [count_map.get(int(day), 0) for day in np.unique(trading_days)], dtype=float
    )
    daily_pnl = np.asarray([pnl[tr.entry_day == day].sum() for day in np.unique(trading_days)])
    loss_run = best_run = 0
    for value in pnl[order]:
        loss_run = loss_run + 1 if value < 0 else 0
        best_run = max(best_run, loss_run)
    remove = np.flatnonzero(r > 0)
    if len(remove):
        remove = remove[np.argsort(r[remove])[-2:]]
    retained = np.delete(r, remove)
    ci = day_clustered_mean_ci(r, tr.entry_day, seed=seed)
    gross_loss = -pnl_losses.sum()
    gross_r_loss = -losses.sum()
    result.update(
        net_pnl_eur=_finite(pnl.sum()),
        gross_pnl_eur=_finite(tr.gross_pnl_eur.sum()),
        win_rate=_finite((pnl > 0).mean()),
        expectancy_r=_finite(r.mean()),
        profit_factor=_finite(pnl_wins.sum() / gross_loss) if gross_loss > 0 else None,
        avg_winner_r=_finite(wins.mean()) if len(wins) else None,
        avg_loser_r=_finite(losses.mean()) if len(losses) else None,
        avg_winner_eur=_finite(pnl_wins.mean()) if len(pnl_wins) else None,
        avg_loser_eur=_finite(pnl_losses.mean()) if len(pnl_losses) else None,
        payoff_ratio=_finite(pnl_wins.mean() / -pnl_losses.mean())
        if len(pnl_wins) and len(pnl_losses)
        else None,
        payoff_r=_finite(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
        max_drawdown_eur=_finite(dd_eur.max()),
        max_drawdown_pct=_finite(dd_pct.max() * 100.0),
        max_drawdown_r=_finite(dd_r.max()),
        max_consecutive_losses=best_run,
        worst_day_eur=_finite(daily_pnl.min()) if len(daily_pnl) else 0.0,
        best_day_eur=_finite(daily_pnl.max()) if len(daily_pnl) else 0.0,
        median_trades_per_day=_finite(np.median(per_day)) if n_days else None,
        zero_trade_days=int((per_day == 0).sum()),
        exposure_time_frac=_finite(tr.holding_bars.sum() / window_bars) if window_bars else None,
        avg_holding_bars=_finite(tr.holding_bars.mean()),
        median_holding_bars=_finite(np.median(tr.holding_bars)),
        avg_holding_minutes=_finite(tr.holding_bars.mean() * 5.0),
        mfe_r_mean=_finite(tr.mfe_r.mean()),
        mfe_r_median=_finite(np.median(tr.mfe_r)),
        mae_r_mean=_finite(tr.mae_r.mean()),
        mae_r_median=_finite(np.median(tr.mae_r)),
        avg_leverage=_finite(tr.leverage.mean()),
        max_leverage=_finite(tr.leverage.max()),
        leverage_capped_trades=int(tr.leverage_capped.sum()),
        est_execution_cost_eur=_finite(tr.cost_eur.sum()),
        cost_per_trade_eur=_finite(tr.cost_eur.mean()),
        cost_burden=_finite(tr.cost_eur.sum() / abs(tr.gross_pnl_eur.sum()))
        if tr.gross_pnl_eur.sum() != 0
        else None,
        expectancy_r_ci95_day_clustered=[
            _finite(ci[0]) if ci[0] is not None else None,
            _finite(ci[1]) if ci[1] is not None else None,
        ],
        expectancy_without_top_2_winners=_finite(retained.mean()) if len(retained) else None,
        r_profit_factor=_finite(wins.sum() / gross_r_loss) if gross_r_loss > 0 else None,
    )
    return result


def _part_context(
    features: FeatureSet, plan: SplitPlan, part: Partition
) -> tuple[np.ndarray, np.ndarray, int]:
    dates = _dates(features)
    in_part = plan.mask(dates, part)
    minute = features["berlin_minute"]
    days = np.unique(
        features["berlin_day_id"][in_part & (minute >= ENTRY_START_MIN) & (minute < ENTRY_END_MIN)]
    )
    window = int((in_part & (minute >= ENTRY_START_MIN) & (minute < FLAT_MIN)).sum())
    return dates, days, window


def _metric_for_part(
    cfg: dict, features: FeatureSet, trades: TradeArrays, plan: SplitPlan, part: Partition
) -> dict:
    dates, days, window = _part_context(features, plan, part)
    return complete_metrics(
        trades,
        _partition_mask(trades, dates, plan, part),
        trading_days=days,
        window_bars=window,
        equity_eur=float(cfg["sizing"]["equity_eur"]),
        min_trades=int(cfg["sample_rules"]["min_trades_flag"]),
        seed=int(cfg["seed"]),
    )


def _serialize_trades(path: Path, trades: TradeArrays) -> None:
    np.savez_compressed(
        path, **{name: getattr(trades, name) for name in trades.__dataclass_fields__}
    )


def _deserialize_trades(path: Path) -> TradeArrays:
    with np.load(path, allow_pickle=False) as data:
        return TradeArrays(*(data[name] for name in TradeArrays.__dataclass_fields__))


class ResultCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def get_or_run(
        self, fingerprint: dict, evaluate: Callable[[], TradeArrays]
    ) -> tuple[TradeArrays, bool]:
        key = stable_hash(fingerprint)
        target = self.root / f"{key}.npz"
        manifest = self.root / f"{key}.json"
        if target.is_file() and manifest.is_file():
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            if saved.get("fingerprint") == fingerprint:
                return _deserialize_trades(target), True
        trades = evaluate()
        _serialize_trades(target, trades)
        _json(manifest, {"fingerprint": fingerprint})
        return trades, False


def _kernel_hashes() -> dict[str, str]:
    paths = [
        REPO_ROOT / "src/alpha/fast/sim.py",
        *sorted((REPO_ROOT / "src/alpha/fast/kernels").glob("*.py")),
    ]
    return {path.name: _sha(path) for path in paths}


def _feature_dataset_hash(features: FeatureSet) -> str:
    digest = hashlib.sha256()
    for name in ("ts_ns", "o", "h", "l", "c", "spread"):
        value = np.ascontiguousarray(features[name])
        digest.update(name.encode())
        digest.update(value.dtype.str.encode())
        digest.update(value.tobytes())
    return digest.hexdigest()


def _strategy_versions() -> dict[str, str]:
    """Read reference versions dynamically without using reference evaluation."""
    from research.runners.ar2_compare import discover_variants

    return {item["strategy_id"]: item["strategy_version"] for item in discover_variants()}


def _simulation_fingerprint(
    *,
    features: FeatureSet,
    dataset_hash: str,
    family: FastFamily,
    params: Any,
    scenario: str,
    split: dict,
    sizing: dict,
    rules: dict,
) -> dict:
    return {
        "dataset_hash": dataset_hash,
        "feature_key": features.metadata.get("cache_key"),
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "strategy_id": family.strategy_id,
        "params": _plain(params),
        "cost_model": _plain(COST_SCENARIOS[scenario]),
        "split": split,
        "evaluator_version": EVALUATOR_VERSION,
        "sizing": _plain(sizing),
        "rules": _plain(rules),
        # fill/session semantics live in the shared AR1 modules, not only in the kernels
        "semantics_sources": {
            name: _sha(REPO_ROOT / "src/alpha/common" / name) for name in ("frame.py", "sim.py")
        },
        "source_hashes": _kernel_hashes(),
    }


def _trade_r_by_partition(rec: dict, name: str) -> np.ndarray:
    return np.asarray(rec.get("_r", {}).get(name, []), dtype=float)


def synthetic_freeze_record(
    strategy: str, variant: int, train: list[float], valid: list[float]
) -> dict:
    def metrics(values: list[float]) -> dict:
        arr = np.asarray(values, dtype=float)
        wins, losses = arr[arr > 0], arr[arr < 0]
        pf = float(wins.sum() / -losses.sum()) if len(losses) else None
        return {
            "trades": len(arr),
            "expectancy_r": float(arr.mean()) if len(arr) else None,
            "profit_factor": pf,
            "max_drawdown_pct": 0.0,
            "expectancy_without_top_2_winners": float(arr.mean()) if len(arr) else None,
        }

    combined = np.asarray([*train, *valid], dtype=float)
    winner_indices = np.flatnonzero(combined > 0)
    if len(winner_indices):
        winner_indices = winner_indices[np.argsort(combined[winner_indices])[-2:]]
    retained = np.delete(combined, winner_indices)
    return {
        "id": f"{strategy}#{variant}",
        "strategy_id": strategy,
        "strategy_version": "test",
        "variant": variant,
        "params": {},
        "train": metrics(train),
        "validation": metrics(valid),
        "_r": {"train": train, "validation": valid},
        "dev_expectancy_without_top": float(retained.mean()) if len(retained) else None,
        "stress_expectancy": {"SPREAD_STRESS": 1.0, "SLIPPAGE_STRESS": 1.0},
        "restriction": None,
    }


def synthetic_trade_arrays(r: np.ndarray, days: np.ndarray) -> TradeArrays:
    """Small deterministic TradeArrays fixture used by runner-level metric tests."""
    r = np.asarray(r, dtype=float)
    days = np.asarray(days, dtype=np.int64)
    n = len(r)
    pnl = r * 50.0
    zeros = np.zeros(n, dtype=float)
    return TradeArrays(
        np.arange(n),
        np.arange(n),
        np.arange(n) + 1,
        np.ones(n, dtype=np.int8),
        days,
        np.full(n, 100.0),
        np.full(n, 101.0),
        np.full(n, 10.0),
        np.ones(n),
        np.ones(n),
        pnl,
        pnl,
        pnl,
        r,
        zeros,
        zeros,
        zeros,
        zeros,
        np.zeros(n, dtype=np.int8),
        np.ones(n, dtype=np.int64),
        np.maximum(r, 0),
        np.minimum(r, 0),
        np.zeros(n, dtype=bool),
        np.zeros(n, dtype=bool),
        np.zeros(8, dtype=np.int64),
    )


def _restriction_for(rec: dict, rule: dict) -> dict | None:
    rows = rec.get("_regime_rows", [])
    best: tuple[float, dict] | None = None
    for dimension in DIMENSIONS:
        values = sorted({row[dimension] for row in rows})
        keep: list[str] = []
        scores: list[float] = []
        for value in values:
            per = {}
            for part in ("train", "validation"):
                samples = [
                    row["r"] for row in rows if row["partition"] == part and row[dimension] == value
                ]
                per[part] = (len(samples), float(np.mean(samples)) if samples else -1.0)
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
                best = (score, {"dimension": dimension, "values": keep})
    return best[1] if best else None


def _restriction_trials(records: list[dict]) -> int:
    """Number of (variant, dimension, value) regime slices examined by the restriction search."""
    total = 0
    for rec in records:
        rows = rec.get("_regime_rows", [])
        total += sum(len({row[dimension] for row in rows}) for dimension in DIMENSIONS)
    return total


def _survives_stress(rec: dict, restriction: dict | None, rule: dict) -> bool:
    key = "whole" if restriction is None else stable_hash(restriction)
    values = rec.get("stress_expectancy_by_restriction", {}).get(
        key, rec.get("stress_expectancy", {})
    )
    return all(
        values.get(scenario) is not None and values[scenario] > 0
        for scenario in rule["expectancy_r_gt_0_under"]
    )


def freeze_rule(cfg: dict, records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Exact ar2_compare whole/restricted/sibling/rejected-early semantics."""
    rule = cfg["freeze_rule"]
    by_strategy: dict[str, list[dict]] = {}
    for rec in records:
        by_strategy.setdefault(rec["strategy_id"], []).append(rec)

    def exp(rec: dict, part: str) -> float | None:
        return rec[part].get("expectancy_r")

    rejected = [
        {"strategy_id": sid, "reason": "REJECTED_EARLY: negative in TRAIN and VALIDATION"}
        for sid, recs in by_strategy.items()
        if all(
            (exp(rec, "train") is None or exp(rec, "train") <= 0)
            and (exp(rec, "validation") is None or exp(rec, "validation") <= 0)
            for rec in recs
        )
    ]
    rejected_ids = {item["strategy_id"] for item in rejected}
    frozen: list[dict] = []
    for sid, recs in sorted(by_strategy.items()):
        if sid in rejected_ids:
            continue
        for rec in recs:
            train, validation = rec["train"], rec["validation"]
            checks = {
                "train_trades": train["trades"] >= rule["min_train_trades"],
                "validation_trades": validation["trades"] >= rule["min_validation_trades"],
                "positive_expectancy_both": (exp(rec, "train") or 0) > 0
                and (exp(rec, "validation") or 0) > 0,
                "profit_factor_both": (train.get("profit_factor") or 0)
                > rule["min_profit_factor_each_partition"]
                and (validation.get("profit_factor") or 0)
                > rule["min_profit_factor_each_partition"],
                "drawdown": max(
                    train.get("max_drawdown_pct") or 0.0, validation.get("max_drawdown_pct") or 0.0
                )
                <= rule["max_drawdown_pct_le"],
                "not_dependent_on_top_winners": rec.get("dev_expectancy_without_top") is not None
                and rec["dev_expectancy_without_top"] > 0,
            }
            siblings = [item for item in recs if item is not rec]
            checks["siblings_positive"] = all(
                (
                    (
                        (exp(item, "train") or 0) * item["train"]["trades"]
                        + (exp(item, "validation") or 0) * item["validation"]["trades"]
                    )
                    / max(item["train"]["trades"] + item["validation"]["trades"], 1)
                )
                > rule["sibling_variant_pooled_expectancy_r_gt"]
                for item in siblings
            )
            restriction = rec.get("restriction") or _restriction_for(
                rec, rule["regime_restriction"]
            )
            if all(checks.values()):
                checks["survives_cost_stress"] = _survives_stress(rec, None, rule)
            rec["freeze_checks"] = checks
            base = {
                "strategy_id": sid,
                "strategy_version": rec["strategy_version"],
                "variant": rec["variant"],
                "params": rec["params"],
            }
            if all(checks.values()):
                frozen.append({"role": "WHOLE_VARIANT", **base, "restriction": None})
            if (
                restriction is not None
                and train["trades"] > 0
                and _survives_stress(rec, restriction, rule)
            ):
                frozen.append(
                    {
                        "role": "REGIME_RESTRICTED",
                        **base,
                        "restriction": restriction,
                        # best-of-many over 4 dimensions and all their values, mined on TRAIN and
                        # VALIDATION: a hypothesis for OOS, NOT equivalent to a pre-registered rule
                        "selection": "VALIDATION_MINED_BEST_OF_MANY",
                    }
                )
    return frozen, rejected


def _regime_rows(features: FeatureSet, trades: TradeArrays, plan: SplitPlan) -> list[dict]:
    dates = _dates(features)
    maps = features.metadata["maps"]["regime"]
    rows = []
    for index, decision in enumerate(trades.decision_idx):
        date = dates[trades.entry_idx[index]]
        part = (
            "train"
            if np.datetime64(plan.train.start) <= date <= np.datetime64(plan.train.end)
            else "validation"
            if np.datetime64(plan.validation.start) <= date <= np.datetime64(plan.validation.end)
            else "oos"
        )
        row = {"partition": part, "r": float(trades.r_multiple[index])}
        for dimension in DIMENSIONS:
            code = int(features[f"regime_{dimension.lower()}"][decision])
            row[dimension] = maps[dimension][str(code)]
        rows.append(row)
    return rows


def _restriction_candidate_mask(
    features: FeatureSet, candidates: CandidateArrays, restriction: dict | None
) -> np.ndarray:
    if restriction is None:
        return np.ones(len(candidates.decision_idx), dtype=bool)
    dimension = restriction["dimension"]
    mapping = features.metadata["maps"]["regime"][dimension]
    codes = {int(code) for code, label in mapping.items() if label in restriction["values"]}
    values = features[f"regime_{dimension.lower()}"][candidates.decision_idx]
    return np.isin(values, list(codes))


def _subset_candidates(candidates: CandidateArrays, mask: np.ndarray) -> CandidateArrays:
    return CandidateArrays(
        *(getattr(candidates, name)[mask] for name in candidates.__dataclass_fields__)
    )


def _matrix_rows(
    features: FeatureSet, records: list[dict], plan: SplitPlan
) -> tuple[list[dict], list[dict]]:
    regime_rows: list[dict] = []
    session_rows: list[dict] = []
    regime_maps = features.metadata["maps"]["regime"]
    phase_map = features.metadata["maps"]["phase"]
    dates = _dates(features)
    for rec in records:
        trades: TradeArrays = rec["_base_trades"]
        for part in (plan.train, plan.validation):
            part_mask = _partition_mask(trades, dates, plan, part)
            indices = np.flatnonzero(part_mask)
            for dimension in DIMENSIONS:
                array = features[f"regime_{dimension.lower()}"]
                for code, label in regime_maps[dimension].items():
                    selected = indices[array[trades.decision_idx[indices]] == int(code)]
                    for context in CONTEXT_NAMES:
                        ctx = features[f"context_{context.lower()}"]
                        sub = selected[ctx[trades.decision_idx[selected]]]
                        if len(sub):
                            regime_rows.append(
                                {
                                    "variant_id": rec["id"],
                                    "strategy_id": rec["strategy_id"],
                                    "partition": part.name.upper(),
                                    "h1_regime_dimension": dimension,
                                    "h1_regime_value": label,
                                    "m15_context": context,
                                    "trades": len(sub),
                                    "expectancy_r": float(trades.r_multiple[sub].mean()),
                                }
                            )
            phases = features["phase_code"][trades.decision_idx[indices]]
            for code, label in phase_map.items():
                sub = indices[phases == int(code)]
                if len(sub):
                    session_rows.append(
                        {
                            "variant_id": rec["id"],
                            "strategy_id": rec["strategy_id"],
                            "partition": part.name.upper(),
                            "phase_code": int(code),
                            "session_phase": label,
                            "trades": len(sub),
                            "expectancy_r": float(trades.r_multiple[sub].mean()),
                        }
                    )
    return regime_rows, session_rows


def _cadence(records: list[dict], features: FeatureSet, plan: SplitPlan) -> list[dict]:
    dates = _dates(features)
    rows = []
    for rec in records:
        tr = rec["_base_trades"]
        for part in (plan.train, plan.validation):
            mask = _partition_mask(tr, dates, plan, part)
            values, counts = np.unique(tr.entry_day[mask], return_counts=True)
            for day, count in zip(values, counts, strict=True):
                first = int(np.flatnonzero(features["berlin_day_id"] == day)[0])
                rows.append(
                    {
                        "variant_id": rec["id"],
                        "partition": part.name.upper(),
                        "date": str(dates[first]),
                        "trades": int(count),
                    }
                )
    return rows


def _conflicts(records: list[dict]) -> list[dict]:
    decisions: dict[int, list[tuple[str, int]]] = {}
    intervals = []
    for rec in records:
        candidates: CandidateArrays = rec["_candidates"]
        for idx, side in zip(candidates.decision_idx, candidates.direction, strict=True):
            decisions.setdefault(int(idx), []).append((rec["id"], int(side)))
        tr: TradeArrays = rec["_base_trades"]
        intervals.extend(
            (rec["id"], int(a), int(b), int(side))
            for a, b, side in zip(tr.entry_idx, tr.exit_idx, tr.side, strict=True)
        )
    out = []
    for idx, items in decisions.items():
        if len(items) > 1:
            sides = {item[1] for item in items}
            out.append(
                {
                    "kind": "OPPOSITE_DECISION" if len(sides) > 1 else "SAME_DIRECTION_DECISION",
                    "decision_idx": idx,
                    "strategies": sorted(item[0] for item in items),
                }
            )
    intervals.sort(key=lambda item: item[1])
    for left, first in enumerate(intervals):
        for second in intervals[left + 1 :]:
            if second[1] > first[2]:
                break
            if first[0] != second[0] and second[1] <= first[2] and first[1] <= second[2]:
                out.append(
                    {
                        "kind": "OVERLAPPING_OPEN_TRADES",
                        "entry_idx": max(first[1], second[1]),
                        "strategies": sorted([first[0], second[0]]),
                        "direction": "same" if first[3] == second[3] else "opposite",
                    }
                )
    return out


def _public_record(rec: dict) -> dict:
    return {key: value for key, value in rec.items() if not key.startswith("_")}


def _report(
    records: list[dict], matrix: list[dict], frozen: list[dict], rejected: list[dict], timings: dict
) -> str:
    lines = [
        "# AR2 Fast Development Report",
        "",
        "## Stage timings",
        "",
        "| Stage | Seconds |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {value:.3f} |" for name, value in timings.items())
    lines += [
        "",
        "## Variant metrics",
        "",
        "| Variant | TRAIN N | TRAIN Exp R | TRAIN PF | TRAIN CI95 | "
        "VALID N | VALID Exp R | VALID PF | VALID CI95 | Flags |",
        "|---|---:|---:|---:|---|---:|---:|---:|---|---|",
    ]
    for rec in records:
        train, valid = rec["train"], rec["validation"]
        flags = []
        if train["insufficient_sample"]:
            flags.append("TRAIN_INSUFFICIENT_SAMPLE")
        if valid["insufficient_sample"]:
            flags.append("VALIDATION_INSUFFICIENT_SAMPLE")
        failed = [key for key, value in rec.get("freeze_checks", {}).items() if not value]
        flags.extend(f"FAIL:{item}" for item in failed)
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                rec["id"],
                train["trades"],
                train.get("expectancy_r"),
                train.get("profit_factor"),
                train.get("expectancy_r_ci95_day_clustered"),
                valid["trades"],
                valid.get("expectancy_r"),
                valid.get("profit_factor"),
                valid.get("expectancy_r_ci95_day_clustered"),
                ", ".join(flags) or "-",
            )
        )
    lines += [
        "",
        "## Matrix highlights",
        "",
        "| Variant | Partition | Dimension | Value | Context | N | Exp R |",
        "|---|---|---|---|---|---:|---:|",
    ]
    highlights = sorted(
        matrix, key=lambda row: (row["trades"], abs(row["expectancy_r"])), reverse=True
    )[:20]
    lines.extend(
        "| {} | {} | {} | {} | {} | {} | {:.4f} |".format(
            row["variant_id"],
            row["partition"],
            row["h1_regime_dimension"],
            row["h1_regime_value"],
            row["m15_context"],
            row["trades"],
            row["expectancy_r"],
        )
        for row in highlights
    )
    lines += [
        "",
        "## Survivors",
        "",
        "| Strategy | Variant | Role | Restriction |",
        "|---|---:|---|---|",
    ]
    lines.extend(
        "| {} | {} | {} | {} |".format(
            item["strategy_id"],
            item["variant"],
            item["role"],
            json.dumps(item["restriction"], sort_keys=True),
        )
        for item in frozen
    )
    if not frozen:
        lines.append("| - | - | NONE | - |")
    lines += ["", "## Rejected early", "", "| Strategy | Reason |", "|---|---|"]
    lines.extend(f"| {item['strategy_id']} | {item['reason']} |" for item in rejected)
    if not rejected:
        lines.append("| - | NONE |")
    return "\n".join(lines) + "\n"


def _dataset_hash(ds: Any) -> str:
    return stable_hash({month.month: month.content_sha256 for month in ds.months})


def run_dev(
    cfg: dict,
    out: Path,
    *,
    cache_dir: Path = DEFAULT_CACHE,
    frame: pd.DataFrame | None = None,
    provenance: dict | None = None,
) -> int:
    out.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    started = time.perf_counter()
    ds = None if frame is not None else load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    full = frame if frame is not None else ds.frame
    plan = _plan(cfg)
    development = dev_frame(full, plan)
    progress("dataset", f"loaded {len(development):,} dev bars (OOS absent)")
    timings["dataset"] = time.perf_counter() - started

    stage = time.perf_counter()
    features = FeatureStore.load_or_build(development, {"point_size": POINT}, cache_dir)
    timings["features"] = time.perf_counter() - stage
    market = _market(features)
    families = discover()
    strategy_versions = _strategy_versions()
    dataset_hash = _feature_dataset_hash(features)
    variants = [
        (family, index, params)
        for _, family in sorted(families.items())
        for index, params in enumerate(family.variants)
    ]
    result_cache = ResultCache(cache_dir / "results")
    records = []
    simulation_count = 0
    candidate_started = time.perf_counter()
    for serial, (family, index, params) in enumerate(variants, 1):
        item_started = time.perf_counter()
        candidates = family.generate(features, params)
        variant_id = f"{family.strategy_id}#{index}"
        progress(
            "candidates",
            f"{serial}/{len(variants)} {variant_id} "
            f"(n={len(candidates.decision_idx)}) {time.perf_counter() - item_started:.2f}s",
        )
        simulations = {}
        reused_map = {}
        for scenario in ("BASE", "SPREAD_STRESS", "SLIPPAGE_STRESS", "COMBINED_ADVERSE"):
            fingerprint = _simulation_fingerprint(
                features=features,
                dataset_hash=dataset_hash,
                family=family,
                params=params,
                scenario=scenario,
                split=plan.to_dict(),
                sizing=cfg["sizing"],
                rules=cfg["rules"],
            )
            simulations[scenario], reused = result_cache.get_or_run(
                fingerprint,
                lambda scenario=scenario, candidates=candidates: simulate_fast(
                    market,
                    candidates,
                    COST_SCENARIOS[scenario],
                    SizingSpec(**cfg["sizing"]),
                    SimRules(**cfg["rules"]),
                ),
            )
            simulation_count += 1
            reused_map[scenario] = reused
            progress("simulation", f"{variant_id} {scenario} {'reused' if reused else 'done'}")
        base = simulations["BASE"]
        train = _metric_for_part(cfg, features, base, plan, plan.train)
        validation = _metric_for_part(cfg, features, base, plan, plan.validation)
        dev_r = np.concatenate(
            [
                base.r_multiple[_partition_mask(base, _dates(features), plan, plan.train)],
                base.r_multiple[_partition_mask(base, _dates(features), plan, plan.validation)],
            ]
        )
        winners = np.flatnonzero(dev_r > 0)
        if len(winners):
            winners = winners[
                np.argsort(dev_r[winners])[
                    -cfg["freeze_rule"]["expectancy_r_gt_0_after_removing_top_n_winners"] :
                ]
            ]
        retained = np.delete(dev_r, winners)
        rec = {
            "id": variant_id,
            "strategy_id": family.strategy_id,
            "strategy_version": strategy_versions[family.strategy_id],
            "variant": index,
            "params": _plain(params),
            "n_candidates": len(candidates.decision_idx),
            "train": train,
            "validation": validation,
            "cost": {
                scenario: {
                    "train": _metric_for_part(cfg, features, tr, plan, plan.train),
                    "validation": _metric_for_part(cfg, features, tr, plan, plan.validation),
                }
                for scenario, tr in simulations.items()
            },
            "dev_expectancy_without_top": float(retained.mean()) if len(retained) else None,
            "stress_expectancy": {
                scenario: float(
                    np.mean(
                        np.concatenate(
                            [
                                tr.r_multiple[
                                    _partition_mask(tr, _dates(features), plan, plan.train)
                                ],
                                tr.r_multiple[
                                    _partition_mask(tr, _dates(features), plan, plan.validation)
                                ],
                            ]
                        )
                    )
                )
                if len(tr)
                else None
                for scenario, tr in simulations.items()
            },
            "cache_reused": reused_map,
            "_candidates": candidates,
            "_base_trades": base,
            "_regime_rows": _regime_rows(features, base, plan),
        }
        rec["restriction"] = _restriction_for(rec, cfg["freeze_rule"]["regime_restriction"])
        if rec["restriction"] is not None:
            restricted = _subset_candidates(
                candidates, _restriction_candidate_mask(features, candidates, rec["restriction"])
            )
            key = stable_hash(rec["restriction"])
            rec["stress_expectancy_by_restriction"] = {key: {}}
            for scenario in cfg["freeze_rule"]["expectancy_r_gt_0_under"]:
                tr = simulate_fast(
                    market,
                    restricted,
                    COST_SCENARIOS[scenario],
                    SizingSpec(**cfg["sizing"]),
                    SimRules(**cfg["rules"]),
                )
                simulation_count += 1
                dates = _dates(features)
                dev_r = np.concatenate(
                    [
                        tr.r_multiple[_partition_mask(tr, dates, plan, plan.train)],
                        tr.r_multiple[_partition_mask(tr, dates, plan, plan.validation)],
                    ]
                )
                rec["stress_expectancy_by_restriction"][key][scenario] = (
                    float(dev_r.mean()) if len(dev_r) else None
                )
        records.append(rec)
    timings["candidates_simulation_metrics"] = time.perf_counter() - candidate_started
    progress("metrics", "done")

    matrix_started = time.perf_counter()
    matrix, sessions = _matrix_rows(features, records, plan)
    cadence = _cadence(records, features, plan)
    conflicts = _conflicts(records)
    pd.DataFrame(matrix).to_csv(out / "regime_context_matrix.csv", index=False)
    pd.DataFrame(sessions).to_csv(out / "session_matrix.csv", index=False)
    pd.DataFrame(cadence).to_csv(out / "cadence.csv", index=False)
    _json(out / "conflicts.json", conflicts)
    timings["matrix"] = time.perf_counter() - matrix_started
    progress("matrix", "done")

    frozen, rejected = freeze_rule(cfg, records)
    frozen_hash = stable_hash(frozen)
    _json(
        out / "frozen_candidates.json",
        {
            "hash": frozen_hash,
            "set": frozen,
            "rejected_early": rejected,
            "dev_provenance": _dev_provenance(cfg, ds, full),
        },
    )
    progress("freeze", f"{len(frozen)} survivors")
    _json(out / "dev_results.json", [_public_record(rec) for rec in records])
    hashes = source_hashes()
    dataset_hashes = _dataset_hashes(ds, full)
    run_data = {
        "trial_accounting": {
            "variants": len(variants),
            "simulations_evaluated": simulation_count,
            "regime_restrictions_considered": _restriction_trials(records),
        },
        "config_hash": stable_hash(cfg),
        "dataset_hash": stable_hash(dataset_hashes),
        "dataset_hashes": dataset_hashes,
        "feature_hash": features.metadata.get("cache_key"),
        "evaluator_version": EVALUATOR_VERSION,
        "git_commit": git_commit(REPO_ROOT),
        "versions": {"nautilus_trader": _version("nautilus_trader"), "numba": _version("numba")},
        "source_hashes": hashes,
        "experiment_components": {
            "config": stable_hash(cfg),
            "dataset": dataset_hashes,
            "features": features.metadata,
            "sources": hashes,
        },
        "split_plan": plan.to_dict(),
        "dev_bars": len(development),
        "timings_s": timings,
        "peak_rss_mb": _peak_rss_mb(),
        "platform": platform.platform(),
    }
    _json(out / "dev_run_record.json", run_data)
    (out / "report.md").write_text(
        _report(records, matrix, frozen, rejected, timings), encoding="utf-8"
    )
    progress("report", "written")
    return 0


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "NOT_INSTALLED"


def _peak_rss_mb() -> float | None:
    try:
        import psutil

        return round(psutil.Process(os.getpid()).memory_info().peak_wset / 1024**2, 3)
    except (ImportError, AttributeError):
        return None


def _dataset_hashes(ds: Any, full: pd.DataFrame) -> dict[str, str]:
    if ds is not None:
        return {month.month: month.content_sha256 for month in ds.months}
    return {
        "injected": stable_hash(
            {"rows": len(full), "first": str(full["ts"].iloc[0]), "last": str(full["ts"].iloc[-1])}
        )
    }


def _dev_provenance(cfg: dict, ds: Any, full: pd.DataFrame) -> dict:
    """Everything the freeze depended on; the OOS run must present the identical provenance."""
    return {
        "config_hash": stable_hash(cfg),
        "dataset_hashes": _dataset_hashes(ds, full),
        "evaluator_version": EVALUATOR_VERSION,
        "source_hashes": source_hashes(),
    }


def experiment_components(cfg: dict, ds: Any, frozen_hash: str, feature_key: str) -> dict:
    return {
        "git_commit": git_commit(REPO_ROOT),
        "frozen_hash": frozen_hash,
        "config_hash": stable_hash(cfg),
        "dataset_hashes": {m.month: m.content_sha256 for m in ds.months},
        "feature_key": feature_key,
        "evaluator_version": EVALUATOR_VERSION,
        "source_hashes": source_hashes(),
        "nautilus_version": _version("nautilus_trader"),
        "numba_version": _version("numba"),
    }


def run_oos(cfg: dict, out: Path, *, cache_dir: Path = DEFAULT_CACHE) -> int:
    frozen_path = out / "frozen_candidates.json"
    if not frozen_path.exists():
        raise SystemExit("REFUSED: no frozen_candidates.json - run the DEV/FREEZE phase first")
    payload = json.loads(frozen_path.read_text(encoding="utf-8"))
    if stable_hash(payload["set"]) != payload["hash"]:
        raise SystemExit("REFUSED: frozen set hash mismatch (file was modified after freezing)")
    if not payload["set"]:
        raise SystemExit("Nothing frozen: no candidate met the pre-registered rule; OOS not run")
    ds = load_research_dataset(REPO_ROOT / cfg["dataset_root"])
    provenance = payload.get("dev_provenance")
    if provenance is None:
        raise SystemExit("REFUSED: frozen file carries no dev provenance")
    current = _dev_provenance(cfg, ds, ds.frame)
    changed = sorted(key for key in current if provenance.get(key) != current[key])
    if changed:
        raise SystemExit(
            f"REFUSED: experiment changed since the freeze (provenance mismatch: {changed})"
        )
    plan = _plan(cfg)
    features = FeatureStore.load_or_build(ds.frame, {"point_size": POINT}, cache_dir)
    components = experiment_components(cfg, ds, payload["hash"], features.metadata["cache_key"])
    fingerprint = stable_hash(components)
    OosGate(out / "oos_access_log.json").evaluate(
        fingerprint, f"AR2 fast frozen set {len(payload['set'])} candidates", components=components
    )
    families = discover()
    strategy_versions = _strategy_versions()
    market = _market(features)
    results = []
    for item in payload["set"]:
        family = families[item["strategy_id"]]
        if strategy_versions[item["strategy_id"]] != item["strategy_version"]:
            raise SystemExit(f"REFUSED: strategy version changed for {item['strategy_id']}")
        params = family.variants[item["variant"]]
        if _plain(params) != item["params"]:
            raise SystemExit(f"REFUSED: parameters changed for {item['strategy_id']}")
        candidates = family.generate(features, params)
        candidates = _subset_candidates(
            candidates, _restriction_candidate_mask(features, candidates, item["restriction"])
        )
        cost = {}
        for scenario in ("BASE", "SPREAD_STRESS", "SLIPPAGE_STRESS", "COMBINED_ADVERSE"):
            trades = simulate_fast(
                market,
                candidates,
                COST_SCENARIOS[scenario],
                SizingSpec(**cfg["sizing"]),
                SimRules(**cfg["rules"]),
            )
            cost[scenario] = {"oos": _metric_for_part(cfg, features, trades, plan, plan.oos)}
        results.append(
            {
                **item,
                "cost": cost,
                "note": "single OOS evaluation of a frozen candidate; never production-ready",
            }
        )
    _json(out / "oos_results.json", results)
    _json(
        out / "oos_run_record.json",
        {
            "experiment_fingerprint": fingerprint,
            "experiment_components": components,
            "evaluations": len(results),
        },
    )
    progress("oos", f"evaluated {len(results)} frozen candidates once")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("legacy_out", nargs="?", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--oos", action="store_true")
    return parser


def cli(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    out = args.out or args.legacy_out or DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    code = 1
    try:
        cfg = json.loads(args.config.read_text(encoding="utf-8"))
        code = (
            run_oos(cfg, out, cache_dir=args.cache_dir)
            if args.oos
            else run_dev(cfg, out, cache_dir=args.cache_dir)
        )
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        (out / "EXIT").write_text(f"{code}\n", encoding="utf-8")
        print(f"EXIT={code}", flush=True)
    return code


def main() -> int:
    return cli()


if __name__ == "__main__":
    raise SystemExit(main())
