from __future__ import annotations

import sys

import pandas as pd

from alpha.signals.candidate import assert_truncation_invariant, generate_candidates
from alpha.strategies.momentum.strategy import MomentumContinuationStrategy


def _bars() -> pd.DataFrame:
    close = [100.0] * 14 + [101.0, 101.1, 101.0, 101.05, 101.1, 102.0]
    return pd.DataFrame(
        {
            "ts": pd.date_range(
                "2026-02-03 09:00", periods=len(close), freq="5min", tz="Europe/Berlin"
            ),
            "open": [value - 0.1 for value in close],
            "high": [value + 0.15 for value in close],
            "low": [value - 0.15 for value in close],
            "close": close,
            "spread_pts": [0.0] * len(close),
        }
    )


def _eligible(strategy: MomentumContinuationStrategy) -> None:
    strategy._regime.loc[:, ["DIRECTION", "TREND_STRENGTH"]] = ["UP", "TRENDING"]
    strategy._context.loc[:, "MOMENTUM_CONTINUATION"] = True


def test_momentum_is_deterministic_resets_and_has_no_mt5_dependency() -> None:
    frame = _bars()
    strategy = MomentumContinuationStrategy(frame)
    _eligible(strategy)
    first = generate_candidates(strategy, frame)
    assert first == generate_candidates(strategy, frame)
    assert first
    assert not any(name == "MetaTrader5" or name.startswith("MetaTrader5.") for name in sys.modules)


def test_momentum_is_truncation_invariant() -> None:
    frame = _bars()

    def factory(data: pd.DataFrame) -> MomentumContinuationStrategy:
        strategy = MomentumContinuationStrategy(data)
        _eligible(strategy)
        return strategy

    assert_truncation_invariant(factory, frame, cutoff=18)


def test_momentum_regime_gate_blocks_range_market() -> None:
    frame = _bars()
    strategy = MomentumContinuationStrategy(frame)
    strategy._regime.loc[:, ["DIRECTION", "TREND_STRENGTH"]] = ["UP", "RANGE_LIKE"]
    strategy._context.loc[:, "MOMENTUM_CONTINUATION"] = True
    assert generate_candidates(strategy, frame) == []


def test_momentum_fires_after_brief_consolidation_with_structural_stop() -> None:
    frame = _bars()
    strategy = MomentumContinuationStrategy(frame)
    _eligible(strategy)
    item = generate_candidates(strategy, frame)[0]
    assert item.direction == 1
    assert item.stop < min(frame.loc[15:18, "low"])
    assert item.setup_metadata["pattern"] == "brief_consolidation_break"
