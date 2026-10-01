# ruff: noqa: E501
"""Lane C / PARTICIPATION group (MT5 tick activity, NOT exchange volume): strictly past-only baselines, NaN handling, invariance, hash."""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from market_observer import participation as P
from market_observer import schema as S

PER_DAY = 288  # 24h M5 market


def make_bars(tv, *, seg=None, bar_seconds=300) -> S.ObserverBars:
    tv = np.asarray(tv, dtype=float)
    n = len(tv)
    idx = np.arange(n, dtype=np.int64)
    ts = (1_700_006_400 + idx * bar_seconds) * 1_000_000_000  # 1_700_006_400 = a UTC midnight
    c = np.full(n, 100.0)
    return S.ObserverBars(
        "T", ts, c, c + 0.1, c - 0.1, c, tv, np.full(n, 0.02), np.full(n, 0.5),
        np.zeros(n, dtype=np.int64) if seg is None else np.asarray(seg, dtype=np.int64),
        (idx % PER_DAY) * 5, idx // PER_DAY, 0.01, S.SessionSpec("UTC", 0, 1440), bar_seconds,
    )


def day_valued(days: int, current: float, cur_bar: int = 10) -> tuple[np.ndarray, int]:
    """Every bar of day d has tick volume 10 + d, except the current bar (day `days`, bar `cur_bar`) = `current`."""
    tv = np.concatenate([np.full(PER_DAY, 10.0 + d) for d in range(days + 1)])
    i = days * PER_DAY + cur_bar
    tv[i] = current
    return tv[: i + 1], i


# ------------------------------------------------------------------ (e) hand-computed baselines
def test_exact_hand_computed_values():
    tv, i = day_valued(25, 20.0)  # baseline = previous 20 days: 15..34 at the same local minute
    v = P.participation_features(make_bars(tv), i).values
    assert v["tick_activity_per_min"] == pytest.approx(20.0 / 5.0)
    assert v["tick_activity_percentile"] == pytest.approx((5 + 0.5) / 20)  # 15..19 below, one equal (mid-rank)
    assert v["tick_activity_z"] == pytest.approx((20.0 - 24.5) / math.sqrt(35.0))  # mean 24.5, sample std sqrt(35)
    assert v["activity_vs_same_tod"] == pytest.approx(20.0 / 24.5)  # median of 15..34
    assert v["activity_vs_session_baseline"] == pytest.approx(20.0 / 35.0)  # earlier bars of the day all 35
    assert v["activity_acceleration"] == pytest.approx(20.0 / 35.0)  # previous three bars all 35
    assert v["tod_baseline_n"] == 20


def test_baseline_uses_only_the_previous_20_days_not_older_ones():
    tv, i = day_valued(40, 20.0)  # days 20..39 are the baseline (30..49); days 0..19 must be ignored
    v = P.participation_features(make_bars(tv), i).values
    assert v["activity_vs_same_tod"] == pytest.approx(20.0 / 39.5)
    assert v["tod_baseline_oldest_ts_ns"] == int(make_bars(tv).ts_ns[20 * PER_DAY + 10])


def test_perturbing_a_future_tick_volume_changes_nothing():
    tv, i = day_valued(25, 20.0)
    full = np.concatenate([tv, np.full(3 * PER_DAY, 9999.0)])
    b0, b1 = make_bars(tv), make_bars(full)
    assert P.participation_features(b0, i) == P.participation_features(b1, i)
    rng = np.random.default_rng(0)
    full2 = full.copy()
    full2[i + 1:] = rng.uniform(1, 1e6, len(full) - i - 1)
    assert P.participation_features(make_bars(full2), i) == P.participation_features(b0, i)


def test_perturbing_a_past_tick_volume_does_change_it():
    tv, i = day_valued(25, 20.0)
    tv2 = tv.copy()
    tv2[(24 * PER_DAY) + 10] = 500.0  # one baseline sample
    assert P.participation_features(make_bars(tv2), i).values["tick_activity_z"] != P.participation_features(make_bars(tv), i).values["tick_activity_z"]


def test_constant_series_percentile_is_well_defined():
    tv = np.full(25 * PER_DAY + 20, 50.0)
    v = P.participation_features(make_bars(tv), len(tv) - 1).values
    assert v["tick_activity_percentile"] == pytest.approx(0.5)  # all equal: mid-rank
    assert v["tick_activity_z"] is None  # std 0: undefined, never inf/NaN
    assert v["activity_vs_same_tod"] == pytest.approx(1.0) and v["activity_vs_session_baseline"] == pytest.approx(1.0)
    assert v["activity_acceleration"] == pytest.approx(1.0)


