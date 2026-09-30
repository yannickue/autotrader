"""Equivalence and causal-contract gates for Group A fast kernels."""

from __future__ import annotations

import importlib
import json
from time import perf_counter

import numpy as np
import pytest

from alpha.fast.store import FeatureSet, FeatureStore
from alpha.strategies.momentum import VARIANTS as MOMENTUM_VARIANTS
from alpha.strategies.opening_drive import VARIANTS as OPENING_DRIVE_VARIANTS
from alpha.strategies.trend_pullback import VARIANTS as TREND_PULLBACK_VARIANTS
from tests._fast_equiv import (
    assert_generator_matches_golden,
    golden_setup,
    requires_dataset,
)

FAMILIES = (
    (
        "GROUP_A_TREND_PULLBACK",
        TREND_PULLBACK_VARIANTS,
        "alpha.fast.kernels.trend_pullback",
    ),
    (
        "GROUP_A_MOMENTUM_CONTINUATION",
        MOMENTUM_VARIANTS,
        "alpha.fast.kernels.momentum",
    ),
    (
        "GROUP_A_OPENING_DRIVE",
        OPENING_DRIVE_VARIANTS,
        "alpha.fast.kernels.opening_drive",
    ),
)


def _generate(module_name: str):
    module = importlib.import_module(module_name)
    return module.generate


def _assert_candidates_equal(left, right) -> None:
    for name in ("decision_idx", "direction", "stop", "target", "target_r", "exit_kind"):
        np.testing.assert_array_equal(getattr(left, name), getattr(right, name), err_msg=name)


@requires_dataset
@pytest.mark.parametrize(("strategy_id", "variants", "module_name"), FAMILIES)
def test_group_a_generator_matches_golden(strategy_id, variants, module_name):
    assert_generator_matches_golden(strategy_id, variants, _generate(module_name))


@requires_dataset
@pytest.mark.parametrize(("_strategy_id", "variants", "module_name"), FAMILIES)
def test_group_a_generator_is_deterministic(_strategy_id, variants, module_name):
    features = golden_setup()[2]
    generate = _generate(module_name)
    for params in variants:
        _assert_candidates_equal(generate(features, params), generate(features, params))


@requires_dataset
@pytest.mark.parametrize(("_strategy_id", "variants", "module_name"), FAMILIES)
def test_group_a_generator_is_truncation_invariant(_strategy_id, variants, module_name):
    df, _, full, _ = golden_setup()
    cutoff = 8_000
    prefix = FeatureStore.build(df.iloc[:cutoff].copy())
    generate = _generate(module_name)
    for params in variants:
        full_candidates = generate(full, params)
        keep = full_candidates.decision_idx < cutoff
        truncated = full_candidates.subset(keep)
        _assert_candidates_equal(truncated, generate(prefix, params))


@requires_dataset
@pytest.mark.parametrize(("_strategy_id", "variants", "module_name"), FAMILIES)
def test_group_a_generator_rejects_ineligible_regimes(_strategy_id, variants, module_name):
    original = golden_setup()[2]
    arrays = dict(original)
    arrays["regime_direction"] = np.zeros(len(original["c"]), dtype=np.int16)
    features = FeatureSet(arrays, original.metadata)
    generate = _generate(module_name)
    for params in variants:
        assert len(generate(features, params).decision_idx) == 0


@requires_dataset
@pytest.mark.parametrize(("strategy_id", "variants", "module_name"), FAMILIES)
def test_group_a_generator_12k_timing(strategy_id, variants, module_name):
    features = golden_setup()[2]
    generate = _generate(module_name)
    generate(features, variants[0])  # exclude one-time Numba compilation
    for index, params in enumerate(variants):
        started = perf_counter()
        generate(features, params)
        elapsed_ms = (perf_counter() - started) * 1_000
        print(f"{strategy_id}#{index} 12k: {elapsed_ms:.3f} ms")
        assert elapsed_ms < 1_000


@requires_dataset
def test_group_a_generator_full_dev_timing(tmp_path):
    golden_setup()  # installs the research runner path
    import ar2_compare

    from alpha.common.dataset import load_research_dataset
    from alpha.common.protocol import Partition, SplitPlan
    from tests._fast_equiv import DATA, ROOT

    config = json.loads((ROOT / "research/configs/ar2_phase2.json").read_text(encoding="utf-8"))
    plan = SplitPlan(**{key: Partition(key, *value) for key, value in config["splits"].items()})
    frame = ar2_compare.dev_frame(load_research_dataset(DATA).frame, plan)
    features = FeatureStore.load_or_build(frame, None, tmp_path)
    for strategy_id, variants, module_name in FAMILIES:
        generate = _generate(module_name)
        generate(features, variants[0])
        for index, params in enumerate(variants):
            started = perf_counter()
            generate(features, params)
            elapsed_ms = (perf_counter() - started) * 1_000
            print(f"{strategy_id}#{index} full-dev: {elapsed_ms:.3f} ms")
            assert elapsed_ms < 5_000
