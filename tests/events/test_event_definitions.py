# ruff: noqa: E501, RUF059
"""Event definitions: tiny hand-built fixtures with hand-computed pulse indices, then real data."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.events import kernels_pattern as kp
from alpha.events import kernels_price as kpr
from alpha.events import kernels_state as ks
from alpha.events import kernels_structure as kst
from alpha.events import kernels_zone as kz
from alpha.events import schema
from alpha.events.store import EventParams, build_events
from tests.events._helpers import build_features, same_array, slice_frame

NAN = np.nan


def A(*v):
    return np.array(v, dtype=float)


def I(*v):  # noqa: E743
    return np.array(v, dtype=np.int64)


# ---------------------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------------------


def test_pivots_confirmed_at_p_plus_order():
    high = A(1, 3, 2, 4, 1)
    low = high - 0.5
    ph, ph_o, pl, pl_o = kst.pivot_confirmations(high, low, 1)
    # pivot highs at p=1 (conf 2) and p=3 (conf 4); pivot low at p=2 (conf 3)
    assert np.flatnonzero(np.isfinite(ph)).tolist() == [2, 4]
    assert ph[2] == 3 and ph_o[2] == 1 and ph[4] == 4 and ph_o[4] == 3
    assert np.flatnonzero(np.isfinite(pl)).tolist() == [3]
    assert pl[3] == 1.5 and pl_o[3] == 2


def test_pivot_requires_strict_unique_extreme():
    high = A(1, 3, 3, 1, 0)  # tie -> no pivot high
    ph, _, _, _ = kst.pivot_confirmations(high, high - 1, 1)
    assert not np.isfinite(ph).any()


def test_swing_levels_forward_fill_from_confirmation():
    ph = A(NAN, NAN, 3, NAN, 4)
    ph_o = I(-1, -1, 1, -1, 3)
    pl = A(NAN, NAN, NAN, 1.5, NAN)
    pl_o = I(-1, -1, -1, 2, -1)
    hi, hi_o, lo, lo_o = kst.swing_levels(ph, ph_o, pl, pl_o)
    assert np.array_equal(hi, A(NAN, NAN, 3, 3, 4), equal_nan=True)
    assert np.array_equal(lo, A(NAN, NAN, NAN, 1.5, 1.5), equal_nan=True)
    assert hi_o.tolist() == [-1, -1, 1, 1, 3]


def _bos_fixture():
    n = 10
    ph = np.full(n, NAN)
    ph_o = np.full(n, -1, np.int64)
    pl = np.full(n, NAN)
    pl_o = np.full(n, -1, np.int64)
    ph[2], ph_o[2] = 10.0, 0  # swing high 10 confirmed at bar 2
    pl[4], pl_o[4] = 5.0, 2  # swing low 5 confirmed at bar 4
    ph[8], ph_o[8] = 13.0, 6  # NEW swing high 13 confirmed at bar 8
    close = A(0, 0, 8, 9, 9, 11, 12, 4, 3, 14)
    return close, ph, ph_o, pl, pl_o


def test_bos_one_pulse_per_swing_and_choch():
    close, ph, ph_o, pl, pl_o = _bos_fixture()
    bu, bd, cu, cd, lu, ld, ou, od, structure = kst.bos_choch(close, ph, ph_o, pl, pl_o)
    # bar 5 = first close above 10 (BOS_UP, no CHOCH: no prior structure); bar 6 closes higher
    # but the swing is already broken -> NO second pulse.  bar 7 = first close below 5 (BOS_DN
    # against the up structure = CHOCH_DN).  bar 9 = first close above the new swing 13 after
    # the down structure = CHOCH_UP.
    assert np.flatnonzero(bu).tolist() == [5, 9]
    assert np.flatnonzero(bd).tolist() == [7]
    assert np.flatnonzero(cu).tolist() == [9]
    assert np.flatnonzero(cd).tolist() == [7]
    assert lu[5] == 10 and ou[5] == 0 and lu[9] == 13 and ou[9] == 6
    assert ld[7] == 5 and od[7] == 2
    assert structure.tolist() == [0, 0, 0, 0, 0, 1, 1, -1, -1, 1]


def test_bos_is_close_based_not_intrabar():
    """A wick above the swing without a close above it is no BOS (V1 ``bos`` would fire)."""
    n = 6
    ph = np.full(n, NAN)
    ph_o = np.full(n, -1, np.int64)
    ph[1], ph_o[1] = 10.0, 0
    close = A(0, 0, 9.5, 9.9, 9.99, 9.0)
    pl = np.full(n, NAN)
    bu, *_ = kst.bos_choch(close, ph, ph_o, pl, ph_o.copy())
    assert bu.sum() == 0


def test_bos_needs_a_confirmed_swing_first():
    n = 4
    nan = np.full(n, NAN)
    m1 = np.full(n, -1, np.int64)
    bu, bd, *_ = kst.bos_choch(A(1, 100, 1, 100), nan, m1, nan, m1)
    assert bu.sum() == 0 and bd.sum() == 0


# ---------------------------------------------------------------------------------------
# price
# ---------------------------------------------------------------------------------------


def test_prior_extreme_window_and_run_local():
    low = A(5, 4, 3, 4, 5, 6)
    level, org = kpr.prior_extreme(low, 2, np.zeros(6, np.int64), True)
    assert np.array_equal(level, A(NAN, NAN, 4, 3, 3, 4), equal_nan=True)
    assert org.tolist() == [-1, -1, 1, 2, 2, 3]
    level, _ = kpr.prior_extreme(low, 2, I(0, 0, 0, 3, 3, 3), True)  # run restarts at bar 3
    assert np.array_equal(level, A(NAN, NAN, 4, NAN, NAN, 4), equal_nan=True)


def test_session_prior_extreme_resets_on_new_day():
    level = kpr.session_prior_extreme(A(5, 4, 6, 3, 2), I(0, 0, 0, 1, 1), True)
    assert np.array_equal(level, A(NAN, 5, 4, NAN, 3), equal_nan=True)


def test_sweep_low_wick_below_level_close_back_inside():
    level = A(NAN, 4, 4, 4, 4)
    low = A(5, 3, 3, 5, 3.5)
    close = A(5, 4.5, 3.5, 5, 4.2)
    pulse, evl, evx, org = kpr.sweep_low(low, close, level, np.full(5, 7, np.int64))
    assert pulse.tolist() == [0, 1, 0, 0, 1]  # bar 2 closes below the level: no sweep
    assert evl[1] == 4 and evx[1] == 3 and evx[4] == 3.5 and org[1] == 7
    assert np.isnan(evl[0]) and np.isnan(evx[2])


def _momentum_fixture():
    #        o     h     l     c
    bars = [
        (10.0, 10.5, 9.8, 10.4),
        (10.4, 11.0, 10.3, 10.9),
        (10.9, 12.0, 10.8, 11.8),
        (11.8, 11.9, 11.2, 11.3),
        (11.3, 11.5, 11.0, 11.1),
        (11.1, 11.4, 10.9, 11.2),
        (11.2, 11.7, 11.1, 11.6),
    ]
    o, h, low, c = (np.array(col, dtype=float) for col in zip(*bars, strict=True))
    return o, h, low, c


def test_momentum_resume_up_hand_fixture():
    o, h, low, c = _momentum_fixture()
    atr = np.ones(len(c))
    start = np.zeros(len(c), np.int64)
    pulse, evl = kpr.momentum_resume_up(o, h, low, c, atr, start, 4, 1.0, 1.5)
    assert pulse.tolist() == [0, 0, 0, 0, 0, 0, 1]
    assert evl[6] == pytest.approx(10.9)  # pullback low
    # too deep a pullback (max 0.5 ATR) or run boundary -> no pulse
    assert kpr.momentum_resume_up(o, h, low, c, atr, start, 4, 1.0, 0.5)[0].sum() == 0
    assert kpr.momentum_resume_up(o, h, low, c, atr, I(*[0] * 3, *[4] * 4), 4, 1.0, 1.5)[0].sum() == 0


def test_momentum_resume_mirror_is_involutive():
    o, h, low, c = _momentum_fixture()
    mo, mh, ml, mc = kpr.mirror_bars(*kpr.mirror_bars(o, h, low, c))
    assert np.array_equal(mo, o) and np.array_equal(mh, h) and np.array_equal(ml, low)
    assert np.array_equal(mc, c)


# ---------------------------------------------------------------------------------------
# zones / trendlines
# ---------------------------------------------------------------------------------------


def test_cluster_zone_forms_at_second_pivot_and_new_object_gets_new_zid():
    n = 16
    pl = np.full(n, NAN)
    pl[3], pl[7], pl[9], pl[12] = 10.0, 10.2, 10.1, 20.0
    ph = np.full(n, NAN)
    atr = np.ones(n)
    lo, hi, zid, born = kz.cluster_zones(ph, pl, atr, 0.5, 0.1, 4, 5)
    assert zid.tolist() == [0] * 7 + [1, 1, 2, 2, 2, 2, 2, 0, 0]
    assert born[7] == 7 and born[9] == 9 and born[14] == -1
    assert lo[7] == pytest.approx(9.9) and hi[7] == pytest.approx(10.3)
    assert lo[9] == pytest.approx(9.9) and hi[9] == pytest.approx(10.3)  # same bounds, new object
    assert np.isnan(lo[6]) and np.isnan(lo[14])  # expired after ttl=5 bars (9+5)
    # boundaries are only ever formed from data known at the stamp bar: prefix equality
    lo2, hi2, zid2, _ = kz.cluster_zones(ph[:9], pl[:9], atr[:9], 0.5, 0.1, 4, 5)
    assert np.array_equal(lo2, lo[:9], equal_nan=True) and np.array_equal(zid2, zid[:9])


def test_level_zone_ids_change_with_bounds():
    lo = A(NAN, 1, 1, 2, 2, NAN, 2)
    zid, born = kz.level_zone_ids(lo, lo + 1)
    assert zid.tolist() == [0, 1, 1, 2, 2, 0, 3]
    assert born.tolist() == [-1, 1, 1, 3, 3, -1, 6]


def test_zone_enter_exit_hand_fixture():
    close = A(5, 5, 10, 10, 12, 5, 10)
    n = len(close)
    kb_lo, kb_hi = np.full(n, 9.0), np.full(n, 11.0)
    zid = np.array([0, 1, 1, 1, 1, 1, 1], np.int32)
    born = np.full(n, 2, np.int64)
    enter, leave, org = kz.zone_events(close, kb_lo, kb_hi, zid, born)
    assert enter.tolist() == [0, 0, 1, 0, 0, 0, 1]
    assert leave.tolist() == [0, 0, 0, 0, 1, 0, 0]
    assert org[2] == 2


def _line_fixture():
    n = 12
    nan = np.full(n, NAN)
    m1 = np.full(n, -1, np.int64)
    pl = nan.copy()
    pl_o = m1.copy()
    pl[3], pl_o[3] = 10.0, 1  # 1st anchor: low 10 at bar 1
    pl[7], pl_o[7] = 12.0, 5  # 2nd anchor: low 12 at bar 5, confirmed at bar 7
    return nan, m1, pl, pl_o


def test_trendline_stamped_at_second_confirmation():
    nan, m1, pl, pl_o = _line_fixture()
    lv, slope, side, zid, born = kz.trendline_lines(nan, m1, pl, pl_o, 100)
    assert not np.isfinite(lv[:7]).any() and zid[:7].sum() == 0
    assert lv[7] == pytest.approx(13.0) and lv[8] == pytest.approx(13.5)  # slope 0.5 through (5, 12)
    assert side[7] == 1 and set(zid[7:]) == {1} and born[7] == 7
    # a lower low (descending pair) kills the support line
    pl2 = pl.copy()
    pl2[9], pl_o2 = 11.0, pl_o.copy()
    pl_o2[9] = 7
    lv2, _, side2, _, _ = kz.trendline_lines(nan, np.full(12, -1, np.int64), pl2, pl_o2, 100)
    assert side2[8] == 1 and side2[9] == 0 and np.isnan(lv2[9])


def test_trendline_touch_and_break_hand_fixture():
    nan, m1, pl, pl_o = _line_fixture()
    lv, slope, side, zid, born = kz.trendline_lines(nan, m1, pl, pl_o, 100)
    n = len(lv)
    low = np.full(n, 30.0)
    high = np.full(n, 31.0)
    close = np.full(n, 30.5)
    low[9], close[9] = 14.0, 14.5  # v(9)=14: low touches the line, close above -> TOUCH
    low[10], close[10] = 14.4, 14.6  # still touching (v=14.5) -> no repeated pulse
    low[11], close[11] = 13.0, 14.0  # v(11)=15: close below the line -> BREAK
    atr = np.ones(n)
    touch, brk, evl_t, evl_b = kz.trendline_events(high, low, close, atr, lv, slope, side, born, 0.0)
    assert np.flatnonzero(touch).tolist() == [9]
    assert np.flatnonzero(brk).tolist() == [11]
    assert evl_t[9] == pytest.approx(14.0) and evl_b[11] == pytest.approx(15.0)
    # a 0.25 ATR tolerance makes bar 8 (low 30) still no touch, but widens the break threshold
    touch, brk, *_ = kz.trendline_events(high, low, close, atr, lv, slope, side, born, 0.25)
    assert np.flatnonzero(brk).tolist() == [11]


# ---------------------------------------------------------------------------------------
# patterns
# ---------------------------------------------------------------------------------------


def test_double_bottom_completes_at_confirming_close_with_invalidation():
    n = 14
    high = np.full(n, 12.0)
    low = np.full(n, 10.6)
    close = np.full(n, 11.0)
    high[2], low[2] = 11.0, 10.0
    high[5] = 13.0  # neckline
    low[8] = 10.1
    close[10], close[11], close[12] = 12.0, 13.5, 14.0
    pl = np.full(n, NAN)
    pl_o = np.full(n, -1, np.int64)
    pl[4], pl_o[4] = 10.0, 2
    pl[10], pl_o[10] = 10.1, 8  # 2nd low confirmed at bar 10 (order 2)
    pulse, evl, evx, org = kp.double_bottom(high, low, close, np.ones(n), pl, pl_o, 0.25, 60, 24)
    assert np.flatnonzero(pulse).tolist() == [11]  # first close > 13 after the 2nd confirmation
    assert evl[11] == 10.0 and evx[11] == 13.0 and org[11] == 2
    # lows too far apart -> no pattern
    pl_far = pl.copy()
    pl_far[10] = 9.0
    assert kp.double_bottom(high, low, close, np.ones(n), pl_far, pl_o, 0.25, 60, 24)[0].sum() == 0


def test_inside_bar_break_hand_fixture():
    high = A(10, 9.5, 11, 11.2)
    low = A(8, 8.5, 8.4, 9)
    close = A(9, 9, 10.5, 11)
    pulse, evl, evx, org = kp.inside_bar_break_up(high, low, close, np.zeros(4, np.int64))
    assert pulse.tolist() == [0, 0, 1, 0]
    assert evl[2] == 8 and evx[2] == 10 and org[2] == 0


# ---------------------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------------------


def test_trend_states_need_slope_sign_and_adx():
    slope = A(NAN, 0.3, -0.3, 0.3, 0.0)
    adx = A(30, 30, 30, 10, 30)
    up, dn = ks.trend_states(slope, adx, 20.0)
    assert up.tolist() == [0, 1, 0, 0, 0] and dn.tolist() == [0, 0, 1, 0, 0]
    assert up.dtype == np.int8


# ---------------------------------------------------------------------------------------
# EventSet over real data
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real():
    frame = slice_frame("2025-03-17", "2025-04-04")  # spans the spring DST change
    feats = build_features(frame)
    return feats, build_events(feats)


def test_array_names_dtypes_and_flags_match_registry(real):
    feats, ev = real
    n = len(feats["c"])
    assert set(ev) == set(schema.all_array_names())
    for name, arr in ev.items():
        assert arr.shape == (n,), name
        assert arr.dtype == np.dtype(schema.PREFIX_DTYPE[name.split("_", 1)[0]]), name
        if name.startswith("ev_"):
            assert set(np.unique(arr)) <= {0, 1}
        if name.startswith(("st_", "zid_", "evo_", "valid_")):
            assert arr.dtype.kind in "iub"
    assert ev.metadata["registry_fingerprint"] == schema.registry_fingerprint()


def test_pulse_companions_defined_exactly_where_the_pulse_is(real):
    _, ev = real
    checked = 0
    for d in schema.all_events():
        if not d.has_arrays or d.kind != "pulse":
            continue
        for tf in d.tfs:
            for variant in d.variants():
                names = schema.array_names(d.name, tf, variant)
                pulse = ev[names[0]].astype(bool)
                for name, prefix in zip(names[1:], d.companions, strict=True):
                    arr = ev[name]
                    if prefix == "evo":
                        assert (arr[~pulse] == -1).all(), name
                    else:
                        assert np.isfinite(arr[pulse]).all(), name
                        assert np.isnan(arr[~pulse]).all(), name
                checked += 1
    assert checked > 100


def test_pulses_are_zero_where_invalid(real):
    _, ev = real
    for d in schema.all_events():
        if not d.has_arrays or d.kind != "pulse":
            continue
        for tf in d.tfs:
            valid = ev[schema.valid_array_name(tf)]
            for variant in d.variants():
                main = ev[schema.array_names(d.name, tf, variant)[0]]
                assert not main[~valid].any(), (d.name, tf, variant)


def test_m5_swing_levels_equal_v1_swing_arrays(real):
    feats, ev = real
    assert same_array(ev["lv_m5_swing_high_lvl"], feats["last_swing_high"])
    assert same_array(ev["lv_m5_swing_low_lvl"], feats["last_swing_low"])
    # confirmation pulses = the bars where the V1 level changes to a NEW pivot
    conf = ev["ev_m5_swing_high_conf"].astype(bool)
    assert (ev["evl_m5_swing_high_conf"][conf] == feats["last_swing_high"][conf]).all()


def test_sweeps_match_v1_sweep_arrays(real):
    feats, ev = real
    # V1 sweep_lo_20 / sweep_hi_20 / sweep_pdl / sweep_pdh (NaN = unknown) are the same events
    # on M5 wherever the M5 ATR (the validity mask) is known
    valid = ev["valid_m5"]
    for v1, name in (
        ("sweep_lo_20", "ev_m5_sweep_low_prior20"),
        ("sweep_hi_20", "ev_m5_sweep_high_prior20"),
        ("sweep_pdl", "ev_m5_sweep_low_pdl"),
        ("sweep_pdh", "ev_m5_sweep_high_pdh"),
    ):
        expect = np.nan_to_num(feats[v1], nan=0.0).astype(np.uint8) * valid
        assert same_array(ev[name], expect.astype(np.uint8)), name


def test_mirror_symmetry_of_long_short_pairs(real):
    """Running the builder on the price mirror maps every SHORT event onto its LONG partner."""
    feats, ev = real
    mirrored = dict(feats)
    mirrored["o"], mirrored["h"], mirrored["l"], mirrored["c"] = (
        -feats["o"],
        -feats["l"],
        -feats["h"],
        -feats["c"],
    )
    mirrored["previous_day_low"] = -feats["previous_day_high"]
    mirrored["previous_day_high"] = -feats["previous_day_low"]
    mev = build_events(type(feats)(mirrored, feats.metadata))
    pairs = 0
    for d in schema.all_events():
        if not d.has_arrays or d.kind != "pulse" or d.name.startswith(("ZONE", "TRENDLINE")):
            continue
        if d.mirror is None or d.name.startswith("SWING"):
            continue
        for tf in d.tfs:
            for variant in d.variants():
                a = schema.array_names(d.name, tf, variant)
                b = schema.array_names(
                    d.mirror, tf, schema.mirror_variant(d.name, variant)
                )
                assert same_array(ev[a[0]], mev[b[0]]), (a[0], b[0])
                for x, y in zip(a[1:], b[1:], strict=True):
                    if x.startswith("evo"):
                        assert same_array(ev[x], mev[y]), (x, y)
                    else:
                        assert same_array(ev[x], -mev[y]), (x, y)
                pairs += 1
    assert pairs >= 60
    # swing confirmation pairs
    assert same_array(ev["ev_m15_swing_low_conf"], mev["ev_m15_swing_high_conf"])


def test_build_is_deterministic(real):
    feats, ev = real
    again = build_events(feats)
    assert set(again) == set(ev)
    assert all(same_array(ev[n], again[n]) for n in ev)


def test_params_change_events():
    feats = build_features(slice_frame("2025-03-17", "2025-03-21"))
    base = build_events(feats)
    other = build_events(feats, EventParams(swing_order=2))
    assert not same_array(base["ev_m5_swing_low_conf"], other["ev_m5_swing_low_conf"])
