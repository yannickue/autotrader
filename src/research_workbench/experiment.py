# ruff: noqa: E501
"""Experiment identity for the research workbench (OFFLINE ONLY).

Timing convention: decision at the close of bar t (features of bar t use its full OHLC and nothing later), fill at the next
open (bar t+1); bar timestamps in the feature set are bar-OPEN times.

``ExperimentSpec`` wraps the existing declarative ``alpha.fast.spec.StrategySpec`` (no second strategy language) together
with everything else that determines a result: markets, dataset identity, date range, split, cost model, sizing, sim
rules, seed, engine mode, feature config. ``experiment_hash()`` is the sha256 of the canonical JSON of that identity
(``research_speed.artifact.canonical_json``).

Excluded from the hash on purpose (they do not change any number): ``artifact_root`` (a location) and ``notes`` (prose).
Everything else, including ``code_commit`` and ``partitions_read``, is part of the identity.

JSON experiment file schema (``scripts/research_strategy.py --spec FILE``)::

    {
      "experiment_version": 1,
      "strategy": {                      # alpha.fast.spec.StrategySpec
        "strategy_id": "X", "version": "1", "direction": "LONG" | "SHORT" | "BOTH",
        "entry_rules": [{"feature": "m5_rsi14", "op": "<", "threshold": 35.0}],   # or "other_feature": "..."
        "stop": {"kind": "atr_multiple", "feature": "m5_atr14", "multiple": 2.0},
        "target": {"kind": "fixed_r", "r": 1.5},
        "regime_filters": {}, "context_filters": [], "or_groups": [], "params": {}, "metadata": {}
      },
      "markets": ["SYN_A"],
      "dataset": {"kind": "synthetic", "seed": 7, "days": 30, "start": "2024-03-04"}
                 | {"kind": "csv" | "parquet", "path": "data/{market}.csv"},   # ts (UTC), open, high, low, close, spread_pts
      "date_range": ["2024-03-04", "2024-04-30"] | null,                       # inclusive Berlin dates
      "split": {"train": ["2024-03-04", "2024-03-29"], "validation": ["2024-04-01", "2024-04-19"],
                "oos": ["2024-04-22", "2024-04-30"], "embargo_days": 0},
      "partitions_read": ["TRAIN", "VALIDATION"],                              # OOS only if listed; FORWARD never
      "cost": "BASE" | {"name": "...", "spread_mult": 1.0, "slippage_pts": 0.5, ...},
      "sizing": {...SizingSpec fields...}, "rules": {...SimRules fields...},
      "window": null | {"entry_start_min": 540, "entry_end_min": 1200, "flat_min": 1290},
      "seed": 0, "engine_mode": "fast" | "fidelity" | "both",
      "feature_config": {...flat FeatureConfig overrides...},
      "artifact_root": "artifacts/research_workbench", "code_commit": null, "notes": ""
    }

Data reading rule: the dataset is clipped to the Berlin date of the LAST permitted partition BEFORE it is hashed and
before any feature is built, so OOS/FORWARD bars are never read unless the split says so (``partitions_read``).
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from alpha.common.protocol import Partition, SplitPlan
from alpha.common.sim import (
    COST_SCENARIOS,
    DEFAULT_RULES,
    DEFAULT_SIZING,
    CostScenario,
    SimRules,
    SizingSpec,
)
from alpha.fast.sim import SimWindow
from alpha.fast.spec import Rule, StopSpec, StrategySpec, TargetSpec
from alpha.fast.store import FeatureConfig
from research_speed.artifact import config_hash

EXPERIMENT_VERSION = 1
PARTITIONS = ("TRAIN", "VALIDATION", "OOS", "FORWARD")
_REQUIRED_COLUMNS = ("ts", "open", "high", "low", "close", "spread_pts")


@dataclass(frozen=True)
class DatasetRef:
    """Where the bars of a market come from. ``path`` may contain ``{market}``."""

    kind: Literal["synthetic", "csv", "parquet"] = "synthetic"
    path: str | None = None
    seed: int = 0
    days: int = 40
    start: str = "2024-03-04"
    base_price: float = 15000.0
    bar_sigma: float = 4.0

    def __post_init__(self) -> None:
        if self.kind not in {"synthetic", "csv", "parquet"}:
            raise ValueError(f"unknown dataset kind: {self.kind}")
        if self.kind != "synthetic" and not self.path:
            raise ValueError("csv/parquet datasets require a path")
        if self.kind == "synthetic" and self.days < 1:
            raise ValueError("synthetic dataset needs days >= 1")

    def load(self, market: str) -> pd.DataFrame:
        if self.kind == "synthetic":
            frame = synthetic_bars(
                seed=self.seed + zlib.crc32(market.encode()) % 10_000,
                days=self.days,
                start=self.start,
                base_price=self.base_price,
                sigma=self.bar_sigma,
            )
        else:
            path = Path(str(self.path).replace("{market}", market))
            frame = pd.read_parquet(path) if self.kind == "parquet" else pd.read_csv(path)
        return normalise_frame(frame)


def synthetic_bars(
    *, seed: int, days: int, start: str, base_price: float = 15000.0, sigma: float = 4.0
) -> pd.DataFrame:
    """Deterministic synthetic M5 bars: weekdays 07:00-20:55 UTC, random walk + slow oscillation."""
    rng = np.random.default_rng(seed)
    day_index = pd.bdate_range(start, periods=days)
    per_day = 168
    ts = np.concatenate(
        [
            (
                pd.Timestamp(day, tz="UTC")
                + pd.Timedelta(hours=7)
                + pd.to_timedelta(np.arange(per_day) * 5, unit="min")
            ).to_numpy()
            for day in day_index
        ]
    )
    n = len(ts)
    steps = rng.standard_normal(n) * sigma
    wave = 40.0 * np.sin(2 * np.pi * np.arange(n) / 300.0)
    close = base_price + np.cumsum(steps) + wave
    open_ = np.concatenate(([base_price], close[:-1]))
    wick_up = np.abs(rng.standard_normal(n)) * sigma * 0.6
    wick_dn = np.abs(rng.standard_normal(n)) * sigma * 0.6
    return pd.DataFrame(
        {
            "ts": pd.DatetimeIndex(ts, tz="UTC"),
            "open": open_,
            "high": np.maximum(open_, close) + wick_up,
            "low": np.minimum(open_, close) - wick_dn,
            "close": close,
            "spread_pts": np.full(n, 1.5),
        }
    )


def normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Canonical frame: required columns, ts as ns-UTC, sorted, unique."""
    frame = frame.copy()
    if "spread_pts" not in frame and "spread" in frame:
        frame = frame.rename(columns={"spread": "spread_pts"})
    missing = [c for c in _REQUIRED_COLUMNS if c not in frame]
    if missing:
        raise ValueError(f"dataset is missing columns: {missing}")
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True).astype("datetime64[ns, UTC]")
    frame = frame.loc[:, list(_REQUIRED_COLUMNS)].sort_values("ts").reset_index(drop=True)
    if frame["ts"].duplicated().any():
        raise ValueError("dataset has duplicate timestamps")
    for col in _REQUIRED_COLUMNS[1:]:
        frame[col] = frame[col].astype(float)
    return frame


