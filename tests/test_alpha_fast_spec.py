from __future__ import annotations

import sys
from dataclasses import replace
from types import ModuleType

import numpy as np
import pytest

from alpha.common.sim import ExitSpec
from alpha.fast.provider import KernelFamilyProvider, SpecProvider, to_signal_candidates
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.spec import (
    Rule,
    RuleMaskCache,
    StopSpec,
    StrategySpec,
    TargetSpec,
    _rule_mask,
    evaluate_spec,
)
from alpha.fast.store import FeatureSet
from tests._fast_equiv import golden_setup, requires_dataset


def _features(n: int = 6) -> FeatureSet:
    arrays = {
        "ts_ns": np.arange(n, dtype=np.int64) * 300_000_000_000,
        "c": np.full(n, 100.0),
        "m5_atr14": np.full(n, 2.0),
        "h1_adx14": np.array([20.0, 26.0, 27.0, 28.0, 29.0, 30.0])[:n],
        "h1_ema_slope": np.array([-1.0, 1.0, 1.0, 1.0, 1.0, 1.0])[:n],
        "context_pullback": np.array([False, True, True, True, True, True])[:n],
        "m5_normalized_return": np.array([0.0, 0.1, 0.3, 0.4, -0.1, 0.5])[:n],
        "regime_direction": np.full(n, 3, dtype=np.int16),
        "regime_trend_strength": np.full(n, 3, dtype=np.int16),
        "regime_volatility": np.full(n, 2, dtype=np.int16),
        "regime_vol_state": np.full(n, 1, dtype=np.int16),
        "phase_code": np.zeros(n, dtype=np.int8),
    }
    for name in (
        "trend_continuation",
        "consolidation",
        "compression",
        "range_extreme",
        "breakout_setup",
        "retest",
        "failed_breakout",
        "momentum_continuation",
        "reversal_context",
    ):
        arrays[f"context_{name}"] = np.zeros(n, dtype=bool)
    return FeatureSet(
        arrays,
        {
            "maps": {
                "phase": {"0": "EUROPEAN_OPEN"},
                "regime": {
                    "DIRECTION": {"3": "UP"},
                    "TREND_STRENGTH": {"3": "TRENDING"},
                    "VOLATILITY": {"2": "NORMAL"},
                    "VOL_STATE": {"1": "COMPRESSION"},
                },
            }
        },
    )


def _spec(**changes: object) -> StrategySpec:
    base = StrategySpec(
        strategy_id="DECLARATIVE_PULLBACK",
        version="1",
        direction="LONG",
        entry_rules=(
            Rule("h1_adx14", ">", threshold=25.0),
            Rule("h1_ema_slope", ">", threshold=0.0),
            Rule("m5_normalized_return", ">", threshold=0.2),
        ),
        context_filters=("PULLBACK",),
        stop=StopSpec("atr_multiple", feature="m5_atr14", multiple=1.5),
        target=TargetSpec("fixed_r", r=2.2),
        params={"threshold": 0.2},
    )
    return replace(base, **changes)


def test_spec_hash_is_deterministic_and_sensitive_to_every_field() -> None:
    original = _spec()
    assert original.spec_hash() == _spec().spec_hash()
    assert original.spec_hash() != _spec(version="2").spec_hash()
    assert original.spec_hash() != _spec(metadata={"owner": "research"}).spec_hash()


def test_spec_rejects_unknown_features_and_invalid_rules() -> None:
    with pytest.raises(ValueError, match="unknown feature"):
        Rule("future_magic", ">", threshold=1.0)
    with pytest.raises(ValueError, match="exactly one"):
        Rule("c", ">", threshold=1.0, other_feature="o")
    with pytest.raises(ValueError, match="direction"):
        _spec(direction="UP")


def test_example_spec_generates_sorted_candidates_and_counts_invalid_stops() -> None:
    features = _features()
    got = evaluate_spec(features, _spec())
    np.testing.assert_array_equal(got.decision_idx, [2, 3, 5])
    np.testing.assert_array_equal(got.direction, [1, 1, 1])
    np.testing.assert_array_equal(got.stop, [97.0, 97.0, 97.0])
    np.testing.assert_array_equal(got.target_r, [2.2, 2.2, 2.2])
    assert evaluate_spec.invalid_stop_count == 0

    invalid = _spec(stop=StopSpec("session_level", level="session_high", offset=1.0))
    invalid_features = FeatureSet(
        {**features, "session_high": np.full(len(features["c"]), 101.0)}, features.metadata
    )
    assert len(evaluate_spec(invalid_features, invalid).decision_idx) == 0
    assert evaluate_spec.invalid_stop_count == 3


