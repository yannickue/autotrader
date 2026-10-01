# ruff: noqa: E501
"""observer-stats-2: per-arm block counts (H1), controls blocked by the day of their EVENT (H3), week blocks, NaN draws counted (M5). The default stays observer-stats-1."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from coverage_analysis.observer_lab import enrichment as EN
from coverage_analysis.observer_lab import stats as ST


def test_per_arm_blocks_do_not_hide_behind_the_other_arm():
    rng = np.random.default_rng(0)
    ye = (rng.random(300) < 0.5).astype(float)
    de = np.repeat(np.arange(40), 8)[:300]  # event arm of the cell: 38 days
    yc = (rng.random(300) < 0.5).astype(float)
    dc = np.repeat(np.arange(5), 60)  # control arm of the SAME cell: only 5 days
    arm_a = (ye, de, yc, dc)
    arm_b = (np.concatenate([ye, ye]), np.concatenate([de, de + 40]), np.concatenate([yc, yc]), np.concatenate([dc, dc + 40]))
    est = ST.block_bootstrap_contrast(arm_a, arm_b, B=100, seed=1)
    assert est.n_blocks_event == len(set(de.tolist())) and est.n_blocks_control == 5
    assert est.n_blocks >= 70  # the union (incl. the base arm) is far above 30 ...
    me = ST.MinEvidence(100, 100, 30, 20)
    assert ST.evidence_status(n_event=300, n_control=300, n_blocks=est.n_blocks, min_evidence=me) == ST.OK  # ... stats-1 would have called this fine
    assert ST.evidence_status(n_event=300, n_control=300, n_blocks=est.n_blocks, min_evidence=me, n_blocks_event=est.n_blocks_event, n_blocks_control=est.n_blocks_control) == ST.INSUFFICIENT_EVIDENCE


def test_blocks_count_only_days_with_a_non_nan_outcome():
    ye = np.array([1.0, np.nan, 0.0, np.nan])
    de = np.array([1, 2, 3, 4])
    est = ST.block_bootstrap_delta(ye, de, np.array([1.0, 0.0]), np.array([1, 3]), B=50, seed=0)
    assert est.n_blocks_event == 2 and est.n_blocks_control == 2


def test_nan_bootstrap_draws_are_counted_not_silently_dropped():
    # events only on day 1, controls on days 1 and 2: a resample of only day 2 leaves the event arm empty -> NaN draw
    est = ST.block_bootstrap_delta(np.array([1.0, 0.0]), np.array([1, 1]), np.array([1.0, 0.0, 1.0, 0.0]), np.array([1, 1, 2, 2]), B=400, seed=3)
    assert est.n_nan_draws > 0 and np.isfinite(est.ci_low)
    clean = ST.block_bootstrap_delta(np.array([1.0, 0.0, 1.0, 0.0]), np.array([1, 1, 2, 2]), np.array([1.0, 0.0, 1.0, 0.0]), np.array([1, 1, 2, 2]), B=100, seed=3)
    assert clean.n_nan_draws == 0


def _tables():
    ev = pd.DataFrame({"event_id": ["e1", "e2"], "m_local_day": [10, 20]})
    ct = pd.DataFrame({"event_id": ["c1", "c2"], "control_of": ["e1", "e2"], "m_local_day": [11, 20]})  # c1 happens on the day AFTER its event
    return ev, ct


def test_controls_take_the_block_of_their_event_in_stats_2_only():
    ev, ct = _tables()
    legacy = EN._block_arrays(ev, ct, EN.EnrichmentConfig())
    assert list(legacy[0]) == [10, 20] and list(legacy[1]) == [11, 20]  # observer-stats-1 (default): the control's own day
    v2 = EN._block_arrays(ev, ct, EN.EnrichmentConfig(stats_version=ST.STATS_V2))
    assert list(v2[1]) == [10, 20]  # observer-stats-2: the day of its EVENT
    with pytest.raises(ValueError):
        EN._block_arrays(ev, ct, EN.EnrichmentConfig(stats_version="nope"))


def test_week_blocks_are_iso_weeks():
    mon = int((pd.Timestamp("2026-01-05") - pd.Timestamp("1970-01-01")).days)  # a Monday
    ev = pd.DataFrame({"event_id": [f"e{i}" for i in range(9)], "m_local_day": [mon + i for i in range(9)]})  # Mon..Mon+8 -> two ISO weeks + the next Monday
    ct = pd.DataFrame({"event_id": ["c0"], "control_of": ["e8"], "m_local_day": [mon]})
    ew, cw = EN._block_arrays(ev, ct, EN.EnrichmentConfig(stats_version=ST.STATS_V2, block_unit="week"))
    assert len(set(ew[:7])) == 1 and ew[7] == ew[0] + 1 and ew[8] == ew[0] + 1 and cw[0] == ew[8]  # Mon..Sun one week, Mon+7 starts the next; the control follows its event
    with pytest.raises(ValueError):
        EN._block_arrays(ev.assign(m_local_day=ev["m_local_day"].astype(float)), ct, EN.EnrichmentConfig(block_unit="week"))
    with pytest.raises(ValueError):
        EN._block_arrays(ev, ct, EN.EnrichmentConfig(block_unit="month"))


def test_versions_are_recorded_in_the_report():
    v = EN._report_versions(EN.EnrichmentConfig(stats_version=ST.STATS_V2, block_unit="week"))
    assert v["stats"] == "observer-stats-2" and v["block_unit"] == "week" and EN._report_versions()["stats"] == "observer-stats-1"
