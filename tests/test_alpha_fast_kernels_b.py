from __future__ import annotations

from dataclasses import replace
from importlib import import_module

import numpy as np
import pytest

from alpha.fast.store import FeatureSet
from tests._fast_equiv import assert_generator_matches_golden, golden_setup

FAMILIES = (
    (
        "OPENING_RANGE_BREAKOUT_RETEST",
        "alpha.fast.kernels.breakout_retest",
    ),
    (
        "COMPRESSION_EXPANSION",
        "alpha.fast.kernels.compression_expansion",
    ),
    (
        "FAILED_BREAKOUT_REVERSAL",
        "alpha.fast.kernels.failed_breakout",
    ),
)


def _family(module_name: str):
    module = import_module(module_name)
    return module.VARIANTS, module.generate


def _prefix(features: FeatureSet, length: int) -> FeatureSet:
    arrays = {name: values[:length] for name, values in features.items()}
    return FeatureSet(arrays, features.metadata)


@pytest.mark.parametrize(("strategy_id", "module_name"), FAMILIES)
def test_generator_matches_golden(strategy_id: str, module_name: str) -> None:
    variants, generate = _family(module_name)
    assert_generator_matches_golden(strategy_id, variants, generate)


@pytest.mark.parametrize(("_strategy_id", "module_name"), FAMILIES)
def test_generator_is_deterministic(_strategy_id: str, module_name: str) -> None:
    variants, generate = _family(module_name)
    features = golden_setup()[2]
    for params in variants:
        first = generate(features, params)
        second = generate(features, params)
        for name in first.__dataclass_fields__:
            np.testing.assert_array_equal(getattr(first, name), getattr(second, name))


@pytest.mark.parametrize(("_strategy_id", "module_name"), FAMILIES)
def test_generator_is_truncation_invariant(_strategy_id: str, module_name: str) -> None:
    variants, generate = _family(module_name)
    features = golden_setup()[2]
    cutoff = 8_000
    prefix = _prefix(features, cutoff)
    for params in variants:
        full = generate(features, params)
        short = generate(prefix, params)
        mask = full.decision_idx < cutoff
        for name in full.__dataclass_fields__:
            np.testing.assert_array_equal(getattr(full, name)[mask], getattr(short, name))


@pytest.mark.parametrize(("strategy_id", "module_name"), FAMILIES)
def test_regime_gating_blocks_all_candidates(strategy_id: str, module_name: str) -> None:
    variants, generate = _family(module_name)
    original = golden_setup()[2]
    arrays = dict(original)
    if strategy_id == "OPENING_RANGE_BREAKOUT_RETEST":
        arrays["regime_trend_strength"] = np.ones(len(original["c"]), dtype=np.int16)
    elif strategy_id == "FAILED_BREAKOUT_REVERSAL":
        arrays["regime_trend_strength"] = np.full(len(original["c"]), 3, dtype=np.int16)
    else:
        arrays["regime_vol_state"] = np.full(len(original["c"]), 2, dtype=np.int16)
        arrays["regime_volatility"] = np.full(len(original["c"]), 2, dtype=np.int16)
    gated = FeatureSet(arrays, original.metadata)
    for params in variants:
        assert len(generate(gated, params).decision_idx) == 0


@pytest.mark.parametrize(("_strategy_id", "module_name"), FAMILIES)
def test_each_variant_executes_on_golden_slice(_strategy_id: str, module_name: str) -> None:
    variants, generate = _family(module_name)
    features = golden_setup()[2]
    for params in variants:
        result = generate(features, replace(params))
        assert result.decision_idx.dtype == np.int64