def test_extreme_current_value_percentile_bounds():
    tv, i = day_valued(25, 10_000.0)
    assert P.participation_features(make_bars(tv), i).values["tick_activity_percentile"] == pytest.approx((20 + 0) / 20)
    tv, i = day_valued(25, 0.5)
    assert P.participation_features(make_bars(tv), i).values["tick_activity_percentile"] == pytest.approx(0.0)


# ------------------------------------------------------------------ NaN / zero / short history
def test_nan_or_zero_current_tick_volume_gives_all_none():
    for bad in (np.nan, 0.0):
        tv, i = day_valued(25, bad)
        r = P.participation_features(make_bars(tv), i)
        assert set(r.values) == set(P.FEATURE_NAMES) and all(x is None for x in r.values.values())


def test_nan_and_zero_baseline_samples_are_excluded_not_imputed():
    tv, i = day_valued(25, 20.0)
    tv[10 * PER_DAY + 10] = np.nan  # day 10 is inside the baseline (days 5..24)
    tv[12 * PER_DAY + 10] = 0.0
    v = P.participation_features(make_bars(tv), i).values
    assert v["tod_baseline_n"] == 18  # the 20 most recent VALID samples would reach day 3 and 4 only if re-sampled: not allowed, window is by day
    assert v["activity_vs_same_tod"] == pytest.approx(20.0 / np.median([10 + d for d in range(5, 25) if d not in (10, 12)]))


def test_too_few_baseline_samples_is_none_but_raw_activity_stays():
    tv, i = day_valued(4, 20.0)
    v = P.participation_features(make_bars(tv), i).values
    assert v["tick_activity_per_min"] == pytest.approx(4.0)
    assert v["tick_activity_percentile"] is None and v["tick_activity_z"] is None and v["activity_vs_same_tod"] is None
    assert v["activity_vs_session_baseline"] is not None  # session baseline needs only earlier bars of the same day


def test_session_baseline_needs_minimum_earlier_bars():
    tv, i = day_valued(25, 20.0, cur_bar=2)  # only 2 earlier bars today
    v = P.participation_features(make_bars(tv), i).values
    assert v["activity_vs_session_baseline"] is None and v["activity_acceleration"] is not None
    v = P.participation_features(make_bars(np.full(5, 50.0)), 1).values
    assert v["activity_acceleration"] is None and v["activity_vs_session_baseline"] is None  # < 3 previous bars; start of day not loaded


def test_acceleration_never_uses_bars_across_a_segment_break():
    tv, i = day_valued(25, 20.0)
    seg = np.zeros(len(tv), dtype=np.int64)
    seg[i - 1:] = 1  # a break right before bar i-1: only 1 previous same-segment bar
    v = P.participation_features(make_bars(tv, seg=seg), i).values
    assert v["activity_acceleration"] is None and v["tick_activity_percentile"] is not None  # baselines ignore segments (statistics only)


# ------------------------------------------------------------------ (a)(b)(h) invariance / determinism
def _random_bars(days=24, seed=11, with_breaks=True):
    rng = np.random.default_rng(seed)
    n = days * PER_DAY
    tv = rng.lognormal(3.0, 0.5, n)
    tv[rng.integers(0, n, 40)] = np.nan
    seg = np.zeros(n, dtype=np.int64)
    if with_breaks:
        seg[5 * PER_DAY + 100:] += 1
        seg[11 * PER_DAY + 130:] += 1
    return make_bars(tv, seg=seg)


def test_prefix_invariance_many_i():
    b = _random_bars()
    rng = np.random.default_rng(2)
    idx = sorted(set(rng.integers(0, len(b), 25).tolist()) | {0, 1, 3, 287, 288, 289, len(b) - 1, 5 * PER_DAY + 100, 5 * PER_DAY + 101})
    for i in idx:
        assert P.participation_features(b, i) == P.participation_features(b.prefix(i + 1), i)


def test_future_bars_never_change_an_earlier_value():
    b = _random_bars()
    i = 20 * PER_DAY + 77
    tv2 = b.tick_volume.copy()
    tv2[i + 1:] = 12345.0
    assert P.participation_features(dataclasses.replace(b, tick_volume=tv2), i) == P.participation_features(b, i)


def test_chunked_restart_determinism():
    b = _random_bars(days=22)
    pts = list(range(21 * PER_DAY, len(b), 11))
    full = [P.participation_features(b, i) for i in pts]
    chunked = []
    for s in range(0, len(pts), 8):
        for i in pts[s: s + 8]:
            chunked.append(P.participation_features(b.prefix(i + 1), i))
    assert full == chunked


