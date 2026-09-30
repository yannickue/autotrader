# ruff: noqa: E501
"""Planted-edge recovery and the null band: the pipeline finds a planted family and stays quiet on a random walk."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.families import registry as R
from alpha.families.evaluate import SimContext, evaluate_grid, null_calibration
from tests.test_v2_families_synth import synth_data


def run_family(d, ctx, fam: str, n_specs: int, n_shifts: int = 12, seed: int = 1):
    specs = R.grid_for(fam, "GER40", n_specs)
    res, cands = evaluate_grid(d, ctx, specs)
    nul = null_calibration(d, ctx, specs, cands, res, n_shifts=n_shifts, seed=seed)
    return specs, res, nul


@pytest.fixture(scope="module")
def gap_world():
    d = synth_data(n_days=300, seed=0, plant="gap_fade", phi=0.4)
    return d, SimContext.default(d)


def test_planted_gap_fade_is_recovered_by_gap_fade(gap_world):
    d, ctx = gap_world
    _, res, nul = run_family(d, ctx, "GAP", 400)
    best = max(res, key=lambda r: r.fitness)
    assert best.spec.mode == "fade" and best.spec.mode != "go"
    assert best.t_adv > 4.0 and best.exp_adv > 0.3
    fade = max((r.t_adv for r in res if r.spec.mode == "fade" and r.t_adv is not None), default=-9)
    go = max((r.t_adv for r in res if r.spec.mode == "go" and r.t_adv is not None), default=-9)
    assert fade > 4.0 > go  # only the planted direction works
    assert best.q_bh is not None and best.q_bh < 0.05  # survives BH inside the grid
    assert nul["fitness"]["pct"] == 1.0 and nul["fitness"]["p_best"] < 0.1  # and beats the best-of-N null


def test_planted_gap_fade_does_not_light_up_unrelated_families(gap_world):
    d, ctx = gap_world
    for fam in ("ROUND", "EOD", "ORB"):
        _, res, _ = run_family(d, ctx, fam, 120, n_shifts=4)
        best_t = max((r.t_adv for r in res if r.t_adv is not None), default=-9)
        assert best_t < 3.0, (fam, best_t)


def test_planted_orb_continuation_is_recovered_by_orb_breakout():
    d = synth_data(n_days=300, seed=0, plant="orb_break", psi=0.6)
    ctx = SimContext.default(d)
    _, res, nul = run_family(d, ctx, "ORB", 288)
    best = max(res, key=lambda r: r.fitness)
    assert best.spec.mode == "breakout" and best.t_adv > 4.0 and best.q_bh < 0.05
    assert nul["fitness"]["pct"] == 1.0


@pytest.mark.parametrize("seed", [0, 1])
@pytest.mark.parametrize("fam", ["ORB", "GAP", "VOLREV", "ROUND"])
def test_random_walk_stays_inside_the_null_band(seed, fam):
    """Pinned seeds: on a pure random walk the best of the grid is not distinguishable from the day-shifted null
    and nothing survives BH (a regression guard for look-ahead / cost-free-lunch bugs)."""
    d = synth_data(n_days=300, seed=seed)
    ctx = SimContext.default(d)
    _, res, nul = run_family(d, ctx, fam, 100, n_shifts=12, seed=3)
    assert nul["fitness"]["p_best"] > 0.05 and nul["expectancy_r_adverse"]["p_best"] > 0.05
    assert all(r.q_bh is None or r.q_bh > 0.10 for r in res)
    best_t = max((r.t_adv for r in res if r.t_adv is not None), default=-9)
    assert best_t < 3.5
    exps = [r.exp_adv for r in res if r.n_trades >= 40 and r.exp_adv is not None]
    assert np.median(exps) < 0.05  # costs make the typical spec a loser; no cost-free lunch
