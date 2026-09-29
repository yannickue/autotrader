from dataclasses import FrozenInstanceError

import pytest

from alpha.signals.candidate import assert_truncation_invariant, generate_candidates
from alpha.strategies.prev_day_levels import (
    VARIANTS,
    PreviousDayLevelsParams,
    PreviousDayLevelsStrategy,
)
from tests._alpha_group_c import bars, disable_labels, force_labels


def configured(frame):
    strategy = PreviousDayLevelsStrategy(frame)
    force_labels(strategy, strength="WEAK", context="REVERSAL_CONTEXT")
    return strategy


def scenario():
    frame = bars()
    frame.loc[300, ["open", "high", "low", "close"]] = [101.5, 102.0, 100.0, 100.5]
    return frame


def test_previous_day_high_rejection_fires_short_with_stop_above_level() -> None:
    frame = scenario()
    items = generate_candidates(configured(frame), frame)
    item = items[-1]
    assert item.direction == -1
    assert item.setup_metadata == {"level_name": "PDH", "level": 101.0}
    assert item.stop > 101.0


def test_previous_day_levels_are_deterministic_reset_causal_and_regime_gated() -> None:
    frame = scenario()
    strategy = configured(frame)
    assert generate_candidates(strategy, frame) == generate_candidates(strategy, frame)
    assert_truncation_invariant(configured, frame, cutoff=300)
    gated = configured(frame)
    disable_labels(gated)
    assert generate_candidates(gated, frame) == []


def test_previous_day_params_are_frozen_and_variants_are_bounded() -> None:
    assert len(VARIANTS) <= 3
    with pytest.raises(FrozenInstanceError):
        PreviousDayLevelsParams().target_r = 3.0
