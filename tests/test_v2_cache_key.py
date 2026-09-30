# ruff: noqa: E501
"""Result-cache key of TemporalEvaluator: event/feature provenance + resolved thresholds must be part of it.

Audit probe: frame B with the SAME OHLC but rolled event/feature arrays returned frame A's cached
result (505 candidates vs a fresh 37 -> too_few_trades) because the per-genome key was
fingerprint(ghash) over OHLC only.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pytest

from alpha.common.protocol import Partition, SplitPlan
from alpha.common.sim import SizingSpec
from alpha.discovery import temporal_genome as tg
from alpha.discovery.temporal_archetypes import random_genome
from alpha.discovery.temporal_evaluate import TemporalEvaluator, market_from_frame
from alpha.temporal.reference import MarketFrame
from scripts.bench_temporal import synth_frame

N = 30_000
DAYS = N // 288
POOL = tg.EventPool.full()
SIZING = SizingSpec(min_risk_pts=0.5, max_risk_pts=60.0)


def _d(k: int) -> str:
    return str(np.datetime64("2024-01-01", "D") + np.timedelta64(k, "D"))


SPLIT = SplitPlan(Partition("train", _d(0), _d(int(DAYS * 0.6))),
                  Partition("validation", _d(int(DAYS * 0.6) + 1), _d(int(DAYS * 0.8))),
                  Partition("oos", _d(int(DAYS * 0.8) + 1), _d(DAYS + 2)))
DATES = (np.datetime64("2024-01-01", "D") + (np.arange(N) // 288)).astype("datetime64[D]")


class _Rolled(Mapping):
    """Same names, event/feature arrays rolled by ``shift`` bars."""

    def __init__(self, base: Mapping, shift: int) -> None:
        self._b, self._s = base, shift

    def __getitem__(self, k):
        return np.roll(np.asarray(self._b[k]), self._s)

    def __iter__(self):
        return iter(self._b)

    def __len__(self):
        return len(self._b)

    def __contains__(self, k):
        return k in self._b


@pytest.fixture(scope="module")
def frame_a() -> MarketFrame:
    return synth_frame(N, seed=3, density=12.0, run_len=(60, 200))


@pytest.fixture(scope="module")
def frame_b(frame_a) -> MarketFrame:
    f = frame_a
    return MarketFrame(f.o, f.h, f.l, f.c, f.atr, f.run_start, f.berlin_minute,
                       _Rolled(f.arrays, 7), f.thresholds)


def _ev(frame, tmp_path=None, **kw) -> TemporalEvaluator:
    if tmp_path is not None:
        kw["cache_dir"] = tmp_path
    return TemporalEvaluator(lambda: frame, market_from_frame(frame), DATES, SPLIT, sizing=SIZING,
                             min_train_trades=15, **kw)


def _genomes(ev, k=6, n=60):
    rng = np.random.default_rng(1)
    out = []
    for _ in range(n):
        g = random_genome(rng, POOL)
        e = ev.evaluate(g, need_base=False)
        if not e.rejected:
            out.append(g)
        if len(out) >= k:
            break
    if not out:
        pytest.skip("no Stage-A survivor")
    return out


def test_disk_cache_without_event_cache_key_fails_closed(frame_a, tmp_path):
    with pytest.raises(ValueError, match="events_cache_key"):
        _ev(frame_a, tmp_path)
    with pytest.raises(ValueError, match="events_cache_key"):
        _ev(frame_a, tmp_path, data_fingerprint="ohlc-only")  # data_fingerprint alone is not enough on disk


def test_in_memory_only_evaluator_needs_no_event_key(frame_a):
    assert _ev(frame_a, data_fingerprint="synthetic").evaluate(_genomes(_ev(frame_a))[0]).genome_hash


def test_same_ohlc_different_event_arrays_never_share_entries(frame_a, frame_b, tmp_path):
    a = _ev(frame_a, tmp_path, events_cache_key="events-A")
    genomes = _genomes(a)
    for g in genomes:
        a.evaluate(g, need_base=False)
    a.flush()
    b = _ev(frame_b, tmp_path, events_cache_key="events-B")
    for g in genomes:
        b.evaluate(g, need_base=False)
    assert b.ledger.cache_hits == 0, "frame B was served frame A's cached result"
    # ... and the results are really different computations, not accidental equality
    fresh = _ev(frame_b, events_cache_key="events-B")
    for g in genomes:
        assert b.evaluate(g, need_base=False).train == fresh.evaluate(g, need_base=False).train


def test_same_frame_twice_shares_entries(frame_a, tmp_path):
    a = _ev(frame_a, tmp_path, events_cache_key="events-A")
    genomes = _genomes(a)
    a.flush()
    a2 = _ev(frame_a, tmp_path, events_cache_key="events-A")
    for g in genomes:
        a2.evaluate(g, need_base=False)
    assert a2.ledger.cache_hits == len(genomes) and a2.sim_count == 0


def test_feature_cache_key_and_version_change_the_key(frame_a, tmp_path):
    base = _ev(frame_a, tmp_path, events_cache_key="e", features_cache_key="f1")
    g = _genomes(base)[0]
    other = _ev(frame_a, tmp_path, events_cache_key="e", features_cache_key="f2")
    assert base.fingerprint(tg.canonical_hash(g), "bk") != other.fingerprint(tg.canonical_hash(g), "bk")


def test_resolved_thresholds_are_part_of_the_key_even_with_equal_provenance(frame_a, tmp_path):
    """Same events key claimed but different resolved thresholds -> behavior_key differs -> no sharing."""
    class _Shift(Mapping):
        def __getitem__(self, k):
            return frame_a.thresholds[k] * 1.5

        def __iter__(self):
            return iter(frame_a.thresholds)

        def __len__(self):
            return len(frame_a.thresholds)

    f = frame_a
    frame_t = MarketFrame(f.o, f.h, f.l, f.c, f.atr, f.run_start, f.berlin_minute, f.arrays, _Shift())
    a = _ev(frame_a, tmp_path, events_cache_key="events-A")
    genomes = [g for g in _genomes(a, 12) if any(isinstance(s, tg.StepGene) for s in g.steps)]
    a.flush()
    t = _ev(frame_t, tmp_path, events_cache_key="events-A")
    changed = 0
    for g in genomes:
        hits = t.ledger.cache_hits
        t.evaluate(g, need_base=False)
        changed += t.ledger.cache_hits == hits
    assert changed >= 1
