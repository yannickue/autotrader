# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from coverage_analysis.observer_lab.labels import DEFAULT_MAX_BARS, EventSpec, label_event
from market_observer import schema as S

FP = {pair: S.label_key(S.first_passage_label_name(*pair)) for pair in S.FIRST_PASSAGE_LABELS}
STEP = 300 * 1_000_000_000


def lab(bars, **kw):
    ev = kw.pop("event", EventSpec(i=0, direction=1, entry=100.0, risk=1.0))
    return label_event(bars, ev, **kw)


def mk(make_bars, highs, lows, **kw):
    n = len(highs)
    return make_bars(100.0, highs, lows, 100.0, **kw) if n else None


def test_output_type_and_columns(make_bars):
    out = lab(mk(make_bars, [100, 100.1, 100.1], [100, 99.9, 99.9]))
    assert isinstance(out, S.PostEventLabels)
    assert set(out.columns) == set(S.all_label_columns())
    S.assert_disjoint({"f_levels__x"}, set(out.columns))


@pytest.mark.parametrize(("a", "b"), S.FIRST_PASSAGE_LABELS)
def test_first_passage_favourable_adverse_and_neither(make_bars, a, b):
    up = mk(make_bars, [100, 100 + a + 0.01], [100, 100 + 0.01])
    dn = mk(make_bars, [100, 100 - 0.01], [100, 100 - b - 0.01])
    flat = mk(make_bars, [100, 100.1, 100.1], [100, 99.9, 99.9])
    assert lab(up).columns[FP[(a, b)]] == 1
    assert lab(dn).columns[FP[(a, b)]] == 0
    assert lab(flat).columns[FP[(a, b)]] is None


def test_exact_touch_counts_and_tie_is_stop_first(make_bars):
    touch = mk(make_bars, [100, 100.5], [100, 100.2])
    assert lab(touch).columns[FP[(0.5, 0.5)]] == 1
    tie = mk(make_bars, [100, 100.6], [100, 99.5])  # same bar reaches +0.6R and -0.5R
    c = lab(tie).columns
    assert c[FP[(0.5, 0.5)]] == 0 and c[FP[(0.25, 0.25)]] == 0 and c[S.label_key("mfe_before_mae")] is False
    # a favourable-only first bar followed by an adverse bar is a favourable win
    seq = mk(make_bars, [100, 100.6, 100.2], [100, 99.8, 99.4])
    assert lab(seq).columns[FP[(0.5, 0.5)]] == 1


def test_short_direction_mirrors(make_bars):
    ev = EventSpec(i=0, direction=-1, entry=100.0, risk=1.0)
    dn = mk(make_bars, [100, 99.9], [100, 99.4])
    assert lab(dn, event=ev).columns[FP[(0.5, 0.5)]] == 1
    up = mk(make_bars, [100, 100.6], [100, 99.9])
    assert lab(up, event=ev).columns[FP[(0.5, 0.5)]] == 0


def test_excursions_stop_truncation_and_post_stop(make_bars):
    highs = [100, 100.4, 100.9, 100.2, 101.5]
    lows = [100, 99.8, 99.7, 98.9, 100.5]
    c = lab(mk(make_bars, highs, lows)).columns
    assert c["y_mfe_r"] == pytest.approx(0.9)  # the stop bar does not update MFE
    assert c["y_mae_r"] == pytest.approx(1.1)  # but it does update MAE
    assert c["y_time_to_mfe_s"] == 300.0  # bar OPEN minus decision time (first bar = 0)
    assert c["y_time_to_mae_s"] == 600.0
    assert c["y_mfe_before_mae"] is True
    assert c["y_post_stop_favorable_excursion_r"] == pytest.approx(1.5)


def test_post_stop_none_when_stop_never_hit_and_zero_excursion_times(make_bars):
    c = lab(mk(make_bars, [100, 100.0, 100.0], [100, 100.0, 100.0])).columns
    assert c["y_post_stop_favorable_excursion_r"] is None
    assert c["y_time_to_mfe_s"] is None and c["y_time_to_mae_s"] is None
    assert c["y_mfe_r"] == 0.0 and c["y_mae_r"] == 0.0