# ------------------------------------------------------------------ (g) serialisation
def test_serialises_through_decision_features_and_timestamp_guard():
    tv, i = day_valued(25, 20.0)
    b = make_bars(tv)
    f = S.DecisionFeatures.from_results(b.decision_ts_ns(i), [P.participation_features(b, i)])
    assert f.columns["f_participation__tod_baseline_oldest_ts_ns"] <= f.decision_ts_ns
    assert f.versions == {"participation": S.GROUP_VERSIONS["participation"]}
    assert set(f.columns) == {f"f_participation__{k}" for k in P.FEATURE_NAMES}
    assert not any("volume" in k for k in f.columns)  # tick activity / participation proxy only


# ------------------------------------------------------------------ (i) definition hash
def test_definition_hash_is_stable_and_detects_changes():
    h0 = P.definition_hash()
    assert h0 == P.DEFINITION_HASH == P.definition_hash(P.ParticipationConfig()) and len(h0) == 64
    assert P.definition_hash(P.ParticipationConfig(tod_baseline_days=10)) != h0
    assert P.definition_hash(P.ParticipationConfig(accel_bars=5)) != h0


def test_definition_hash_pinned_literal():
    assert P.definition_hash() == "452f5122fbfc26c12ed207281e1fcbd3152c3ecd265d25b5ce177c13529c92b3"


# ------------------------------------------------------------------ RECURSIVE / WARM-UP INVARIANCE
def _window(b: S.ObserverBars, s: int, e: int) -> S.ObserverBars:
    sl = slice(s, e)
    return dataclasses.replace(
        b, ts_ns=b.ts_ns[sl], o=b.o[sl], h=b.h[sl], l=b.l[sl], c=b.c[sl], tick_volume=b.tick_volume[sl], spread=b.spread[sl],
        atr=b.atr[sl], segment_id=b.segment_id[sl], local_minute=b.local_minute[sl], local_day=b.local_day[sl],
    )


TOD_KEYS = ("tick_activity_percentile", "tick_activity_z", "activity_vs_same_tod", "tod_baseline_n", "tod_baseline_oldest_ts_ns")


def test_min_history_constant():
    assert P.MIN_HISTORY_PREV_DAYS == 21 == P.ParticipationConfig().tod_baseline_days + 1


def test_short_windows_never_present_a_partial_baseline_as_full():
    b = _random_bars(days=30, with_breaks=False)
    for T in (28 * PER_DAY + 50, 29 * PER_DAY + 200, len(b) - 1):
        ref = P.participation_features(b, T).values
        assert ref["tod_baseline_n"] is not None  # the full history DOES provide the baseline (so the test is not vacuous)
        for loaded in (120, 240, 500):
            got = P.participation_features(_window(b, T + 1 - loaded, T + 1), loaded - 1).values
            assert all(got[k] is None for k in TOD_KEYS), "a partial TOD baseline leaked out"
            for k, v in got.items():  # whatever IS reported must equal the full-history value
                assert v is None or v == ref[k], k
            assert got["activity_acceleration"] == ref["activity_acceleration"]


def test_exact_equality_once_the_documented_history_is_loaded():
    b = _random_bars(days=34, with_breaks=False)
    rng = np.random.default_rng(5)
    for T in sorted(rng.integers(28 * PER_DAY, len(b), 12).tolist()):
        ref = P.participation_features(b, T)
        d = int(b.local_day[T])
        first_of = lambda day: int(np.searchsorted(b.local_day, day, side="left"))  # noqa: E731
        # (a) loaded from the START of day D-22, (b) from the MIDDLE of day D-21 (truncated 21st previous day: exactly the minimum)
        for s in (first_of(d - 22), first_of(d - 21) + 137, 0):
            assert P.participation_features(_window(b, s, T + 1), T - s) == ref, (T, s)
        # (c) one previous day fewer than the minimum: TOD group None, never a 20-day-baseline lookalike
        short = P.participation_features(_window(b, first_of(d - 20) + 137, T + 1), T - (first_of(d - 20) + 137)).values
        assert all(short[k] is None for k in TOD_KEYS)


def test_session_baseline_needs_the_start_of_the_trading_day_loaded():
    tv, i = day_valued(25, 20.0, cur_bar=40)
    b = make_bars(tv)
    ref = P.participation_features(b, i).values
    assert ref["activity_vs_session_baseline"] == pytest.approx(20.0 / 35.0)
    inside_today = P.participation_features(_window(b, 25 * PER_DAY + 5, i + 1), i - (25 * PER_DAY + 5)).values
    assert inside_today["activity_vs_session_baseline"] is None  # today's first bars are missing: a truncated baseline is never reported
    from_today_open = P.participation_features(_window(b, 24 * PER_DAY + 280, i + 1), i - (24 * PER_DAY + 280)).values
    assert from_today_open["activity_vs_session_baseline"] == pytest.approx(ref["activity_vs_session_baseline"])
