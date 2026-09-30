# ruff: noqa: E501
"""V2 per-market sim window: `simulate_fast(..., window=SimWindow)` flows the MarketSpec calendar
(local-minute basis of `Frame.minute`), the default stays bit-identical to V1 GER40."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from alpha.common.frame import Frame, params_from_spec
from alpha.common.sim import COST_SCENARIOS, DEFAULT_RULES, DEFAULT_SIZING, SizingSpec
from alpha.fast.sim import (
    EXIT_FIXED_R,
    GER40_WINDOW,
    REASON_SESSION_END,
    SKIP_LABELS,
    CandidateArrays,
    MarketArrays,
    SimWindow,
    simulate_fast,
    simulate_many,
)
from markets.spec import load_market_spec
from tests._v2mm_helpers import make_frame

SIZING = SizingSpec(equity_eur=1e6, risk_fraction=0.0005, lot_step=0.1, min_lot=0.1,
                    max_leverage=30.0, min_risk_pts=1.0, max_risk_pts=50.0, contract_size=1.0)
COST = COST_SCENARIOS["BASE"]

# sha256 of (as_matrix, skip_counts, exit_reason) of the REAL GER40 stream below, computed with the
# pre-change simulate_fast (hard-coded constants) at c0814d6 (before the SimWindow parameter existed).
PINNED_REAL_GER40 = {
    "BASE": "15524750cab2149e915548ba4cb7e06340c40c04f03490330e40b111af91816b",
    "COMBINED_ADVERSE": "ba1cb0c3ec5fd1e688489f5916379679f6a7b72df0082d6458821931c53a4482",
}


def _digest(t) -> str:
    h = hashlib.sha256()
    # skip_counts[:10]: the label appended after the pin (space_below_min_at_fill) must be 0 and is not hashed
    assert t.skip_counts[10:].sum() == 0
    for x in (t.as_matrix(), t.skip_counts[:10], t.exit_reason):
        h.update(x.tobytes())
    return h.hexdigest()


def _every_bar_candidates(fr: Frame, stride: int = 1) -> CandidateArrays:
    idx = np.arange(10, len(fr) - 2, stride, dtype=np.int64)
    d = np.ones(len(idx), dtype=np.int8)
    return CandidateArrays(idx, d, fr.c[idx] - 5.0, np.full(len(idx), np.nan),
                           np.full(len(idx), 1000.0), np.zeros(len(idx), dtype=np.int8))


# ------------------------------------------------------------------ window value object
def test_simwindow_defaults_are_the_v1_ger40_constants():
    assert (GER40_WINDOW.entry_start_min, GER40_WINDOW.entry_end_min, GER40_WINDOW.flat_min) == (
        9 * 60, 20 * 60, 21 * 60 + 30)
    assert SimWindow.from_spec(load_market_spec("GER40")) == GER40_WINDOW
    assert SimWindow.from_params(params_from_spec(load_market_spec("GER40"))) == GER40_WINDOW


@pytest.mark.parametrize("bad", [(600, 600, 700), (700, 600, 800), (0, 900, 800), (0, 900, 1441), (-1, 5, 6)])
def test_simwindow_rejects_inconsistent_windows(bad):
    with pytest.raises(ValueError):
        SimWindow(*bad)


def test_simwindow_from_spec_matches_calendar_for_every_market():
    for name in ("NAS100", "SPX500", "XAUUSD", "EURUSD"):
        spec = load_market_spec(name)
        w = SimWindow.from_spec(spec)
        assert (w.entry_start_min, w.entry_end_min, w.flat_min) == (
            spec.calendar.entry_start_min, spec.calendar.entry_end_min, spec.calendar.forced_flat_min)


# ------------------------------------------------------------------ NYC-like market, DST safe
@pytest.fixture(scope="module")
def nyc():
    """24h weekday M5 bars 2025-03-03..2025-03-21 UTC: crosses the US DST change (Mar 9) while the
    EU clock is still CET until Mar 30 (mismatch weeks). Local (America/New_York) minutes via the
    real market frame builder."""
    spec = load_market_spec("NAS100")
    df = make_frame("2025-03-03", "2025-03-22", seed=5, spread_pts=75.0, price=100.0)
    fr = Frame.from_dataframe(df, params_from_spec(spec))
    return spec, fr


def test_nyc_entries_only_inside_local_cash_window_and_flat_at_local_time(nyc):
    spec, fr = nyc
    cal = spec.calendar
    assert (cal.entry_start_min, cal.entry_end_min, cal.forced_flat_min) == (570, 900, 955)
    t = simulate_fast(MarketArrays.from_frame(fr), _every_bar_candidates(fr), COST, SIZING,
                      DEFAULT_RULES, SimWindow.from_spec(spec))
    assert len(t) >= 14  # one position per local day (flat 15:55, window closes 15:00)
    em = fr.minute[t.entry_idx]
    assert em.min() >= 570 and em.max() < 900
    sess = t.exit_reason == REASON_SESSION_END
    assert sess.sum() >= 10
    xi = t.exit_idx[sess]
    assert (fr.minute[xi] >= 955).all()
    assert (fr.minute[xi - 1] < 955).all()  # flat at the FIRST bar at/after 15:55 local
    assert t.skips["outside_window"] > 0  # counted, not silently dropped
    assert t.skips["outside_window"] == t.skip_counts[SKIP_LABELS.index("outside_window")]


def test_nyc_default_window_is_the_berlin_constants_and_is_wrong_for_the_market(nyc):
    """Regression for the defect: without a window the GER40 09:00-20:00 / 21:30 rule is applied."""
    _spec, fr = nyc
    t = simulate_fast(MarketArrays.from_frame(fr), _every_bar_candidates(fr), COST, SIZING, DEFAULT_RULES)
    em = fr.minute[t.entry_idx]
    assert em.min() >= 540 and em.max() < 1200
    assert (em >= 900).any()  # entries after the NAS100 entry window closed


def test_nyc_dst_week_first_entry_is_local_0930_in_both_offsets(nyc):
    spec, fr = nyc
    t = simulate_fast(MarketArrays.from_frame(fr), _every_bar_candidates(fr), COST, SIZING,
                      DEFAULT_RULES, SimWindow.from_spec(spec))
    ts = fr.ts[t.entry_idx]
    days = np.asarray(fr.day[t.entry_idx])
    seen_utc_hours = set()
    for d in np.unique(days):
        first = np.flatnonzero(days == d)[0]
        assert fr.minute[t.entry_idx[first]] == 570, str(ts[first])
        seen_utc_hours.add((ts[first].hour, ts[first].minute))
    assert seen_utc_hours == {(13, 30), (14, 30)}  # EDT and EST both present, both = 09:30 New York


def test_simulate_many_passes_the_window(nyc):
    spec, fr = nyc
    m, c = MarketArrays.from_frame(fr), _every_bar_candidates(fr, 3)
    w = SimWindow.from_spec(spec)
    a = simulate_many(m, [c], COST, SIZING, DEFAULT_RULES, w)[0]
    b = simulate_fast(m, c, COST, SIZING, DEFAULT_RULES, w)
    assert np.array_equal(a.as_matrix(), b.as_matrix())


# ------------------------------------------------------------------ GER40 bit-identity
def test_ger40_default_equals_explicit_ger40_window_synthetic():
    from tests.unit.alpha.helpers import synthetic_dataframe

    fr = Frame.from_dataframe(synthetic_dataframe(days=30))
    m, c = MarketArrays.from_frame(fr), _every_bar_candidates(fr, 2)
    sz = SizingSpec(min_risk_pts=0.5, max_risk_pts=100.0)
    a = simulate_fast(m, c, COST, sz)
    for w in (GER40_WINDOW, SimWindow.from_spec(load_market_spec("GER40")), SimWindow()):
        b = simulate_fast(m, c, COST, sz, DEFAULT_RULES, w)
        assert _digest(a) == _digest(b)
    assert len(a) > 0


@pytest.mark.skipif(not Path("data/ar1_ger40").exists(), reason="research dataset not present")
def test_ger40_real_bars_trade_arrays_identical_to_pre_change_pin():
    from alpha.common.dataset import load_research_dataset

    ds = load_research_dataset("data/ar1_ger40")
    fr = Frame.from_dataframe(ds.data if hasattr(ds, "data") else ds.frame)
    m = MarketArrays.from_frame(fr)
    atr = fr.atr(14)
    rng = np.random.default_rng(7)
    idx = np.flatnonzero(np.isfinite(atr))[::5]
    idx = idx[idx < len(fr) - 2]
    d = np.where(rng.random(len(idx)) < 0.5, 1, -1).astype(np.int8)
    dist = atr[idx] * 1.5
    stop = fr.c[idx] - d * dist
    tgt = np.full(len(idx), np.nan)
    sel = rng.random(len(idx)) < 0.3
    tgt[sel] = fr.c[idx][sel] + d[sel] * dist[sel] * 2
    cands = CandidateArrays(idx, d, stop, tgt, np.full(len(idx), 1.5), np.full(len(idx), EXIT_FIXED_R))
    for name, pin in PINNED_REAL_GER40.items():
        default = simulate_fast(m, cands, COST_SCENARIOS[name], DEFAULT_SIZING, DEFAULT_RULES)
        explicit = simulate_fast(m, cands, COST_SCENARIOS[name], DEFAULT_SIZING, DEFAULT_RULES,
                                 SimWindow.from_spec(load_market_spec("GER40")))
        assert _digest(default) == pin == _digest(explicit), name
        assert len(default) > 1000 and default.skips["outside_window"] > 0
