# ruff: noqa: E501
"""FAST path of the research workbench (OFFLINE ONLY): features -> candidates -> simulate_fast -> metrics -> status.

Reuses, never re-implements: ``FeatureStore.load_or_build`` (features), ``alpha.fast.spec.evaluate_spec`` (signals),
``alpha.fast.sim.simulate_fast`` (simulation), ``alpha.fast.screen`` (Train/Validation screening + Stage-A rejection),
``research_speed.scheduler.run_dag`` + ``parallel.clamp_jobs`` (multi-market fan-out).

Outcome per market is ONLY ``REJECT_FAST`` or ``PROMOTE_TO_FIDELITY`` (a promotion to fidelity checks, never to live).

Documented reject reasons (``RejectCode``; every reject names ALL reasons that apply; thresholds are the predeclared
``FastGate`` defaults and are part of the METRICS key, so a different gate is a different artifact):

* ``zero_candidates`` / ``invalid_stop``   cheap Stage-A rejection (``alpha.fast.screen.reject_reason``)
* ``too_few_trades_train``                 fewer than ``gate.min_train_trades`` simulated TRAIN trades
* ``too_few_trades_validation``            fewer than ``gate.min_validation_trades`` simulated VALIDATION trades
* ``negative_expectancy_train``            mean net R on TRAIN <= 0
* ``negative_expectancy_validation``       mean net R on VALIDATION <= 0
* ``cost_burden_too_high``                 mean per-trade cost in R on TRAIN > ``gate.max_cost_burden_r``

Stage skipping: a stage is recomputed only if its key (see ``dag``) misses; features are loaded only when SIGNALS must be
recomputed; SIGNALS also stores the compact market arrays so SIMULATION / METRICS never need the feature set.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np

from alpha.fast.screen import (
    RejectReason,
    reject_reason,
    screen_partition_trades,
    screen_trades,
)
from alpha.fast.sim import CandidateArrays, MarketArrays, SimWindow, TradeArrays, simulate_fast
from alpha.fast.spec import evaluate_spec
from alpha.fast.store import FeatureStore
from demo.entry_exit_quality import capture_ratio
from research_speed import importgraph
from research_speed.parallel import available_memory_mb, clamp_jobs
from research_speed.scheduler import Task, run_dag
from research_speed.segments import file_sha256

from . import dag
from .dag import ArtifactStore, StageKeys
from .entry_exit_adapter import right_tail
from .experiment import (
    ExperimentSpec,
    data_scope,
    dataset_hash,
    load_market_frame,
)
from .stage_adapters import (
    _CAND_FIELDS,
    _trade_fields,
    candidates_from,
    json_bytes,
    load_npz,
    market_arrays,
    market_from_features,
    npz_bytes,
    trades_from,
)
from .status import PromotionStatus

METRIC_VERSION = "wb-metrics-1"
BAR_MINUTES = 5
_SIGNAL_FILE = "candidates.npz"
_MARKET_FILE = "market.npz"
_TRADES_FILE = "trades.npz"
_METRICS_FILE = "metrics.json"
_REF_FILE = "features_ref.json"
_ENTRY_EXIT_FILE = "entry_exit.json"


class RejectCode(StrEnum):
    ZERO_CANDIDATES = "zero_candidates"
    INVALID_STOP = "invalid_stop"
    TOO_FEW_TRADES_TRAIN = "too_few_trades_train"
    TOO_FEW_TRADES_VALIDATION = "too_few_trades_validation"
    NEGATIVE_EXPECTANCY_TRAIN = "negative_expectancy_train"
    NEGATIVE_EXPECTANCY_VALIDATION = "negative_expectancy_validation"
    COST_BURDEN_TOO_HIGH = "cost_burden_too_high"


@dataclass(frozen=True)
class FastGate:
    """Predeclared fast-screen thresholds (not tuned on results)."""

    min_train_trades: int = 30
    min_validation_trades: int = 10
    max_cost_burden_r: float = 0.5
    require_positive_expectancy: bool = True  # False only for tests / exploratory lookups


# ---- helpers ----
def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        return _clean(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _code_digest() -> str:
    here = Path(__file__).resolve().parent
    return "|".join(
        importgraph.content_digest(here / name) for name in ("fastrun.py", "entry_exit_adapter.py")
    )


def _permitted_mask(experiment: ExperimentSpec, dates: np.ndarray) -> np.ndarray:
    split = experiment.split
    parts = {"TRAIN": split.train, "VALIDATION": split.validation, "OOS": split.oos}
    mask = np.zeros(len(dates), dtype=bool)
    for name in experiment.partitions_read:
        mask |= split.mask(dates, parts[name])
    return mask


def blocked_while_busy(candidates: CandidateArrays, trades: TradeArrays) -> int:
    """Candidates FAST dropped because a position was open (decision strictly inside a trade: dec < i < exit).

    Same definition as ``differential.netting_report`` (``candidates_blocked_by_open_position``). FAST already enforces one
    position at a time (``next_free`` in sim.py), so this is only a disclosure, never a 'netting-adjusted' count."""
    if not len(trades):
        return 0
    dec, ext = trades.decision_idx, trades.exit_idx
    cand = candidates.decision_idx
    traded = np.isin(cand, dec)
    pos = np.searchsorted(dec, cand, side="left") - 1  # last trade with decision < i
    inside = np.zeros(len(cand), dtype=bool)
    ok = pos >= 0
    inside[ok] = ext[pos[ok]] > cand[ok]
    return int((inside & ~traded).sum())


# ---- metrics ----
def compute_metrics(
    experiment: ExperimentSpec,
    market: MarketArrays,
    dates: np.ndarray,
    candidates: CandidateArrays,
    trades: TradeArrays,
    *,
    invalid_stop_count: int,
    gate: FastGate,
    metric_version: str,
) -> dict[str, Any]:
    """Standard result contract + PartitionScreen metrics + the fast gate decision."""
    split = experiment.split
    sizing = experiment.sizing
    screen = screen_trades(trades, market, split, dates=dates, sizing=sizing)
    oos = None
    if "OOS" in experiment.partitions_read:
        bar_mask = split.mask(dates, split.oos)
        trade_mask = (
            split.mask(dates[trades.entry_idx], split.oos) if len(trades) else np.zeros(0, bool)
        )
        oos = asdict(
            screen_partition_trades(
                trades, trade_mask, len(np.unique(market.day[bar_mask])),
                contract_size=sizing.contract_size,
            )
        )  # fmt: skip
    bar_mask = _permitted_mask(experiment, dates)
    n_days = len(np.unique(market.day[bar_mask]))
    keep = (
        _permitted_mask(experiment, dates[trades.entry_idx]) if len(trades) else np.zeros(0, bool)
    )
    r = trades.r_multiple[keep]
    mfe = trades.mfe_r[keep]
    mae = trades.mae_r[keep]
    count = len(r)
    contract: dict[str, Any] = {
        "trade_count": count,
        "days": n_days,
        "trades_per_day": (count / n_days) if n_days else None,
    }
    if count:
        cumulative = np.concatenate((np.zeros(1), np.cumsum(r)))
        drawdown = np.maximum.accumulate(cumulative) - cumulative
        capture = [capture_ratio(float(m), float(x))[1] for m, x in zip(mfe, r, strict=True)]
        capture = [c for c in capture if c is not None]
        initial_risk = (trades.risk_pts * trades.qty * sizing.contract_size)[keep]
        valid = np.isfinite(initial_risk) & (initial_risk > 0)
        holding = trades.holding_bars[keep] * BAR_MINUTES
        contract.update(
            expectancy_R=float(r.mean()),
            median_R=float(np.median(r)),
            win_rate=float((r > 0).mean()),
            gross_PnL=float(trades.gross_pnl_eur[keep].sum()),
            net_PnL=float(trades.net_pnl_eur[keep].sum()),
            cost_burden=(
                float(np.mean(trades.cost_eur[keep][valid] / initial_risk[valid]))
                if valid.any()
                else None
            ),
            max_drawdown_R=float(drawdown.max()),
            MFE=float(mfe.mean()),
            MAE=float(mae.mean()),
            capture=float(np.mean(capture)) if capture else None,
            n_capture_defined=len(capture),
            giveback=float(np.mean(np.maximum(0.0, mfe - r))),
            favorable_before_adverse="NOT_AVAILABLE_IN_FAST (see ENTRY QUALITY share_mfe_first)",
            holding_time_min_mean=float(holding.mean()),
            holding_time_min_median=float(np.median(holding)),
            right_tail=right_tail(r, mfe),
        )
    else:
        for name in [
            "expectancy_R",
            "median_R",
            "win_rate",
            "gross_PnL",
            "net_PnL",
            "cost_burden",
            "max_drawdown_R",
            "MFE",
            "MAE",
            "capture",
            "giveback",
            "holding_time_min_mean",
            "holding_time_min_median",
        ]:
            contract[name] = None
        contract.update(favorable_before_adverse=None, right_tail=right_tail(r, mfe))
    reasons: list[str] = []
    stage_a = reject_reason(
        experiment.strategy_spec, candidates, min_trades=1, market=market, sizing=sizing,
        cost=experiment.cost_model,
    )  # fmt: skip
    if stage_a in (RejectReason.ZERO_CANDIDATES, RejectReason.INVALID_STOP):
        reasons.append(str(stage_a))
    train, val = screen.train, screen.validation
    if train.n_trades < gate.min_train_trades:
        reasons.append(RejectCode.TOO_FEW_TRADES_TRAIN)
    if val.n_trades < gate.min_validation_trades:
        reasons.append(RejectCode.TOO_FEW_TRADES_VALIDATION)
    positive = gate.require_positive_expectancy
    if positive and train.n_trades and (train.expectancy_r or 0.0) <= 0.0:
        reasons.append(RejectCode.NEGATIVE_EXPECTANCY_TRAIN)
    if positive and val.n_trades and (val.expectancy_r or 0.0) <= 0.0:
        reasons.append(RejectCode.NEGATIVE_EXPECTANCY_VALIDATION)
    if train.cost_burden is not None and train.cost_burden > gate.max_cost_burden_r:
        reasons.append(RejectCode.COST_BURDEN_TOO_HIGH)
    status = PromotionStatus.REJECT_FAST if reasons else PromotionStatus.PROMOTE_TO_FIDELITY
    return _clean(
        {
            "metric_version": metric_version,
            "status": str(status),
            "reasons": [str(x) for x in reasons],
            "gate": asdict(gate),
            "contract": contract,
            "train": asdict(train),
            "validation": asdict(val),
            "oos": oos,
            "partitions_read": experiment.partitions_actually_read(),
            "n_candidates": len(candidates.decision_idx),
            "fast_trades": len(trades),
            "candidates_blocked_while_busy": blocked_while_busy(candidates, trades),
            "invalid_stop_count": int(invalid_stop_count),
            "skips": trades.skips,
        }
    )


# ---- one market ----
def _features_lookup(store: ArtifactStore, market: str, keys: StageKeys) -> dag.Lookup:
    lk = store.lookup(market, "FEATURES", keys.features, cacheable=keys.cacheable)
    if not lk.hit:
        return lk
    try:
        ref = json.loads(
            (store.stage_dir(market, "FEATURES", keys.features) / _REF_FILE).read_text("utf-8")
        )
        npz = store.feature_cache_dir() / ref["store_key"] / "features.npz"
        if not npz.is_file() or npz.stat().st_size != ref["npz_size"]:
            return dag.Lookup(False, "FEATURE_CACHE_MISSING")
        if file_sha256(npz) != ref["npz_sha256"]:  # same-size corruption must also be a MISS
            return dag.Lookup(False, "FEATURE_CACHE_CHANGED")
    except Exception:
        return dag.Lookup(False, "FEATURE_CACHE_UNREADABLE")
    return lk


def _all_lookups(store: ArtifactStore, market: str, keys: StageKeys) -> dict[str, dag.Lookup]:
    out = {"FEATURES": _features_lookup(store, market, keys)}
    for stage in ("SIGNALS", "SIMULATION", "METRICS"):
        out[stage] = store.lookup(market, stage, keys.key(stage), cacheable=keys.cacheable)
    return out


def _prepare(experiment: ExperimentSpec, market: str, metric_version: str, gate: FastGate):
    frame = load_market_frame(experiment, market)
    if len(frame) < 100:
        raise ValueError(f"{market}: only {len(frame)} bars inside the permitted date range")
    dhash = dataset_hash(frame)
    keys = dag.compute_keys(
        experiment, market, dhash, metric_version=metric_version, gate=asdict(gate),
        fastrun_digest=_code_digest(),
    )  # fmt: skip
    return frame, dhash, keys


def run_market(
    experiment: ExperimentSpec,
    market: str,
    *,
    metric_version: str = METRIC_VERSION,
    gate: FastGate = FastGate(),  # noqa: B008 - frozen dataclass
    entry_exit: bool = False,
    entry_exit_config: dict[str, Any] | None = None,
    store: ArtifactStore | None = None,
) -> dict[str, Any]:
    """Run (or cache-hit) every stage of one market. Failure here never touches other markets' artifacts."""
    store = store or ArtifactStore(experiment.artifact_root)
    exp_id = experiment.experiment_id
    frame, dhash, keys = _prepare(experiment, market, metric_version, gate)
    lookups = _all_lookups(store, market, keys)
    stages: dict[str, dict[str, Any]] = {
        stage: {
            "key": keys.key(stage),
            "cache": "HIT" if lookups[stage].hit else "MISS",
            "reason": lookups[stage].reason,
            "computed": False,
            "runtime_s": (lookups[stage].manifest or {}).get("runtime_s"),
        }
        for stage in ("FEATURES", "SIGNALS", "SIMULATION", "METRICS")
    }
    memo: dict[str, Any] = {}
    cacheable = keys.cacheable

    def stage_dir(stage: str) -> Path:
        return store.stage_dir(market, stage, keys.key(stage))

    def get_signals() -> tuple[CandidateArrays, dict[str, np.ndarray], dict[str, Any]]:
        if "signals" in memo:
            return memo["signals"]
        if lookups["SIGNALS"].hit:
            directory = stage_dir("SIGNALS")
            memo["signals"] = (
                candidates_from(load_npz(directory / _SIGNAL_FILE)),
                load_npz(directory / _MARKET_FILE),
                dict(lookups["SIGNALS"].manifest or {}),
            )
            return memo["signals"]

        def compute():
            features = FeatureStore.load_or_build(
                frame, experiment.feature_config, store.feature_cache_dir()
            )
            _record_features(features)
            candidates = evaluate_spec(features, experiment.strategy_spec)
            invalid = int(evaluate_spec.invalid_stop_count)
            arrays = market_from_features(features)
            files = {
                _SIGNAL_FILE: npz_bytes({n: getattr(candidates, n) for n in _CAND_FIELDS}),
                _MARKET_FILE: npz_bytes(arrays),
            }
            extra = {"n_candidates": len(candidates.decision_idx), "invalid_stop_count": invalid}
            return files, extra, (candidates, arrays, extra)

        value, runtime, _ = store.compute_and_publish(
            exp_id, market, "SIGNALS", keys.signals, compute,
            code=keys.components["SIGNALS"]["signal_code_hash"], cacheable=cacheable,
        )  # fmt: skip
        stages["SIGNALS"].update(computed=True, runtime_s=runtime, cache="MISS")
        memo["signals"] = value
        return value

    def _record_features(features: Any) -> None:
        meta = features.metadata
        rec = stages["FEATURES"]
        rec["runtime_s"] = meta.get("timing_s")
        rec["store_key"] = meta.get("cache_key")
        if not lookups["FEATURES"].hit:
            rec.update(
                computed=not meta.get("cache_hit"), cache="HIT" if meta.get("cache_hit") else "MISS"
            )
        if cacheable and not lookups["FEATURES"].hit and meta.get("cache_key"):
            try:
                manifest = json.loads(
                    (store.feature_cache_dir() / meta["cache_key"] / "manifest.json").read_text(
                        "utf-8"
                    )
                )
                ref = {
                    "store_key": meta["cache_key"],
                    "npz_size": manifest["artifact"]["size_bytes"],
                    "npz_sha256": manifest["artifact"]["sha256"],
                }
            except Exception:
                return  # feature cache was bypassed (uncacheable) -> no reference, next run is a MISS
            store.publish(
                market, "FEATURES", keys.features, {_REF_FILE: json_bytes(ref)},
                experiment_id=exp_id, code=keys.components["FEATURES"]["feature_code_hash"],
                runtime_s=float(meta.get("timing_s") or 0.0), extra={"store_key": meta["cache_key"]},
            )  # fmt: skip

    def get_trades() -> TradeArrays:
        if "trades" in memo:
            return memo["trades"]
        if lookups["SIMULATION"].hit:
            memo["trades"] = trades_from(load_npz(stage_dir("SIMULATION") / _TRADES_FILE))
            return memo["trades"]
        candidates, arrays, _ = get_signals()

        def compute():
            trades = simulate_fast(
                market_arrays(arrays), candidates, experiment.cost_model, experiment.sizing,
                experiment.rules, experiment.window,
            )  # fmt: skip
            files = {_TRADES_FILE: npz_bytes({n: getattr(trades, n) for n in _trade_fields()})}
            return files, {"n_trades": len(trades)}, trades

        trades, runtime, _ = store.compute_and_publish(
            exp_id, market, "SIMULATION", keys.simulation, compute,
            code=keys.components["SIMULATION"]["sim_code_hash"], cacheable=cacheable,
        )  # fmt: skip
        stages["SIMULATION"].update(computed=True, runtime_s=runtime, cache="MISS")
        memo["trades"] = trades
        return trades

    metrics: dict[str, Any] | None = None
    if lookups["METRICS"].hit:
        try:
            metrics = json.loads((stage_dir("METRICS") / _METRICS_FILE).read_text("utf-8"))
        except Exception:
            lookups["METRICS"] = dag.Lookup(False, "UNREADABLE")
            stages["METRICS"].update(cache="MISS", reason="UNREADABLE")
    if metrics is None:
        candidates, arrays, sig_meta = get_signals()
        trades = get_trades()

        def compute_m():
            metrics = compute_metrics(
                experiment, market_arrays(arrays), arrays["date_days"].astype("datetime64[D]"),
                candidates, trades, invalid_stop_count=int(sig_meta.get("invalid_stop_count", 0)),
                gate=gate, metric_version=metric_version,
            )  # fmt: skip
            return {_METRICS_FILE: json_bytes(metrics)}, {"status": metrics["status"]}, metrics

        metrics, runtime, _ = store.compute_and_publish(
            exp_id, market, "METRICS", keys.metrics, compute_m,
            code=keys.components["METRICS"]["metric_code_hash"], cacheable=cacheable,
        )  # fmt: skip
        stages["METRICS"].update(computed=True, runtime_s=runtime, cache="MISS")

    entry_exit_record = None
    if entry_exit:
        entry_exit_record = _entry_exit_stage(
            experiment,
            market,
            keys,
            store,
            exp_id,
            get_signals,
            get_trades,
            entry_exit_config or {},
        )
    record = {
        "experiment_id": exp_id,
        "experiment_hash": experiment.experiment_hash(),
        "market": market,
        "status": metrics["status"],
        "reasons": metrics["reasons"],
        "dataset_hash": dhash,
        "n_bars": len(frame),
        "data_scope": data_scope(experiment, frame),
        "keys": keys.as_dict(),
        "stages": stages,
        "metric_version": metric_version,
        "entry_exit": entry_exit_record,
        "cacheable": cacheable,
    }
    store.write_run_record(exp_id, market, record)
    return record


