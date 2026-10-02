# ruff: noqa: E501
"""Small frozen labels of the MarketMap (balance / volatility / participation) on constructed bars."""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from research_workbench.thesis import marketmap as MM
from tests.unit.research_workbench.thesis._obs_bars import bars_from_closes, mirror_bars


def _closes(kind: str, n: int = 120) -> np.ndarray:
    k = np.arange(n, dtype=float)
    if kind == "balance":
        return 100.0 + 0.6 * np.sin(2 * np.pi * k / 7.0)
    if kind == "trend":
        return 100.0 + 0.3 * k
    if kind == "mixed":
        return 100.0 + 0.05 * k + 1.5 * np.sin(2 * np.pi * k / 40.0)
    raise ValueError(kind)


def test_balance_label_balance_directional_mixed() -> None:
    assert MM.balance_label(bars_from_closes(_closes("balance"), wick=0.6), 119) == "BALANCE"
    assert MM.balance_label(bars_from_closes(_closes("trend")), 119) == "DIRECTIONAL"
    assert MM.balance_label(bars_from_closes(_closes("mixed")), 119) == "MIXED"
    assert (
        MM.balance_label(bars_from_closes(_closes("balance")), 30) is None
    )  # window 48 not loaded


def test_balance_label_is_mirror_invariant() -> None:
    for kind in ("balance", "trend", "mixed"):
        b = bars_from_closes(_closes(kind))
        assert MM.balance_label(b, 119) == MM.balance_label(mirror_bars(b), 119)


def test_volatility_label_thresholds_and_warmup() -> None:
    b = bars_from_closes(_closes("balance", 200))
    base = np.full(200, 1.0)

    def with_last(v: float) -> object:
        atr = base.copy()
        atr[-1] = v
        return replace(b, atr=atr)

    assert MM.volatility_label(with_last(2.0), 199) == "HIGH"
    assert MM.volatility_label(with_last(0.5), 199) == "LOW"
    assert MM.volatility_label(with_last(1.0), 199) == "NORMAL"
    assert MM.volatility_label(with_last(1.0), 50) is None  # < 96 bars
    nan_atr = base.copy()
    nan_atr[150] = np.nan
    assert MM.volatility_label(replace(b, atr=nan_atr), 199) is None


def test_participation_label_needs_21_previous_days_then_ranks_the_current_bar() -> None:
    n = 22 * 40
    b = bars_from_closes(_closes("balance", n), bars_per_day=40, seed=5)
    last = n - 1
    assert MM.participation_label(b, 20 * 40 + 5) is None  # only 20 previous days
    tv = b.tick_volume.copy()
    tv[last] = 10_000.0
    assert MM.participation_label(replace(b, tick_volume=tv), last) == "HIGH"
    tv[last] = 1.0
    assert MM.participation_label(replace(b, tick_volume=tv), last) == "LOW"
    tv[last] = float(np.median(b.tick_volume))
    assert MM.participation_label(replace(b, tick_volume=tv), last) == "NORMAL"


def test_participation_label_is_prefix_invariant_and_a_proxy_of_ticks_only() -> None:
    n = 22 * 40
    b = bars_from_closes(_closes("balance", n), bars_per_day=40, seed=5)
    i = n - 3
    assert MM.participation_label(b, i) == MM.participation_label(b.prefix(i + 1), i)
    # prices play no role: mirrored prices, same ticks -> same label
    assert MM.participation_label(b, i) == MM.participation_label(mirror_bars(b), i)
