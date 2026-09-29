from dataclasses import FrozenInstanceError

import pytest

from alpha.signals.candidate import assert_truncation_invariant, generate_candidates
from alpha.strategies.vwap_reference import (
    VARIANTS,
    SessionTwapReferenceParams,
    SessionTwapReferenceStrategy,
)
from tests._alpha_group_c import bars, disable_labels, force_labels


def configured(frame):
    strategy = SessionTwapReferenceStrategy(frame)
    force_labels(strategy, strength="TRENDING", direction="UP", context="PULLBACK")
    return strategy


def scenario():
    frame = bars()
    frame.loc[299, ["open", "high", "low", "close"]] = [100.0, 100.0, 98.0, 99.0]
    frame.loc[300, ["open", "high", "low", "close"]] = [99.0, 102.0, 98.5, 101.0]
    return frame


def test_session_twap_reclaim_fires_and_documents_equal_time_weighting() -> None:
    frame = scenario()
    items = generate_candidates(configured(frame), frame)
    item = items[-1]
    assert item.direction == 1 and item.stop < item.signal_price
    assert item.setup_metadata["weighting_source"] == "EQUAL_TIME_M5_TYPICAL_PRICE"
    assert "session_twap_reference" in item.setup_metadata


def test_session_twap_is_deterministic_reset_causal_and_regime_gated() -> None:
    frame = scenario()
    strategy = configured(frame)
    assert generate_candidates(strategy, frame) == generate_candidates(strategy, frame)
    assert_truncation_invariant(configured, frame, cutoff=300)
    gated = configured(frame)
    disable_labels(gated)
    assert generate_candidates(gated, frame) == []


def test_session_twap_params_are_frozen_and_variants_are_bounded() -> None:
    assert len(VARIANTS) <= 3
    with pytest.raises(FrozenInstanceError):
        SessionTwapReferenceParams().target_r = 3.0
