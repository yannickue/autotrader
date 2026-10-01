# ruff: noqa: E501
"""Swing market-structure state: prefix invariance, confirmation timing, labels, sequences, segments, mirror symmetry, EMA parity."""

from __future__ import annotations

import math
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from market_observer import schema as S
from market_observer import swings as W

NS = 1_000_000_000
T0 = 1_767_571_200 * NS  # 2026-01-05 00:00:00 UTC, aligned to the 15-minute grid


def _mk(ts, o, h, l, c, atr, seg, tick=0.01) -> S.ObserverBars:  # noqa: E741
    n = len(ts)
    return S.ObserverBars(
        "T", np.asarray(ts, dtype=np.int64), np.asarray(o, float), np.asarray(h, float), np.asarray(l, float), np.asarray(c, float),
        np.full(n, 50.0), np.full(n, 0.02), np.asarray(atr, float), np.asarray(seg, dtype=np.int64), np.zeros(n, dtype=np.int64),
        np.zeros(n, dtype=np.int64), tick, S.SessionSpec("UTC", None, None),
    )


def zigzag(pivots: list[float], leg: int = 4, atr: float = 1.0, half_range: float = 0.1, start_ns: int = T0, tick: float = 0.01) -> S.ObserverBars:
    """Linear legs between pivots (first/last pivot are dummies that can never be swings). h/l = path +- half_range."""
    path = [pivots[0]]
    for a, b in pairwise(pivots):
        path += [a + (b - a) * k / leg for k in range(1, leg + 1)]
    p = np.array(path)
    n = len(p)
    ts = start_ns + np.arange(n, dtype=np.int64) * 300 * NS
    return _mk(ts, p, p + half_range, p - half_range, p, np.full(n, atr), np.zeros(n), tick)


def random_walk(n: int, seed: int, breaks: tuple[int, ...] = (), warm: int = 14) -> S.ObserverBars:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.4, n)
    c = 100 + np.cumsum(steps)
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.15, n))
    lo = np.minimum(o, c) - np.abs(rng.normal(0, 0.15, n))
    seg = np.zeros(n, dtype=np.int64)
    ts = np.zeros(n, dtype=np.int64)
    t = T0
    s = 0
    for j in range(n):
        if j in breaks:
            s += 1
            t += (36 + 1) * 300 * NS  # gap of 36 bars + one 5-minute offset => breaks the M15 grid alignment of the next segment
        ts[j] = t
        seg[j] = s
        t += 300 * NS
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - lo, np.maximum(abs(h - pc), abs(lo - pc)))
    atr = np.full(n, np.nan)
    for j in range(warm - 1, n):
        atr[j] = tr[max(0, j - warm + 1) : j + 1].mean()
    return _mk(ts, o, h, lo, c, atr, seg)


def mirror(b: S.ObserverBars) -> S.ObserverBars:
    return _mk(b.ts_ns, -b.o, -b.l, -b.h, -b.c, b.atr, b.segment_id, b.tick_size)


def _state_at_end(b: S.ObserverBars, tf: str = "M5") -> W.SwingStructureState:
    st = W.swing_state(b, len(b) - 1, tf)
    assert st is not None
    return st


# ----------------------------------------------------------------------------------------------- (a) prefix invariance
@pytest.mark.parametrize("seed,breaks", [(1, ()), (2, ()), (3, (150, 330)), (4, (90, 91, 260))])
def test_prefix_invariance_functional(seed, breaks):
    b = random_walk(420, seed, breaks)
    for i in [*range(0, 420, 13), 419]:
        assert W.swing_features(b, i) == W.swing_features(b.prefix(i + 1), i), i
        for tf in ("M5", "M15"):
            assert W.swing_state(b, i, tf) == W.swing_state(b.prefix(i + 1), i, tf)


def test_functional_and_incremental_replay_agree():
    b = random_walk(500, 7, (140, 141, 300))
    rp = W.SwingReplay(b)
    for i in range(0, 500, 3):
        assert rp.features(i) == W.swing_features(b, i), i
        for tf in ("M5", "M15"):
            assert rp.state(i, tf) == W.swing_state(b, i, tf)