def test_gap_through_stop_uses_open_gap_rule(make_bars):
    bars = make_bars([100, 98.0, 99.0], [100, 98.5, 101.0], [100, 97.5, 98.0], 100.0)
    c = lab(bars).columns
    assert c["y_mae_r"] == pytest.approx(2.5) and c["y_mfe_r"] == 0.0
    assert c["y_post_stop_favorable_excursion_r"] == pytest.approx(1.0)


def test_horizon_max_bars_segment_and_day_break_and_flat(make_bars):
    highs = [100, 100.1, 100.1, 100.7]
    lows = [100, 99.9, 99.9, 99.9]
    bars = mk(make_bars, highs, lows)
    assert lab(bars).columns[FP[(0.5, 0.5)]] == 1
    assert lab(bars, max_bars=2).columns[FP[(0.5, 0.5)]] is None
    assert lab(bars, max_bars=2).horizon_end_ts_ns == int(bars.ts_ns[2]) + STEP
    assert lab(mk(make_bars, highs, lows, segment=[0, 0, 1, 1])).columns[FP[(0.5, 0.5)]] is None
    assert lab(mk(make_bars, highs, lows, local_day=[0, 0, 0, 1])).columns[FP[(0.5, 0.5)]] is None
    flat = mk(make_bars, highs, lows, local_minute=[500, 505, 510, 515])
    assert lab(flat, flat_local_minute=515).columns[FP[(0.5, 0.5)]] is None
    assert lab(flat, flat_local_minute=520).columns[FP[(0.5, 0.5)]] == 1
    assert DEFAULT_MAX_BARS == 48


def test_no_following_bar_gives_none_labels(make_bars):
    out = lab(mk(make_bars, [100], [100]))
    assert all(v is None for v in out.columns.values())
    assert out.horizon_end_ts_ns == make_bars([100], [100], [100], [100]).decision_ts_ns(0)


def test_target_and_reclaim(make_bars):
    highs = [100, 100.4, 101.2, 100.3]
    lows = [100, 99.9, 100.1, 98.0]
    bars = mk(make_bars, highs, lows)
    c = lab(bars, event=EventSpec(0, 1, 100.0, 1.0, target=101.0)).columns
    assert c["y_structural_target_reached"] is True
    stopped = mk(make_bars, [100, 100.2, 100.4, 101.3], [100, 98.9, 100.0, 100.0])  # target only AFTER stop bar
    assert lab(stopped, event=EventSpec(0, 1, 100.0, 1.0, target=101.0)).columns["y_structural_target_reached"] is False
    assert lab(bars).columns["y_structural_target_reached"] is None
    cl = make_bars(100.0, highs, lows, [100, 100.3, 100.9, 100.1])
    assert lab(cl, event=EventSpec(0, 1, 100.0, 1.0, reclaim_level=100.8)).columns["y_reclaim_or_followthrough"] is True
    assert lab(cl, event=EventSpec(0, 1, 100.0, 1.0, reclaim_level=101.0)).columns["y_reclaim_or_followthrough"] is False
    assert lab(cl).columns["y_reclaim_or_followthrough"] is None


def test_invalid_events_rejected(make_bars):
    bars = mk(make_bars, [100, 101], [100, 99])
    for ev in (EventSpec(0, 0, 100.0, 1.0), EventSpec(0, 1, 100.0, 0.0), EventSpec(0, 1, 100.0, float("nan")), EventSpec(5, 1, 100.0, 1.0)):
        with pytest.raises(ValueError):
            lab(bars, event=ev)
    with pytest.raises(ValueError):
        lab(bars, event=EventSpec(0, 1, 100.0, 1.0, target=99.0))


def test_spread_adjusted_variant_only_moves_short_exit_prices(make_bars):
    bars = mk(make_bars, [100, 100.1], [100, 99.55], spread=0.1)
    short = EventSpec(0, -1, 100.0, 1.0)
    assert lab(bars, event=short).columns["y_mfe_r"] == pytest.approx(0.45)
    assert lab(bars, event=short, spread_adjusted=True).columns["y_mfe_r"] == pytest.approx(0.35)
    assert lab(bars).columns["y_mae_r"] == lab(bars, spread_adjusted=True).columns["y_mae_r"]  # long exits at bid


