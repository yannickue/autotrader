# ruff: noqa: E501
"""Level group invariants: prefix invariance, causality, restart/chunk equality, warm-up (recursive) invariance, mirror symmetry, serialisation."""

from __future__ import annotations

import copy
import dataclasses
import pickle

import numpy as np
import pytest
from test_levels_support import mirror, random_walk, window

from market_observer import levels as L
from market_observer import schema as S

CFG = L.LevelConfig(round_major_step=5.0, round_minor_step=1.0)
SAMPLE = (0, 1, 5, 13, 14, 20, 33, 39, 40, 41, 57, 71, 90, 119, 120, 121, 150, 187, 219)


def worlds():
    return {
        "days_are_segments": random_walk(220, 1, bars_per_day=40),
        "mid_day_segment_breaks": random_walk(220, 2, segment_breaks=(50, 120), bars_per_day=70),
    }


# ---------------------------------------------------------------------------------------------- (a) prefix invariance
@pytest.mark.parametrize("world", ["days_are_segments", "mid_day_segment_breaks"])
def test_prefix_invariance_replay_and_incremental(world):
    bars = worlds()[world]
    reg = L.LevelRegistry(CFG)
    inc = {}
    for i in range(len(bars)):
        c = reg.update(bars, i, build_context=i in SAMPLE)
        if i in SAMPLE:
            inc[i] = c
    n_with_levels = 0
    for i in SAMPLE:
        full = L.build_level_context(bars, i, CFG)
        pre = L.build_level_context(bars.prefix(i + 1), i, CFG)
        assert full == pre, f"prefix invariance broken at i={i}"
        assert inc[i] == full, f"incremental != replay at i={i}"
        n_with_levels += len(full.levels) > 0
    assert n_with_levels >= 10


def test_registry_is_strictly_sequential():
    bars = worlds()["days_are_segments"]
    reg = L.LevelRegistry(CFG)
    with pytest.raises(ValueError):
        reg.update(bars, 3)  # must start at 0
    reg.update(bars, 0)
    with pytest.raises(ValueError):
        reg.update(bars, 0)
    with pytest.raises(ValueError):
        reg.update(bars, 2)


# ---------------------------------------------------------------------------------------------- (b)(c) causality
def test_future_bars_never_change_a_confirmed_level_and_no_level_exists_before_confirmation():
    bars = worlds()["mid_day_segment_breaks"]
    reg = L.LevelRegistry(CFG)
    prev = None
    for i in range(len(bars)):
        ctx = reg.update(bars, i)
        dts = bars.decision_ts_ns(i)
        for lv in ctx.levels:
            assert lv.created_at_ts_ns <= lv.confirmed_at_ts_ns <= lv.first_tradeable_at_ts_ns <= dts, (i, lv.source)
            for t in (lv.last_touch_ts_ns, lv.last_break_ts_ns, lv.last_reclaim_ts_ns):
                assert t is None or lv.confirmed_at_ts_ns < t <= dts  # no event is counted before/at confirmation
        if prev is not None:
            now = {lv.level_id: lv for lv in ctx.levels}
            for old in prev.levels:
                new = now.get(old.level_id)
                if new is None:
                    continue  # retired (segment/day/age/superseded): allowed, never silently changed
                for f in ("market", "source", "price", "zone_low", "zone_high", "created_at_ts_ns", "confirmed_at_ts_ns", "first_tradeable_at_ts_ns"):
                    assert getattr(new, f) == getattr(old, f), (i, f)
                assert new.age_bars == old.age_bars + 1
                for f in ("touch_count", "clean_rejection_count", "penetration_count", "break_count", "reclaim_count"):
                    assert getattr(new, f) >= getattr(old, f)
        prev = ctx


# ---------------------------------------------------------------------------------------------- (k) chunked restart
def test_chunked_restart_equals_continuous_run():
    bars = worlds()["mid_day_segment_breaks"]
    cont = L.LevelRegistry(CFG)
    for i in range(len(bars)):
        cont.update(bars, i, build_context=False)
    expected = cont.context(bars)
    chunk1 = L.LevelRegistry(CFG)
    for i in range(130):  # the first chunk only knows the first 130 bars (a live feed that grows)
        chunk1.update(bars.prefix(130), i, build_context=False)
    for restored in (copy.deepcopy(chunk1), pickle.loads(pickle.dumps(chunk1))):  # in-memory and persisted state
        for i in range(130, len(bars)):
            restored.update(bars, i, build_context=False)
        assert restored.context(bars) == expected
    again = L.LevelRegistry(CFG)  # a cold restart replays from bar 0
    for i in range(len(bars)):
        again.update(bars, i, build_context=False)
    assert again.context(bars) == expected