# ----------------------------------------------------------------------------------------------- (b) confirmation timing
def test_m5_swing_never_visible_before_j_plus_n_close():
    # dummy start, LOW pivot, HIGH pivot, dummy end ; leg 4 => pivot low at bar 4, pivot high at bar 8
    b = zigzag([20.0, 10.0, 30.0, 15.0])
    j = 4
    assert W.SWING_N == 2
    for i in range(len(b)):
        st = W.swing_state(b, i, "M5")
        assert st is not None
        if i < j + W.SWING_N:
            assert st.last_low_price is None and st.confirmed_at_ts_ns is None
        else:
            assert st.last_low_price == pytest.approx(10.0 - 0.1)
            assert st.last_low_confirmed_at_ts_ns == b.decision_ts_ns(j + W.SWING_N)
    st = W.swing_state(b, j + W.SWING_N, "M5")
    assert st.structure_age_bars == 0 and st.structure_age_minutes == 0.0


def test_m15_swing_usable_only_after_m15_close_plus_n_m15_bars():
    # M15 grid: leg of 12 M5 bars so that pivots sit on whole M15 bars; start aligned on the grid
    b = zigzag([20.0, 10.0, 30.0, 15.0], leg=12)
    j = 12  # M5 index of the pivot low; M15 group g = j // 3 = 4
    g = j // 3
    last_bar_of_confirming_group = 3 * (g + W.SWING_N) + 2
    for i in range(len(b)):
        st = W.swing_state(b, i, "M15")
        assert st is not None
        if i < last_bar_of_confirming_group:
            assert st.last_low_price is None
        else:
            assert st.last_low_price == pytest.approx(10.0 - 0.1)
            # = M15 bar open + (n+1) M15 bars = close of the n-th M15 bar after the swing bar
            assert st.last_low_confirmed_at_ts_ns == b.decision_ts_ns(last_bar_of_confirming_group)
            assert st.last_low_confirmed_at_ts_ns == int(b.ts_ns[3 * g]) + (W.SWING_N + 1) * 900 * NS


# ----------------------------------------------------------------------------------------------- (c) future never changes the past
def test_future_bars_never_change_confirmed_state():
    b = random_walk(400, 11, (200,))
    for i in range(0, 380, 17):
        base = W.swing_features(b, i)
        for extra in (5, 40, 399 - i):
            longer = b.prefix(min(len(b), i + 1 + extra))
            assert W.swing_features(longer, i) == base


def test_confirmed_labels_are_immutable_when_atr_changes_later():
    b = zigzag([20.0, 10.0, 30.0, 12.0, 30.04, 15.0])  # last high is EQ to the previous one (diff 0.04 <= 0.05 * ATR 1.0)
    base = W.swing_state(b, len(b) - 1, "M5")
    assert base.high_label == W.SwingLabel.EQ
    atr2 = b.atr.copy()
    atr2[-2:] = 0.001  # later ATR collapse (after confirmation) must not relabel the formed pair (tolerance would fall to one tick)
    b2 = _mk(b.ts_ns, b.o, b.h, b.l, b.c, atr2, b.segment_id)
    assert W.swing_state(b2, len(b) - 1, "M5").high_label == W.SwingLabel.EQ


# ----------------------------------------------------------------------------------------------- (d) labels
def _labels(pivots, **kw):
    st = _state_at_end(zigzag(pivots, **kw))
    return st.high_label, st.low_label


def test_hh_hl_labels():
    # dummy, L, H, L, H, dummy : highs 20 -> 25 (HH); lows 10 -> 12 (HL)
    assert _labels([18.0, 10.0, 20.0, 12.0, 25.0, 15.0]) == (W.SwingLabel.HH, W.SwingLabel.HL)


def test_lh_ll_labels():
    assert _labels([18.0, 10.0, 25.0, 8.0, 20.0, 15.0]) == (W.SwingLabel.LH, W.SwingLabel.LL)


def test_flat_tops_and_bottoms_are_eq_within_tolerance():
    tol = W.EQ_TOL_ATR * 1.0  # ATR = 1.0 in the hand-built series
    assert tol >= 0.01
    h, lo = _labels([18.0, 10.0, 20.0, 10.0 + 0.5 * tol, 20.0 + 0.5 * tol, 15.0])
    assert (h, lo) == (W.SwingLabel.EQ, W.SwingLabel.EQ)
    h, lo = _labels([18.0, 10.0, 20.0, 10.0 + 2 * tol, 20.0 + 2 * tol, 15.0])
    assert (h, lo) == (W.SwingLabel.HH, W.SwingLabel.HL)


