"""PromotionStatus + ExperimentSpec identity + data-reading rules (fast, no feature build)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec
from alpha.fast.sim import SimWindow
from alpha.fast.store import FeatureConfig
from research_workbench.experiment import (
    DatasetRef,
    berlin_dates,
    dataset_hash,
    experiment_from_dict,
    load_market_frame,
)
from research_workbench.status import PromotionStatus
from tests.unit.research_workbench.test_workbench_helpers import experiment, strategy


def test_promotion_status_has_no_live_member() -> None:
    names = {m.name for m in PromotionStatus}
    assert names == {
        "REJECT_FAST",
        "PROMOTE_TO_FIDELITY",
        "FIDELITY_MISMATCH",
        "READY_FOR_ROBUSTNESS",
        "ROBUSTNESS_FAILED",
        "READY_FOR_SHADOW_RESEARCH",
    }
    assert not any("LIVE" in n for n in names)
    assert not any("LIVE" in str(m.value) for m in PromotionStatus)


def test_experiment_hash_is_deterministic_and_ignores_location_and_notes(tmp_path) -> None:
    a = experiment(str(tmp_path / "x"))
    b = experiment(str(tmp_path / "y"), notes="other prose")
    assert a.experiment_hash() == experiment(str(tmp_path / "x")).experiment_hash()
    assert a.experiment_hash() == b.experiment_hash()
    assert a.experiment_id == "exp-" + a.experiment_hash()[:16]


@pytest.mark.parametrize(
    "change",
    [
        {"strategy_spec": strategy(threshold=30.0)},
        {"markets": ("SYN_A", "SYN_B")},
        {"dataset": DatasetRef(kind="synthetic", seed=6, days=30, start="2024-03-04")},
        {"date_range": ("2024-03-05", "2024-04-10")},
        {"cost_model": COST_SCENARIOS["SPREAD_STRESS"]},
        {"sizing": SizingSpec(risk_fraction=0.01)},
        {"rules": SimRules(max_trades_per_day=3)},
        {"window": SimWindow(540, 1200, 1290)},
        {"seed": 1},
        {"engine_mode": "both"},
        {"feature_config": FeatureConfig(swing_order=4)},
        {"partitions_read": ("TRAIN", "VALIDATION", "OOS")},
        {"experiment_version": 2},
        {"code_commit": "abc123"},
    ],
)
def test_experiment_hash_is_sensitive_to_each_field(tmp_path, change) -> None:
    base = experiment(str(tmp_path))
    assert replace(base, **change).experiment_hash() != base.experiment_hash()


def test_split_change_changes_hash(tmp_path) -> None:
    from alpha.common.protocol import Partition, SplitPlan

    base = experiment(str(tmp_path))
    other = SplitPlan(
        Partition("TRAIN", "2024-03-04", "2024-03-28"),
        base.split.validation,
        base.split.oos,
    )
    assert replace(base, split=other).experiment_hash() != base.experiment_hash()


def test_forward_is_never_readable_and_train_validation_are_required(tmp_path) -> None:
    with pytest.raises(ValueError, match="FORWARD"):
        experiment(str(tmp_path), partitions_read=("TRAIN", "VALIDATION", "FORWARD"))
    with pytest.raises(ValueError, match="TRAIN and VALIDATION"):
        experiment(str(tmp_path), partitions_read=("TRAIN",))


def test_oos_bars_are_not_even_loaded_unless_the_split_permits(tmp_path) -> None:
    exp = experiment(
        str(tmp_path), dataset=DatasetRef(kind="synthetic", seed=5, days=40, start="2024-03-04")
    )
    full = exp.dataset.load("SYN_A")
    assert berlin_dates(full["ts"]).max() > np.datetime64(exp.split.validation.end)
    clipped = load_market_frame(exp, "SYN_A")
    assert berlin_dates(clipped["ts"]).max() <= np.datetime64(exp.split.validation.end)
    assert exp.partitions_actually_read() == {
        "TRAIN": True, "VALIDATION": True, "OOS": False, "FORWARD": False,
    }  # fmt: skip
    permitted = replace(exp, partitions_read=("TRAIN", "VALIDATION", "OOS"))
    assert len(load_market_frame(permitted, "SYN_A")) > len(clipped)
    assert permitted.partitions_actually_read()["OOS"] is True


def test_dataset_hash_covers_the_actual_frame(tmp_path) -> None:
    frame = load_market_frame(experiment(str(tmp_path)), "SYN_A")
    changed = frame.copy()
    changed.loc[10, "close"] += 0.01
    assert dataset_hash(frame) == dataset_hash(frame.copy())
    assert dataset_hash(frame) != dataset_hash(changed)
    # a different market is different data
    assert dataset_hash(frame) != dataset_hash(
        load_market_frame(experiment(str(tmp_path)), "SYN_B")
    )


def test_experiment_json_roundtrip_matches_python_construction(tmp_path) -> None:
    exp = experiment(str(tmp_path))
    data = {
        "strategy": {
            "strategy_id": "WB_TEST_RSI", "version": "1", "direction": "LONG",
            "entry_rules": [{"feature": "m5_rsi14", "op": "<", "threshold": 35.0}],
            "stop": {"kind": "atr_multiple", "feature": "m5_atr14", "multiple": 2.0},
            "target": {"kind": "fixed_r", "r": 1.5},
        },
        "markets": ["SYN_A"],
        "dataset": {"kind": "synthetic", "seed": 5, "days": 30, "start": "2024-03-04"},
        "split": {
            "train": ["2024-03-04", "2024-03-29"], "validation": ["2024-04-01", "2024-04-12"],
            "oos": ["2024-04-15", "2024-04-30"], "embargo_days": 0,
        },
        "artifact_root": str(tmp_path),
    }  # fmt: skip
    assert experiment_from_dict(data).experiment_hash() == exp.experiment_hash()
