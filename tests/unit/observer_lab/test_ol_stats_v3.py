# ruff: noqa: E501
"""observer-stats-3: contiguous blocks of >= 21 trading days (controls sit +-10 trading days from their event and share label windows with neighbouring events).
observer-stats-1 / -2 stay unchanged (their own tests: test_ol_stats.py / test_ol_stats_v2.py)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from coverage_analysis.observer_lab import enrichment as EN
from coverage_analysis.observer_lab import stats as ST


def _sizes(mp: dict) -> list[int]:
    ids = list(mp.values())
    return [ids.count(i) for i in sorted(set(ids))]


@pytest.mark.parametrize("n_days", [1, 20, 21, 22, 41, 42, 43, 63, 500, 503])
def test_every_block_holds_at_least_21_distinct_days_and_blocks_are_contiguous(n_days):
    days = np.arange(1000, 1000 + n_days)
    mp = ST.contiguous_day_blocks(days)
    sizes = _sizes(mp)
    assert sum(sizes) == n_days
    if n_days >= 21:
        assert min(sizes) >= 21 and len(sizes) == n_days // 21
    else:
        assert sizes == [n_days]  # fewer than 21 days: one block (the minimum-blocks evidence rule then makes the cell insufficient)
    ids = [mp[d] for d in sorted(mp)]
    assert ids == sorted(ids)  # contiguous in time, never interleaved


def test_the_mapping_depends_on_the_days_only_not_on_order_or_duplicates():
    days = np.array([5, 3, 3, 9, 1, 1, 1, 7] * 20)
    assert ST.contiguous_day_blocks(days) == ST.contiguous_day_blocks(np.unique(days)) == ST.contiguous_day_blocks(days[::-1])


def test_blocks_shorter_than_21_trading_days_are_refused():
    with pytest.raises(ValueError):
        ST.contiguous_day_blocks(np.arange(100), 20)
    with pytest.raises(ValueError):
        EN._block_arrays(pd.DataFrame({"event_id": ["e"], "m_local_day": [1]}), pd.DataFrame({"event_id": ["c"], "control_of": ["e"], "m_local_day": [1]}),
                         EN.EnrichmentConfig(stats_version=ST.STATS_V3, block_unit="tdays", block_len_days=10))


def test_stats_3_takes_the_event_block_for_controls_and_refuses_day_and_week_blocks():
    n = 120
    ev = pd.DataFrame({"event_id": [f"e{i}" for i in range(n)], "m_local_day": np.arange(n)})
    ct = pd.DataFrame({"event_id": [f"c{i}" for i in range(n)], "control_of": [f"e{i}" for i in range(n)], "m_local_day": (np.arange(n) + 9) % n})  # own day up to 9 days away
    cfg = EN.EnrichmentConfig(stats_version=ST.STATS_V3, block_unit="tdays")
    eb, cb = EN._block_arrays(ev, ct, cfg)
    assert list(eb) == list(cb)  # the control follows its event, not its own day
    assert len(set(eb.tolist())) == n // 21 and min(np.bincount(eb)) >= 21
    for bad in ("day", "week"):
        with pytest.raises(ValueError, match="observer-stats-3"):
            EN._block_arrays(ev, ct, EN.EnrichmentConfig(stats_version=ST.STATS_V3, block_unit=bad))
    for v in (ST.STATS_LEGACY, ST.STATS_V2):  # 'tdays' is not available to the older versions
        with pytest.raises(ValueError):
            EN._block_arrays(ev, ct, EN.EnrichmentConfig(stats_version=v, block_unit="tdays"))


def test_older_versions_are_unchanged():
    ev = pd.DataFrame({"event_id": ["e1", "e2"], "m_local_day": [10, 20]})
    ct = pd.DataFrame({"event_id": ["c1", "c2"], "control_of": ["e1", "e2"], "m_local_day": [11, 20]})
    assert [list(a) for a in EN._block_arrays(ev, ct, EN.EnrichmentConfig())] == [[10, 20], [11, 20]]
    assert [list(a) for a in EN._block_arrays(ev, ct, EN.EnrichmentConfig(stats_version=ST.STATS_V2))] == [[10, 20], [10, 20]]
    assert EN.EnrichmentConfig().stats_version == ST.STATS_LEGACY and ST.STATS_V2 == "observer-stats-2" and ST.STATS_V3 == "observer-stats-3"


def test_per_arm_evidence_applies_under_stats_3_and_the_version_is_recorded():
    cfg = EN.EnrichmentConfig(stats_version=ST.STATS_V3, block_unit="tdays", min_evidence=ST.MinEvidence(100, 100, 30, 20))
    est = ST.DeltaEstimate(300, 300, 150, 150, 0.5, 0.5, 0.0, -0.1, 0.1, 1.0, 80, None, 100, 0.05, n_blocks_event=80, n_blocks_control=5)
    assert EN._evidence(est, cfg, None) == ST.INSUFFICIENT_EVIDENCE  # the control arm hides behind nothing
    v = EN._report_versions(cfg)
    assert v["stats"] == "observer-stats-3" and v["block_unit"] == "tdays" and v["block_len_days"] == "21"


def test_larger_blocks_widen_the_interval_of_serially_dependent_data():
    """Calibration sanity: when the event-vs-control gap itself drifts slowly (one regime level per 21 days), single-day blocks treat 420 days as independent and
    are overconfident; 21-day blocks give a wider interval. A property test of the construction, not a claim about real data."""
    rng = np.random.default_rng(7)
    n_days, per = 420, 6
    regime = np.repeat(rng.normal(0, 0.15, n_days // 21), 21)  # one regime level per 21 days
    day = np.repeat(np.arange(n_days), per)
    r = np.repeat(regime, per)
    ye, yc = (rng.random(len(day)) < np.clip(0.5 + r, 0.05, 0.95)).astype(float), (rng.random(len(day)) < np.clip(0.5 - r, 0.05, 0.95)).astype(float)
    narrow = ST.block_bootstrap_delta(ye, day, yc, day, B=600, seed=1)
    mp = ST.contiguous_day_blocks(day)
    blk = np.array([mp[d] for d in day.tolist()])
    wide = ST.block_bootstrap_delta(ye, blk, yc, blk, B=600, seed=1)
    assert (wide.ci_high - wide.ci_low) > (narrow.ci_high - narrow.ci_low)
    assert wide.n_blocks == n_days // 21
