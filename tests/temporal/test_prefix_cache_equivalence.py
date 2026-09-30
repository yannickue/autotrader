"""Prefix sharing / caching / batching / workers must not change a single byte of any result."""

from __future__ import annotations

import random

import numpy as np
import pytest

from alpha.temporal import batch
from alpha.temporal.evaluate import (
    PrefixCache,
    SharedFrame,
    evaluate_temporal_full,
    evaluate_temporal_many,
)
from scripts.bench_temporal import freeze_frame, golden_like, random_specs, synth_frame

N_SPECS = 160


@pytest.fixture(scope="module")
def corpus():
    specs = random_specs(N_SPECS, seed=21)
    rng = random.Random(3)
    # golden-structure variants share long prefixes: the trie must actually share
    specs += [golden_like(rng, 900 + i) for i in range(40)]
    frame = freeze_frame(specs, synth_frame(1800, 4, 6.0, (60, 250)))
    return specs, frame


@pytest.fixture(scope="module")
def reference_bytes(corpus):
    specs, frame = corpus
    return [evaluate_temporal_full(s, frame).to_bytes() for s in specs]


def _bytes(results):
    return [r.to_bytes() for r in results]


def test_cache_on_equals_cache_off_equals_single(corpus, reference_bytes):
    specs, frame = corpus
    on = _bytes(evaluate_temporal_many(specs, frame, use_cache=True))
    off = _bytes(evaluate_temporal_many(specs, frame, use_cache=False))
    assert on == reference_bytes
    assert off == reference_bytes
    assert len({len(b) for b in on}) > 3  # results are not all empty/identical


def _family(frame, n: int = 30):
    """Specs sharing anchor + first transitions, differing only after the shared prefix."""
    from dataclasses import replace

    base = next(
        b for b in (golden_like(random.Random(sd), 0) for sd in range(11, 400))
        if len(b.states) >= 3
    )
    fam = []
    for i in range(n):
        last = replace(base.states[-1], within=(None, 2, 3, 5, 8, 12, 24, 48)[i % 8])
        fam.append(replace(
            base, strategy_id=f"fam{i}", states=(*base.states[:-1], last),
            session_window=(None, (480, 1050), (540, 1200))[i % 3],
            stop=replace(base.stop, buffer_atr=(0.0, 0.1, 0.25)[i % 3]),
        ))
    return fam


def test_trie_actually_shares_prefixes(corpus):
    _, frame = corpus
    fam = _family(frame)
    frame = freeze_frame(fam, synth_frame(1800, 4, 6.0, (60, 250)))
    st = batch.BatchStats()
    got = evaluate_temporal_many(fam, frame, stats=st)
    assert st.stages_computed < st.stage_demands
    assert st.hit_rate > 0.5  # 30 specs, ~4 distinct final stages
    assert [r.to_bytes() for r in got] == [evaluate_temporal_full(s, frame).to_bytes() for s in fam]


def test_shuffled_batch_order_identical(corpus, reference_bytes):
    specs, frame = corpus
    order = list(range(len(specs)))
    random.Random(1).shuffle(order)
    res = evaluate_temporal_many([specs[i] for i in order], frame)
    got = {i: r.to_bytes() for i, r in zip(order, res, strict=True)}
    assert [got[i] for i in range(len(specs))] == reference_bytes


def test_same_spec_twice_identical(corpus, reference_bytes):
    specs, frame = corpus
    r = evaluate_temporal_many([specs[7], specs[7], specs[7]], frame)
    assert r[0].to_bytes() == r[1].to_bytes() == r[2].to_bytes() == reference_bytes[7]


def test_tiny_cache_budget_and_warm_cache_identical(corpus, reference_bytes):
    specs, frame = corpus
    tiny = PrefixCache(budget_bytes=20_000)  # evicts constantly, stores almost nothing
    assert _bytes(evaluate_temporal_many(specs, frame, cache=tiny)) == reference_bytes
    assert tiny.bytes <= 20_000
    warm = PrefixCache(budget_bytes=256 << 20)
    st1, st2 = batch.BatchStats(), batch.BatchStats()
    a = evaluate_temporal_many(specs, frame, cache=warm, stats=st1)
    b = evaluate_temporal_many(list(reversed(specs)), frame, cache=warm, stats=st2)
    assert _bytes(a) == reference_bytes
    assert _bytes(b) == list(reversed(reference_bytes))
    assert st2.stages_computed == 0 and st2.cache_hits > 0  # second pass fully from the cache


def test_advance_modes_scan_and_jump_agree(corpus, reference_bytes):
    specs, frame = corpus
    for mode in (1, 2):
        res = batch.evaluate_many_local(specs, frame, use_cache=False, advance_mode=mode)
        assert _bytes(res) == reference_bytes, mode


def test_one_vs_two_workers_identical_bytes(corpus, reference_bytes):
    specs, frame = corpus
    sub = specs[:80]
    with SharedFrame(frame) as sh:
        two = evaluate_temporal_many(sub, frame, workers=2, shared=sh)
    assert _bytes(two) == reference_bytes[:80]
    assert all(isinstance(r.candidates.stop, np.ndarray) for r in two)
