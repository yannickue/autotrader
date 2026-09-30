from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from alpha.fast.store import FeatureSet
from tests._fast_equiv import (
    assert_generator_matches_golden,
    golden_setup,
    requires_dataset,
)

FAMILIES = (
    ("RANGE_MEAN_REVERSION", "alpha.fast.kernels.mean_reversion"),
    ("SESSION_TWAP_REFERENCE", "alpha.fast.kernels.vwap_reference"),
    ("PREVIOUS_DAY_LEVELS", "alpha.fast.kernels.prev_day_levels"),
    ("SESSION_SWEEP_REVERSAL", "alpha.fast.kernels.session_sweep"),
)

ROOT = Path(__file__).resolve().parents[1]


def _family(module_name: str):
    import importlib

    module = importlib.import_module(module_name)
    return module.VARIANTS, module.generate


@requires_dataset
@pytest.mark.parametrize(("strategy_id", "module_name"), FAMILIES)
def test_group_c_generator_matches_golden(strategy_id: str, module_name: str) -> None:
    variants, generate = _family(module_name)
    assert_generator_matches_golden(strategy_id, variants, generate)


@requires_dataset
@pytest.mark.parametrize(("strategy_id", "module_name"), FAMILIES)
def test_group_c_generator_is_deterministic(strategy_id: str, module_name: str) -> None:
    del strategy_id
    _, _, features, _ = golden_setup()
    variants, generate = _family(module_name)
    first = generate(features, variants[0])
    second = generate(features, variants[0])
    for name in first.__dataclass_fields__:
        np.testing.assert_array_equal(getattr(first, name), getattr(second, name))


@requires_dataset
@pytest.mark.parametrize(("strategy_id", "module_name"), FAMILIES)
def test_group_c_generator_is_truncation_invariant(strategy_id: str, module_name: str) -> None:
    del strategy_id
    _, _, features, _ = golden_setup()
    variants, generate = _family(module_name)
    cutoff = len(features["o"]) * 2 // 3
    prefix = FeatureSet(
        {name: values[:cutoff] for name, values in features.items()}, features.metadata
    )
    short = generate(prefix, variants[0])
    full = generate(features, variants[0])
    known = full.decision_idx < cutoff
    for name in full.__dataclass_fields__:
        if getattr(full, name) is None:
            assert getattr(short, name) is None
            continue
        np.testing.assert_array_equal(getattr(short, name), getattr(full, name)[known])


@requires_dataset
@pytest.mark.parametrize(("strategy_id", "module_name"), FAMILIES)
def test_group_c_generator_obeys_regime_gate(strategy_id: str, module_name: str) -> None:
    del strategy_id
    _, _, features, _ = golden_setup()
    arrays = dict(features)
    arrays["regime_trend_strength"] = np.zeros(len(features["o"]), dtype=np.int16)
    gated = FeatureSet(arrays, features.metadata)
    variants, generate = _family(module_name)
    assert len(generate(gated, variants[0]).decision_idx) == 0


@requires_dataset
@pytest.mark.parametrize(("strategy_id", "module_name"), FAMILIES)
def test_group_c_variant_generation_timing(strategy_id: str, module_name: str) -> None:
    del strategy_id
    _, _, features, _ = golden_setup()
    variants, generate = _family(module_name)
    generate(features, variants[0])  # exclude one-off Numba compilation
    started = time.perf_counter()
    for params in variants:
        generate(features, params)
    assert (time.perf_counter() - started) / len(variants) < 1.0


@pytest.fixture(scope="module")
def full_dev_features(tmp_path_factory):
    sys.path.insert(0, str(ROOT / "research" / "runners"))
    import ar2_compare

    from alpha.common.dataset import load_research_dataset
    from alpha.common.protocol import Partition, SplitPlan
    from alpha.fast.store import FeatureStore

    cfg = json.loads((ROOT / "research/configs/ar2_phase2.json").read_text(encoding="utf-8"))
    plan = SplitPlan(**{key: Partition(key, *value) for key, value in cfg["splits"].items()})
    frame = ar2_compare.dev_frame(load_research_dataset(ROOT / "data/ar1_ger40").frame, plan)
    return FeatureStore.load_or_build(frame, None, tmp_path_factory.mktemp("gc-cache"))


@requires_dataset
@pytest.mark.parametrize(("strategy_id", "module_name"), FAMILIES)
def test_group_c_full_dev_variant_generation_timing(
    strategy_id: str, module_name: str, full_dev_features: FeatureSet
) -> None:
    del strategy_id
    variants, generate = _family(module_name)
    generate(full_dev_features, variants[0])
    started = time.perf_counter()
    for params in variants:
        generate(full_dev_features, params)
    assert (time.perf_counter() - started) / len(variants) < 2.0
