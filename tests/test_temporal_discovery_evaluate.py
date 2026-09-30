# ruff: noqa: E501
"""TemporalEvaluator on a SYNTHETIC frame: ledger accounting, sealing, cache, twins, DEAP + Optuna drivers."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from alpha.common.protocol import Partition, SplitPlan
from alpha.common.sim import SizingSpec
from alpha.discovery import temporal_genome as tg
from alpha.discovery.fitness import train_fitness
from alpha.discovery.temporal_archetypes import random_genome
from alpha.discovery.temporal_evaluate import (
    TemporalEvaluator,
    TemporalTrialLedger,
    market_from_frame,
    temporal_validation_gate_view,
)
from alpha.discovery.temporal_search import evolve_temporal, optimize_temporal_structure
from alpha.temporal.reference import MarketFrame
from scripts.bench_temporal import synth_frame

N = 30_000
MIN_TRADES = 15
POOL = tg.EventPool.full()
SIZING = SizingSpec(min_risk_pts=0.5, max_risk_pts=60.0)
DAYS = N // 288


def _d(k: int) -> str:
    return str(np.datetime64("2024-01-01", "D") + np.timedelta64(k, "D"))


SPLIT = SplitPlan(Partition("train", _d(0), _d(int(DAYS * 0.6))),
                  Partition("validation", _d(int(DAYS * 0.6) + 1), _d(int(DAYS * 0.8))),
                  Partition("oos", _d(int(DAYS * 0.8) + 1), _d(DAYS + 2)))
DATES = (np.datetime64("2024-01-01", "D") + (np.arange(N) // 288)).astype("datetime64[D]")


@pytest.fixture(scope="module")
def frame() -> MarketFrame:
    return synth_frame(N, seed=3, density=12.0, run_len=(60, 200))


def _evaluator(frame, **kw) -> TemporalEvaluator:
    return TemporalEvaluator(lambda: frame, market_from_frame(frame), DATES, SPLIT, sizing=SIZING,
                             min_train_trades=MIN_TRADES, **kw)


def _good(ev, n=80, seed=1, need=None):
    rng = np.random.default_rng(seed)
    for _ in range(n):
        g = random_genome(rng, POOL)
        e = ev.evaluate(g, need_base=False)
        if not e.rejected and (need is None or need(g, e)):
            return g, e
    pytest.skip("no Stage-A survivor among random genomes")


def test_frame_provider_is_lazy_and_called_once(frame):
    calls = []
    ev = TemporalEvaluator(lambda: calls.append(1) or frame, market_from_frame(frame), DATES, SPLIT,
                           sizing=SIZING, min_train_trades=MIN_TRADES)
    assert calls == []
    g, _ = _good(ev)
    ev.evaluate(g)
    assert calls == [1]


def test_ledger_accounting(frame):
    led = TemporalTrialLedger()
    ev = _evaluator(frame, ledger=led)
    g, _e = _good(ev)
    led2 = TemporalTrialLedger()  # a clean ledger for exact counting on the same genome
    ev = _evaluator(frame, ledger=led2)
    a = ev.evaluate(g, kind="structural")
    assert (led2.total_trials, led2.structural_trials, led2.param_trials, led2.unique) == (1, 1, 0, 1)
    b = ev.evaluate(g, kind="param")  # duplicate canonical hash: counted as a trial, served from cache
    assert (led2.total_trials, led2.structural_trials, led2.param_trials) == (2, 1, 1)
    assert led2.duplicate_rejects == 1 and led2.cache_hits == 1 and led2.unique == 1
    assert a.genome_hash == b.genome_hash and a.train == b.train
    bad = ev.evaluate(replace(g, steps=()), kind="structural")
    assert bad.reject == "invalid_genome" and led2.invalid_rejects == 1 and led2.total_trials == 3
    assert led2.unique_behaviors == 1 and led2.behaviors
    with pytest.raises(ValueError):
        ev.evaluate(g, kind="nonsense")
    # JSON round trip keeps every counter incl. twins
    back = TemporalTrialLedger.from_json(led2.to_json())
    assert (back.total_trials, back.param_trials, back.structural_trials, back.cache_hits,
            back.invalid_rejects, back.unique, back.unique_behaviors) == (
        led2.total_trials, led2.param_trials, led2.structural_trials, led2.cache_hits,
        led2.invalid_rejects, led2.unique, led2.unique_behaviors)


def test_ledger_callables_are_generalised():
    led = TemporalTrialLedger(validate_fn=lambda g: (_ for _ in ()).throw(ValueError("no")) if g == "bad" else None,
                              hash_fn=lambda g: f"h:{g}")
    assert [led.record(x) for x in ("a", "bad", "a", "b")] == ["new", "invalid", "duplicate", "new"]
    assert (led.total_trials, led.invalid_rejects, led.duplicate_rejects, led.unique) == (4, 1, 1, 2)


def test_lean_equals_full_train_and_validation_is_sealed(frame):
    ev = _evaluator(frame)
    g, lean = _good(ev)
    full = ev.ensure_full(lean, g)
    assert lean.train.adverse == full.train.adverse
    assert train_fitness(lean.train, MIN_TRADES) == train_fitness(full.train, MIN_TRADES)
    assert not lean.is_full and full.is_full and full.train.base is not None
    with pytest.raises(ValueError):
        temporal_validation_gate_view(lean)
    val = temporal_validation_gate_view(full)
    assert val.adverse.screen.n_trades >= 0
    assert all("valid" not in f for f in full.train.__dataclass_fields__)  # TrainView has no validation field


def test_train_view_is_invariant_to_validation_data(frame):
    """Scramble prices on Validation/OOS days only: the Train view (hence fitness) is bit-identical."""
    ev = _evaluator(frame)
    g, lean = _good(ev)
    full = ev.ensure_full(lean, g)
    day = DATES.astype("datetime64[D]")
    later = day > np.datetime64(SPLIT.train.end)
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 5.0, N)
    kw = {k: getattr(frame, k).copy() for k in ("o", "h", "l", "c")}
    for k in kw:
        kw[k][later] += noise[later]
    frame2 = MarketFrame(kw["o"], kw["h"], kw["l"], kw["c"], frame.atr, frame.run_start, frame.berlin_minute,
                         frame.arrays, frame.thresholds)
    ev2 = _evaluator(frame2)
    full2 = ev2.ensure_full(ev2.evaluate(g, need_base=False), g)
    assert full2.train == full.train and full2.twin_hash == full.twin_hash
    assert temporal_validation_gate_view(full2) != temporal_validation_gate_view(full)


def test_twin_detection_by_train_decision_stream(frame):
    led = TemporalTrialLedger()
    ev = _evaluator(frame, ledger=led)
    g, e1 = _good(ev, need=lambda g, e: g.target.kind == "fixed_r")
    g2 = replace(g, target=replace(g.target, r=3.5 if g.target.r != 3.5 else 1.5))
    e2 = ev.evaluate(g2, need_base=False)
    assert tg.canonical_hash(g) != tg.canonical_hash(g2)
    assert e1.twin_hash and e1.twin_hash == e2.twin_hash  # same TRAIN decisions, different exits
    assert e1.behavior_key != e2.behavior_key
    assert led.behavioral_twins >= 1 and e2.genome_hash in led.twins[e1.twin_hash]


def test_result_cache_persists_across_evaluators(frame, tmp_path):
    ev = _evaluator(frame, cache_dir=tmp_path)
    g, lean = _good(ev)
    ev.flush()
    ev2 = _evaluator(frame, cache_dir=tmp_path)
    again = ev2.evaluate(g, need_base=False)
    assert ev2.sim_count == 0 and ev2.ledger.cache_hits == 1 and again.train == lean.train


def test_evolve_improves_is_valid_deterministic_and_qd(frame):
    def run(seed, **kw):
        ev = _evaluator(frame)
        return ev, evolve_temporal(ev, POOL, population=24, generations=5, seed=seed, **kw)

    ev, res = run(7)
    assert len(res.stats) == 6
    assert res.stats[-1].best > res.stats[0].best  # improves on the synthetic frame
    assert res.stats[-1].best >= max(s.best for s in res.stats) - 1e-12  # elitist
    for c in res.population + res.hall_of_fame:
        tg.validate(c.genome)
        assert not c.evaluation.rejected and c.genome_hash == tg.canonical_hash(c.genome)
    assert len(set(res.hof_hashes)) == len(res.hof_hashes)
    twins = [c.evaluation.twin_hash for c in res.hall_of_fame]
    assert len(twins) == len(set(twins))  # no behavioural twin pair in the hall of fame
    assert res.archive.n_niches >= 3 and all(res.archive.occupancy(k) == 1 for k in res.archive.cells)
    assert all(s.unique_hashes == sum(s.lineage_histogram.values()) for s in res.stats)
    led = ev.ledger
    assert led.param_trials == 0 and led.structural_trials >= res.evaluations_used
    assert led.unique <= led.structural_trials and led.unique_behaviors <= led.unique
    # deterministic per seed
    _, res2 = run(7)
    assert res2.hof_hashes == res.hof_hashes and [s.best for s in res2.stats] == [s.best for s in res.stats]
    assert run(8)[1].hof_hashes != res.hof_hashes


def test_evolve_respects_budget_and_family_cap(frame):
    ev = _evaluator(frame)
    res = evolve_temporal(ev, POOL, population=16, generations=6, seed=3, max_evaluations=30, lineage_cap=0.25)
    assert res.evaluations_used <= 30 and res.budget_exhausted
    for s in res.stats:
        if not s.cap_relaxed:
            assert max(s.lineage_histogram.values()) <= 4  # ceil(0.25 * 16)


def test_optuna_structure_search_counts_param_trials(frame):
    led = TemporalTrialLedger()
    ev = _evaluator(frame, ledger=led)
    g, _ = _good(ev)
    before = led.param_trials
    study = optimize_temporal_structure(ev, g, n_trials=12, seed=1)
    assert led.param_trials - before == 12 and study.n_trials == 12
    for c in study.top:
        tg.validate(c.genome)
        assert not c.evaluation.rejected
    if study.top:
        fits = [c.fitness for c in study.top]
        assert fits == sorted(fits, reverse=True) and study.best_fitness == fits[0]
