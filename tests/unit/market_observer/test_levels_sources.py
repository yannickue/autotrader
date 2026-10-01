# ruff: noqa: E501
"""Level SOURCES: swing timing + M15 closed-bar alignment, previous-day/session/overnight/ORB, STRUCT range, ROUND grid, segments, DST-like jumps."""

from __future__ import annotations

import numpy as np
import pandas as pd
from test_levels_support import DAY0, NS, make_bars, random_walk

from demo import structure as DS
from market_observer import levels as L
from market_observer import schema as S

SWING_M5 = L.LevelConfig(swing_timeframes=("M5",), struct_range_n=None)
SWING_M15 = L.LevelConfig(swing_timeframes=("M15",), struct_range_n=None, max_level_age_bars=100_000)
DAILY_ONLY = L.LevelConfig(swing_timeframes=(), struct_range_n=None)


def run(bars, cfg, upto=None):
    reg = L.LevelRegistry(cfg)
    return [reg.update(bars, i) for i in range(len(bars) if upto is None else upto + 1)]


def of(ctx, source):
    return [lv for lv in ctx.levels if lv.source == source]


def prices(ctx, source):
    return sorted(lv.price for lv in of(ctx, source))


# ---------------------------------------------------------------------------------------------- (d) exact swing confirmation timing
def test_m5_swing_is_confirmed_exactly_at_the_close_of_bar_i_plus_n():
    highs = [10.0, 10.1, 10.2, 11.0, 10.3, 10.2, 10.1, 10.0]
    rows = [(h - 0.1, h, h - 0.3, h - 0.1) for h in highs]
    bars = make_bars(rows)
    ctxs = run(bars, SWING_M5)
    assert prices(ctxs[4], "SWING_M5") == [] or 11.0 not in prices(ctxs[4], "SWING_M5")  # only 1 bar right of the peak: not confirmed
    s = [lv for lv in of(ctxs[5], "SWING_M5") if lv.price == 11.0]
    assert len(s) == 1
    assert s[0].created_at_ts_ns == int(bars.ts_ns[3])  # time_created = OPEN of the swing bar
    assert s[0].confirmed_at_ts_ns == bars.decision_ts_ns(5)  # = close of bar i+n
    assert s[0].first_tradeable_at_ts_ns == s[0].confirmed_at_ts_ns
    assert s[0].age_bars == 0 and s[0].touch_count == 0
    s6 = next(lv for lv in of(ctxs[6], "SWING_M5") if lv.price == 11.0)
    assert s6.level_id == s[0].level_id and s6.age_bars == 1 and s6.age_minutes == 5.0
    assert s6.confirmed_at_ts_ns == s[0].confirmed_at_ts_ns


def test_swing_levels_equal_the_demo_structure_oracle_per_segment():
    bars = random_walk(260, 7, segment_breaks=(70, 150), bars_per_day=10_000)
    ctxs = run(bars, L.LevelConfig(swing_timeframes=("M5", "M15"), struct_range_n=None, max_level_age_bars=100_000))
    frame = pd.DataFrame({"ts": pd.to_datetime(bars.ts_ns, utc=True), "open": bars.o, "high": bars.h, "low": bars.l, "close": bars.c})
    for i in (80, 140, 149, 160, 259):
        seg = int(bars.segment_id[i])
        idx = np.nonzero(bars.segment_id[: i + 1] == seg)[0]
        part = frame.iloc[idx[0]: i + 1].reset_index(drop=True)
        exp: set[tuple[str, float, int, int]] = set()
        for tf, fr in (("M5", part), ("M15", DS.resample_m15(part))):
            for s in DS.confirmed_swings(fr, 2, tf):
                exp.add((f"SWING_{tf}", s.price, int(pd.Timestamp(s.bar_open).value), int(pd.Timestamp(s.confirmed_at).value)))
        got = {(lv.source.value, lv.price, lv.created_at_ts_ns, lv.confirmed_at_ts_ns) for lv in ctxs[i].levels if lv.source.value.startswith("SWING")}
        assert got == exp, f"i={i}"


