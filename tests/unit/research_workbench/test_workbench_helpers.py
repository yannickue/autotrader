# ruff: noqa: E501
"""Shared helpers for the workbench tests (no tests in here)."""

from __future__ import annotations

from dataclasses import replace

from alpha.common.protocol import Partition, SplitPlan
from alpha.fast.spec import Rule, StopSpec, StrategySpec, TargetSpec
from research_workbench.experiment import DatasetRef, ExperimentSpec
from research_workbench.fastrun import FastGate

# loose gate so a PROMOTE outcome is reachable on synthetic noise; the status itself is not what the cache tests check
LOOSE_GATE = FastGate(
    min_train_trades=1, min_validation_trades=1, require_positive_expectancy=False
)


def small_split() -> SplitPlan:
    return SplitPlan(
        Partition("TRAIN", "2024-03-04", "2024-03-29"),
        Partition("VALIDATION", "2024-04-01", "2024-04-12"),
        Partition("OOS", "2024-04-15", "2024-04-30"),
        embargo_days=0,
    )


def strategy(threshold: float = 35.0, target_r: float = 1.5) -> StrategySpec:
    return StrategySpec(
        strategy_id="WB_TEST_RSI",
        version="1",
        direction="LONG",
        entry_rules=(Rule("m5_rsi14", "<", threshold),),
        stop=StopSpec("atr_multiple", feature="m5_atr14", multiple=2.0),
        target=TargetSpec("fixed_r", target_r),
    )


def experiment(root: str, *, markets: tuple[str, ...] = ("SYN_A",), **changes) -> ExperimentSpec:
    base = ExperimentSpec(
        strategy_spec=strategy(),
        markets=markets,
        dataset=DatasetRef(kind="synthetic", seed=5, days=30, start="2024-03-04"),
        split=small_split(),
        artifact_root=str(root),
    )
    return replace(base, **changes)


def stage_cache(record: dict) -> dict[str, str]:
    return {s: v["cache"] for s, v in record["stages"].items()}


def stage_computed(record: dict) -> dict[str, bool]:
    return {s: v["computed"] for s, v in record["stages"].items()}