def _entry_exit_stage(
    experiment, market, keys, store, exp_id, get_signals, get_trades, config
) -> dict[str, Any]:
    from . import entry_exit_adapter as adapter

    flat_min = (experiment.window or SimWindow()).flat_min
    cfg = {**adapter.DEFAULT_CONFIG, "flat_min": flat_min, **config}
    key = dag.entry_exit_key(keys.simulation, cfg)
    lk = store.lookup(market, "ENTRY_EXIT", key, cacheable=keys.cacheable)
    if lk.hit:
        try:
            adapter_record = json.loads(
                (store.stage_dir(market, "ENTRY_EXIT", key) / _ENTRY_EXIT_FILE).read_text("utf-8")
            )
            return {
                "key": key,
                "cache": "HIT",
                "computed": False,
                "summary_status": adapter_record["status"],
            }
        except Exception:
            pass
    candidates, arrays, _ = get_signals()
    trades = get_trades()

    def compute():
        result = _clean(
            adapter.run_entry_exit_diagnostic(market, arrays, candidates, trades, **cfg)
        )
        return {_ENTRY_EXIT_FILE: json_bytes(result)}, {"status": result["status"]}, result

    result, runtime, _ = store.compute_and_publish(
        exp_id, market, "ENTRY_EXIT", key, compute, code=dag.code_hash(dag.ENTRY_EXIT_CODE),
        cacheable=keys.cacheable,
    )  # fmt: skip
    return {
        "key": key,
        "cache": "MISS",
        "computed": True,
        "runtime_s": runtime,
        "summary_status": result["status"],
    }