def test_eq_tolerance_is_at_least_one_tick():
    # ATR tiny => tolerance falls back to one tick
    b = zigzag([18.0, 10.0, 20.0, 10.0, 20.0 + 0.01, 15.0], atr=0.01, tick=0.01)
    st = _state_at_end(b)
    assert st.high_label == W.SwingLabel.EQ
    b = zigzag([18.0, 10.0, 20.0, 10.0, 20.0 + 0.05, 15.0], atr=0.01, tick=0.01)
    assert _state_at_end(b).high_label == W.SwingLabel.HH


def test_deltas_in_atr_at_decision_bar():
    b = zigzag([18.0, 10.0, 20.0, 12.0, 25.0, 15.0], atr=2.0)
    st = _state_at_end(b)
    assert st.high_delta_atr == pytest.approx((25.0 - 20.0) / 2.0)
    assert st.low_delta_atr == pytest.approx((12.0 - 10.0) / 2.0)


# ----------------------------------------------------------------------------------------------- (e) sequences
def test_every_sequence_outcome():
    assert _state_at_end(zigzag([18.0, 10.0, 20.0, 12.0, 25.0, 15.0])).sequence == W.SwingSequence.UP_SEQUENCE
    assert _state_at_end(zigzag([18.0, 10.0, 25.0, 8.0, 20.0, 15.0])).sequence == W.SwingSequence.DOWN_SEQUENCE
    # HH with LL (expansion) and LH with HL (contraction) are both MIXED_TRANSITION
    assert _state_at_end(zigzag([18.0, 10.0, 20.0, 8.0, 25.0, 15.0])).sequence == W.SwingSequence.MIXED_TRANSITION
    assert _state_at_end(zigzag([18.0, 10.0, 25.0, 12.0, 20.0, 15.0])).sequence == W.SwingSequence.MIXED_TRANSITION
    # EQ on either side
    assert _state_at_end(zigzag([18.0, 10.0, 20.0, 12.0, 20.0, 15.0])).sequence == W.SwingSequence.RANGE_OR_UNDEFINED
    # insufficient swings (one high, one low)
    st = _state_at_end(zigzag([18.0, 10.0, 20.0, 15.0]))
    assert st.sequence == W.SwingSequence.RANGE_OR_UNDEFINED and st.high_label is None and st.sequence_length == 0
    # no swings at all
    flat = zigzag([10.0, 10.0])
    st = _state_at_end(flat)
    assert st.sequence == W.SwingSequence.RANGE_OR_UNDEFINED and st.confirmed_at_ts_ns is None and st.structure_age_bars is None


def test_warmup_none_when_atr_unavailable():
    b = zigzag([18.0, 10.0, 20.0, 12.0, 25.0, 15.0])
    atr = b.atr.copy()
    atr[-1] = np.nan
    nb = _mk(b.ts_ns, b.o, b.h, b.l, b.c, atr, b.segment_id)
    assert W.swing_state(nb, len(nb) - 1, "M5") is None
    r = W.swing_features(nb, len(nb) - 1)
    assert r.group == "swings" and r.values["m5_sequence"] is None and r.values["m5_confirmed_at_ts_ns"] is None
    assert W.swing_state(b, len(b) - 1, "M5") is not None


def test_sequence_length_counts_consecutive_same_direction_confirmations():
    # lows 10,12,14 (HL,HL) highs 20,25,30 (HH,HH): 4 labelled events in a row, all up
    st = _state_at_end(zigzag([18.0, 10.0, 20.0, 12.0, 25.0, 14.0, 30.0, 20.0]))
    assert st.sequence == W.SwingSequence.UP_SEQUENCE and st.sequence_length == 4
    # a lower high interrupts: ... highs 20,25,22 -> latest label LH; lows 10,12 -> HL => MIXED, length 0
    st = _state_at_end(zigzag([18.0, 10.0, 20.0, 12.0, 25.0, 14.0, 22.0, 20.0]))
    assert st.sequence == W.SwingSequence.MIXED_TRANSITION and st.sequence_length == 0


