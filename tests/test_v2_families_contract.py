# ruff: noqa: E501
"""Candidate contract + the simulator feed with the per-market window (GER40-like and New-York-like calendars)."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.families import registry as R
from alpha.families.data import build_family_data, build_leader_features
from alpha.families.evaluate import ADVERSE, SimContext, evaluate_grid, simulate
from alpha.families.spec import MarketCalendar
from alpha.fast.sim import EXIT_FIXED_R, REASON_SESSION_END, CandidateArrays
from tests.test_v2_families_causality import leader_cross
from tests.test_v2_families_synth import CAL, synth_frame

NY = MarketCalendar("America/New_York", 570, 960, 570, 900, 955)


def make(cal: MarketCalendar):
    kw = {} if cal == CAL else {"tz": "America/New_York", "cash_open_min": 570, "cash_close_min": 960}
    frame = synth_frame(n_days=90, seed=5, plant="orb_break", **kw)
    lead, _ = leader_cross(frame)
    cross = {"SPX500": build_leader_features(frame, lead, cal, "SPX500")}
    return build_family_data(frame, cal, name="SYN", point_size=0.01, tick_size=0.01, cross=cross)


@pytest.fixture(scope="module", params=["GER", "NY"])
def world(request):
    cal = CAL if request.param == "GER" else NY
    d = make(cal)
    return d, SimContext.default(d)


def specs_of(fam: str, k: int = 12):
    g = R.grid_for(fam, "NAS100" if fam == "LEADLAG" else "GER40", None)
    if fam == "LEADLAG":
        g = [s for s in g if s.leader == "SPX500"]
    return g[:: max(1, len(g) // k)][:k]


def check_contract(d, spec, c: CandidateArrays) -> None:
    win = spec.effective_window(d.cal)
    assert c.decision_idx.dtype == np.int64 and c.direction.dtype == np.int8 and c.exit_kind.dtype == np.int8
    if len(c.decision_idx) == 0:
        return
    i = c.decision_idx
    assert (np.diff(i) > 0).all() and i.min() >= 0 and i.max() < len(d) - 1
    assert set(np.unique(c.direction)) <= {-1, 1}
    assert (c.exit_kind == EXIT_FIXED_R).all()
    assert d.contig_next[i].all() and (d.day[i + 1] == d.day[i]).all()
    nm = d.minute[i + 1]
    assert ((nm >= win.entry_start_min) & (nm < win.entry_end_min)).all()  # entry bar inside the EFFECTIVE window
    assert np.isfinite(d.atr[i]).all()
    dr = c.direction.astype(float)
    assert (dr * (d.c[i] - c.stop) > 0).all()  # stop on the loss side
    fin = np.isfinite(c.target)
    assert (dr[fin] * (c.target[fin] - d.c[i][fin]) > 0).all()  # finite target on the profit side
    assert (c.target_r[~fin] > 0).all()


@pytest.mark.parametrize("fam", R.FAMILY_NAMES)
def test_candidates_satisfy_contract_and_feed_the_simulator(world, fam):
    d, ctx = world
    total_trades = 0
    for spec in specs_of(fam):
        thr = R.fit_thresholds(d.prefix(9000), spec)
        c = R.generate_candidates(d, spec, thr)
        check_contract(d, spec, c)
        if len(c.decision_idx) == 0:
            continue
        tr = simulate(ctx, spec, c, ADVERSE)
        total_trades += len(tr)
        assert len(tr) <= len(c.decision_idx)
        win = spec.effective_window(d.cal)
        if len(tr):
            em = d.minute[tr.entry_idx]
            assert ((em >= win.entry_start_min) & (em < win.entry_end_min)).all()
            xm = d.minute[tr.exit_idx]
            sess = tr.exit_reason == REASON_SESSION_END
            assert (xm[sess] >= win.exit_min).all()  # clock exits happen at/after the clock
            assert (d.minute[tr.entry_idx] < win.exit_min).all()
            assert np.isfinite(tr.r_multiple).all()
    assert total_trades > 5, fam


def test_clock_exit_is_earlier_than_the_forced_flat(world):
    """exit_clock='close' really ends positions at the cash close (VOLREV / LEADLAG / EOD 'T')."""
    d, ctx = world
    specs = [s for s in R.grid_for("VOLREV", "GER40", None) if s.exit_clock == "close"][:40]
    n_sess = 0
    for spec in specs:
        thr = R.fit_thresholds(d.prefix(9000), spec)
        tr = simulate(ctx, spec, R.generate_candidates(d, spec, thr), ADVERSE)
        if len(tr):
            sess = tr.exit_reason == REASON_SESSION_END
            n_sess += int(sess.sum())
            assert (d.minute[tr.exit_idx][sess] >= spec.exit_min(d.cal)).all()
            assert (d.minute[tr.exit_idx] <= spec.exit_min(d.cal)).all()
    assert n_sess > 0


def test_evaluate_grid_runs_and_reports_consistent_numbers(world):
    d, ctx = world
    specs = R.grid_for("ORB", "GER40", 24)
    res, cands = evaluate_grid(d, ctx, specs, min_trades=20)
    assert len(res) == len(cands) == 24
    for r, c in zip(res, cands, strict=True):
        assert r.n_candidates == len(c.decision_idx) and r.n_trades <= r.n_candidates
        assert r.spec_hash == r.spec.canonical_hash()
        if r.n_trades >= 20:
            assert r.q_bh is not None and 0.0 <= r.p_one_sided <= r.q_bh <= 1.0
            assert r.cost_burden_adv is not None and r.cost_burden_adv > 0
        json_row = r.row()
        assert json_row["hash"] == r.spec_hash
