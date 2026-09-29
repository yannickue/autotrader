from __future__ import annotations

import sys

import pandas as pd

from alpha.signals.candidate import assert_truncation_invariant, generate_candidates
from alpha.strategies.opening_drive.strategy import OpeningDriveStrategy


def _bars() -> pd.DataFrame:
    close = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0]
    close += [111.5, 110.5, 110.0, 111.8, 112.2, 112.5]
    return pd.DataFrame(
        {
            "ts": pd.date_range(
                "2026-02-03 09:00", periods=len(close), freq="5min", tz="Europe/Berlin"
            ),
            "open": [value - 0.2 for value in close],
            "high": [value + 0.3 for value in close],
            "low": [value - 0.4 for value in close],
            "close": close,
            "spread_pts": [0.0] * len(close),
        }
    )


def _eligible(strategy: OpeningDriveStrategy) -> None:
    strategy._regime.loc[:, ["DIRECTION", "TREND_STRENGTH"]] = ["UP", "TRENDING"]
    strategy._context.loc[:, ["PULLBACK", "TREND_CONTINUATION"]] = [True, True]


def test_opening_drive_is_deterministic_resets_and_has_no_mt5_dependency() -> None:
    frame = _bars()
    strategy = OpeningDriveStrategy(frame)
    _eligible(strategy)
    first = generate_candidates(strategy, frame)
    assert first == generate_candidates(strategy, frame)
    assert first
    assert not any(name == "MetaTrader5" or name.startswith("MetaTrader5.") for name in sys.modules)


def test_opening_drive_is_truncation_invariant() -> None:
    frame = _bars()

    def factory(data: pd.DataFrame) -> OpeningDriveStrategy:
        strategy = OpeningDriveStrategy(data)
        _eligible(strategy)
        return strategy

    assert_truncation_invariant(factory, frame, cutoff=16)


def test_opening_drive_regime_gate_blocks_wrong_direction() -> None:
    frame = _bars()
    strategy = OpeningDriveStrategy(frame)
    strategy._regime.loc[:, ["DIRECTION", "TREND_STRENGTH"]] = ["DOWN", "TRENDING"]
    strategy._context.loc[:, ["PULLBACK", "TREND_CONTINUATION"]] = [True, True]
    assert generate_candidates(strategy, frame) == []


def test_opening_drive_fires_in_morning_after_controlled_pullback() -> None:
    frame = _bars()
    strategy = OpeningDriveStrategy(frame)
    _eligible(strategy)
    item = generate_candidates(strategy, frame)[0]
    assert item.direction == 1
    assert item.session_phase == "MORNING"
    assert item.stop < min(frame.loc[12:15, "low"])
    assert item.setup_metadata["opening_range_high"] == 111.3