def dataset_hash(frame: pd.DataFrame) -> str:
    """sha256 over the ACTUAL frame (ts/ohlc/spread), unit-independent."""
    hashed = pd.util.hash_pandas_object(frame.loc[:, list(_REQUIRED_COLUMNS)], index=False)
    return hashlib.sha256(hashed.to_numpy(np.uint64).tobytes()).hexdigest()


def berlin_dates(ts: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    index = pd.DatetimeIndex(ts)
    return (
        index.tz_convert("Europe/Berlin")
        .tz_localize(None)
        .normalize()
        .to_numpy()
        .astype("datetime64[D]")
    )


def default_split() -> SplitPlan:
    return SplitPlan(
        Partition("TRAIN", "2024-03-04", "2024-03-29"),
        Partition("VALIDATION", "2024-04-01", "2024-04-19"),
        Partition("OOS", "2024-04-22", "2024-04-30"),
        embargo_days=0,
    )


@dataclass(frozen=True)
class ExperimentSpec:
    strategy_spec: StrategySpec
    markets: tuple[str, ...]
    dataset: DatasetRef = field(default_factory=DatasetRef)
    date_range: tuple[str, str] | None = None
    split: SplitPlan = field(default_factory=default_split)
    cost_model: CostScenario = COST_SCENARIOS["BASE"]
    sizing: SizingSpec = DEFAULT_SIZING
    rules: SimRules = DEFAULT_RULES
    window: SimWindow | None = None
    seed: int = 0
    engine_mode: Literal["fast", "fidelity", "both"] = "fast"
    feature_config: FeatureConfig = field(default_factory=FeatureConfig)
    partitions_read: tuple[str, ...] = ("TRAIN", "VALIDATION")
    experiment_version: int = EXPERIMENT_VERSION
    code_commit: str | None = None
    artifact_root: str = "artifacts/research_workbench"
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.markets or len(set(self.markets)) != len(self.markets):
            raise ValueError("markets must be a non-empty tuple of unique names")
        if self.engine_mode not in {"fast", "fidelity", "both"}:
            raise ValueError("engine_mode must be fast, fidelity or both")
        unknown = set(self.partitions_read).difference(PARTITIONS)
        if unknown:
            raise ValueError(f"unknown partitions: {sorted(unknown)}")
        if "FORWARD" in self.partitions_read:
            raise ValueError("FORWARD data is never read by the offline workbench")
        if not {"TRAIN", "VALIDATION"} <= set(self.partitions_read):
            raise ValueError("partitions_read must include TRAIN and VALIDATION")
        if self.date_range is not None and not self.date_range[0] <= self.date_range[1]:
            raise ValueError("date_range start must not be after its end")

    # ---- identity ----
    def identity(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("artifact_root")
        data.pop("notes")
        data["strategy_spec_hash"] = self.strategy_spec.spec_hash()
        data["split"] = self.split.to_dict()
        return data

    def experiment_hash(self) -> str:
        return config_hash(self.identity())

    @property
    def experiment_id(self) -> str:
        return "exp-" + self.experiment_hash()[:16]

    # ---- data reading ----
    def last_read_date(self) -> str:
        """Last Berlin date any permitted partition covers (data after it is never read)."""
        ends = {
            "TRAIN": self.split.train.end,
            "VALIDATION": self.split.validation.end,
            "OOS": self.split.oos.end,
        }
        last = max(ends[name] for name in self.partitions_read)
        if self.date_range is not None:
            last = min(last, self.date_range[1])
        return last

    def partitions_actually_read(self) -> dict[str, bool]:
        read = {name: name in self.partitions_read for name in PARTITIONS}
        read["FORWARD"] = False
        return read


def load_market_frame(experiment: ExperimentSpec, market: str) -> pd.DataFrame:
    """Load a market and clip to the permitted date range (OOS/FORWARD never read unless permitted)."""
    frame = experiment.dataset.load(market)
    dates = berlin_dates(frame["ts"])
    keep = dates <= np.datetime64(experiment.last_read_date())
    if experiment.date_range is not None:
        keep &= dates >= np.datetime64(experiment.date_range[0])
    return frame.loc[keep].reset_index(drop=True)


CAUSALITY_STATEMENT = (
    "TIMING: features are keyed at bar-OPEN timestamps but use that bar's full OHLC (known only at the bar CLOSE) and nothing after it: the decision is made at the close of bar t and the fill happens at the next bar open (t+1). "
    "Features are computed over ALL loaded bars (including warm-up, gap and embargo bars) and are CAUSAL by the "
    "existing FeatureStore guarantee: tests/test_alpha_fast_store.py::test_all_features_are_truncation_invariant "
    "asserts that every feature array at bar t is unchanged when later bars are removed (so rolling windows, "
    "percentiles and swings do not look ahead) and that higher-timeframe values change only on completed "
    "boundaries. LIMITATION: that guarantee is verified by that test on one synthetic frame, not re-proven by the "
    "workbench; it is not a proof for every possible dataset. Metrics and screens use ONLY bars/trades whose "
    "entry date lies inside the declared partition masks (embargo days and inter-partition gaps are excluded)."
)


def data_scope(experiment: ExperimentSpec, frame: pd.DataFrame) -> dict[str, Any]:
    """Truthful statement of which bars were LOADED and which partitions feed the metrics."""
    dates = berlin_dates(frame["ts"])
    split = experiment.split
    parts = {"TRAIN": split.train, "VALIDATION": split.validation, "OOS": split.oos}
    masks = {name: split.mask(dates, part) for name, part in parts.items()}
    permitted = np.zeros(len(dates), dtype=bool)
    for name in experiment.partitions_read:
        permitted |= masks[name]
    train_start = np.datetime64(split.train.start)
    pre_train = dates < train_start
    inside = np.zeros(len(dates), dtype=bool)
    for mask in masks.values():
        inside |= mask
    gap = (~inside) & (~pre_train)
    return {
        "bars_loaded": {
            "n_bars": len(frame),
            "first_date": str(dates.min()) if len(dates) else None,
            "last_date": str(dates.max()) if len(dates) else None,
        },
        "warmup_before_train_bars": int(pre_train.sum()),
        "gap_or_embargo_bars": int(gap.sum()),
        "bars_in_partitions": {name: int(m.sum()) for name, m in masks.items()},
        "partitions_feeding_metrics": list(experiment.partitions_read),
        "bars_feeding_metrics": int(permitted.sum()),
        "bars_loaded_but_not_in_metrics": int(len(frame) - permitted.sum()),
        "causality": CAUSALITY_STATEMENT,
    }


# ---- JSON loading ----
def _cost(value: Any) -> CostScenario:
    if value is None:
        return COST_SCENARIOS["BASE"]
    if isinstance(value, str):
        return COST_SCENARIOS[value]
    return CostScenario(**value)


def _rule(item: dict[str, Any]) -> Rule:
    return Rule(item["feature"], item["op"], item.get("threshold"), item.get("other_feature"))


def strategy_from_dict(d: dict[str, Any]) -> StrategySpec:
    stop = d["stop"]
    return StrategySpec(
        strategy_id=d["strategy_id"],
        version=str(d["version"]),
        direction=d["direction"],
        entry_rules=tuple(_rule(r) for r in d["entry_rules"]),
        stop=StopSpec(**stop),
        target=TargetSpec(**d["target"]),
        regime_filters={k: tuple(v) for k, v in d.get("regime_filters", {}).items()},
        context_filters=tuple(d.get("context_filters", ())),
        or_groups=tuple(tuple(_rule(r) for r in g) for g in d.get("or_groups", ())),
        params=dict(d.get("params", {})),
        metadata=dict(d.get("metadata", {})),
    )


def experiment_from_dict(d: dict[str, Any]) -> ExperimentSpec:
    split = d.get("split")
    plan = default_split()
    if split is not None:
        plan = SplitPlan(
            Partition("TRAIN", *split["train"]),
            Partition("VALIDATION", *split["validation"]),
            Partition("OOS", *split["oos"]),
            int(split.get("embargo_days", 0)),
        )
    fc = dict(d.get("feature_config", {}))
    if "timeframes" in fc:
        fc["timeframes"] = tuple(fc["timeframes"])
    window = d.get("window")
    base = ExperimentSpec(strategy_spec=strategy_from_dict(d["strategy"]), markets=("X",))
    kwargs: dict[str, Any] = {
        "strategy_spec": base.strategy_spec,
        "markets": tuple(d["markets"]),
        "dataset": DatasetRef(**d.get("dataset", {})),
        "date_range": tuple(d["date_range"]) if d.get("date_range") else None,
        "split": plan,
        "cost_model": _cost(d.get("cost")),
        "sizing": SizingSpec(**d["sizing"]) if "sizing" in d else DEFAULT_SIZING,
        "rules": SimRules(**d["rules"]) if "rules" in d else DEFAULT_RULES,
        "window": SimWindow(**window) if window else None,
        "feature_config": FeatureConfig(**fc),
        "partitions_read": tuple(d.get("partitions_read", ("TRAIN", "VALIDATION"))),
    }
    for key in (
        "seed",
        "engine_mode",
        "experiment_version",
        "code_commit",
        "artifact_root",
        "notes",
    ):
        if key in d:
            kwargs[key] = d[key]
    allowed = {f.name for f in fields(ExperimentSpec)}
    return ExperimentSpec(**{k: v for k, v in kwargs.items() if k in allowed})


def demo_synthetic_experiment(artifact_root: str | None = None) -> ExperimentSpec:
    """Built-in smoke-test experiment: one synthetic market, RSI mean-reversion long."""
    strategy = StrategySpec(
        strategy_id="WB_DEMO_RSI",
        version="1",
        direction="LONG",
        entry_rules=(Rule("m5_rsi14", "<", 35.0),),
        stop=StopSpec("atr_multiple", feature="m5_atr14", multiple=2.0),
        target=TargetSpec("fixed_r", 1.5),
    )
    exp = ExperimentSpec(
        strategy_spec=strategy,
        markets=("SYN_A",),
        dataset=DatasetRef(kind="synthetic", seed=11, days=40, start="2024-03-04"),
    )
    return replace(exp, artifact_root=artifact_root) if artifact_root else exp


__all__ = (
    "CAUSALITY_STATEMENT",
    "EXPERIMENT_VERSION",
    "PARTITIONS",
    "DatasetRef",
    "ExperimentSpec",
    "berlin_dates",
    "data_scope",
    "dataset_hash",
    "default_split",
    "demo_synthetic_experiment",
    "experiment_from_dict",
    "load_market_frame",
    "normalise_frame",
    "strategy_from_dict",
    "synthetic_bars",
)