# ----------------------------------------------------------------------------------------------- (f) segment reset
def test_segment_break_resets_sequence_no_cross_gap_comparison():
    up = zigzag([18.0, 10.0, 20.0, 12.0, 25.0, 15.0])
    gap = T0 + (len(up) + 50) * 300 * NS
    dn = zigzag([18.0, 10.0, 20.0, 12.0, 25.0, 15.0], start_ns=gap)
    ts = np.concatenate([up.ts_ns, dn.ts_ns])
    cat = lambda f: np.concatenate([getattr(up, f), getattr(dn, f)])  # noqa: E731
    seg = np.concatenate([np.zeros(len(up)), np.ones(len(dn))])
    b = _mk(ts, cat("o"), cat("h"), cat("l"), cat("c"), cat("atr"), seg)
    k = len(up)
    # right after the break nothing from segment 0 is visible
    for i in range(k, k + 8):
        st = W.swing_state(b, i, "M5")
        assert st.last_low_price is None or st.last_low_confirmed_at_ts_ns >= int(b.ts_ns[k])
    end = W.swing_state(b, len(b) - 1, "M5")
    alone = W.swing_state(dn, len(dn) - 1, "M5")
    assert (end.sequence, end.high_label, end.low_label, end.sequence_length, end.last_high_price, end.prev_high_price) == (
        alone.sequence, alone.high_label, alone.low_label, alone.sequence_length, alone.last_high_price, alone.prev_high_price)
    # first swing of the new segment has no predecessor even though the old segment had swings of that kind
    early = W.swing_state(b, k + 4 + 2, "M5")
    assert early.last_low_price is not None and early.prev_low_price is None and early.low_label is None


# ----------------------------------------------------------------------------------------------- (g) mirror symmetry
_LABEL_MIRROR = {"HH": "LL", "LL": "HH", "HL": "LH", "LH": "HL", "EQ": "EQ", None: None}
_SEQ_MIRROR = {"UP_SEQUENCE": "DOWN_SEQUENCE", "DOWN_SEQUENCE": "UP_SEQUENCE", "MIXED_TRANSITION": "MIXED_TRANSITION",
               "RANGE_OR_UNDEFINED": "RANGE_OR_UNDEFINED", None: None}


def _neg(x):
    return None if x is None else -x


@pytest.mark.parametrize("src", ["zig", "walk", "walk_breaks"])
def test_long_short_mirror_symmetry(src):
    b = {"zig": zigzag([18.0, 10.0, 20.0, 12.0, 25.0, 14.0, 22.0, 9.0, 21.0, 15.0]),
         "walk": random_walk(400, 5), "walk_breaks": random_walk(400, 6, (120, 250))}[src]
    m = mirror(b)
    for i in range(0, len(b), 7):
        for tf in ("M5", "M15"):
            a, z = W.swing_state(b, i, tf), W.swing_state(m, i, tf)
            if a is None:
                assert z is None
                continue
            assert z.sequence == _SEQ_MIRROR[a.sequence]
            assert z.high_label == _LABEL_MIRROR[a.low_label] and z.low_label == _LABEL_MIRROR[a.high_label]
            assert z.sequence_length == a.sequence_length and z.confirmed_at_ts_ns == a.confirmed_at_ts_ns
            assert z.structure_age_bars == a.structure_age_bars
            for x, y in ((z.high_delta_atr, _neg(a.low_delta_atr)), (z.low_delta_atr, _neg(a.high_delta_atr)),
                         (z.close_beyond_last_swing_atr_high, _neg(a.close_beyond_last_swing_atr_low)),
                         (z.close_beyond_last_swing_atr_low, _neg(a.close_beyond_last_swing_atr_high))):
                assert (x is None and y is None) or x == pytest.approx(y, abs=1e-9)
            assert z.bars_since_beyond_high == a.bars_since_beyond_low and z.bars_since_beyond_low == a.bars_since_beyond_high
            assert z.last_high_price == pytest.approx(_neg(a.last_low_price)) if a.last_low_price is not None else z.last_high_price is None
        fa, fz = W.swing_features(b, i).values["ema_trend"], W.swing_features(m, i).values["ema_trend"]
        assert fz == {"up": "down", "down": "up", "flat": "flat", None: None}[fa]