# ---------------------------------------------------------------------------------------------- (i) long/short mirror symmetry
SRC_MIRROR = {
    "PREV_DAY_HIGH": "PREV_DAY_LOW", "PREV_DAY_LOW": "PREV_DAY_HIGH", "SESSION_HIGH": "SESSION_LOW", "SESSION_LOW": "SESSION_HIGH",
    "OVERNIGHT_HIGH": "OVERNIGHT_LOW", "OVERNIGHT_LOW": "OVERNIGHT_HIGH", "ORB_HIGH": "ORB_LOW", "ORB_LOW": "ORB_HIGH",
    "STRUCT_RANGE_HIGH": "STRUCT_RANGE_LOW", "STRUCT_RANGE_LOW": "STRUCT_RANGE_HIGH",
}
ROLE_MIRROR = {
    "SUPPORT": "RESISTANCE", "RESISTANCE": "SUPPORT", "BROKEN_UP": "BROKEN_DOWN", "BROKEN_DOWN": "BROKEN_UP", "ACCEPTED_ABOVE": "ACCEPTED_BELOW",
    "ACCEPTED_BELOW": "ACCEPTED_ABOVE", "RECLAIMED_FROM_ABOVE": "RECLAIMED_FROM_BELOW", "RECLAIMED_FROM_BELOW": "RECLAIMED_FROM_ABOVE",
    "FLIPPED_TO_SUPPORT": "FLIPPED_TO_RESISTANCE", "FLIPPED_TO_RESISTANCE": "FLIPPED_TO_SUPPORT", "UNCLASSIFIED": "UNCLASSIFIED",
}


def test_long_short_mirror_symmetry_of_state_and_features():
    bars = worlds()["days_are_segments"]
    mb = mirror(bars)
    ra, rb = L.LevelRegistry(CFG), L.LevelRegistry(CFG)
    compared = 0
    for i in range(len(bars)):
        a, b = ra.update(bars, i), rb.update(mb, i)
        la = sorted((SRC_MIRROR.get(lv.source.value, lv.source.value), -lv.price, lv.confirmed_at_ts_ns) for lv in a.levels)
        lb = sorted((lv.source.value, lv.price, lv.confirmed_at_ts_ns) for lv in b.levels)
        assert [(x[0], x[2]) for x in la] == [(x[0], x[2]) for x in lb]
        assert [x[1] for x in la] == pytest.approx([x[1] for x in lb])
        sa = {(SRC_MIRROR.get(lv.source.value, lv.source.value), lv.confirmed_at_ts_ns, round(-lv.price, 6)): lv for lv in a.levels}
        sb = {(lv.source.value, lv.confirmed_at_ts_ns, round(lv.price, 6)): lv for lv in b.levels}
        for k, x in sa.items():
            y = sb[k]
            assert (x.touch_count, x.clean_rejection_count, x.penetration_count, x.break_count, x.reclaim_count) == (
                y.touch_count, y.clean_rejection_count, y.penetration_count, y.break_count, y.reclaim_count)
            assert ROLE_MIRROR[x.current_role.value] == y.current_role.value
            assert (None if x.previous_role is None else ROLE_MIRROR[x.previous_role.value]) == (None if y.previous_role is None else y.previous_role.value)
            assert (x.age_bars, x.last_touch_ts_ns, x.last_break_ts_ns, x.last_reclaim_ts_ns) == (y.age_bars, y.last_touch_ts_ns, y.last_break_ts_ns, y.last_reclaim_ts_ns)
        if i % 3 == 0 and a.levels:
            fa = L.level_features(a, 1, float(bars.c[i])).values
            fb = L.level_features(b, -1, -float(bars.c[i])).values
            for key, v in fa.items():
                w = fb[key]
                if key == "nearest_level_source" and v is not None:
                    v = SRC_MIRROR.get(v, v)
                elif key in ("role", "previous_role") and v is not None:
                    v = ROLE_MIRROR[v]
                if isinstance(v, float):
                    assert w == pytest.approx(v), (i, key)
                else:
                    assert v == w, (i, key)
            compared += 1
    assert compared > 20


# ---------------------------------------------------------------------------------------------- (j) serialisation through DecisionFeatures
def test_features_serialise_through_decision_features_and_respect_the_causality_guard():
    bars = worlds()["mid_day_segment_breaks"]
    reg = L.LevelRegistry(CFG)
    n_ts = 0
    for i in range(len(bars)):
        ctx = reg.update(bars, i)
        for direction in (1, -1):
            res = L.level_features(ctx, direction, float(bars.c[i]))
            assert res.group == "levels" and res.version == S.GROUP_VERSIONS["levels"]
            df = S.DecisionFeatures.from_results(bars.decision_ts_ns(i), [res])  # raises CausalityError on any future *_ts_ns
            assert all(k.startswith("f_levels__") for k in df.columns)
            assert all(isinstance(v, (int, float, str, bool, type(None))) for v in df.columns.values())
            n_ts += sum(1 for k, v in df.columns.items() if k.endswith("_ts_ns") and v is not None)
    assert n_ts > 0
    # the guard really bites: the same columns judged at an EARLIER decision time must be rejected
    ctx = L.build_level_context(bars, len(bars) - 1, CFG)
    res = L.level_features(ctx, 1, float(bars.c[-1]))
    stamps = [v for k, v in res.values.items() if k.endswith("_ts_ns") and v is not None]
    if stamps:
        with pytest.raises(S.CausalityError):
            S.DecisionFeatures.from_results(min(stamps) - 1, [res])


