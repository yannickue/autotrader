# ruff: noqa: E501
"""Lane X: entry quality vs exit quality - pure measurement, classification, capture ratio, clusters, verdicts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from demo import entry_exit_quality as eq
from demo.entry_exit_quality import (
    ANALYSIS_VERSION,
    CAPTURE_GOOD,
    CAPTURE_NA,
    CAPTURE_POOR,
    ENTRY_AMBIGUOUS,
    ENTRY_FAILURE,
    ENTRY_USEFUL,
    LABEL_FAILURE,
    LABEL_GIVEBACK,
    LABEL_GOOD,
    LABEL_SPEC_VERSION,
    Step,
    assign_event_clusters,
    assign_market_clusters,
    capture_ratio,
    classify,
    entry_exit_fields,
    summarise_entries,
    verdict_for,
    walk_entry_path,
)

T0 = datetime(2026, 6, 9, 8, 0, tzinfo=UTC)


def _steps(rows, start=T0, bar_s=300):
    """rows: (o, hi, lo, c) in exit-side prices, one per 5-minute bar."""
    return [Step(start + timedelta(seconds=bar_s * i), *r[:1], r[1], r[2], r[3]) for i, r in enumerate(rows)]


# ---- canonical cases (the binding user requirement) -----------------------------------------------------
def test_mfe_then_full_loss_is_useful_entry_with_giveback_not_entry_failure():
    # long 100, stop 99 (1R): +0.6R MFE, then the stop is hit
    steps = _steps([(100.0, 100.3, 99.9, 100.2), (100.2, 100.6, 100.1, 100.5), (100.5, 100.5, 98.8, 99.0)])
    f = entry_exit_fields(direction=1, entry=100.0, stop=99.0, steps=steps, entry_ts=T0, final_r=-1.0, apply_stop=True)
    assert f["mfe_r"] == pytest.approx(0.6)
    assert f["entry_label"] == ENTRY_USEFUL
    assert f["capture_label"] == CAPTURE_POOR
    assert f["entry_exit_label"] == LABEL_GIVEBACK
    assert f["capture_ratio"] == pytest.approx(-1.0 / 0.6)
    assert f["capture_ratio_floored"] == 0.0
    assert f["mfe_giveback_r"] == pytest.approx(1.6)
    assert f["path_exit_kind"] == "STOP"
    assert f["mfe_before_mae"] is True and f["first_touch_0.5R"] == "FAV_FIRST"


def test_no_favourable_excursion_and_fast_adverse_move_is_entry_failure():
    steps = _steps([(100.0, 100.05, 99.3, 99.4), (99.4, 99.5, 98.9, 99.0)])  # MFE 0.05R, stop hit in bar 2
    f = entry_exit_fields(direction=1, entry=100.0, stop=99.0, steps=steps, entry_ts=T0, final_r=-1.0, apply_stop=True)
    assert f["mfe_r"] == pytest.approx(0.05) and f["mae_r"] >= 1.0
    assert f["entry_label"] == ENTRY_FAILURE and f["entry_exit_label"] == LABEL_FAILURE
    assert f["capture_label"] == CAPTURE_NA
    assert f["capture_ratio"] is None  # MFE below the 0.10R floor: undefined
    assert f["fast_failure"] is True  # adverse extreme within 30 min
    assert f["first_touch_0.5R"] == "ADV_FIRST" and f["mfe_before_mae"] is False


def test_entry_and_capture_are_separate_axes_same_final_r():
    """Same final R (-1): one entry offered +0.6R, the other never went green -> different entry labels."""
    bad = classify(mfe_r=0.0, mae_r=1.0, final_r=-1.0)
    useful = classify(mfe_r=0.6, mae_r=1.0, final_r=-1.0)
    assert bad["entry_label"] == ENTRY_FAILURE and useful["entry_label"] == ENTRY_USEFUL
    assert bad["final_r_judged"] == useful["final_r_judged"] == -1.0


def test_good_entry_good_capture_and_ambiguous():
    g = classify(mfe_r=2.0, mae_r=0.3, final_r=1.5)
    assert g["entry_exit_label"] == LABEL_GOOD and g["capture_label"] == CAPTURE_GOOD
    assert g["capture_ratio"] == pytest.approx(0.75)
    amb = classify(mfe_r=0.3, mae_r=0.4, final_r=-0.2)  # went nowhere: neither useful nor a clear failure
    assert amb["entry_label"] == ENTRY_AMBIGUOUS and amb["entry_exit_label"] == "AMBIGUOUS"
    amb2 = classify(mfe_r=0.1, mae_r=0.7, final_r=-0.7)  # MAE just below the failure threshold
    assert amb2["entry_label"] == ENTRY_AMBIGUOUS


def test_thresholds_are_predeclared_and_versioned():
    th = eq.DEFAULT_THRESHOLDS
    assert (th.useful_mfe_r, th.failure_mfe_r, th.failure_mae_r, th.good_capture, th.min_mfe_for_capture) == (0.5, 0.25, 0.75, 0.5, 0.10)
    assert ANALYSIS_VERSION == "eeq-1.0" and LABEL_SPEC_VERSION == "eeq-labels-1"
    assert set(th.as_dict()) >= {"useful_mfe_r", "failure_mae_r", "good_capture", "poor_capture_share"}


# ---- capture ratio math ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mfe", "final", "ratio", "floored"),
    [
        (2.0, 1.0, 0.5, 0.5),  # kept half
        (1.5, 1.5, 1.0, 1.0),  # exit at the peak
        (1.0, -1.0, -1.0, 0.0),  # loser: signed negative, floored zero
        (0.5, 0.75, 1.5, 1.0),  # final above the path MFE (cost-free numerics): floored clamps to 1
        (0.10, 0.05, 0.5, 0.5),  # exactly the floor is defined
    ],
)
def test_capture_ratio_math(mfe, final, ratio, floored):
    r, f = capture_ratio(mfe, final)
    assert r == pytest.approx(ratio) and f == pytest.approx(floored)


@pytest.mark.parametrize("mfe", [0.0, -0.3, 0.0999, float("nan")])
def test_capture_ratio_undefined_without_favourable_excursion(mfe):
    assert capture_ratio(mfe, -1.0) == (None, None)


# ---- path walker ----------------------------------------------------------------------------------------
def test_walker_reach_times_levels_and_bar_dating():
    steps = _steps([(100, 100.3, 99.9, 100.2), (100.2, 100.8, 100.1, 100.7), (100.7, 102.1, 100.6, 102.0), (102, 102.0, 101.0, 101.2)])
    m = walk_entry_path(direction=1, entry=100.0, stop=99.0, steps=steps, entry_ts=T0)
    assert m.reach_s[0.25] == 0.0 and m.reach_s[0.5] == 300.0 and m.reach_s[0.75] == 300.0
    assert m.reach_s[1.0] == 600.0 and m.reach_s[1.5] == 600.0 and m.reach_s[2.0] == 600.0
    assert m.time_to_mfe_s == 600.0 and m.mfe_r == pytest.approx(2.1)
    assert m.exit_kind == "DATA_END" and m.bars == 4
    assert m.bars_above_entry == 4 and m.bars_below_entry == 0


def test_walker_short_uses_exit_side_prices_and_mirrors_long():
    # mirrored path around 200: long steps -> short steps
    long_steps = _steps([(100, 100.3, 99.6, 100.1), (100.1, 100.9, 99.2, 100.5), (100.5, 101.6, 100.4, 101.5)])
    k = 200.0
    short_steps = [Step(s.ts, k - s.o, k - s.lo, k - s.hi, k - s.c) for s in long_steps]
    ml = walk_entry_path(direction=1, entry=100.0, stop=99.0, steps=long_steps, entry_ts=T0)
    ms = walk_entry_path(direction=-1, entry=100.0, stop=101.0, steps=short_steps, entry_ts=T0)
    # the short mirror sees price 200-x: entry at 100 needs k-100 = 100, stop 101 == mirror of 99
    assert ml.mfe_r == pytest.approx(ms.mfe_r) and ml.mae_r == pytest.approx(ms.mae_r)
    assert ml.reach_s == ms.reach_s and ml.first_touch == ms.first_touch and ml.exit_kind == ms.exit_kind


def test_walker_stop_first_inside_the_stop_bar_and_gap():
    # one bar touches +1R and the stop: stop-first -> no MFE credit, MAE credited, tie first-touch = ADV
    steps = _steps([(100, 101.2, 98.9, 100.0)])
    m = walk_entry_path(direction=1, entry=100.0, stop=99.0, steps=steps, entry_ts=T0)
    assert m.exit_kind == "STOP" and m.mfe_r == 0.0 and m.mae_r == pytest.approx(1.1) and m.first_touch == "ADV_FIRST"
    gap = walk_entry_path(direction=1, entry=100.0, stop=99.0, steps=_steps([(98.5, 99.0, 98.0, 98.5)]), entry_ts=T0)
    assert gap.exit_kind == "STOP_GAP"


def test_walker_flat_deadline_excludes_the_flat_bar_and_later_bars():
    steps = _steps([(100, 100.4, 99.8, 100.3), (100.3, 100.4, 99.9, 100.2), (100.2, 105.0, 100.0, 104.0)])
    m = walk_entry_path(direction=1, entry=100.0, stop=99.0, steps=steps, entry_ts=T0, flat_ts=T0 + timedelta(minutes=10))
    assert m.exit_kind == "FLAT" and m.bars == 2 and m.mfe_r == pytest.approx(0.4)


def test_walker_structure_levels_and_tp_flags():
    steps = _steps([(100, 100.6, 99.9, 100.5), (100.5, 101.3, 100.4, 101.2), (101.2, 101.4, 100.9, 101.0)])
    m = walk_entry_path(direction=1, entry=100.0, stop=99.0, steps=steps, entry_ts=T0, tp1=101.0, tp2=102.0, structure_levels=[100.5, 101.0, 102.0])
    assert m.tp1_reached is True and m.tp2_reached is False and m.time_to_tp1_s == 300.0 and m.time_to_tp2_s is None
    assert m.max_structure_reached_r == pytest.approx(1.0)
    none = walk_entry_path(direction=1, entry=100.0, stop=99.0, steps=steps, entry_ts=T0)
    assert none.tp1_reached is None and none.max_structure_reached_r is None


def test_walker_time_above_below_entry_and_closed_path_without_close():
    steps = _steps([(100, 100.4, 99.6, 100.2), (100.2, 100.3, 99.5, 99.8), (99.8, 100.2, 99.6, 99.9)])
    m = walk_entry_path(direction=1, entry=100.0, stop=98.0, steps=steps, entry_ts=T0)
    assert (m.bars_above_entry, m.bars_below_entry) == (1, 2)
    live = [Step(s.ts, None, s.hi, s.lo, None) for s in steps]
    m2 = walk_entry_path(direction=1, entry=100.0, stop=98.0, steps=live, entry_ts=T0, apply_stop=False)
    assert m2.bars_above_entry is None and m2.exit_kind == "PATH_END"


def test_walker_rejects_bad_geometry():
    with pytest.raises(ValueError):
        walk_entry_path(direction=1, entry=100.0, stop=100.0, steps=[], entry_ts=T0)
    with pytest.raises(ValueError):
        walk_entry_path(direction=1, entry=100.0, stop=101.0, steps=[], entry_ts=T0)


# ---- causality ------------------------------------------------------------------------------------------
def test_truncating_future_bars_does_not_change_a_resolved_measurement():
    steps = _steps([(100, 100.4, 99.8, 100.3), (100.3, 100.9, 100.1, 100.8), (100.8, 101.0, 98.9, 99.0), (99.0, 120.0, 90.0, 110.0), (110, 130, 100, 120)])
    full = walk_entry_path(direction=1, entry=100.0, stop=99.0, steps=steps, entry_ts=T0)
    cut = walk_entry_path(direction=1, entry=100.0, stop=99.0, steps=steps[:3], entry_ts=T0)
    assert full.as_dict() == cut.as_dict()  # the path ended at the stop bar; nothing after it can matter


def test_measurement_up_to_k_equals_measurement_of_the_prefix():
    """No lookahead: the running MFE/MAE at bar k is exactly what a walk over bars[:k+1] reports."""
    import random

    rnd = random.Random(7)
    px, rows = 100.0, []
    for _ in range(40):
        o = px
        hi, lo = o + rnd.random() * 0.4, o - rnd.random() * 0.4
        px = rnd.uniform(lo, hi)
        rows.append((o, hi, lo, px))
    steps = _steps(rows)
    best = 0.0
    for k in range(1, len(steps) + 1):
        part = walk_entry_path(direction=1, entry=100.0, stop=90.0, steps=steps[:k], entry_ts=T0)
        assert part.mfe_r >= best - 1e-12
        best = part.mfe_r
        assert part.bars == k


# ---- clusters -------------------------------------------------------------------------------------------
def _row(market, direction, minutes, event=None):
    return {"market": market, "direction": direction, "signal_ts": T0 + timedelta(minutes=minutes), "structure_event_id": event}


def test_four_struct_variants_of_one_break_are_one_cluster():
    rows = [_row("BTCUSD", 1, m, event="BTCUSD:1:break-a") for m in (0, 5, 20, 90)] + [_row("BTCUSD", 1, 200, event="BTCUSD:1:break-b")]
    ids = assign_event_clusters(rows)
    assert len(set(ids[:4])) == 1 and ids[4] != ids[0]


def test_nearby_same_market_same_direction_signals_chain_and_others_split():
    rows = [_row("GER40", 1, 0), _row("GER40", 1, 30), _row("GER40", 1, 80), _row("GER40", -1, 10), _row("NAS100", 1, 10), _row("GER40", 1, 400)]
    ids = assign_event_clusters(rows, window_s=3600)
    assert ids[0] == ids[1] == ids[2]  # chain-linked: 0-30 and 30-80 are each within 1 h
    assert len({ids[0], ids[3], ids[4], ids[5]}) == 4  # other direction / market / far in time


def test_market_clusters_merge_correlated_index_markets():
    from demo.execution.risk_policy import cluster_of

    rows = [_row("GER40", 1, 0), _row("NAS100", 1, 5), _row("SPX500", 1, 9), _row("XAUUSD", 1, 5), _row("GER40", -1, 5)]
    ids = assign_market_clusters(rows, cluster_of)
    assert ids[0] == ids[1] == ids[2]
    assert ids[3] != ids[0] and ids[4] != ids[0]


def test_cluster_equal_mean_counts_each_event_once():
    assert eq.cluster_equal_mean([1.0, 1.0, 1.0, -1.0], ["a", "a", "a", "b"]) == pytest.approx(0.0)  # raw mean would be 0.5


# ---- summaries / verdicts -------------------------------------------------------------------------------
def _rows(n_useful_poor, n_failure, n_good=0, n_amb=0):
    rows = []
    for _ in range(n_useful_poor):
        rows.append({**classify(mfe_r=0.8, mae_r=1.0, final_r=-1.0), "mfe_r": 0.8, "mae_r": 1.0, "baseline_r": -1.0, "mfe_giveback_r": 1.8})
    for _ in range(n_failure):
        rows.append({**classify(mfe_r=0.0, mae_r=1.0, final_r=-1.0), "mfe_r": 0.0, "mae_r": 1.0, "baseline_r": -1.0, "mfe_giveback_r": 1.0})
    for _ in range(n_good):
        rows.append({**classify(mfe_r=2.0, mae_r=0.2, final_r=1.5), "mfe_r": 2.0, "mae_r": 0.2, "baseline_r": 1.5, "mfe_giveback_r": 0.5})
    for _ in range(n_amb):
        rows.append({**classify(mfe_r=0.3, mae_r=0.3, final_r=0.0), "mfe_r": 0.3, "mae_r": 0.3, "baseline_r": 0.0, "mfe_giveback_r": 0.3})
    return rows


def test_small_n_is_inconclusive_and_flagged():
    rows = _rows(3, 3)
    s = summarise_entries(rows, event_clusters=[f"c{i}" for i in range(len(rows))])
    assert s["n"] == 6 and s["verdict"] == "INCONCLUSIVE-n" and "n too small" in s["flag"]


def test_few_independent_clusters_is_inconclusive_even_with_large_raw_n():
    rows = _rows(20, 20)
    s = summarise_entries(rows, event_clusters=["same"] * 40)
    assert s["n"] == 40 and s["n_event_clusters"] == 1 and s["verdict"] == "INCONCLUSIVE-n"


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        (dict(n_useful_poor=0, n_failure=30, n_good=0, n_amb=10), "BAD ENTRIES"),
        (dict(n_useful_poor=30, n_failure=2, n_good=6, n_amb=6), "USEFUL ENTRIES + BAD CAPTURE"),
        (dict(n_useful_poor=12, n_failure=20, n_good=4, n_amb=4), "BOTH"),
        (dict(n_useful_poor=2, n_failure=4, n_good=30, n_amb=10), "NO CLEAR DEFICIT"),
    ],
)
def test_verdict_categories(kw, expected):
    rows = _rows(**kw)
    s = summarise_entries(rows, event_clusters=[f"c{i}" for i in range(len(rows))])
    assert s["verdict"] == expected
    assert s["label_counts"][LABEL_FAILURE] == kw["n_failure"]


def test_verdict_for_boundaries():
    assert verdict_for(n=29, n_clusters=29, failure_share=0.9, useful_share=0.0, poor_capture_share_of_useful=None) == "INCONCLUSIVE-n"
    assert verdict_for(n=30, n_clusters=19, failure_share=0.9, useful_share=0.0, poor_capture_share_of_useful=None) == "INCONCLUSIVE-n"
    assert verdict_for(n=30, n_clusters=20, failure_share=0.4, useful_share=0.0, poor_capture_share_of_useful=None) == "BAD ENTRIES"


def test_summary_reports_raw_and_cluster_counts_side_by_side():
    rows = _rows(10, 10, 10, 10)
    cl = [f"c{i // 2}" for i in range(len(rows))]
    s = summarise_entries(rows, event_clusters=cl)
    assert s["n"] == 40 and s["n_event_clusters"] == 20
    assert set(s["p_mfe_ge"]) == {"0.25", "0.5", "0.75", "1", "1.5", "2"}
    assert s["entry_failure_share"] == pytest.approx(0.25)