def test_ema_swing_agreement_table():
    up, dn, mx, rg = W.SwingSequence.UP_SEQUENCE, W.SwingSequence.DOWN_SEQUENCE, W.SwingSequence.MIXED_TRANSITION, W.SwingSequence.RANGE_OR_UNDEFINED
    f = W.ema_swing_agreement
    assert f("up", up) == W.EmaSwingAgreement.AGREE_UP and f("down", dn) == W.EmaSwingAgreement.AGREE_DOWN
    assert f("up", dn) == W.EmaSwingAgreement.DISAGREE and f("down", up) == W.EmaSwingAgreement.DISAGREE
    for e in ("up", "down", "flat", None):
        assert f(e, mx) == W.EmaSwingAgreement.UNDEFINED and f(e, rg) == W.EmaSwingAgreement.UNDEFINED and f(e, None) == W.EmaSwingAgreement.UNDEFINED
    assert f("flat", up) == W.EmaSwingAgreement.UNDEFINED and f(None, dn) == W.EmaSwingAgreement.UNDEFINED


# ----------------------------------------------------------------------------------------------- close beyond last swing
def test_close_beyond_last_swing_definition():
    # last confirmed high = 25 + 0.1 ; afterwards the path goes on up past it
    b = zigzag([18.0, 10.0, 20.0, 12.0, 25.0, 22.0, 40.0], leg=4, atr=2.0)
    j_conf = 4 * 4 + W.SWING_N  # pivot high at bar 16, confirmed at bar 18
    i = len(b) - 1
    st = W.swing_state(b, i, "M5")
    assert st.last_high_price == pytest.approx(25.1)
    # at the confirmation bar the close (23.0 + ...) is still below
    assert W.swing_state(b, j_conf, "M5").close_beyond_last_swing_atr_high == 0.0
    assert W.swing_state(b, j_conf, "M5").bars_since_beyond_high is None
    assert st.close_beyond_last_swing_atr_high == pytest.approx((b.c[i] - 25.1) / 2.0)
    first = next(k for k in range(j_conf + 1, i + 1) if b.c[k] > 25.1)
    assert st.bars_since_beyond_high == i - first
    assert st.close_beyond_last_swing_atr_low == 0.0 and st.bars_since_beyond_low is None


# ----------------------------------------------------------------------------------------------- (h) EMA parity
def test_ema_trend_parity_with_repo_snapshot_diagnostic():
    from demo.opportunity.snapshot import _ema as repo_ema
    from demo.opportunity.snapshot import build_context

    b = random_walk(330, 21)
    for i in (5, 7, 8, 19, 20, 21, 60, 199, 200, 260, 329):
        c = b.c[: i + 1]
        fast, slow = repo_ema(c[-200:], 8), repo_ema(c[-200:], 21)
        got = W.ema_values(b, i)
        if fast is None or slow is None:
            assert got is None and W.ema_trend_state(b, i) is None
        else:
            assert got == (pytest.approx(fast, abs=1e-12), pytest.approx(slow, abs=1e-12))
            expected = "up" if fast > slow else "down" if fast < slow else "flat"
            assert W.ema_trend_state(b, i) == expected
    # and the repo's full context builder (M5 trend), tolerance: exact label equality
    for i in (40, 120, 329):
        frame = pd.DataFrame({"ts": pd.to_datetime(b.ts_ns[: i + 1], unit="ns", utc=True), "open": b.o[: i + 1], "high": b.h[: i + 1],
                              "low": b.l[: i + 1], "close": b.c[: i + 1]})
        decision = pd.Timestamp(b.decision_ts_ns(i), unit="ns", tz="UTC").to_pydatetime()
        assert build_context(frame, decision, "UTC")["M5"]["trend"] == W.ema_trend_state(b, i)


