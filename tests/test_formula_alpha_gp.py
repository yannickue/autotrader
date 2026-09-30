# ruff: noqa: E501
"""DEAP formula GP: determinism, improvement, planted-signal recovery vs random baseline, budget, null hook."""

from __future__ import annotations

import dataclasses
import random
from pathlib import Path

import numpy as np
import pytest

from alpha.formula import gp, tree
from alpha.formula.data import FormulaData
from alpha.formula.evaluate import make_ledger
from alpha.formula.fitness import FactorScorer, forward_labels
from alpha.formula.prepare import make_train_view
from alpha.formula.tree import EvalContext, T, op
from tests.test_formula_alpha_synth import planted_data, split_plan_for

SEEDS = (0, 1, 2, 3, 4, 5)
BUDGET, POP = 400, 40


@pytest.fixture(scope="module")
def world():
    fd, market, dates = planted_data(40, 0, beta=0.25)
    scorer, ctx = FactorScorer(fd), EvalContext(fd)
    pset = gp.build_pset(list(fd.terminals))
    gps = [gp.evolve(scorer, ctx, pset, s, max_evals=BUDGET, pop_size=POP) for s in SEEDS]
    rnd = [gp.random_baseline(scorer, ctx, pset, s + 100, max_evals=BUDGET) for s in SEEDS]
    planted = scorer.score_array(ctx.evaluate(op("Zscore", T("c"), params=(10,))), 2)
    return dict(fd=fd, scorer=scorer, ctx=ctx, pset=pset, gps=gps, rnd=rnd, planted=planted, market=market, dates=dates)


def _best(res):
    return res.ranked()[0].fitness


def test_gp_improves_over_generations(world):
    improved = 0
    for r in world["gps"]:
        assert r.generations >= 3
        improved += r.gen_best[-1] > r.gen_best[0]
        assert r.gen_best[-1] >= r.gen_best[0] - 1e-9
    assert improved >= 2


def test_gp_beats_random_baseline_on_planted_signal(world):
    g = np.array([_best(r) for r in world["gps"]])
    r = np.array([_best(x) for x in world["rnd"]])
    assert g.mean() > r.mean() + 0.02
    assert (g > r).sum() >= 4
    med_g = [np.median(x.fitness_values()) for x in world["gps"]]
    med_r = [np.median(x.fitness_values()) for x in world["rnd"]]
    assert sum(a > b for a, b in zip(med_g, med_r, strict=True)) >= 5  # GP concentrates on better formulas
    # the planted signal (mean reversion of a trailing z-score) is recovered at a comparable level
    assert g.max() >= 0.8 * world["planted"].fitness
    assert world["planted"].sign == -1  # mean reversion: fitted direction is negative


def test_budget_limits_and_ledger(world):
    for r in (*world["gps"], *world["rnd"]):
        assert r.evals <= BUDGET
        assert len(r.records) <= BUDGET
        for rec in r.records.values():
            tree.validate(rec.node)
            assert tree.depth(rec.node) <= tree.MAX_DEPTH and tree.size(rec.node) <= tree.MAX_NODES
    led = make_ledger()
    fd = world["fd"]
    res = gp.evolve(world["scorer"], EvalContext(fd), world["pset"], 7, max_evals=60, pop_size=20, ledger=led)
    assert led.total_trials >= res.evals and led.unique <= led.total_trials
    assert led.structural_trials == led.total_trials and led.param_trials == 0
    assert led.duplicate_rejects + led.unique + led.invalid_rejects == led.total_trials


def test_determinism_per_seed(world):
    fd = world["fd"]
    a = gp.evolve(FactorScorer(fd), EvalContext(fd), world["pset"], 5, max_evals=80, pop_size=20)
    b = gp.evolve(FactorScorer(fd), EvalContext(fd), world["pset"], 5, max_evals=80, pop_size=20)
    c = gp.evolve(FactorScorer(fd), EvalContext(fd), world["pset"], 6, max_evals=80, pop_size=20)
    assert sorted(a.records) == sorted(b.records) and a.gen_best == b.gen_best
    assert [r.fitness for r in a.ranked()] == [r.fitness for r in b.ranked()]
    assert sorted(a.records) != sorted(c.records)
    state = random.getstate()
    gp.random_baseline(FactorScorer(fd), EvalContext(fd), world["pset"], 1, max_evals=10)
    assert random.getstate() == state  # global RNG restored


