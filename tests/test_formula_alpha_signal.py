# ruff: noqa: E501
"""Factor -> CandidateArrays contract, Train-only thresholds, simulate_fast feed, trial ledger."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.common.frame import ENTRY_END_MIN, ENTRY_START_MIN
from alpha.common.sim import COST_SCENARIOS
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, simulate_fast
from alpha.formula import signal as sg
from alpha.formula.evaluate import load_ledger, make_ledger, save_ledger, train_screen
from alpha.formula.fitness import FactorScorer
from alpha.formula.prepare import make_train_view
from alpha.formula.tree import EvalContext, T, canonical_hash, op
from tests.test_formula_alpha_synth import planted_data, split_plan_for

PLANTED = op("Zscore", T("c"), params=(10,))


@pytest.fixture(scope="module")
def world():
    fd, market, dates = planted_data(40, 0, beta=0.25)
    plan = split_plan_for(dates)
    view = make_train_view(fd, market, dates, plan)
    scorer, ctx = FactorScorer(view.data), EvalContext(view.data)
    f = ctx.evaluate(PLANTED)
    score = scorer.score_array(f, 2)
    return dict(view=view, f=f, score=score, fd=fd, market=market, dates=dates, plan=plan)


def test_candidate_contract_and_simulate_fast(world):
    view, f, score = world["view"], world["f"], world["score"]
    assert score.valid and score.sign == -1
    spec = sg.SignalSpec(sign=score.sign, q=0.9, stop_k=1.5, target_r=1.5, cooldown=12)
    thr = sg.fit_thresholds(spec.sign * f, view.data.minute, spec.q, spec.session)
    cands = sg.build_candidates(f, view.data, spec, thr)
    n = len(cands.decision_idx)
    assert n > 10 and isinstance(cands, CandidateArrays)
    assert (np.diff(cands.decision_idx) >= spec.cooldown).all()
    assert np.isnan(cands.target).all() and (cands.exit_kind == EXIT_FIXED_R).all()
    assert np.allclose(cands.target_r, 1.5)
    d, i = cands.direction.astype(int), cands.decision_idx
    assert set(np.unique(d)) <= {-1, 1}
    assert np.allclose(cands.stop, view.data.c[i] - d * 1.5 * view.data.atr[i])
    nm = np.asarray(view.data.minute)[i + 1]
    assert ((nm >= ENTRY_START_MIN) & (nm < ENTRY_END_MIN)).all()  # entry bar inside the window
    assert (view.data.run_start[i + 1] == view.data.run_start[i]).all()  # next bar in the same run
    trades = simulate_fast(view.market, cands, COST_SCENARIOS["BASE"])
    assert len(trades) > 0 and len(trades) <= n
    # a long candidate = g crossing UP through hi; g = sign * f
    g = spec.sign * f
    hi = thr.per_bar(view.data.minute)[0]
    up = d == 1
    assert (g[i[up]] >= hi[i[up]]).all() and (g[i[up] - 1] < hi[i[up]]).all()


def test_cross_semantics_and_cooldown():
    fd, _, _ = planted_data(3, 1)
    n = len(fd)
    g = np.zeros(n)
    g[[110, 111, 112, 130, 210]] = 5.0  # bars 110-112 is one cross; 130 second (cooldown 12 -> kept: 20 apart)
    thr = sg.Thresholds(hi=1.0, lo=-1.0)
    spec = sg.SignalSpec(sign=1, q=0.9, cooldown=12)
    c = sg.build_candidates(g, fd, spec, thr)
    assert list(c.decision_idx) == [110, 130, 210]
    assert (c.direction == 1).all()
    spec2 = sg.SignalSpec(sign=1, q=0.9, cooldown=25)
    assert list(sg.build_candidates(g, fd, spec2, thr).decision_idx) == [110, 210]
    # contrarian factor (sign=-1): the working series is -f; f = -g spikes UP in g' = -f -> LONG again
    c3 = sg.build_candidates(-g, fd, sg.SignalSpec(sign=-1, q=0.9, cooldown=12), thr)
    assert list(c3.decision_idx) == [110, 130, 210] and (c3.direction == 1).all()
    # the same series with sign=-1 is a down-cross of g' = -f through lo -> SHORT, stop ABOVE the close
    c4 = sg.build_candidates(g, fd, sg.SignalSpec(sign=-1, q=0.9, cooldown=12), thr)
    assert list(c4.decision_idx) == [110, 130, 210] and (c4.direction == -1).all()
    assert (c4.stop > fd.c[c4.decision_idx]).all() and (c.stop < fd.c[c.decision_idx]).all()
    with pytest.raises(ValueError):
        sg.SignalSpec(sign=0)
    with pytest.raises(ValueError):
        sg.SignalSpec(sign=1, q=0.4)


def test_thresholds_use_only_given_bars_and_session_table():
    rng = np.random.default_rng(0)
    g = rng.normal(size=4000)
    minute = (np.arange(4000) % 96) * 5 + 8 * 60
    a = sg.fit_thresholds(g[:3000], minute[:3000], 0.9, False)
    g2 = g.copy()
    g2[3000:] += 100.0  # later bars must not matter when only the first 3000 are passed
    b = sg.fit_thresholds(g2[:3000], minute[:3000], 0.9, False)
    assert (a.hi, a.lo) == (b.hi, b.lo) and a.hi > 1.0 > -1.0 > a.lo
    s = sg.fit_thresholds(g[:3000], minute[:3000], 0.9, True)
    assert s.hi_bucket is not None and np.isfinite(np.asarray(s.hi_bucket)[9])
    hi, lo = s.per_bar(minute[:200])
    assert hi.shape == lo.shape == (200,) and (hi > lo).all()
    # a per-bucket threshold differs from the global one when the bucket distribution differs
    g3 = g[:3000] + np.where(minute[:3000] // 60 == 9, 3.0, 0.0)
    s3 = sg.fit_thresholds(g3, minute[:3000], 0.9, True)
    assert np.asarray(s3.hi_bucket)[9] > s3.hi + 1.0
    with pytest.raises(ValueError):
        sg.fit_thresholds(g[:10], minute[:10], 0.9, False)


def test_candidates_are_causal_prefix_invariant(world):
    view, f = world["view"], world["f"]
    spec = sg.SignalSpec(sign=-1, q=0.9)
    thr = sg.fit_thresholds(-f, view.data.minute, 0.9, False)
    full = sg.build_candidates(f, view.data, spec, thr)
    m = 1500
    pref = sg.build_candidates(f[:m], view.data.prefix(m), spec, thr)
    keep = full.decision_idx < m - 1
    # cooldown chain is identical up to the cut (candidates only look back)
    assert np.array_equal(pref.decision_idx[pref.decision_idx < m - 1], full.decision_idx[keep])
    assert np.array_equal(pref.stop[pref.decision_idx < m - 1], full.stop[keep])


def test_train_screen_and_sign_guard(world):
    view, f, score = world["view"], world["f"], world["score"]
    res = train_screen(view, f, score, sg.SignalSpec(sign=score.sign, q=0.9))
    assert res is not None and res.n_candidates > 10
    assert res.adverse.n_trades > 0 and res.candidates_per_day > 0
    assert res.adverse.expectancy_r is not None and res.base.n_trades >= res.adverse.n_trades
    assert res.base.expectancy_r >= res.adverse.expectancy_r - 1e-9  # costs only hurt
    with pytest.raises(ValueError):
        train_screen(view, f, score, sg.SignalSpec(sign=-score.sign))


def test_trial_ledger_roundtrip(tmp_path, world):
    led = make_ledger()
    assert led.record(PLANTED, "structural") == "new"
    assert led.record(op("Zscore", T("c"), params=(10,)), "structural") == "duplicate"
    assert led.record(op("MA", T("c"), params=(7,)), "structural") == "invalid"
    assert led.record(PLANTED, "param") == "duplicate"
    assert (led.total_trials, led.structural_trials, led.param_trials, led.unique, led.invalid_rejects) == (4, 3, 1, 1, 1)
    save_ledger(led, tmp_path / "l.json")
    back = load_ledger(tmp_path / "l.json")
    assert back.total_trials == 4 and canonical_hash(PLANTED) in back.seen
    assert back.record(PLANTED, "structural") == "duplicate" and back.total_trials == 5