def test_ema_values_match_alpha_frame_ema_within_window_tolerance():
    from alpha.common.frame import Frame

    b = random_walk(400, 22)
    df = pd.DataFrame({"ts": pd.to_datetime(b.ts_ns, unit="ns", utc=True), "open": b.o, "high": b.h, "low": b.l, "close": b.c,
                       "spread_pts": np.zeros(len(b))})
    fr = Frame.from_dataframe(df)
    e8, e21 = fr.ema(8), fr.ema(21)
    # Frame.ema runs over the full history, the observer over the last 200 closes (as the repo snapshot does): the seed influence is
    # (1-2/22)^199 ~ 6e-9 of the seed gap, so the tolerance is 1e-6 in price units
    for i in (250, 300, 399):
        fast, slow = W.ema_values(b, i)
        assert fast == pytest.approx(e8[i], abs=1e-6) and slow == pytest.approx(e21[i], abs=1e-6)
        assert W.ema_trend_state(b, i) == ("up" if e8[i] > e21[i] else "down" if e8[i] < e21[i] else "flat")


# ----------------------------------------------------------------------------------------------- (i) serialisation + causality guard
def test_serialises_through_decision_features_with_causality_guard():
    b = random_walk(300, 31, (150,))
    for i in (60, 149, 151, 299):
        res = W.swing_features(b, i)
        assert res.group == "swings" and res.version == S.GROUP_VERSIONS["swings"]
        df = S.DecisionFeatures.from_results(b.decision_ts_ns(i), [res])
        assert all(k.startswith("f_swings__") for k in df.columns)
        ts_cols = [k for k in df.columns if k.endswith(S.TS_SUFFIX)]
        assert ts_cols and all(df.columns[k] is None or int(df.columns[k]) <= df.decision_ts_ns for k in ts_cols)
        assert all(isinstance(v, (float, int, str, bool)) or v is None for v in df.columns.values())
        assert not any(isinstance(v, float) and math.isnan(v) for v in df.columns.values())
        assert not any(k.startswith("y_") for k in df.columns)
    # a timestamp from the future must be rejected by the guard
    with pytest.raises(S.CausalityError):
        S.DecisionFeatures(10, {"f_swings__m5_confirmed_at_ts_ns": 11}, {})


def test_feature_names_cover_both_timeframes_and_ema():
    names = set(W.swing_features(random_walk(200, 3), 150).values)
    for p in ("m5", "m15"):
        for n in ("sequence", "high_label", "low_label", "sequence_length", "structure_age_bars", "structure_age_minutes", "high_delta_atr",
                  "low_delta_atr", "confirmed_at_ts_ns", "last_high_confirmed_at_ts_ns", "last_low_confirmed_at_ts_ns",
                  "close_beyond_last_swing_atr_high", "close_beyond_last_swing_atr_low", "bars_since_beyond_high", "bars_since_beyond_low"):
            assert f"{p}_{n}" in names
    assert {"ema_trend", "ema_swing_agreement"} <= names
    assert all("__" not in n for n in names)