def test_hof_is_decorrelated_and_niches_exist(world):
    r = world["gps"][0]
    assert 1 <= len(r.hof) <= 20
    sigs = np.stack([h.sig for h in r.hof])
    corr = np.abs(sigs @ sigs.T)
    np.fill_diagonal(corr, 0)
    assert corr.max() < gp.CORR_CLUSTER + 1e-6
    assert len(r.archive) >= 3 and r.n_clusters >= 2
    assert gp.decorrelated_count(list(r.records.values()), top=50) >= 2
    fams = {rec.family for rec in r.records.values()}
    assert len(fams) >= 3


def test_label_never_reaches_factor_inputs(world):
    fd = world["fd"]
    fields = {f.name for f in dataclasses.fields(FormulaData)}
    assert not any("label" in f or "fwd" in f or "forward" in f for f in fields | set(fd.arrays))
    root = Path(gp.__file__).parent
    for mod in ("ops.py", "tree.py", "data.py", "signal.py"):
        src = (root / mod).read_text(encoding="utf-8")
        assert "forward_labels" not in src and "from alpha.formula.fitness" not in src, mod
    # evaluating factors must not touch the label builder at all
    import alpha.formula.fitness as fit

    orig = fit.forward_labels
    fit.forward_labels = lambda *a, **k: (_ for _ in ()).throw(AssertionError("label touched"))  # type: ignore[assignment]
    try:
        ctx = EvalContext(fd)
        for n in [op("Zscore", T("c"), params=(10,)), op("Ret", T("b_ret1"), params=(5,))]:
            ctx.evaluate(n)
    finally:
        fit.forward_labels = orig
    # labels do look forward (by design) ... and are the ONLY thing that does
    labs = forward_labels(fd, (6,))[6]
    i = 150
    assert labs[i] == pytest.approx((fd.c[i + 6] - fd.c[i]) / fd.atr[i])
    starts = np.unique(fd.run_start)
    assert np.isnan(labs[starts[3] - 6: starts[3]]).all()  # label never crosses a run (day) boundary


def test_factor_independent_of_future_bars_search_view_has_no_later_partition(world):
    """The GP sees the Train slice only: rewriting every later bar of the development frame changes nothing."""
    fd, market, dates = world["fd"], world["market"], world["dates"]
    plan = split_plan_for(dates)
    view = make_train_view(fd, market, dates, plan)
    assert len(view.data) < len(fd) and view.dates.max() <= np.datetime64(plan.train.end)
    tail = view.stop
    o, h, lo, c, atr = (a.copy() for a in (fd.o, fd.h, fd.l, fd.c, fd.atr))
    rng = np.random.default_rng(1)
    for a in (o, h, lo, c):
        a[tail:] = rng.normal(500, 50, len(a) - tail)
    arrays = {k: v.copy() for k, v in fd.arrays.items()}
    for k in ("c", "h", "l", "o"):
        arrays[k] = {"c": c, "h": h, "l": lo, "o": o}[k]
    fd2 = FormulaData(o, h, lo, c, atr, fd.run_start, fd.minute, arrays)
    view2 = make_train_view(fd2, market, dates, plan)
    for name in ("c", "h", "atr"):
        np.testing.assert_array_equal(getattr(view.data, name), getattr(view2.data, name))
    r1 = gp.evolve(FactorScorer(view.data), EvalContext(view.data), world["pset"], 3, max_evals=60, pop_size=20)
    r2 = gp.evolve(FactorScorer(view2.data), EvalContext(view2.data), world["pset"], 3, max_evals=60, pop_size=20)
    assert [(k, v.fitness) for k, v in sorted(r1.records.items())] == [(k, v.fitness) for k, v in sorted(r2.records.items())]


def test_null_hook_separates_planted_signal(world):
    scorer, ctx = world["scorer"], world["ctx"]
    planted = op("Zscore", T("c"), params=(10,))
    noise = op("MA", T("noise0"), params=(5,))
    dist = gp.null_fitness_distribution(scorer, ctx, [planted, noise], n_shifts=12, seed=4)
    assert dist.shape == (12, 2)
    real_planted = scorer.score_array(ctx.evaluate(planted), 2).fitness
    real_noise = scorer.score_array(ctx.evaluate(noise), 2).fitness
    assert real_planted > np.nanmax(dist[:, 0])  # beats every shifted-label draw
    assert real_noise <= np.nanmax(dist[:, 1]) + 0.15  # a noise formula is NOT separated
    rows = gp.run_null_frames(lambda s: planted_data(20, 100 + s, beta=0.0)[0], seeds=(1,), max_evals=40, pop_size=15)
    assert rows[0]["gp_best"] is not None and rows[0]["random_best"] is not None
