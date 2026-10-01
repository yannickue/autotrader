# ruff: noqa: E501
"""Lane C / BALANCE group: exact hand-computed values, prefix invariance, segment handling, serialisation, definition hash."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from market_observer import balance as B
from market_observer import schema as S


def make_bars(c, *, half=0.5, atr=1.0, seg=None, h=None, l=None, tv=None, gap_at=None) -> S.ObserverBars:
    c = np.asarray(c, dtype=float)
    n = len(c)
    h = c + half if h is None else np.asarray(h, dtype=float)
    l = c - half if l is None else np.asarray(l, dtype=float)  # noqa: E741
    step = np.arange(n, dtype=np.int64) * 300
    if gap_at is not None:
        step = step + np.where(np.arange(n) >= gap_at, 2 * 86400, 0)
    ts = (1_700_000_000 + step) * 1_000_000_000
    if seg is None:
        seg = np.zeros(n, dtype=np.int64) if gap_at is None else (np.arange(n) >= gap_at).astype(np.int64)
    o = np.r_[c[:1], c[:-1]]
    return S.ObserverBars(
        "T", ts, o, h, l, c, np.full(n, 50.0) if tv is None else np.asarray(tv, dtype=float), np.full(n, 0.02),
        np.full(n, atr) if np.isscalar(atr) else np.asarray(atr, dtype=float), np.asarray(seg, dtype=np.int64),
        (step // 60) % 1440, step // 86400, 0.01, S.SessionSpec("UTC", 0, 1440),
    )


def cfg(*windows: int) -> B.BalanceConfig:
    return B.BalanceConfig(windows=tuple(windows))


def val(bars, i, key, *windows):
    return B.balance_features(bars, i, cfg(*windows)).values[key]


# ------------------------------------------------------------------ (c) exact values
def test_flat_range_exact():
    b = make_bars([10, 10, 10, 10])
    v = B.balance_features(b, 3, cfg(4)).values
    assert v["range_width_atr_w4"] == pytest.approx(1.0)
    assert v["directional_efficiency_w4"] is None  # no movement at all: undefined, never imputed
    assert v["bar_overlap_ratio_w4"] == pytest.approx(1.0)
    assert v["close_occupancy_ratio_w4"] == pytest.approx(1.0)
    assert v["midpoint_cross_count_w4"] == 0


def test_pure_trend_exact():
    b = make_bars([10, 11, 12, 13], atr=2.0)
    v = B.balance_features(b, 3, cfg(4)).values
    assert v["range_width_atr_w4"] == pytest.approx(2.0)  # (13.5 - 9.5) / 2
    assert v["directional_efficiency_w4"] == pytest.approx(1.0)
    assert v["bar_overlap_ratio_w4"] == pytest.approx(0.0)  # ranges only touch
    assert v["close_occupancy_ratio_w4"] == pytest.approx(0.5)  # central [10.5, 12.5]: 11 and 12
    assert v["midpoint_cross_count_w4"] == 1


def test_oscillation_exact():
    b = make_bars([10, 12, 10, 12])
    v = B.balance_features(b, 3, cfg(4)).values
    assert v["range_width_atr_w4"] == pytest.approx(3.0)
    assert v["directional_efficiency_w4"] == pytest.approx(2 / 6)
    assert v["bar_overlap_ratio_w4"] == pytest.approx(0.0)
    assert v["close_occupancy_ratio_w4"] == pytest.approx(0.0)
    assert v["midpoint_cross_count_w4"] == 3


def test_partial_overlap_ratio_is_intersection_over_union():
    # ranges [9.5,10.5] and [10.0,11.0]: intersection 0.5, union 1.5
    b = make_bars([10, 10.5], half=0.5)
    assert val(b, 1, "bar_overlap_ratio_w2", 2) == pytest.approx(0.5 / 1.5)


def test_true_balance_and_slow_trend_inside_same_range_are_distinguishable():
    trend = make_bars([10.0, 10.2, 10.4, 10.6, 10.8, 11.0], half=0.3)
    bal = make_bars([10.4, 10.0, 11.0, 10.2, 10.9, 10.6], half=0.3)
    vt, vb = (B.balance_features(x, 5, cfg(6)).values for x in (trend, bal))
    assert vt["range_width_atr_w6"] == pytest.approx(vb["range_width_atr_w6"])  # identical range width
    assert vt["directional_efficiency_w6"] == pytest.approx(1.0)
    assert vb["directional_efficiency_w6"] == pytest.approx(0.2 / 3.2)
    assert vt["midpoint_cross_count_w6"] == 1 and vb["midpoint_cross_count_w6"] == 3
    assert vt["directional_efficiency_w6"] > 5 * vb["directional_efficiency_w6"]


def test_two_default_windows_and_documented_keys():
    b = make_bars(np.cumsum(np.random.default_rng(1).normal(size=80)) + 100)
    r = B.balance_features(b, 70)
    assert r.group == "balance" and r.version == S.GROUP_VERSIONS["balance"]
    assert B.BalanceConfig().windows == (24, 48)
    assert set(r.values) == {f"{m}_w{w}" for m in B.METRICS for w in (24, 48)}
    assert all(v is not None for v in r.values.values())
    assert "entropy" not in " ".join(r.values)  # deliberately left out (documented)


def test_atr_missing_only_blanks_the_atr_normalised_metric():
    b = make_bars([10, 12, 10, 12], atr=np.nan)
    v = B.balance_features(b, 3, cfg(4)).values
    assert v["range_width_atr_w4"] is None and v["midpoint_cross_count_w4"] == 3


# ------------------------------------------------------------------ (f) segments / short history
def test_fewer_bars_than_window_is_none():
    b = make_bars([10, 11, 12])
    assert all(v is None for v in B.balance_features(b, 2, cfg(4)).values.values())
    assert all(v is None for v in B.balance_features(make_bars(np.arange(30.0)), 10, cfg(24)).values.values())


def test_window_never_spans_a_segment_break():
    c = np.arange(10.0)
    b = make_bars(c, gap_at=5)  # bars 0-4 | weekend gap | bars 5-9
    assert all(v is None for v in B.balance_features(b, 7, cfg(4)).values.values())  # window 4..7 spans the break
    assert all(v is not None for v in B.balance_features(b, 8, cfg(4)).values.values())  # window 5..8 inside segment 1
    assert all(v is not None for v in B.balance_features(b, 3, cfg(4)).values.values())


# ------------------------------------------------------------------ (a)(b)(h) invariance / determinism
def _walk(n=260, seed=3, with_breaks=True):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 0.3, n))
    return make_bars(c, half=0.2, atr=0.4, gap_at=120 if with_breaks else None)


@pytest.mark.parametrize("breaks", [False, True])
def test_prefix_invariance_many_i(breaks):
    b = _walk(with_breaks=breaks)
    for i in list(range(0, 260, 7)) + [119, 120, 121, 167, 168, 259]:
        assert B.balance_features(b, i) == B.balance_features(b.prefix(i + 1), i)


def test_future_bars_never_change_an_earlier_value():
    b = _walk()
    i = 150
    base = B.balance_features(b, i)
    c2 = b.c.copy()
    c2[i + 1:] += 50.0
    h2, l2 = b.h.copy(), b.l.copy()
    h2[i + 1:] += 50.0
    l2[i + 1:] += 50.0
    b2 = dataclasses.replace(b, c=c2, h=h2, l=l2)
    assert B.balance_features(b2, i) == base


def test_chunked_restart_determinism():
    b = _walk()
    full = [B.balance_features(b, i) for i in range(260)]
    chunked = []
    for start in range(0, 260, 50):
        end = min(start + 50, 260)
        part = b.prefix(end)  # a restart that re-reads history up to the chunk end
        chunked += [B.balance_features(part, i) for i in range(start, end)]
    assert full == chunked
    assert [B.balance_features(b, i) for i in range(260)] == full  # idempotent


# ------------------------------------------------------------------ (g) serialisation
def test_serialises_through_decision_features_with_causality_guard():
    b = _walk()
    r = B.balance_features(b, 200)
    f = S.DecisionFeatures.from_results(b.decision_ts_ns(200), [r])
    assert f.versions == {"balance": S.GROUP_VERSIONS["balance"]}
    assert "f_balance__range_width_atr_w24" in f.columns and "f_balance__midpoint_cross_count_w48" in f.columns
    assert all(k.startswith("f_balance__") for k in f.columns)


# ------------------------------------------------------------------ (i) definition hash
def test_definition_hash_is_stable_and_detects_changes():
    h0 = B.definition_hash()
    assert h0 == B.DEFINITION_HASH == B.definition_hash(B.BalanceConfig())
    assert len(h0) == 64
    assert B.definition_hash(B.BalanceConfig(windows=(24, 60))) != h0
    assert B.definition_hash(B.BalanceConfig(central_zone_fraction=0.6)) != h0


def test_definition_hash_pinned_literal():
    assert B.definition_hash() == "060c27799e7091c531b7f2f2c26554a934a729476c755a281c48a61faa1580af"