# ---------------------------------------------------------------------------------------------- warm-up / recursive invariance
WARM_CFG = L.LevelConfig(round_major_step=5.0, round_minor_step=1.0, max_level_age_bars=100)


def same(ctx_a, ctx_b, i_a, i_b):
    a = dataclasses.replace(ctx_a, index=0)
    b = dataclasses.replace(ctx_b, index=0)
    assert i_a >= 0 and i_b >= 0
    assert a == b


def test_min_history_is_explicit_and_derived_from_the_config():
    assert L.min_history_bars(L.LevelConfig()) == L.MIN_HISTORY_BARS
    assert L.LevelConfig().max_level_age_bars < L.MIN_HISTORY_BARS  # memory horizon + lineage margin
    assert L.min_history_bars(WARM_CFG) == 100 + 26 + 1  # horizon + max(M15 swing lineage 17, STRUCT N+2=26, 2) + 1


@pytest.mark.parametrize("t", [400, 433, 520, 611, 650])
def test_warmup_invariance_exact_equality_after_the_documented_warmup(t):
    bars = random_walk(700, 5, bars_per_day=40)
    full = L.build_level_context(bars, t, WARM_CFG)
    assert len(full.levels) > 5
    mh = L.min_history_bars(WARM_CFG)
    for w in sorted({mh, min(240, t + 1), min(500, t + 1), t + 1}):  # 240 / 500 / the full history are all beyond the warm-up
        s = t + 1 - w
        win = L.build_level_context(window(bars, s), t - s, WARM_CFG)
        same(win, full, t - s, t)


@pytest.mark.parametrize("t", [400, 520, 650])
def test_short_windows_exclude_levels_that_began_before_the_window_never_truncate_them(t):
    bars = random_walk(700, 5, bars_per_day=40)
    full = L.build_level_context(bars, t, WARM_CFG)
    for w in (30, 60, 90, 120):  # < MIN_HISTORY_BARS: contexts may MISS levels, but every level shown is a real event inside the window
        s = t + 1 - w
        win = L.build_level_context(window(bars, s), t - s, WARM_CFG)
        start_ns = int(bars.ts_ns[s])
        by_id = {lv.level_id: lv for lv in full.levels}
        for lv in win.levels:
            assert lv.created_at_ts_ns >= start_ns and lv.confirmed_at_ts_ns >= start_ns, (w, lv.source)  # nothing from before the window
            assert lv.age_bars <= t - s
            twin = by_id.get(lv.level_id)
            if twin is not None:  # the very same level (same id) carries the very same state: no silently truncated counts or age
                assert dataclasses.replace(twin, sources_in_cluster=None) == dataclasses.replace(lv, sources_in_cluster=None)


def wilder_atr(h, lo, c, n=14):
    tr = np.maximum(h - lo, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(lo - np.roll(c, 1))))
    tr[0] = h[0] - lo[0]
    out = np.full(len(h), np.nan)
    out[n - 1] = tr[:n].mean()
    for k in range(n, len(h)):
        out[k] = (out[k - 1] * (n - 1) + tr[k]) / n
    return out


def assert_close(a, b, path="ctx"):
    if dataclasses.is_dataclass(a):
        assert type(a) is type(b)
        for f in dataclasses.fields(a):
            assert_close(getattr(a, f.name), getattr(b, f.name), f"{path}.{f.name}")
    elif isinstance(a, (tuple, list)):
        assert len(a) == len(b), path
        for k, (x, y) in enumerate(zip(a, b, strict=True)):
            assert_close(x, y, f"{path}[{k}]")
    elif isinstance(a, float):
        assert b == pytest.approx(a, rel=1e-6, abs=1e-9), path
    else:
        assert a == b, path


def test_float_recursive_atr_inside_a_predeclared_tolerance_after_warmup():
    base = random_walk(700, 5, bars_per_day=40)
    t = 650
    atr_full = wilder_atr(base.h, base.l, base.c)
    full_bars = dataclasses.replace(base, atr=atr_full)
    full = L.build_level_context(full_bars, t, WARM_CFG)
    s = t + 1 - 500
    w = window(base, s)
    win_bars = dataclasses.replace(w, atr=wilder_atr(w.h, w.l, w.c))  # the adapter recomputes ATR from ITS loaded history
    win = L.build_level_context(win_bars, t - s, WARM_CFG)
    assert_close(dataclasses.replace(win, index=0), dataclasses.replace(full, index=0))