# ---------------------------------------------------------------------------------------------- (2) HTF closed-bar alignment
def test_m15_swings_use_only_completely_closed_m15_bars_at_every_m5_offset():
    bars = random_walk(330, 11, bars_per_day=10_000)  # one segment; the 08:00 start is aligned to the 15-minute grid
    ctxs = run(bars, SWING_M15)
    frame = pd.DataFrame({"ts": pd.to_datetime(bars.ts_ns, utc=True), "open": bars.o, "high": bars.h, "low": bars.l, "close": bars.c})
    first_seen: dict[str, int] = {}
    for i in range(len(bars)):
        # oracle: M15 bars formed ONLY from the closed M5 prefix; a trailing forming (incomplete) group is dropped, never extrapolated
        m15 = DS.resample_m15(frame.iloc[: i + 1])
        exp = {(s.price, int(pd.Timestamp(s.bar_open).value), int(pd.Timestamp(s.confirmed_at).value)) for s in DS.confirmed_swings(m15, 2, "M15")}
        got = {(lv.price, lv.created_at_ts_ns, lv.confirmed_at_ts_ns) for lv in of(ctxs[i], "SWING_M15")}
        assert got == exp, f"i={i} offset={int(bars.local_minute[i]) % 15 // 5}"
        assert all(lv.confirmed_at_ts_ns <= bars.decision_ts_ns(i) for lv in of(ctxs[i], "SWING_M15"))
        for lv in of(ctxs[i], "SWING_M15"):
            first_seen.setdefault(lv.level_id, i)
    assert first_seen, "random walk must produce M15 swings"
    for lv in of(ctxs[-1], "SWING_M15"):
        j = first_seen[lv.level_id]
        assert lv.confirmed_at_ts_ns == bars.decision_ts_ns(j)  # visible from the very bar whose close completes the confirming M15 bar
        assert int(bars.local_minute[j]) % 15 == 10  # ... which is always the THIRD M5 bar of an M15 group (a forming bar is never closed)
        assert lv.confirmed_at_ts_ns % (900 * NS) == 0  # timestamps align on the M15 CLOSE
        assert lv.created_at_ts_ns % (900 * NS) == 0  # ... and on the M15 OPEN of the swing bar


def test_a_forming_m15_bar_cannot_confirm_an_m15_swing_early():
    # M15 highs per group: 10, 10.2, 12 (peak), 10.5, 10.4(forming/closing) -> n=2 confirmation needs M15 bar 4 completely closed
    group_highs = [10.0, 10.2, 12.0, 10.5, 10.4, 10.3]
    rows = []
    for g in group_highs:
        rows += [(g - 0.5, g - 0.3, g - 0.6, g - 0.5), (g - 0.5, g, g - 0.6, g - 0.4), (g - 0.4, g - 0.2, g - 0.7, g - 0.5)]
    bars = make_bars(rows, minutes=[480 + 5 * k for k in range(len(rows))])
    ctxs = run(bars, SWING_M15)
    third_bar_of_group4 = 4 * 3 + 2
    for i in range(0, third_bar_of_group4):  # offsets 0 and 1 of group 4 (and everything earlier): the peak is NOT yet a swing
        assert 12.0 not in prices(ctxs[i], "SWING_M15"), f"i={i}"
    got = [lv for lv in of(ctxs[third_bar_of_group4], "SWING_M15") if lv.price == 12.0]
    assert len(got) == 1 and got[0].confirmed_at_ts_ns == bars.decision_ts_ns(third_bar_of_group4)
    assert 12.0 in prices(ctxs[-1], "SWING_M15")  # later bars never change it


# ---------------------------------------------------------------------------------------------- (h) daily sources on a multi-day series
SESSION = S.SessionSpec("UTC", 540, 660)  # 09:00-11:00 cash session


def day_rows(day, minutes, seg, over=None):
    over = over or {}
    out = []
    for m in minutes:
        h, lo = over.get((day, m), (100.1, 99.9))
        out.append(((100.0, h, lo, 100.0), m, day, seg))
    return out