# ---- multi-market ----
def _market_task(arg: tuple) -> dict[str, Any]:
    experiment, market, metric_version, gate, entry_exit, cfg = arg
    return run_market(
        experiment, market, metric_version=metric_version, gate=gate, entry_exit=entry_exit,
        entry_exit_config=cfg,
    )  # fmt: skip


def _init_worker(src: str) -> None:  # pragma: no cover - runs in pool workers
    import sys

    if src not in sys.path:
        sys.path.insert(0, src)


def run_fast(
    experiment: ExperimentSpec,
    *,
    jobs: int = 1,
    markets: tuple[str, ...] | None = None,
    metric_version: str = METRIC_VERSION,
    gate: FastGate = FastGate(),  # noqa: B008
    entry_exit: bool = False,
    entry_exit_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run every market as an independent task (jobs clamped by ``clamp_jobs``); one failure never aborts the rest."""
    names = tuple(markets or experiment.markets)
    tasks = [
        Task(m, _market_task, (experiment, m, metric_version, gate, entry_exit, entry_exit_config))
        for m in names
    ]
    src = str(Path(__file__).resolve().parents[1])
    results, errors = run_dag(tasks, jobs, initializer=_init_worker, initargs=(src,))
    per_market: dict[str, Any] = {}
    for m in names:
        if m in results:
            per_market[m] = results[m]
        else:
            per_market[m] = {
                "market": m,
                "status": "FAILED",
                "error": (errors.get(m) or "unknown").strip().splitlines()[-1],
            }
    effective = 1 if jobs <= 1 else clamp_jobs(jobs, len(tasks))
    return {
        "experiment_id": experiment.experiment_id,
        "markets": per_market,
        "n_failed": sum(1 for r in per_market.values() if r["status"] == "FAILED"),
        "all_promote": all(
            r["status"] == str(PromotionStatus.PROMOTE_TO_FIDELITY) for r in per_market.values()
        ),
        "jobs_requested": jobs,
        "jobs_effective": effective,
    }


# ---- plan (read-only) ----
def classify(experiment: ExperimentSpec) -> str:
    """LIGHT = synthetic in-memory data; HEAVY = real files (csv/parquet) or anything Nautilus-related."""
    return "LIGHT" if experiment.dataset.kind == "synthetic" else "HEAVY"


def plan_experiment(
    experiment: ExperimentSpec,
    *,
    jobs: int = 1,
    metric_version: str = METRIC_VERSION,
    gate: FastGate = FastGate(),  # noqa: B008
) -> dict[str, Any]:
    """Read-only: cache HIT/MISS per market x stage. Runs no simulation, writes nothing."""
    store = ArtifactStore(experiment.artifact_root)
    markets: dict[str, Any] = {}
    for market in experiment.markets:
        try:
            frame, dhash, keys = _prepare(experiment, market, metric_version, gate)
        except Exception as exc:
            markets[market] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        lookups = _all_lookups(store, market, keys)
        markets[market] = {
            "n_bars": len(frame),
            "dataset_hash": dhash,
            "data_scope": data_scope(experiment, frame),
            "cacheable": keys.cacheable,
            "stages": {
                s: {
                    "key": keys.key(s),
                    "cache": "HIT" if lookups[s].hit else "MISS",
                    "reason": lookups[s].reason,
                }
                for s in ("FEATURES", "SIGNALS", "SIMULATION", "METRICS")
            },
            "expected_segments": [
                f"{market}/{s}"
                for s in ("FEATURES", "SIGNALS", "SIMULATION", "METRICS")
                if not lookups[s].hit
            ],
        }
    try:
        effective = 1 if jobs <= 1 else clamp_jobs(jobs, len(experiment.markets))
        guard = "OK"
    except Exception as exc:
        effective, guard = 0, f"REFUSED: {exc}"
    return {
        "experiment_id": experiment.experiment_id,
        "experiment_hash": experiment.experiment_hash(),
        "strategy_id": experiment.strategy_spec.strategy_id,
        "spec_hash": experiment.strategy_spec.spec_hash(),
        "dataset": asdict(experiment.dataset),
        "partitions_read": experiment.partitions_actually_read(),
        "classification": classify(experiment),
        "markets": markets,
        "workers": {"requested": jobs, "effective": effective},
        "ram_guard": {"available_mb": available_memory_mb(), "status": guard},
    }


__all__ = (
    "METRIC_VERSION",
    "FastGate",
    "RejectCode",
    "classify",
    "compute_metrics",
    "market_from_features",
    "plan_experiment",
    "run_fast",
    "run_market",
)
