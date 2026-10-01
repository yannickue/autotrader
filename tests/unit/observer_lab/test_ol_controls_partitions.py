# ruff: noqa: E501
"""Matched controls: partition rules (no cross-split controls), partition / causal ranks, exclusion of every opportunity bar."""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from coverage_analysis.observer_lab import controls as CT
from coverage_analysis.observer_lab import splits as SP

SPAN_T0 = int(pd.Timestamp("2026-06-12", tz="UTC").value)  # crosses TRAIN/VALIDATION, OOS (07-01) and FORWARD (09-01)
PER_DAY = 288


@pytest.fixture(scope="module")
def span(make_bars):
    rng = np.random.default_rng(3)
    days = 96
    n = days * PER_DAY
    minute = np.tile(5 * np.arange(PER_DAY), days)
    close = 100 + np.cumsum(rng.normal(0, 0.05, n))
    atr = np.exp(rng.normal(0, 0.5, n))
    return make_bars(close, close + 0.1, close - 0.1, close, spread=rng.uniform(0.01, 0.05, n), atr=atr, local_minute=minute,
                     local_day=np.repeat(np.arange(days), PER_DAY), t0_ns=SPAN_T0)


@pytest.fixture(scope="module")
def part(span):
    return CT.bar_partitions(span)


def _events_per_partition(part, k=8, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for name in (SP.TRAIN, SP.VALIDATION, SP.OOS):
        pool = np.flatnonzero(part == name)
        pool = pool[(pool % PER_DAY > 100) & (pool % PER_DAY < 150)]  # office-hours bars: plenty of same-session candidates
        out += rng.choice(pool, size=k, replace=False).tolist()
    return np.sort(np.array(out))


def test_partition_labels_cover_all_four_partitions_and_purge_boundaries(part):
    assert {SP.TRAIN, SP.VALIDATION, SP.OOS, SP.FORWARD} <= set(part.tolist())
    assert SP.PURGED in set(part.tolist()) or SP.EMBARGO in set(part.tolist())


def test_controls_never_cross_the_train_validation_oos_forward_boundaries(span, part):
    ev = _events_per_partition(part)
    cs = CT.match_controls(span, ev, spec=CT.MatchSpec(n_controls=2), seed=4)
    assert len(cs.control_idx) > 0
    for pos, j in zip(cs.event_pos, cs.control_idx, strict=True):
        assert part[j] == part[ev[pos]] and part[j] in (SP.TRAIN, SP.VALIDATION, SP.OOS, SP.FORWARD)  # never a PURGED/EMBARGO bar either
    assert cs.control_partition == tuple(part[cs.control_idx].tolist())
    r = cs.report
    assert r.partition_mode == "PARTITIONED" and r.rank_mode == "partition"
    for name in (SP.TRAIN, SP.VALIDATION, SP.OOS):
        assert r.by_partition[name]["n_events"] == 8 and r.by_partition[name]["match_rate"] > 0.0


def test_events_in_purged_or_embargo_bars_are_reported_unmatched_not_dropped(span, part):
    bad = np.flatnonzero((part == SP.PURGED) | (part == SP.EMBARGO))
    ev = np.array([int(bad[0]), int(bad[-1])])
    cs = CT.match_controls(span, ev, seed=0)
    assert cs.control_idx.size == 0 and cs.report.unmatched_event_pos == (0, 1)
    assert cs.report.n_events_in_excluded_partition == 2


def test_train_ranks_and_controls_do_not_see_oos_or_forward_bars(span, part):
    train = part == SP.TRAIN
    base = CT.bar_covariates(span, rank_mode="partition", partition=part)
    later = np.arange(len(span)) > np.flatnonzero(train).max()
    span2 = dataclasses.replace(span, atr=np.where(later, np.asarray(span.atr) * 7.0, np.asarray(span.atr)))  # vol regime change after TRAIN only
    moved = CT.bar_covariates(span2, rank_mode="partition", partition=part)
    assert np.array_equal(base["atr_pct"][train], moved["atr_pct"][train], equal_nan=True)
    legacy_a, legacy_b = CT.bar_covariates(span)["atr_pct"], CT.bar_covariates(span2)["atr_pct"]
    assert not np.allclose(legacy_a[train], legacy_b[train], equal_nan=True)  # the old whole-sample rank WAS contaminated by later data
    ev = _events_per_partition(part)
    ev = ev[part[ev] == SP.TRAIN]
    a, b = CT.match_controls(span, ev, seed=1), CT.match_controls(span2, ev, seed=1)
    assert a.control_idx.size > 0 and np.array_equal(a.control_idx, b.control_idx)


def test_causal_rank_is_past_only_and_prefix_invariant(span):
    cov = CT.bar_covariates(span, rank_mode="causal", min_history=30)
    cut = 5000
    pre = CT.bar_covariates(span.prefix(cut), rank_mode="causal", min_history=30)
    assert np.array_equal(cov["atr_pct"][:cut], pre["atr_pct"], equal_nan=True)
    assert np.array_equal(cov["spread_pct"][:cut], pre["spread_pct"], equal_nan=True)
    assert np.isnan(cov["atr_pct"][:29]).all() and not np.isnan(cov["atr_pct"][40])
    future = dataclasses.replace(span, atr=np.where(np.arange(len(span)) > cut, 1e6, np.asarray(span.atr)))
    assert np.array_equal(CT.bar_covariates(future, rank_mode="causal", min_history=30)["atr_pct"][: cut + 1], cov["atr_pct"][: cut + 1], equal_nan=True)


def test_forward_events_require_causal_ranks(span, part):
    fwd = np.flatnonzero(part == SP.FORWARD)
    ev = fwd[(fwd % PER_DAY > 100) & (fwd % PER_DAY < 140)][::40][:6]
    with pytest.raises(ValueError, match="causal"):
        CT.match_controls(span, ev, seed=0)
    cs = CT.match_controls(span, ev, seed=0, rank_mode="causal")
    assert cs.report.rank_mode == "causal" and cs.control_idx.size > 0 and (part[cs.control_idx] == SP.FORWARD).all()


def test_exclude_idx_closes_the_neighbourhood_of_every_other_family_opportunity(span, part):
    ev = _events_per_partition(part, k=6)
    rng = np.random.default_rng(9)
    others = np.sort(rng.choice(np.flatnonzero(part == SP.TRAIN), size=400, replace=False))  # opportunities of OTHER families
    plain = CT.match_controls(span, ev, seed=2)
    guarded = CT.match_controls(span, ev, seed=2, exclude_idx=others)
    assert (np.abs(plain.control_idx[:, None] - others[None, :]).min(axis=1) <= 48).any()  # without the argument, "controls" land on real opportunities
    assert guarded.control_idx.size > 0
    assert np.abs(guarded.control_idx[:, None] - others[None, :]).min(axis=1).min() > 48
    assert guarded.report.n_exclusion_bars_blocked == len(set(ev.tolist()) | set(others.tolist()))
    with pytest.raises(ValueError):
        CT.match_controls(span, ev, exclude_idx=[len(span)])


def test_partition_argument_variants_and_errors(span, part):
    ev = _events_per_partition(part, k=5)
    legacy = CT.match_controls(span, ev, seed=1, partition=None, rank_mode="sample")
    assert legacy.report.partition_mode == "UNPARTITIONED" and set(legacy.control_partition) == {"ALL"}
    labels = np.full(len(span), "TRAIN", dtype=object)
    explicit = CT.match_controls(span, ev, seed=1, partition=labels)
    assert explicit.report.partition_mode == "PARTITIONED" and set(explicit.control_partition) == {"TRAIN"}
    with pytest.raises(ValueError, match="labels"):
        CT.match_controls(span, ev, partition=labels[:-1])
    with pytest.raises(ValueError, match="auto"):
        CT.match_controls(span, ev, partition="nope")
    with pytest.raises(ValueError, match="rank_mode"):
        CT.match_controls(span, ev, rank_mode="bogus")
    with pytest.raises(ValueError, match="partitions"):
        CT.match_controls(span.prefix(30), np.array([5]))  # too few trading days: a clear error, not a silent fallback


def test_placebo_draws_in_another_partition_are_rejected(span, part):
    ev = np.flatnonzero(part == SP.TRAIN)[500:3000:700][:3]
    oos = int(np.flatnonzero(part == SP.OOS)[1000])

    def toy(bars, i, rng):
        return [CT.PlaceboDraw(idx=oos), CT.PlaceboDraw(idx=int(i) + 200)]

    r = CT.placebo_controls(span, ev, toy, partition="auto", n_per_event=1)
    assert r.report.n_rejected_partition == len(ev) and set(r.control_partition) <= {SP.TRAIN}
