from dataclasses import FrozenInstanceError

import pytest

from alpha.signals.candidate import assert_truncation_invariant, generate_candidates
from alpha.strategies.breakout_retest import (
    VARIANTS as RETEST_VARIANTS,
)
from alpha.strategies.breakout_retest import BreakoutRetestParams, BreakoutRetestStrategy
from alpha.strategies.compression_expansion import (
    VARIANTS as COMPRESSION_VARIANTS,
)
from alpha.strategies.compression_expansion import (
    CompressionExpansionParams,
    CompressionExpansionStrategy,
)
from alpha.strategies.failed_breakout import (
    VARIANTS as FAILED_VARIANTS,
)
from alpha.strategies.failed_breakout import FailedBreakoutParams, FailedBreakoutStrategy
from tests._alpha_group_c import bars, disable_labels, force_labels


# ---- Opening-range breakout + retest ---------------------------------------------------------
def retest_configured(frame, strength="TRENDING"):
    strategy = BreakoutRetestStrategy(frame)
    force_labels(strategy, strength=strength, direction="UP", context="PULLBACK")
    return strategy


def retest_scenario():
    frame = bars()  # 08:00 Berlin start; 09:00-09:25 flat opening range 99..101
    frame.loc[20, ["open", "high", "low", "close"]] = [100.5, 102.5, 100.5, 102.0]  # breakout
    frame.loc[22, ["open", "high", "low", "close"]] = [101.2, 101.8, 100.9, 101.6]  # retest
    return frame


def test_breakout_retest_fires_long_only_after_close_breakout_and_retest() -> None:
    frame = retest_scenario()
    items = generate_candidates(retest_configured(frame), frame)
    assert len(items) == 1
    item = items[0]
    assert item.direction == 1 and item.stop < 101.0 < item.signal_price
    assert item.signal_ts == frame.loc[22, "ts"] + (frame.loc[1, "ts"] - frame.loc[0, "ts"])
    assert item.setup_metadata["retested_level"] == 101.0


def test_breakout_retest_no_retest_no_trade_and_regime_gated() -> None:
    frame = retest_scenario()
    frame.loc[22, ["open", "high", "low", "close"]] = [102.0, 103.0, 101.9, 102.5]  # never retests
    assert generate_candidates(retest_configured(frame), frame) == []
    scenario = retest_scenario()
    assert generate_candidates(retest_configured(scenario, "RANGE_LIKE"), scenario) == []


def test_breakout_retest_deterministic_reset_and_truncation_invariant() -> None:
    frame = retest_scenario()
    strategy = retest_configured(frame)
    assert generate_candidates(strategy, frame) == generate_candidates(strategy, frame)
    assert_truncation_invariant(retest_configured, frame, cutoff=22)


# ---- Compression -> expansion ----------------------------------------------------------------
def compression_configured(frame):
    strategy = CompressionExpansionStrategy(frame)
    force_labels(strategy, strength="WEAK", context="COMPRESSION")
    strategy._regime.loc[:, "VOL_STATE"] = "COMPRESSION"
    return strategy


def compression_scenario():
    frame = bars()
    frame.loc[100, ["open", "high", "low", "close"]] = [100.0, 105.0, 100.0, 104.5]
    return frame


def test_compression_expansion_fires_long_with_stop_below_box() -> None:
    frame = compression_scenario()
    items = generate_candidates(compression_configured(frame), frame)
    assert len(items) == 1
    item = items[0]
    assert item.direction == 1 and item.stop < 99.0
    assert item.setup_metadata == {"box_high": 101.0, "box_low": 99.0}


def test_compression_expansion_needs_compression_context_and_is_causal() -> None:
    frame = compression_scenario()
    gated = compression_configured(frame)
    disable_labels(gated)
    assert generate_candidates(gated, frame) == []
    strategy = compression_configured(frame)
    assert generate_candidates(strategy, frame) == generate_candidates(strategy, frame)
    assert_truncation_invariant(compression_configured, frame, cutoff=100)


# ---- Failed breakout -------------------------------------------------------------------------
def failed_configured(frame):
    strategy = FailedBreakoutStrategy(frame)
    force_labels(strategy, strength="RANGE_LIKE", context="REVERSAL_CONTEXT")
    return strategy


def failed_scenario():
    frame = bars()  # day 2 (index 300 ~ 09:00 Berlin) breaks day-1 high 101 by close, then fails
    frame.loc[300, ["open", "high", "low", "close"]] = [101.0, 102.2, 100.5, 102.0]
    return frame


def test_failed_breakout_fades_after_close_back_inside() -> None:
    frame = failed_scenario()
    items = generate_candidates(failed_configured(frame), frame)
    assert len(items) == 1
    item = items[0]
    assert item.direction == -1 and item.stop > 102.2
    assert item.setup_metadata["failed_level"] == 101.0


def test_failed_breakout_regime_gated_deterministic_and_truncation_invariant() -> None:
    frame = failed_scenario()
    gated = failed_configured(frame)
    disable_labels(gated)
    assert generate_candidates(gated, frame) == []
    strategy = failed_configured(frame)
    assert generate_candidates(strategy, frame) == generate_candidates(strategy, frame)
    assert_truncation_invariant(failed_configured, frame, cutoff=301)


def test_group_b_params_frozen_and_variants_bounded() -> None:
    for variants, cls in (
        (RETEST_VARIANTS, BreakoutRetestParams),
        (COMPRESSION_VARIANTS, CompressionExpansionParams),
        (FAILED_VARIANTS, FailedBreakoutParams),
    ):
        assert len(variants) <= 3
        with pytest.raises(FrozenInstanceError):
            cls().target_r = 3.0
