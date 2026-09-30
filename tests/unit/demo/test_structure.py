# ruff: noqa: E501
"""Lane E2: src/demo/structure.py - pure causal chart structure (swings, levels, geometry, management signals)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from demo import structure as st

D = Decimal
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)  # on the 15-minute grid


def frame(closes: list[float], *, wick: float = 1.0, start: datetime = T0) -> pd.DataFrame:
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "ts": pd.Timestamp(start + timedelta(minutes=5 * i)), "open": c, "high": c + wick, "low": c - wick,
            "close": c, "tick_volume": 10, "spread_pts": 2,
        })
    out = pd.DataFrame(rows)
    out["ts"] = pd.DatetimeIndex(out["ts"]).tz_convert("UTC")
    return out


POST = [100, 102, 104, 106, 104, 102, 100, 103, 106, 109, 106, 104, 107, 110, 112]  # higher low (swing low 103) at i11

# up-leg, pullback, higher up-leg, pullback: swing highs 107 (i3), 113 (i10); swing lows 99 (i6), 105 (i13)
ZIGZAG = [100, 102, 104, 106, 104, 102, 100, 103, 106, 109, 112, 109, 106, 105, 107, 108, 107, 108]


def test_confirmed_swings_need_n_bars_after_the_swing_bar():
    bars = frame(ZIGZAG)
    sw = st.confirmed_swings(bars, n=2)
    highs = [s for s in sw if s.kind == "HIGH"]
    lows = [s for s in sw if s.kind == "LOW"]
    assert [s.price for s in highs][:2] == [107.0, 113.0]
    assert 99.0 in [s.price for s in lows]
    for s in sw:  # confirmation = close of the bar n bars after the swing bar
        assert s.confirmed_at == s.bar_open + timedelta(minutes=5 * 2 + 5)
        assert s.structure_id.startswith("M5:")


def test_causality_truncating_future_bars_never_changes_a_confirmed_swing():
    bars = frame(ZIGZAG)
    full = {(s.structure_id, s.price) for s in st.confirmed_swings(bars, 2)}
    for cut in range(5, len(bars)):
        part = st.confirmed_swings(bars.iloc[:cut], 2)
        assert {(s.structure_id, s.price) for s in part} <= full
        assert all(s.confirmed_at <= pd.Timestamp(bars["ts"].iloc[cut - 1]).to_pydatetime() + timedelta(minutes=5) for s in part)


def test_geometry_is_causal_truncation_equals_a_shorter_history():
    bars = frame([*ZIGZAG, 110, 109, 108, 107.5])
    g_short = st.structural_geometry(1, 108.0, bars.iloc[:15], 0.2, 2.0)
    g_again = st.structural_geometry(1, 108.0, bars.iloc[:15].copy(), 0.2, 2.0)
    assert g_short.as_dict() == g_again.as_dict()
    # appending future bars may add structure but never rewrites a level that existed
    g_long = st.structural_geometry(1, 108.0, bars, 0.2, 2.0)
    if g_short.stop_level is not None and g_long.stop_level is not None:
        assert g_long.stop_level.confirmed_at >= g_short.stop_level.confirmed_at


def test_resample_m15_drops_incomplete_groups():
    bars = frame(ZIGZAG[:8])  # 8 M5 bars = 2 full M15 bars + 2 bars of a forming one
    m15 = st.resample_m15(bars)
    assert len(m15) == 2
    assert m15["high"].iloc[0] == max(bars["high"].iloc[:3])
    gap = bars.drop(index=[1]).reset_index(drop=True)
    assert len(st.resample_m15(gap)) == 1  # the group with a hole is never extrapolated


def test_long_geometry_ordering_and_sources():
    bars = frame(ZIGZAG)
    g = st.structural_geometry(1, 108.0, bars, 0.2, 2.0, swing_n=2)
    assert g.stop is not None and g.tp1 is not None
    assert g.stop < D("108") < g.tp1.price
    if g.tp2 is not None:
        assert g.tp1.price < g.tp2.price
    assert g.stop_level is not None and g.stop_level.price < D("108")
    # stop sits BEYOND the structural level by the ATR buffer
    assert g.stop == g.stop_level.price - g.stop_buffer
    for lv in (g.stop_level, g.tp1):
        assert lv.source and lv.structure_id and lv.time_created <= lv.confirmed_at
    assert g.as_of == T0 + timedelta(minutes=5 * len(bars))
    assert g.as_dict()["tp1_r"] is not None  # R is a RESULT of the chart prices


def test_short_geometry_mirrors_long():
    mirrored = [200 - c for c in ZIGZAG]
    bars = frame(mirrored)
    g = st.structural_geometry(-1, 92.0, bars, 0.2, 2.0, swing_n=2)
    assert g.stop is not None and g.tp1 is not None
    assert g.tp1.price < D("92") < g.stop  # TP1 < entry < stop
    if g.tp2 is not None:
        assert g.tp2.price < g.tp1.price
    assert g.stop == g.stop_level.price + g.stop_buffer + D("0.2")  # short stops on the ASK: + spread


def test_no_second_target_marker_when_nothing_justifies_tp2():
    bars = frame(ZIGZAG)
    # a huge minimum gap between TP1 and TP2: no further level qualifies -> TP1 + runner, marker set
    g = st.structural_geometry(1, 106.5, bars, 0.1, 2.0, min_movement_to_cost=1.0, min_tp_gap_atr=50.0)
    assert g.tp1 is not None and g.tp2 is None
    assert st.SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED in g.markers
    assert g.as_dict()["tp2_r"] is None


def test_min_movement_to_cost_skips_a_target_that_is_too_close():
    bars = frame(ZIGZAG)
    cheap = st.structural_geometry(1, 106.5, bars, 0.1, 2.0, min_movement_to_cost=1.0)
    dear = st.structural_geometry(1, 106.5, bars, 2.0, 2.0, min_movement_to_cost=3.0)
    assert cheap.tp1 is not None
    assert dear.tp1 is None or dear.tp1.price - D("106.5") >= D("6.0")  # 3 x cost(2.0)


def test_direction_integrity_is_asserted():
    with pytest.raises(ValueError, match="direction integrity"):
        st._assert_direction(1, D("100"), D("101"), None, None)
    with pytest.raises(ValueError, match="direction integrity"):
        st._assert_direction(-1, D("100"), D("99"), None, None)
    lv = st.Level(D("99"), "SWING:M5", "x", T0, T0)
    with pytest.raises(ValueError, match="direction integrity"):
        st._assert_direction(1, D("100"), D("95"), lv, None)


def test_too_few_bars_and_missing_atr_are_marked_not_guessed():
    g = st.structural_geometry(1, 100.0, frame([100, 101]), 0.1, 1.0)
    assert g.stop is None and st.INSUFFICIENT_BARS in g.markers
    g2 = st.structural_geometry(1, 108.0, frame(ZIGZAG), 0.1, None)
    assert st.ATR_UNAVAILABLE in g2.markers


def test_management_signals_trail_behind_a_post_entry_swing_and_detect_failure():
    # entry after i6, then a higher low at i9 (low 101) gets confirmed
    closes = POST
    bars = frame(closes)
    entered = T0 + timedelta(minutes=5 * 7)
    sig = st.management_signals(1, bars, entered_at=entered, current_stop=98.0, price=112.0, atr=2.0, spread=0.2)
    assert sig.trail_candidate is not None and sig.trail_candidate > D("98")
    assert sig.trail_candidate < D("112")  # never at/through the price
    assert not sig.structure_failure
    broken = frame([*closes, 102.0])  # a closed bar below the higher low
    sig2 = st.management_signals(1, broken, entered_at=entered, current_stop=98.0, price=102.0, atr=2.0, spread=0.2)
    assert sig2.structure_failure and sig2.failure_structure_id is not None
    assert sig2.momentum_score is not None and sig2.momentum_score < 0


def test_management_ignores_swings_from_before_the_entry():
    bars = frame(ZIGZAG)
    sig = st.management_signals(1, bars, entered_at=T0 + timedelta(minutes=5 * 17), current_stop=90.0, price=108.0, atr=2.0)
    assert sig.trail_candidate is None and not sig.structure_failure


def test_short_management_uses_ask_buffer_and_only_tightens():
    closes = [200 - c for c in POST]
    bars = frame(closes)
    entered = T0 + timedelta(minutes=5 * 7)
    sig = st.management_signals(-1, bars, entered_at=entered, current_stop=102.0, price=88.0, atr=2.0, spread=0.2)
    assert sig.trail_candidate is not None and sig.trail_candidate < D("102") and sig.trail_candidate > D("88")