def two_day_series(session=SESSION):
    W, A, B, C = DAY0, DAY0 + 1, DAY0 + 2, DAY0 + 5  # W = short warm-up day, B -> C crosses a weekend
    over = {
        (A, 485): (104.0, 99.9), (A, 600): (103.0, 99.9), (A, 620): (100.1, 97.0), (A, 670): (100.1, 95.0), (A, 680): (101.5, 99.9),
        (B, 500): (100.8, 99.5), (B, 540): (102.0, 99.0), (B, 545): (100.5, 99.5), (B, 550): (101.0, 99.2), (B, 580): (101.9, 99.9), (B, 590): (100.1, 98.5),
    }
    rows = day_rows(W, [480, 485, 490], 0)
    rows += day_rows(A, range(480, 700, 5), 1, over)
    rows += day_rows(B, range(480, 605, 5), 2, over) + day_rows(B, range(630, 700, 5), 3, over)  # pause 605..625 inside day B = segment break
    rows += day_rows(C, range(480, 560, 5), 4, over)
    bars = make_bars([r[0] for r in rows], minutes=[r[1] for r in rows], days=[r[2] for r in rows], segments=[r[3] for r in rows], session=session)
    return bars, (W, A, B, C)


def idx_of(bars, day, minute):
    return int(np.nonzero((bars.local_day == day) & (bars.local_minute == minute))[0][0])


def test_previous_day_session_overnight_orb_on_a_series_with_weekend_and_pause():
    bars, (_, A, B, C) = two_day_series()
    ctxs = run(bars, DAILY_ONLY)
    # first observed day is a warm-up day: nothing is derived from it (its true start is unknown), and nothing exists before day B/A completed
    assert not any(lv.source.value.startswith(("PREV_DAY", "SESSION", "OVERNIGHT")) for lv in ctxs[idx_of(bars, A, 480)].levels)
    assert not any(lv.source.value.startswith("PREV_DAY") for lv in ctxs[idx_of(bars, A, 695)].levels)  # day A is still running
    # session of day A: known from the first post-close bar on (minute 660), defined by cash bars only
    assert prices(ctxs[idx_of(bars, A, 655)], "SESSION_HIGH") == []
    c660 = ctxs[idx_of(bars, A, 660)]
    assert prices(c660, "SESSION_HIGH") == [103.0] and prices(c660, "SESSION_LOW") == [97.0]
    assert of(c660, "SESSION_HIGH")[0].confirmed_at_ts_ns == int(bars.ts_ns[idx_of(bars, A, 660)])
    # day B, first bar: previous-day levels of A
    cb = ctxs[idx_of(bars, B, 480)]
    assert prices(cb, "PREV_DAY_HIGH") == [104.0] and prices(cb, "PREV_DAY_LOW") == [95.0] and prices(cb, "PREV_DAY_CLOSE") == [100.0]
    ph = of(cb, "PREV_DAY_HIGH")[0]
    assert ph.created_at_ts_ns == int(bars.ts_ns[idx_of(bars, A, 485)]) and ph.confirmed_at_ts_ns == int(bars.ts_ns[idx_of(bars, B, 480)])
    assert prices(cb, "OVERNIGHT_HIGH") == []  # the overnight range is only complete at the session open
    assert prices(ctxs[idx_of(bars, B, 535)], "OVERNIGHT_HIGH") == []
    c540 = ctxs[idx_of(bars, B, 540)]
    # overnight = post-close bars of A + pre-open bars of B
    assert prices(c540, "OVERNIGHT_HIGH") == [101.5] and prices(c540, "OVERNIGHT_LOW") == [95.0]
    assert of(c540, "OVERNIGHT_HIGH")[0].created_at_ts_ns == int(bars.ts_ns[idx_of(bars, A, 680)])
    assert of(c540, "OVERNIGHT_HIGH")[0].confirmed_at_ts_ns == int(bars.ts_ns[idx_of(bars, B, 540)])
    # ORB (15 minutes = 3 bars 540/545/550): usable only after the window completed (close of the 550 bar)
    assert prices(ctxs[idx_of(bars, B, 545)], "ORB_HIGH") == []
    c550 = ctxs[idx_of(bars, B, 550)]
    assert prices(c550, "ORB_HIGH") == [102.0] and prices(c550, "ORB_LOW") == [99.0]
    assert of(c550, "ORB_HIGH")[0].confirmed_at_ts_ns == bars.decision_ts_ns(idx_of(bars, B, 550))
    # the pause (segment break) inside day B does not drop the daily-anchored levels
    cp = ctxs[idx_of(bars, B, 630)]
    assert bars.segment_id[idx_of(bars, B, 630)] != bars.segment_id[idx_of(bars, B, 600)]
    assert prices(cp, "PREV_DAY_HIGH") == [104.0] and prices(cp, "ORB_HIGH") == [102.0] and prices(cp, "OVERNIGHT_LOW") == [95.0]
    # day C after the weekend: previous TRADING day = B (complete day, incl. both sides of the pause); B's ORB/overnight are gone
    cc = ctxs[idx_of(bars, C, 480)]
    assert prices(cc, "PREV_DAY_HIGH") == [102.0] and prices(cc, "PREV_DAY_LOW") == [98.5] and prices(cc, "PREV_DAY_CLOSE") == [100.0]
    assert prices(cc, "ORB_HIGH") == [] and prices(cc, "OVERNIGHT_HIGH") == []
    assert prices(cc, "SESSION_HIGH") == [102.0] and prices(cc, "SESSION_LOW") == [98.5]  # session of B (cash bars only)
    # every daily level is causal
    for i, ctx in enumerate(ctxs):
        for lv in ctx.levels:
            assert lv.confirmed_at_ts_ns <= bars.decision_ts_ns(i) and lv.created_at_ts_ns <= lv.confirmed_at_ts_ns


