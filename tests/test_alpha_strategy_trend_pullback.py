from __future__ import annotations

import sys

import pandas as pd

from alpha.signals.candidate import assert_truncation_invariant, generate_candidates
from alpha.strategies.trend_pullback.strategy import TrendPullbackStrategy


def _bars() -> pd.DataFrame:
    close = [100.0] * 14 + [101.0, 100.5, 100.0, 100.5, 102.0, 103.0]
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


def _eligible(strategy: TrendPullbackStrategy, *, direction: str = "UP") -> None:
    strategy._regime.loc[:, ["DIRECTION", "TREND_STRENGTH"]] = [direction, "TRENDING"]
    strategy._context.loc[:, "PULLBACK"] = True


def test_trend_pullback_is_deterministic_resets_and_has_no_mt5_dependency() -> None:
    frame = _bars()
    strategy = TrendPullbackStrategy(frame)
    _eligible(strategy)
    first = generate_candidates(strategy, frame)
    second = generate_candidates(strategy, frame)
    assert first == second
    assert first
    assert not any(name == "MetaTrader5" or name.startswith("MetaTrader5.") for name in sys.modules)


def test_trend_pullback_is_truncation_invariant() -> None:
    frame = _bars()

    def factory(data: pd.DataFrame) -> TrendPullbackStrategy:
        strategy = TrendPullbackStrategy(data)
        _eligible(strategy)
        return strategy

    assert_truncation_invariant(factory, frame, cutoff=18)


def test_trend_pullback_regime_gate_blocks_non_trending_market() -> None:
    frame = _bars()
    strategy = TrendPullbackStrategy(frame)
    strategy._regime.loc[:, ["DIRECTION", "TREND_STRENGTH"]] = ["NEUTRAL", "RANGE_LIKE"]
    strategy._context.loc[:, "PULLBACK"] = True
    assert generate_candidates(strategy, frame) == []


def test_trend_pullback_fires_long_with_structural_stop() -> None:
    frame = _bars()
    strategy = TrendPullbackStrategy(frame)
    _eligible(strategy)
    item = generate_candidates(strategy, frame)[0]
    assert item.direction == 1
    assert item.stop < min(frame.loc[14:18, "low"])
    assert item.exit_spec.kind == "fixed_r"
    assert item.entry_intent == "NEXT_BAR_OPEN_MARKET"
