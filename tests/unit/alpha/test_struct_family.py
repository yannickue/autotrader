# ruff: noqa: E501
"""STRUCT family (Lane F): causality, mirror, gap awareness, determinism, variants, metadata (PHASE2_DISCOVERY)."""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from alpha.families import registry as R
from alpha.families import structbrk as S
from alpha.families.data import ATR_WINDOW, _assemble, round_steps
from alpha.families.spec import MarketCalendar

CAL = MarketCalendar("UTC", 0, 1440, 0, 1440, 1440)  # 24 h, no session anchor
K = 200.0


def _pattern(n: int, amp: float = 0.5) -> np.ndarray:
    """Bounded, 22-periodic alternating pattern: after one period no bar ever closes beyond its prior 24-bar range, so only
    the planted tail can break out. ``amp`` scales the slow swing (amp=6 -> an uncompressed, wide range)."""
    t = np.arange(n)
    noise = 0.4 * (((t * 7) % 11) / 5.0 - 1.0)
    return 100.0 + 0.5 * np.where(t % 2 == 0, 1.0, -1.0) + noise + (amp - 0.5) * np.sin(2 * np.pi * t / 48) * (amp > 0.5)


def _data(closes, *, gap_at: int | None = None, gap_bars: int = 0, name: str = "BTCUSD"):
    closes = np.asarray(closes, float)
    o = np.r_[closes[0], closes[:-1]]
    h = np.maximum(o, closes) + 0.1
    low = np.minimum(o, closes) - 0.1
    n = len(closes)
    step = np.full(n, 300, dtype=np.int64)
    if gap_at is not None:
        step[gap_at] += 300 * gap_bars  # bar gap_at starts gap_bars bars after the previous one
    ts = (pd.Timestamp("2026-03-10", tz="UTC").value // 10**9) + np.cumsum(step)
    ts_ns = ts.astype(np.int64) * 10**9
    return _assemble(name, CAL, ts_ns, o, h, low, closes, np.full(n, 0.1), np.full(n, 50.0), round_steps("crypto_cfd", 0.01), {})


def _scenario(tail: list[float]) -> np.ndarray:
    base = _pattern(120)
    return np.r_[base, tail]


def _gen(d, mode, **kw):
    spec = S.STRUCTSpec(mode=mode, **kw)
    return spec, R.generate_candidates(d, spec, S.fit(d, spec))


def test_registered_and_fit_free():
    assert "STRUCT" in R.FAMILY_NAMES and R.SPEC_CLASSES["STRUCT"] is S.STRUCTSpec
    spec = S.STRUCTSpec()
    assert S.fit(None, spec).values == () and spec.constants_version == S.CONSTANTS_VERSION  # type: ignore[arg-type]
    assert S.constants()["status"] == "DISCOVERY_PLACEHOLDER_NOT_FITTED"
    assert R.spec_from_json(spec.to_json()) == spec


def test_constants_are_bound_into_the_spec_hash():
    a = S.STRUCTSpec()
    b = dataclasses.replace(a, constants_version="other")
    assert a.canonical_hash() != b.canonical_hash()


def test_breakout_long_decision_at_break_bar_with_structural_stop():
    d = _data(_scenario([102.6, 102.5]))
    _, c = _gen(d, "breakout")
    assert list(c.decision_idx) == [120] and list(c.direction) == [1]
    lo = float(pd.Series(d.l).rolling(24).min().shift(1)[120])
    assert c.stop[0] == pytest.approx(lo - S.STOP_BUFFER_ATR * d.atr[120])  # opposite range edge - ATR buffer
    assert c.stop[0] < d.c[120]


def test_confirmed_needs_the_next_close_beyond_the_edge():
    d = _data(_scenario([102.6, 102.5, 102.5]))
    _, c = _gen(d, "confirmed")
    assert list(c.decision_idx) == [121] and list(c.direction) == [1]
    d2 = _data(_scenario([102.6, 100.5, 100.5]))  # next close falls back inside the range
    _, c2 = _gen(d2, "confirmed")
    assert len(c2.decision_idx) == 0


def test_retest_fires_on_the_touch_that_holds_beyond_the_edge():
    # break at 120, bar 121 pulls back to the edge zone and still closes beyond it
    closes = _scenario([102.6, 101.6, 103.0])
    d = _data(closes)
    h = d.h.copy()
    low = d.l.copy()
    low[121] = 101.15  # touches the range high (~101.1) within RETEST_TOL_ATR
    d = d.with_ohlc(d.o, h, low, d.c)
    _, c = _gen(d, "retest")
    assert list(c.decision_idx) == [121] and list(c.direction) == [1]
    # a close back inside before any touch-and-hold cancels the break
    d2 = _data(_scenario([102.6, 100.4, 100.4, 100.4]))
    _, c2 = _gen(d2, "retest")
    assert len(c2.decision_idx) == 0


def test_fade_is_the_opposite_direction_of_a_failed_break():
    d = _data(_scenario([102.6, 100.6, 100.5]))
    _, c = _gen(d, "fade")
    assert list(c.decision_idx) == [121] and list(c.direction) == [-1]
    assert c.stop[0] > d.c[121]  # stop above the failed excursion
    assert c.stop[0] >= d.h[120] + S.STOP_BUFFER_ATR * d.atr[121] - 1e-9


@pytest.mark.parametrize("mode", S.MODES)
def test_long_short_mirror(mode):
    tail = {"breakout": [102.6, 102.5], "confirmed": [102.6, 102.5, 102.5], "retest": [102.6, 101.6, 103.0], "fade": [102.6, 100.6, 100.5]}[mode]
    closes = _scenario(tail)
    d = _data(closes)
    m = _data(K - closes)
    if mode == "retest":
        d = d.with_ohlc(d.o, d.h, np.r_[d.l[:121], 101.15, d.l[122:]], d.c)
        m = m.with_ohlc(m.o, K - np.asarray(d.l), K - np.asarray(d.h), m.c)
    _, a = _gen(d, mode)
    _, b = _gen(m, mode)
    assert len(a.decision_idx) == 1
    assert np.array_equal(a.decision_idx, b.decision_idx) and np.array_equal(a.direction, -b.direction)
    assert np.allclose(b.stop, K - a.stop, atol=1e-6)


def test_short_breakout_direction():
    d = _data(K - _scenario([102.6, 102.5]))
    _, c = _gen(d, "breakout")
    assert list(c.direction) == [-1]


def test_expanded_uncompressed_range_gives_no_signal():
    closes = np.r_[_pattern(120, amp=6.0), [130.0]]  # slow wide swing: range >> 0.8*sqrt(n)*ATR -> not compressed
    d = _data(closes)
    _, c = _gen(d, "breakout")
    assert len(c.decision_idx) == 0


def test_weak_break_without_volatility_expansion_is_ignored():
    base = _data(_pattern(120))
    hi = float(pd.Series(base.h).rolling(24).max().shift(1)[119])
    c = np.r_[_pattern(120), hi + 0.02]  # closes just beyond the range with a tiny bar (TR << ATR)
    o = np.r_[c[0], c[:-1]]
    o[120] = hi - 0.05
    d = _data(c).with_ohlc(o, np.maximum(o, c), np.minimum(o, c), c)
    assert d.atr[119] > 0.5
    _, cand = _gen(d, "breakout")
    assert len(cand.decision_idx) == 0


# ------------------------------------------------------------------ gap awareness
def test_no_range_or_atr_is_built_across_a_broker_gap():
    closes = _scenario([102.6, 102.5] + [102.5] * 10)
    # a 40-bar break appears at index 100: bars >= 100 belong to a NEW segment
    d = _data(closes, gap_at=100, gap_bars=40)
    seg = S.segment_start(d)
    assert seg[99] == 0 and seg[100] == 100 and seg[120] == 100
    b = S._break_arrays(d, 24)
    # the planted break at 120 has only 20 bars in its segment (< max(n_range, ATR_WINDOW + 1) = 24): no signal
    assert not b["up"][120]
    for mode in S.MODES:
        _, c = _gen(d, mode)
        assert not np.any((c.decision_idx >= 100) & (c.decision_idx < 100 + max(24, ATR_WINDOW + 1)))
    # the very same bars WITHOUT the gap do signal (control: the gap, not the pattern, suppressed it)
    _, c0 = _gen(_data(closes), "breakout")
    assert list(c0.decision_idx) == [120]


def test_every_signal_has_its_range_and_atr_window_inside_one_segment():
    rng = np.random.default_rng(3)
    n = 4000
    closes = 100 + np.cumsum(rng.normal(0, 0.3, n))
    d = _data(closes, gap_at=1500, gap_bars=60)
    seg = S.segment_start(d)
    for r in (12, 24):
        b = S._break_arrays(d, r)
        ks = np.flatnonzero(b["up"] | b["dn"])
        assert len(ks) > 0
        assert ((ks - seg[ks]) >= max(r, ATR_WINDOW + 1)).all()


# ------------------------------------------------------------------ causality / determinism
def _random_data(seed=5, n=3000):
    rng = np.random.default_rng(seed)
    base = 100 + np.cumsum(rng.normal(0, 0.3, n))
    # alternate calm and busy stretches so compression + expansion both occur
    scale = np.where((np.arange(n) // 150) % 2 == 0, 0.15, 0.8)
    closes = 100 + np.cumsum(rng.normal(0, 1, n) * scale)
    del base
    return _data(closes, gap_at=2000, gap_bars=30)


@pytest.mark.parametrize("mode", S.MODES)
def test_truncation_and_future_perturbation_do_not_change_earlier_signals(mode):
    d = _random_data()
    spec, full = _gen(d, mode)
    assert len(full.decision_idx) >= 5
    for m in (800, 1500, 2100, 2600):
        pre = R.generate_candidates(d.prefix(m), spec, S.fit(d, spec))
        want = full.decision_idx < m - 1  # the last bar of a truncated frame has no entry bar
        assert np.array_equal(pre.decision_idx, full.decision_idx[want])
        assert np.allclose(pre.stop, full.stop[want])
        rng = np.random.default_rng(m)
        d2 = d.with_ohlc(*(np.r_[a[:m], a[m:] * rng.uniform(0.3, 3.0, len(a) - m)] for a in (d.o, d.h, d.l, d.c)))
        c2 = R.generate_candidates(d2, spec, S.fit(d2, spec))
        assert np.array_equal(c2.decision_idx[c2.decision_idx < m - 1], full.decision_idx[want])


def test_determinism_and_single_candidate_per_bar_with_cooldown():
    d = _random_data()
    for mode in S.MODES:
        spec, a = _gen(d, mode)
        b = R.generate_candidates(_random_data(), spec, S.fit(d, spec))
        assert np.array_equal(a.decision_idx, b.decision_idx) and np.array_equal(a.stop, b.stop)
        assert (np.diff(a.decision_idx) >= S.COOLDOWN).all()


def test_structure_levels_metadata_is_causal_and_additive():
    d = _data(_scenario([102.6, 102.5]))
    spec = S.STRUCTSpec(mode="breakout")
    _, c = _gen(d, "breakout")
    lv = R.describe_candidate(d, spec, int(c.decision_idx[0]), int(c.direction[0]))
    assert lv["range_high"] > lv["range_low"] and lv["phase"] == "PHASE2_DISCOVERY" and lv["alpha_status"] == "NOT_ALPHA_VALIDATED"
    assert lv["kind"] == "range" and lv["n_range"] == 24 and "swing_high" in lv and "swing_low" in lv
    assert lv["range_high"] == pytest.approx(float(pd.Series(d.h).rolling(24).max().shift(1)[120]))
    # truncated data gives the same levels at the same decision bar
    lv2 = R.describe_candidate(d.prefix(122), spec, 120, 1)
    assert lv2["range_high"] == lv["range_high"] and lv2["swing_high"] == lv["swing_high"]
    # families without the hook return {}
    from alpha.families.orb import ORBSpec

    assert R.describe_candidate(d, ORBSpec(), 5, 1) == {}


def test_grid_is_bounded_and_unique():
    g = R.grid_for("STRUCT", "BTCUSD", None)
    assert 20 <= len(g) <= 400 and len({s.canonical_hash() for s in g}) == len(g)
