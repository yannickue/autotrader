"""Truncation-invariance: nothing computed at bar i may change when later bars are removed."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.common.frame import Frame
from alpha.common.regime import regime_labels
from alpha.common.sim import COST_SCENARIOS, ExitSpec, simulate
from alpha.registry import FAMILIES
from tests.unit.alpha.helpers import synthetic_dataframe

CUTS = (2300, 5100, 7777, 11000)


@pytest.fixture(scope="module")
def frame() -> Frame:
    return Frame.from_dataframe(synthetic_dataframe(days=60))


def _eq(a: np.ndarray, b: np.ndarray) -> bool:
    if a.dtype == object:
        return bool((a == b).all())
    return bool(np.allclose(a, b, equal_nan=True))


@pytest.mark.parametrize("cut", CUTS)
def test_features_and_regimes_do_not_use_future_bars(frame: Frame, cut: int) -> None:
    head = frame.head(cut)
    for name, full_arr, head_arr in (
        ("ema21", frame.ema(21)[:cut], head.ema(21)),
        ("ema200", frame.ema(200)[:cut], head.ema(200)),
        ("atr14", frame.atr(14)[:cut], head.atr(14)),
        ("er48", frame.efficiency_ratio(48)[:cut], head.efficiency_ratio(48)),
        ("lag12", frame.lagged(frame.c, 12)[:cut], head.lagged(head.c, 12)),
    ):
        assert _eq(full_arr, head_arr), name
    rf, rh = regime_labels(frame), regime_labels(head)
    for key in ("trend", "vol"):
        assert _eq(rf[key][:cut], rh[key]), key
    # previous-day levels for bars of the *current* day never see this day's later bars
    pf = frame.prior_day_levels()
    ph = head.prior_day_levels()
    for a, b in zip(pf, ph, strict=True):
        assert _eq(a[:cut], b)


@pytest.mark.parametrize("family", list(FAMILIES))
@pytest.mark.parametrize("cut", CUTS)
def test_signals_are_truncation_invariant(frame: Frame, family: str, cut: int) -> None:
    mod = FAMILIES[family]
    head = frame.head(cut)
    for params in mod.GRID:
        full = mod.signals(frame, params)
        part = mod.signals(head, params)
        assert (full.side[:cut] == part.side).all(), (family, params)
        both = full.side[:cut] != 0
        assert _eq(full.stop[:cut][both], part.stop[both]), (family, params)


@pytest.mark.parametrize("family", list(FAMILIES))
def test_simulated_trades_are_truncation_invariant(frame: Frame, family: str) -> None:
    mod = FAMILIES[family]
    params = mod.GRID[0]
    cut = 9000
    head = frame.head(cut)
    ex = ExitSpec("fixed_r", 1.5)
    full, _ = simulate(frame, mod.signals(frame, params), ex, COST_SCENARIOS["BASE"])
    part, _ = simulate(head, mod.signals(head, params), ex, COST_SCENARIOS["BASE"])
    part = part[part["exit_idx"] < cut - 1]  # drop the cut-forced DAY_END/last-bar exits
    expect = full[full["exit_idx"] < cut - 1]
    cols = ["entry_idx", "exit_idx", "side", "entry_price", "exit_price", "qty", "exit_reason"]
    assert len(expect) > 0
    assert expect[cols].reset_index(drop=True).equals(part[cols].reset_index(drop=True))


def test_no_trade_is_entered_on_the_decision_bar(frame: Frame) -> None:
    for fam, mod in FAMILIES.items():
        tr, _ = simulate(
            frame, mod.signals(frame, mod.GRID[0]), ExitSpec("fixed_r", 1.0), COST_SCENARIOS["BASE"]
        )
        assert len(tr) > 0, fam
        assert (tr["entry_idx"] == tr["decision_idx"] + 1).all(), fam