def test_labels_use_only_later_bars_never_decision_bar_or_earlier(make_bars):
    rng = np.random.default_rng(3)
    n = 60
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    h, low = close + 0.2, close - 0.2
    base = make_bars(close, h, low, close)
    i = 20
    ev = EventSpec(i, 1, float(close[i]), 0.8)
    ref = label_event(base, ev).columns
    mut_h, mut_l = h.copy(), low.copy()
    mut_h[: i + 1] += 50
    mut_l[: i + 1] -= 50
    assert label_event(make_bars(close, mut_h, mut_l, close), ev).columns == ref
    # bars beyond the horizon do not matter either
    mut_h2, mut_l2 = h.copy(), low.copy()
    mut_h2[i + 1 + DEFAULT_MAX_BARS :] += 50
    assert label_event(make_bars(close, mut_h2, mut_l2, close), ev).columns == ref


# ------------------------------------------------------------------------------ cross-check with the repo labeller
def _utc(ts_ns: int) -> datetime:
    return datetime.fromtimestamp(ts_ns / 1e9, UTC)


def _random_case(make_bars, seed: int, direction: int, spread: float):
    rng = np.random.default_rng(seed)
    n = 40
    o = 100 + np.cumsum(rng.normal(0, 0.4, n))
    h = o + np.abs(rng.normal(0.3, 0.2, n))
    low = o - np.abs(rng.normal(0.3, 0.2, n))
    c = np.clip(o + rng.normal(0, 0.2, n), low, h)
    bars = make_bars(o, h, low, c, spread=spread)
    return bars, EventSpec(0, direction, 100.0, 1.0)


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("spread", [0.0, 0.07])
def test_equals_demo_labeling_on_shared_definitions(make_bars, direction, spread):
    from demo.entry_exit_quality import FIRST_ADV, FIRST_FAV, Step, walk_entry_path
    from demo.labeling import Bar, simulate_hypothetical

    for seed in range(60):
        bars, ev = _random_case(make_bars, seed, direction, spread)
        later = [
            Bar(_utc(int(bars.ts_ns[j])).isoformat(), bars.o[j], bars.h[j], bars.l[j], bars.c[j], float(bars.spread[j]))
            for j in range(1, len(bars))
        ]
        got = label_event(bars, ev, max_bars=len(bars), spread_adjusted=True).columns
        stop = 100.0 - direction * 1.0
        ref = simulate_hypothetical(direction=direction, entry=100.0, stop=stop, target=None, bars=later)
        assert got["y_mfe_r"] == pytest.approx(ref.mfe_r) and got["y_mae_r"] == pytest.approx(ref.mae_r), seed
        # first-passage pairs == target_before_stop of the repo simulator with stop = b*R, target = a*R
        for a, b in S.FIRST_PASSAGE_LABELS:
            r2 = simulate_hypothetical(
                direction=direction, entry=100.0, stop=100.0 - direction * b, target=100.0 + direction * a, bars=later,
            )
            exp = {True: 1, False: 0, None: None}[r2.target_before_stop]
            assert got[FP[(a, b)]] == exp, (seed, a, b)
        # path analytics of the entry/exit-quality module (exit-side prices, entry at the decision time)
        spv = np.zeros(len(bars)) if direction > 0 else np.asarray(bars.spread, float)
        steps = [
            Step(_utc(int(bars.ts_ns[j])), bars.o[j] + spv[j], bars.h[j] + spv[j], bars.l[j] + spv[j], bars.c[j] + spv[j])
            for j in range(1, len(bars))
        ]
        pm = walk_entry_path(direction=direction, entry=100.0, stop=stop, steps=steps, entry_ts=_utc(bars.decision_ts_ns(0)))
        assert got["y_time_to_mfe_s"] == pm.time_to_mfe_s and got["y_time_to_mae_s"] == pm.time_to_mae_s, seed
        assert got["y_mfe_before_mae"] == {FIRST_FAV: True, FIRST_ADV: False}.get(pm.first_touch), seed
