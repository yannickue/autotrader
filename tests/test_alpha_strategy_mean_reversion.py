from dataclasses import FrozenInstanceError

import pytest

from alpha.signals.candidate import assert_truncation_invariant, generate_candidates
from alpha.strategies.mean_reversion import (
    VARIANTS,
    RangeMeanReversionParams,
    RangeMeanReversionStrategy,
)
from tests._alpha_group_c import bars, disable_labels, force_labels


def configured(frame):
    strategy = RangeMeanReversionStrategy(frame)
    force_labels(strategy, strength="RANGE_LIKE", context="RANGE_EXTREME")
    strategy._context.loc[:, "range_low"] = 90.0
    strategy._context.loc[:, "range_high"] = 110.0
    return strategy


def scenario():
    frame = bars()
    frame.loc[300, ["open", "high", "low", "close"]] = [90.0, 92.0, 89.0, 91.5]
    return frame


def test_mean_reversion_fires_long_with_structural_stop() -> None:
    frame = scenario()
    items = generate_candidates(configured(frame), frame)
    item = next(
        item
        for item in items
        if item.signal_ts == frame.loc[300, "ts"] + __import__("pandas").Timedelta("5min")
    )
    assert item.direction == 1
    assert item.stop == 88.0
    assert item.setup_metadata["range_mid"] == 100.0


def test_mean_reversion_is_deterministic_reset_causal_and_regime_gated() -> None:
    frame = scenario()
    strategy = configured(frame)
    assert generate_candidates(strategy, frame) == generate_candidates(strategy, frame)
    assert_truncation_invariant(configured, frame, cutoff=300)
    gated = configured(frame)
    disable_labels(gated)
    assert generate_candidates(gated, frame) == []


def test_mean_reversion_params_are_frozen_and_variants_are_bounded() -> None:
    assert len(VARIANTS) <= 3
    with pytest.raises(FrozenInstanceError):
        RangeMeanReversionParams().target_r = 3.0
