# ruff: noqa: E501, E741, RUF005
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
    l = c - half if l is None else np.asarray(l, dtype=float)
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
    assert B.definition_hash() == "ddac1af18d7032ca1ee3acf276d5f2ccd6101710a5bd8476158872b7ca8d3aec"


# ------------------------------------------------------------------ RECURSIVE / WARM-UP INVARIANCE
def _window(b: S.ObserverBars, s: int, e: int) -> S.ObserverBars:
    sl = slice(s, e)
    return dataclasses.replace(
        b, ts_ns=b.ts_ns[sl], o=b.o[sl], h=b.h[sl], l=b.l[sl], c=b.c[sl], tick_volume=b.tick_volume[sl], spread=b.spread[sl],
        atr=b.atr[sl], segment_id=b.segment_id[sl], local_minute=b.local_minute[sl], local_day=b.local_day[sl],
    )


def _sma_atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    out = np.full(len(c), np.nan)
    cs = np.cumsum(np.r_[0.0, tr])
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _wilder_atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    out = np.empty(len(c))
    out[0] = tr[0]
    for k in range(1, len(c)):
        out[k] = out[k - 1] + (tr[k] - out[k - 1]) / n
    return out


def test_min_history_constant_is_the_longest_window():
    assert B.MIN_HISTORY_BARS == 48 == B.min_history_bars()


def test_recursive_invariance_exact_with_an_exact_window_atr():
    rng = np.random.default_rng(21)
    n = 700
    c = 100 + np.cumsum(rng.normal(0, 0.3, n))
    h, l = c + rng.uniform(0.05, 0.3, n), c - rng.uniform(0.05, 0.3, n)
    b = dataclasses.replace(make_bars(c, h=h, l=l), atr=_sma_atr(h, l, c))
    for T in range(520, 700, 11):
        ref = B.balance_features(b, T)
        assert all(v is not None for v in ref.values.values())
        for loaded in (120, 240, 500, T + 1):
            assert B.balance_features(_window(b, T + 1 - loaded, T + 1), loaded - 1) == ref


def test_recursive_atr_normalised_value_inherits_only_the_atr_warmup_not_more():
    rng = np.random.default_rng(22)
    n = 700
    c = 100 + np.cumsum(rng.normal(0, 0.3, n))
    h, l = c + rng.uniform(0.05, 0.3, n), c - rng.uniform(0.05, 0.3, n)
    full = dataclasses.replace(make_bars(c, h=h, l=l), atr=_wilder_atr(h, l, c))
    worst = 0.0
    for T in range(520, 700, 11):
        ref = B.balance_features(full, T).values
        for loaded in (120, 240, 500):
            s = T + 1 - loaded
            w = dataclasses.replace(make_bars(c[s: T + 1], h=h[s: T + 1], l=l[s: T + 1]), atr=_wilder_atr(h[s: T + 1], l[s: T + 1], c[s: T + 1]))
            got = B.balance_features(w, loaded - 1).values
            for k, v in ref.items():
                if k.startswith("range_width_atr"):
                    worst = max(worst, abs(got[k] / v - 1.0))
                else:
                    assert got[k] == v, k  # everything that does not touch ATR stays EXACT
    assert worst < 1e-3, worst


def test_longer_window_is_none_below_min_history_but_exact_after():
    b = _walk(with_breaks=False)
    r = B.balance_features(b, 30)
    assert r.values["midpoint_cross_count_w24"] is not None and r.values["midpoint_cross_count_w48"] is None
    assert B.balance_features(b, 47).values["midpoint_cross_count_w48"] is not None