def test_no_session_market_has_previous_day_levels_but_no_session_overnight_or_orb():
    bars, (_, _, B, _) = two_day_series(session=S.SessionSpec("UTC", None, None))
    ctxs = run(bars, DAILY_ONLY)
    srcs = {lv.source.value for ctx in ctxs for lv in ctx.levels}
    assert {"PREV_DAY_HIGH", "PREV_DAY_LOW", "PREV_DAY_CLOSE"} <= srcs
    assert not (srcs & {"SESSION_HIGH", "SESSION_LOW", "OVERNIGHT_HIGH", "OVERNIGHT_LOW", "ORB_HIGH", "ORB_LOW"})
    assert prices(ctxs[idx_of(bars, B, 480)], "PREV_DAY_HIGH") == [104.0]


def test_dst_like_jump_of_local_minute_is_taken_as_supplied_no_timezone_math():
    cfg = DAILY_ONLY
    ok = make_bars([(100, 100.5, 99.5, 100)] * 6, minutes=[540, 545, 550, 555, 560, 565], days=[DAY0] * 6,
                   session=S.SessionSpec("Not/AZone", 540, 660))
    assert prices(run(ok, cfg)[-1], "ORB_HIGH") == [100.5]
    # a wall-clock jump (e.g. 02:00 -> 03:00 style) INSIDE the opening range: the window is incomplete => no ORB, nothing guessed
    jump = make_bars([(100, 100.5, 99.5, 100)] * 6, minutes=[540, 545, 600, 605, 610, 615], days=[DAY0] * 6,
                     session=S.SessionSpec("Not/AZone", 540, 660))
    assert prices(run(jump, cfg)[-1], "ORB_HIGH") == []


def test_bar_seconds_other_than_300_is_rejected_for_swings():
    import pytest
    b = make_bars([(1, 2, 0, 1)] * 3)
    b = S.ObserverBars(b.market, b.ts_ns, b.o, b.h, b.l, b.c, b.tick_volume, b.spread, b.atr, b.segment_id, b.local_minute, b.local_day,
                       b.tick_size, b.session, 900)
    with pytest.raises(ValueError):
        L.LevelRegistry(L.LevelConfig()).update(b, 0)