# ----------------------------------------------------------------------------------------------- (j) chunked-restart determinism
def test_chunked_restart_determinism():
    b = random_walk(450, 41, (200, 330))
    full = [W.swing_features(b, i) for i in range(0, 450, 5)]
    # a restart on a prefix that ends at a different chunk boundary + a freshly built replay object must give identical rows
    for cut in (137, 301, 449):
        part = b.prefix(cut + 1)
        rp = W.SwingReplay(part)
        for i in range(0, cut + 1, 5):
            assert rp.features(i) == full[i // 5]
    rp1, rp2 = W.SwingReplay(b), W.SwingReplay(b)
    assert [rp1.features(i) for i in range(449, -1, -37)] == [rp2.features(i) for i in range(449, -1, -37)][::1]


# ----------------------------------------------------------------------------------------------- (k) constants / definition hash
def test_constants_and_definition_hash_are_pinned():
    assert W.SWING_N == 2 and W.EQ_TOL_ATR == 0.05
    assert W.EMA_FAST == 8 and W.EMA_SLOW == 21 and W.EMA_WINDOW == 200
    assert W.GROUP == "swings" and S.GROUP_VERSIONS["swings"] == "mso-swings-1"
    assert W.LOOKBACK_BARS == 480 and W.MIN_HISTORY_BARS == 480 and W.MIN_HISTORY_BARS >= W.EMA_WINDOW
    assert W.definition_hash() == "eb1857c684e21d19"



def test_detector_is_pluggable_but_default_is_the_repo_detector():
    from demo.structure import confirmed_swings

    calls = []

    def wrapped(frame, n, timeframe):
        calls.append(timeframe)
        return confirmed_swings(frame, n, timeframe)

    b = random_walk(200, 8)
    assert W.swing_features(b, 150, wrapped) == W.swing_features(b, 150)
    assert set(calls) == {"M5", "M15"}
    assert W.SwingReplay(b, wrapped).features(150) == W.swing_features(b, 150)


# ----------------------------------------------------------------------------------------------- recursive / warm-up invariance
def tail(b: S.ObserverBars, t: int, load: int) -> tuple[S.ObserverBars, int]:
    """Only the ``load`` bars up to and including ``t`` are loaded; returns (bars, decision index inside them)."""
    lo = max(0, t + 1 - load)
    sl = slice(lo, t + 1)
    out = S.ObserverBars(b.market, b.ts_ns[sl], b.o[sl], b.h[sl], b.l[sl], b.c[sl], b.tick_volume[sl], b.spread[sl], b.atr[sl], b.segment_id[sl],
                         b.local_minute[sl], b.local_day[sl], b.tick_size, b.session, b.bar_seconds)
    return out, t - lo


_T_POINTS = (700, 1100, 1450)


@pytest.mark.parametrize("seed", [51, 52])
def test_warmup_invariance_exact_from_min_history(seed):
    b = random_walk(1500, seed)
    for t in _T_POINTS:
        ref = W.swing_features(b, t)
        for load in (W.MIN_HISTORY_BARS, 500, 700, t + 1):
            nb, j = tail(b, t, load)
            assert W.history_sufficient(j)
            assert W.swing_features(nb, j) == ref, (t, load)
            assert W.SwingReplay(nb).features(j) == ref


def test_warmup_invariance_with_segment_break_inside_the_loaded_history():
    b = random_walk(1500, 53, (1000,))
    for t in (1300, 1450):
        ref = W.swing_features(b, t)
        for load in (W.MIN_HISTORY_BARS, 700, t + 1):
            nb, j = tail(b, t, load)
            assert W.swing_features(nb, j) == ref


_SKIP = {"ema_trend", "ema_swing_agreement"}


@pytest.mark.parametrize("load", [120, 240])
def test_short_history_only_loses_information_never_changes_values(load):
    """Below MIN_HISTORY_BARS differences are allowed ONLY as 'unknown': None / RANGE_OR_UNDEFINED / shorter sequence_length."""
    b = random_walk(1500, 54)
    n_diff = 0
    for t in range(600, 1450, 25):
        ref = W.swing_features(b, t).values
        nb, j = tail(b, t, load)
        got = W.swing_features(nb, j).values
        for k, v in got.items():
            if k in _SKIP or v == ref[k]:
                continue
            n_diff += 1
            if k.endswith("_sequence_length"):
                assert v <= ref[k], (t, k, v, ref[k])
            else:
                assert v is None or v == "RANGE_OR_UNDEFINED", (t, k, v, ref[k])
    assert n_diff > 0 or load >= 240  # the 120-bar load must actually exercise the unknown path


def test_ema_history_independence_exact_from_window_and_bounded_below():
    b = random_walk(1500, 55)
    worst = 0.0
    for t in _T_POINTS:
        ref = W.ema_values(b, t)
        for load in (W.EMA_WINDOW, 240, 500, t + 1):
            nb, j = tail(b, t, load)
            assert W.ema_values(nb, j) == ref  # finite-memory definition: EXACT once EMA_WINDOW closes are loaded
        nb, j = tail(b, t, 120)  # < EMA_WINDOW: real difference by the seed influence; bounded and documented
        got = W.ema_values(nb, j)
        worst = max(worst, abs(got[0] - ref[0]), abs(got[1] - ref[1]))
        nb, j = tail(b, t, W.EMA_SLOW - 1)
        assert W.ema_values(nb, j) is None  # fewer than EMA_SLOW closes => None, never a guess
    assert 0.0 < worst < 5e-2  # the seed gap decays like (1-2/22)^(200-120)


def _wilder_atr(h, lo, c, n=14):
    pc = np.concatenate([[np.nan], c[:-1]])
    tr = np.fmax(h - lo, np.fmax(abs(h - pc), abs(lo - pc)))
    tr[0] = h[0] - lo[0]
    return pd.Series(tr).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().to_numpy()


def test_atr_normalised_deltas_inherit_the_suppliers_atr_warmup_error_and_it_decays():
    """If the adapter recomputes a Wilder ATR on only the loaded history, the ATR-normalised deltas differ slightly (infinite memory)."""
    b0 = random_walk(1500, 56)
    full = _mk(b0.ts_ns, b0.o, b0.h, b0.l, b0.c, _wilder_atr(b0.h, b0.l, b0.c), b0.segment_id)
    tol = {120: 5e-3, 240: 1e-6, 500: 1e-9}  # relative; (1-1/14)^(load-14): 3e-4 / 6e-8 / ~1e-16 of the seed error
    for load, rel in tol.items():
        worst = 0.0
        for t in _T_POINTS:
            ref = W.swing_state(full, t, "M5")
            nb, j = tail(b0, t, load)
            nb = _mk(nb.ts_ns, nb.o, nb.h, nb.l, nb.c, _wilder_atr(nb.h, nb.l, nb.c), nb.segment_id)
            got = W.swing_state(nb, j, "M5")
            for name in ("high_delta_atr", "low_delta_atr"):
                a, g = getattr(ref, name), getattr(got, name)
                if a is not None and g is not None:
                    worst = max(worst, abs(g - a) / max(abs(a), 1e-9))
        assert worst <= rel, (load, worst)


# ----------------------------------------------------------------------------------------------- HTF (M15) closed-bar alignment
def test_m15_uses_only_completely_closed_bars_at_every_m5_offset():
    b = random_walk(600, 61, (200, 401))
    identity = ("last_high_price", "prev_high_price", "last_low_price", "prev_low_price", "last_high_confirmed_at_ts_ns",
                "last_low_confirmed_at_ts_ns", "high_label", "low_label", "sequence", "sequence_length", "confirmed_at_ts_ns")
    grid = 900 * NS
    for i in range(60, 600):
        st = W.swing_state(b, i, "M15")
        if st is None:
            continue
        # position of M5 bar i inside its M15 bar (valid inside a grid-aligned segment; 2 = last bar: the M15 bar closes with it)
        offset = (int(b.ts_ns[i]) // (300 * NS)) % 3
        for name in ("last_high_confirmed_at_ts_ns", "last_low_confirmed_at_ts_ns", "confirmed_at_ts_ns"):
            v = getattr(st, name)
            assert v is None or (v % grid == 0 and v <= b.decision_ts_ns(i))  # aligned on an M15 CLOSE, never in the future
        if offset < 2:
            k = i - (offset + 1)  # last M5 bar of the previous (complete) M15 bar
            if k >= 0 and b.segment_id[k] == b.segment_id[i]:
                prev = W.swing_state(b, k, "M15")
                assert prev is not None
                for name in identity:
                    assert getattr(st, name) == getattr(prev, name), (i, offset, name)


def test_forming_m15_bar_cannot_influence_m15_state_even_with_extreme_m5_values():
    b = random_walk(500, 62)
    identity = ("last_high_price", "last_low_price", "prev_high_price", "prev_low_price", "high_label", "low_label", "sequence",
                "confirmed_at_ts_ns")
    for g in range(60, 150, 7):  # M15 group g = M5 bars 3g, 3g+1, 3g+2 (T0 is on the grid)
        for offset in (0, 1):
            i = 3 * g + offset
            h, lo = b.h.copy(), b.l.copy()
            h[3 * g : i + 1] += 50.0  # absurd spike inside the still-forming M15 bar (closed M5 bars only)
            lo[3 * g : i + 1] -= 50.0
            sp = _mk(b.ts_ns, b.o, h, lo, b.c, b.atr, b.segment_id)
            a, z = W.swing_state(b, i, "M15"), W.swing_state(sp, i, "M15")
            for name in identity:
                assert getattr(a, name) == getattr(z, name), (g, offset, name)
        # once complete (offset 2) the spiked bar is a candidate swing but needs n further M15 bars: not confirmed yet
        i = 3 * g + 2
        h = b.h.copy()
        h[3 * g : i + 1] += 50.0
        sp = _mk(b.ts_ns, b.o, h, b.l, b.c, b.atr, b.segment_id)
        z = W.swing_state(sp, i, "M15")
        assert z.last_high_confirmed_at_ts_ns is None or z.last_high_confirmed_at_ts_ns <= b.decision_ts_ns(3 * g - 1)
