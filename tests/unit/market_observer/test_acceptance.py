# ruff: noqa: E501, E741, RUF005
"""Lane C / ACCEPTANCE group: continuous break/hold/reclaim features, long/short mirror symmetry, causality, definition hash."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from market_observer import acceptance as A
from market_observer import schema as S

LEVEL = S.LevelRef("L1", 100.0, 99.9, 100.1, ("ROUND_MAJOR",), 0, 0)
# (open, high, low, close); edge = zone_high = 100.1 for a long break. Break bar = index 2.
SEQ = [
    (99.5, 99.8, 99.4, 99.7),
    (99.7, 100.0, 99.6, 99.9),
    (99.9, 101.0, 99.8, 100.9),  # break: close beyond the edge, previous close was not
    (100.9, 101.5, 100.6, 101.2),  # holds
    (101.2, 101.3, 99.9, 100.0),  # closes back inside (reclaim), wick re-enters 0.2 deep
    (100.0, 100.8, 99.95, 100.5),  # beyond again
]


def make_bars(seq, *, atr=1.0, gap_at=None, level_ts=None) -> S.ObserverBars:
    a = np.asarray(seq, dtype=float)
    n = len(a)
    step = np.arange(n, dtype=np.int64) * 300
    if gap_at is not None:
        step = step + np.where(np.arange(n) >= gap_at, 2 * 86400, 0)
    ts = (1_700_000_000 + step) * 1_000_000_000
    seg = np.zeros(n, dtype=np.int64) if gap_at is None else (np.arange(n) >= gap_at).astype(np.int64)
    return S.ObserverBars(
        "T", ts, a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.full(n, 50.0), np.full(n, 0.02),
        np.full(n, atr) if np.isscalar(atr) else np.asarray(atr, dtype=float), seg, (step // 60) % 1440, step // 86400, 0.01, S.SessionSpec("UTC", 0, 1440),
    )


def mirror_seq(seq):
    return [(-o, -l, -h, -c) for (o, h, l, c) in seq]


def mirror_level(lv):
    return dataclasses.replace(lv, price=-lv.price, zone_low=-lv.zone_high, zone_high=-lv.zone_low)


# ------------------------------------------------------------------ (d) hand-built break / hold / reclaim sequence
def test_break_hold_reclaim_exact_values_at_decision_bar_5():
    v = A.acceptance_features(make_bars(SEQ), 5, LEVEL, +1).values
    assert v["break_found"] is True
    assert v["break_close_distance_atr"] == pytest.approx(0.8)  # 100.9 - 100.1
    assert v["break_body_atr"] == pytest.approx(1.0)  # 100.9 - 99.9
    assert v["break_true_range_atr"] == pytest.approx(1.2)  # max(1.2, |101.0-99.9|, |99.8-99.9|)
    assert v["closes_beyond_level_count"] == 3  # bars 2, 3, 5
    assert v["bars_since_break"] == 3
    assert v["max_reentry_depth_atr"] == pytest.approx(0.2)  # 100.1 - 99.9 after the break bar
    assert v["followthrough_high_atr"] == pytest.approx(1.4)  # 101.5 - 100.1
    assert v["followthrough_low_atr"] == pytest.approx(-0.3)  # 99.8 - 100.1 (break bar included)
    assert v["followthrough_dir_atr"] == pytest.approx(1.4)
    assert v["close_location_in_bar"] == pytest.approx((100.9 - 99.8) / 1.2)
    assert v["time_held_beyond_level_bars"] == 1 and v["time_held_beyond_level_minutes"] == pytest.approx(5.0)
    assert v["reclaim_occurred"] is True
    assert v["break_bar_ts_ns"] == int(make_bars(SEQ).ts_ns[2])


def test_hold_without_reclaim_at_decision_bar_3():
    v = A.acceptance_features(make_bars(SEQ), 3, LEVEL, +1).values
    assert v["closes_beyond_level_count"] == 2 and v["bars_since_break"] == 1
    assert v["max_reentry_depth_atr"] == pytest.approx(0.0)  # bar 3 low 100.6 stays above the edge
    assert v["reclaim_occurred"] is False
    assert v["time_held_beyond_level_bars"] == 2 and v["time_held_beyond_level_minutes"] == pytest.approx(10.0)


def test_decision_on_break_bar_itself_and_before_break():
    v = A.acceptance_features(make_bars(SEQ), 2, LEVEL, +1).values
    assert v["bars_since_break"] == 0 and v["closes_beyond_level_count"] == 1 and v["max_reentry_depth_atr"] == 0.0
    assert v["reclaim_occurred"] is False
    v = A.acceptance_features(make_bars(SEQ), 1, LEVEL, +1).values
    assert v["break_found"] is False
    assert all(x is None for k, x in v.items() if k != "break_found")


def test_reclaim_currently_inside_has_zero_time_held():
    v = A.acceptance_features(make_bars(SEQ), 4, LEVEL, +1).values
    assert v["reclaim_occurred"] is True and v["time_held_beyond_level_bars"] == 0 and v["closes_beyond_level_count"] == 2


def test_none_level_gives_all_none():
    r = A.acceptance_features(make_bars(SEQ), 5, None, +1)
    assert r.group == "acceptance" and r.version == S.GROUP_VERSIONS["acceptance"]
    assert set(r.values) == set(A.FEATURE_NAMES) and all(x is None for x in r.values.values())


def test_invalid_direction_is_rejected():
    with pytest.raises(ValueError):
        A.acceptance_features(make_bars(SEQ), 5, LEVEL, 0)


def test_level_confirmed_in_the_future_is_unknown():
    lv = dataclasses.replace(LEVEL, confirmed_at_ts_ns=int(make_bars(SEQ).ts_ns[5]) + 1_000_000_000 * 400)
    assert all(x is None for x in A.acceptance_features(make_bars(SEQ), 5, lv, +1).values.values())


def test_breaks_before_level_confirmation_do_not_count():
    b = make_bars(SEQ)
    lv = dataclasses.replace(LEVEL, confirmed_at_ts_ns=int(b.ts_ns[3]))  # confirmed when bar 3 opens: break at bar 2 predates it
    v = A.acceptance_features(b, 5, lv, +1).values
    # bar 3 is already beyond and bar 2 is not eligible; the first FRESH cross after confirmation is the re-break at bar 5
    assert v["bars_since_break"] == 0 and v["reclaim_occurred"] is False and v["closes_beyond_level_count"] == 1
    lv2 = dataclasses.replace(LEVEL, confirmed_at_ts_ns=int(b.ts_ns[2]))
    assert A.acceptance_features(b, 5, lv2, +1).values["bars_since_break"] == 3


# ------------------------------------------------------------------ mirror symmetry
SIGNED_SAME = (
    "break_close_distance_atr", "break_body_atr", "break_true_range_atr", "closes_beyond_level_count", "bars_since_break",
    "max_reentry_depth_atr", "followthrough_dir_atr", "close_location_in_bar", "time_held_beyond_level_bars",
    "time_held_beyond_level_minutes", "reclaim_occurred", "break_found",
)


@pytest.mark.parametrize("i", [2, 3, 4, 5])
def test_long_short_mirror_symmetry(i):
    lo = A.acceptance_features(make_bars(SEQ), i, LEVEL, +1).values
    sh = A.acceptance_features(make_bars(mirror_seq(SEQ)), i, mirror_level(LEVEL), -1).values
    for k in SIGNED_SAME:
        assert sh[k] == pytest.approx(lo[k]), k
    assert sh["followthrough_high_atr"] == pytest.approx(-lo["followthrough_low_atr"])
    assert sh["followthrough_low_atr"] == pytest.approx(-lo["followthrough_high_atr"])


def test_short_break_close_location_is_direction_relative():
    # short break closing at its LOW: location 1.0 (closing at the extreme in the break direction)
    seq = [(100.0, 100.2, 99.8, 100.0), (100.0, 100.1, 98.0, 98.0)]
    lv = S.LevelRef("L", 99.0, 98.9, 99.1, (), 0, 0)
    v = A.acceptance_features(make_bars(seq), 1, lv, -1).values
    assert v["close_location_in_bar"] == pytest.approx(1.0) and v["break_close_distance_atr"] == pytest.approx(0.9)


# ------------------------------------------------------------------ ATR and segments
def test_atr_missing_only_blanks_atr_normalised_values():
    v = A.acceptance_features(make_bars(SEQ, atr=np.nan), 5, LEVEL, +1).values
    assert v["break_close_distance_atr"] is None and v["followthrough_high_atr"] is None
    assert v["closes_beyond_level_count"] == 3 and v["bars_since_break"] == 3 and v["close_location_in_bar"] is not None


def test_break_must_not_be_searched_across_a_segment_boundary():
    # gap before bar 3: bars 0-2 are segment 0 (the break happens there), 3-5 segment 1
    b = make_bars(SEQ, gap_at=3)
    v = A.acceptance_features(b, 4, LEVEL, +1).values
    # bar 3 is beyond but its predecessor (bar 2) lies in another segment: no fresh cross inside segment 1 -> no break
    assert v["break_found"] is False
    assert A.acceptance_features(b, 5, LEVEL, +1).values["bars_since_break"] == 0  # only the bar-5 re-cross counts
    v2 = A.acceptance_features(b, 2, LEVEL, +1).values
    assert v2["break_found"] is True and v2["bars_since_break"] == 0


# ------------------------------------------------------------------ (a)(b)(h) invariance / determinism
def _walk(n=300, seed=5, gap=150):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 0.3, n))
    o = np.r_[c[:1], c[:-1]]
    seq = np.c_[o, np.maximum(o, c) + 0.1, np.minimum(o, c) - 0.1, c]
    return make_bars(seq, atr=0.4, gap_at=gap)


@pytest.mark.parametrize("direction", [1, -1])
def test_prefix_invariance_many_i(direction):
    b = _walk()
    lv = S.LevelRef("W", 100.0, 99.8, 100.2, ("SWING_M5",), 0, int(b.ts_ns[20]))
    for i in list(range(0, 300, 9)) + [148, 149, 150, 151, 299]:
        assert A.acceptance_features(b, i, lv, direction) == A.acceptance_features(b.prefix(i + 1), i, lv, direction)


def test_future_bars_never_change_an_earlier_value():
    b = _walk()
    lv = S.LevelRef("W", 100.0, 99.8, 100.2, (), 0, int(b.ts_ns[20]))
    i = 200
    base = A.acceptance_features(b, i, lv, 1)
    f = dataclasses.replace(b, c=np.where(np.arange(300) > i, b.c + 40, b.c), h=np.where(np.arange(300) > i, b.h + 40, b.h),
                            l=np.where(np.arange(300) > i, b.l + 40, b.l))
    assert A.acceptance_features(f, i, lv, 1) == base


def test_chunked_restart_determinism():
    b = _walk()
    lv = S.LevelRef("W", 100.0, 99.8, 100.2, (), 0, int(b.ts_ns[20]))
    full = [A.acceptance_features(b, i, lv, 1) for i in range(300)]
    chunked = []
    for s in range(0, 300, 60):
        e = min(s + 60, 300)
        chunked += [A.acceptance_features(b.prefix(e), i, lv, 1) for i in range(s, e)]
    assert full == chunked


# ------------------------------------------------------------------ (g) serialisation
def test_serialises_through_decision_features_and_timestamp_guard():
    b = make_bars(SEQ)
    r = A.acceptance_features(b, 5, LEVEL, +1)
    f = S.DecisionFeatures.from_results(b.decision_ts_ns(5), [r])
    assert f.columns["f_acceptance__break_bar_ts_ns"] <= f.decision_ts_ns
    assert f.versions == {"acceptance": S.GROUP_VERSIONS["acceptance"]}
    assert set(f.columns) == {f"f_acceptance__{k}" for k in A.FEATURE_NAMES}


# ------------------------------------------------------------------ (i) definition hash
def test_definition_hash_is_stable_and_detects_changes():
    h0 = A.definition_hash()
    assert h0 == A.DEFINITION_HASH == A.definition_hash(A.AcceptanceConfig()) and len(h0) == 64
    assert A.definition_hash(A.AcceptanceConfig(max_break_lookback_bars=48)) != h0


def test_definition_hash_pinned_literal():
    assert A.definition_hash() == "ddeb7b66e6caa06860faf96754568180cc6951ffab2b16fef3dac89442535589"