# ---------------------------------------------------------------------------------------------- STRUCT range
def test_struct_range_needs_n_bars_in_one_segment_and_matches_the_demo_structure_edges():
    cfg = L.LevelConfig(swing_timeframes=(), struct_range_n=4)
    highs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    rows = [(h - 0.5, h, h - 1.0, h - 0.5) for h in highs]
    seg = [0, 0, 0, 1, 1, 1, 1, 1, 1]
    bars = make_bars(rows, segments=seg)
    ctxs = run(bars, cfg)
    assert prices(ctxs[2], "STRUCT_RANGE_HIGH") == []  # fewer than N bars so far
    # the segment that starts after bar 2 has its first N bars at i=6: the range of bars 3..6 (a real segment start => created from nothing)
    assert prices(ctxs[5], "STRUCT_RANGE_HIGH") == []
    assert prices(ctxs[6], "STRUCT_RANGE_HIGH") == [7.0] and prices(ctxs[6], "STRUCT_RANGE_LOW") == [3.0]
    assert of(ctxs[6], "STRUCT_RANGE_HIGH")[0].confirmed_at_ts_ns == bars.decision_ts_ns(6)
    assert prices(ctxs[7], "STRUCT_RANGE_HIGH") == [8.0]  # the extreme changed: the old level is superseded, a new one is born
    frame = pd.DataFrame({"ts": pd.to_datetime(bars.ts_ns, utc=True), "open": bars.o, "high": bars.h, "low": bars.l, "close": bars.c})
    edges = DS.prior_range_edges(frame.iloc[3:9], 4)
    assert edges is not None and float(edges[0].price) == prices(ctxs[8], "STRUCT_RANGE_HIGH")[0] and float(edges[1].price) == prices(ctxs[8], "STRUCT_RANGE_LOW")[0]


def test_struct_range_in_the_registry_first_segment_is_not_created_from_unknown_history():
    cfg = L.LevelConfig(swing_timeframes=(), struct_range_n=4)
    rows = [(h - 0.5, h, h - 1.0, h - 0.5) for h in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]]
    ctxs = run(make_bars(rows), cfg)
    assert prices(ctxs[3], "STRUCT_RANGE_HIGH") == []  # the first definable range of the FIRST segment: its true start may be before the loaded bars
    assert prices(ctxs[4], "STRUCT_RANGE_HIGH") == [5.0]  # a later CHANGE of the range is an observed event


# ---------------------------------------------------------------------------------------------- segment break resets structures
def test_segment_break_drops_swing_struct_and_round_levels_but_not_daily_levels():
    bars = random_walk(160, 3, segment_breaks=(90,), bars_per_day=10_000, session=S.SessionSpec("UTC", 8 * 60 + 30, 8 * 60 + 150))
    cfg = L.LevelConfig(round_major_step=5.0, round_minor_step=1.0)
    ctxs = run(bars, cfg)
    first = 90
    assert bars.segment_id[first] != bars.segment_id[first - 1]
    seg_scoped = ("SWING_M5", "SWING_M15", "STRUCT_RANGE_HIGH", "STRUCT_RANGE_LOW", "ROUND_MAJOR", "ROUND_MINOR")
    seg_start_ts = int(bars.ts_ns[first])
    for i in range(first, len(bars)):
        for lv in ctxs[i].levels:
            if lv.source.value in seg_scoped:
                assert lv.created_at_ts_ns >= seg_start_ts and lv.confirmed_at_ts_ns >= seg_start_ts, (i, lv.source)
    assert any(lv.source.value in seg_scoped for lv in ctxs[first - 1].levels)
    assert not any(lv.source.value in seg_scoped for lv in ctxs[first - 1].levels if lv.confirmed_at_ts_ns >= seg_start_ts)


# ---------------------------------------------------------------------------------------------- ROUND grid
def test_round_levels_come_only_from_the_supplied_grid_with_nearest_levels_around_the_close():
    rows = [(103, 103.2, 102.8, 103), (103, 103.2, 102.8, 103), (103, 103.2, 102.8, 103)]
    bars = make_bars(rows)
    assert not any(lv.source.value.startswith("ROUND") for lv in run(bars, L.LevelConfig(swing_timeframes=(), struct_range_n=None))[-1].levels)  # never guessed
    # a new grid band entered AFTER the registry start is an observed event => levels created; the starting band is unknown history
    rows = [(103, 103.2, 102.8, 103), (103, 109.0, 102.8, 108.0), (108, 108.2, 107.8, 108.0)]
    ctx = run(make_bars(rows), L.LevelConfig(swing_timeframes=(), struct_range_n=None, round_major_step=10.0, round_minor_step=5.0))[-1]
    assert prices(ctx, "ROUND_MAJOR") == [] or all(p % 10 == 0 for p in prices(ctx, "ROUND_MAJOR"))
    assert all(p % 5 == 0 and p % 10 != 0 for p in prices(ctx, "ROUND_MINOR"))  # minor excludes points that are on the major grid
