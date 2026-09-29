from __future__ import annotations

import pandas as pd

from alpha.context import CONTEXT_LABELS
from alpha.regime import REGIME_DIMENSIONS


def bars() -> pd.DataFrame:
    stamps = pd.date_range("2026-02-02 08:00", periods=400, freq="5min", tz="Europe/Berlin")
    return pd.DataFrame(
        {"ts": stamps, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "spread_pts": 1.0}
    )


def force_labels(strategy, *, strength: str, direction: str = "NEUTRAL", context: str) -> None:
    strategy._regime.loc[:, list(REGIME_DIMENSIONS)] = [direction, strength, "NORMAL", "EXPANSION"]
    strategy._context.loc[:, list(CONTEXT_LABELS)] = False
    strategy._context.loc[:, context] = True


def disable_labels(strategy) -> None:
    strategy._regime.loc[:, "TREND_STRENGTH"] = "TRENDING"
    strategy._context.loc[:, list(CONTEXT_LABELS)] = False