def test_cross_rule_and_or_group_are_causal_and_prefix_invariant() -> None:
    features = _features()
    spec = _spec(
        entry_rules=(Rule("m5_normalized_return", "crosses_above", threshold=0.2),),
        or_groups=(
            (Rule("h1_adx14", ">", threshold=99.0), Rule("h1_ema_slope", ">", threshold=0.0)),
        ),
        context_filters=(),
    )
    full = evaluate_spec(features, spec)
    cutoff = 4
    prefix = FeatureSet(
        {name: values[:cutoff] for name, values in features.items()}, features.metadata
    )
    short = evaluate_spec(prefix, spec)
    np.testing.assert_array_equal(short.decision_idx, full.decision_idx[full.decision_idx < cutoff])


def test_spec_and_kernel_provider_adapters() -> None:
    features = _features()
    provided = list(SpecProvider((_spec(),)).candidates(features))
    assert provided[0][:3] == ("DECLARATIVE_PULLBACK", "1", _spec().spec_hash())

    class Params:
        value = 1

    family = type(
        "Family",
        (),
        {
            "strategy_id": "KERNEL",
            "variants": (Params(),),
            "generate": staticmethod(lambda _features, _params: provided[0][3]),
        },
    )()
    kernel = list(
        KernelFamilyProvider({"KERNEL": family}, versions={"KERNEL": "7"}).candidates(features)
    )
    assert kernel[0][0:2] == ("KERNEL", "7")
    assert kernel[0][3] is provided[0][3]


def test_provider_path_makes_zero_mt5_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    class PoisonMt5(ModuleType):
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"unexpected MT5 access: {name}")

    monkeypatch.setitem(sys.modules, "MetaTrader5", PoisonMt5("MetaTrader5"))
    assert list(SpecProvider((_spec(),)).candidates(_features()))


def test_to_signal_candidates_maps_feature_labels_and_close_timestamp() -> None:
    features = _features()
    arrays = CandidateArrays(
        np.array([2]),
        np.array([1]),
        np.array([97.0]),
        np.array([np.nan]),
        np.array([2.2]),
        np.array([EXIT_FIXED_R]),
    )
    got = to_signal_candidates(features, arrays, "S", "1", "fp", ExitSpec("fixed_r", 2.2))
    item = got[0]
    assert item.signal_ts.value == int(features["ts_ns"][2]) + 300_000_000_000
    assert item.signal_price == 100.0
    assert item.h1_regime == {
        "DIRECTION": "UP",
        "TREND_STRENGTH": "TRENDING",
        "VOLATILITY": "NORMAL",
        "VOL_STATE": "COMPRESSION",
    }
    assert item.m15_context["PULLBACK"] is True
    assert item.session_phase == "EUROPEAN_OPEN"
    assert item.setup_metadata == {}


@requires_dataset
def test_conversion_matches_reference_fields_for_golden_variants() -> None:
    _, _, features, golden = golden_setup()
    from alpha.fast.registry import discover

    families = discover()
    checked = 0
    for strategy_id in sorted(families)[:3]:
        family = families[strategy_id]
        arrays = family.generate(features, family.variants[0])
        reference = golden[f"{strategy_id}#0"]
        if not reference:
            continue
        got = to_signal_candidates(
            features,
            arrays,
            strategy_id,
            reference[0].strategy_version,
            reference[0].param_fingerprint,
            reference[0].exit_spec,
        )
        assert [
            (
                item.signal_ts,
                item.direction,
                item.signal_price,
                item.stop,
                item.h1_regime,
                item.m15_context,
                item.session_phase,
            )
            for item in got
        ] == [
            (
                item.signal_ts,
                item.direction,
                item.signal_price,
                item.stop,
                item.h1_regime,
                item.m15_context,
                item.session_phase,
            )
            for item in reference
        ]
        checked += 1
    assert checked >= 2
def test_rule_mask_cache_matches_uncached_for_every_operator_and_nan() -> None:
    features = _features()
    features["m5_normalized_return"] = np.array([np.nan, -1.0, 0.0, 1.0, 2.0, np.nan])
    features["h1_ema_slope"] = np.array([0.0, -1.0, np.nan, 0.5, 2.0, 3.0])
    cache = RuleMaskCache(features, max_entries=20)
    for op in (">", ">=", "<", "<=", "==", "!=", "crosses_above", "crosses_below"):
        rule = Rule("m5_normalized_return", op, other_feature="h1_ema_slope")
        np.testing.assert_array_equal(cache.get(rule), _rule_mask(features, rule))
        np.testing.assert_array_equal(cache.get(rule), _rule_mask(features, rule))
    assert cache.hits == 8
