# ruff: noqa: E501
"""Causality of every family generator: prefix equality of the features, truncation invariance and future
perturbation of the candidates (synthetic frame and a REAL GER40 slice), leader-market perturbation (LEADLAG)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha.families import registry as R
from alpha.families.data import FamilyData, build_family_data, build_leader_features
from alpha.fast.sim import CandidateArrays
from tests.test_v2_families_synth import CAL, perturb_future, synth_frame

ROOT = Path(__file__).resolve().parents[1]
FAMS = R.FAMILY_NAMES
CAND_FIELDS = ("decision_idx", "direction", "stop", "target", "target_r", "exit_kind")


def sample_specs(fam: str, k: int = 8):
    g = R.grid_for(fam, "GER40", None) if fam != "LEADLAG" else R.grid_for(fam, "NAS100", None)
    if fam == "LEADLAG":
        g = [s for s in g if s.leader == "SPX500"]
    step = max(1, len(g) // k)
    return g[::step][:k]


def leader_cross(frame: pd.DataFrame, seed: int = 7) -> tuple[pd.DataFrame, dict]:
    rng = np.random.default_rng(seed)
    lf = frame.copy()
    # correlated leader: follower returns + noise, same timestamps (fully overlapping calendars)
    c = frame["close"].to_numpy(float)
    ret = np.diff(c, prepend=c[0])
    lc = 15000.0 + np.cumsum(0.7 * np.roll(ret, -1) + rng.normal(0, 3.0, len(c)))  # LEADS the follower by one bar
    lo_ = np.r_[lc[0], lc[:-1]]
    lf["open"], lf["close"] = lo_, lc
    lf["high"] = np.maximum(lo_, lc) + np.abs(rng.normal(0, 1.5, len(c)))
    lf["low"] = np.minimum(lo_, lc) - np.abs(rng.normal(0, 1.5, len(c)))
    return lf, {"SPX500": build_leader_features(frame, lf, CAL, "SPX500")}


def build(frame: pd.DataFrame, leader: pd.DataFrame | None = None) -> FamilyData:
    cross = {"SPX500": build_leader_features(frame, leader, CAL, "SPX500")} if leader is not None else None
    return build_family_data(frame, CAL, name="SYN", point_size=0.01, tick_size=0.01, cross=cross)


@pytest.fixture(scope="module")
def world():
    frame = synth_frame(n_days=70, seed=3, plant="gap_fade", phi=0.4)
    leader, _ = leader_cross(frame)
    return frame, leader, build(frame, leader)


def same(a: CandidateArrays, b: CandidateArrays) -> bool:
    return all(np.array_equal(getattr(a, f), getattr(b, f), equal_nan=True) for f in CAND_FIELDS)


def upto(c: CandidateArrays, last: int) -> CandidateArrays:
    return c.subset(c.decision_idx <= last)


# ----------------------------------------------------------------------------- feature prefix equality
def assert_prefix_equal(full: FamilyData, other: FamilyData) -> None:
    for name in ("ts_ns", "o", "h", "l", "c", "spread", "vol", "atr", "minute", "day", "contig_next", "run_start", "vwap"):
        a, b = getattr(full, name), getattr(other, name)
        assert np.array_equal(a, b, equal_nan=a.dtype.kind == "f"), name
    assert set(full.cash) == set(other.cash)
    for k in full.cash:
        assert np.array_equal(full.cash[k], other.cash[k], equal_nan=True), k
    for k, lf in full.cross.items():
        for n, arr in lf.ret.items():
            assert np.array_equal(arr, other.cross[k].ret[n], equal_nan=True), (k, n)
        assert np.array_equal(lf.active, other.cross[k].active)


@pytest.mark.parametrize("cut", [2500, 5001, 9000, 11999])
def test_features_are_prefix_stable_synthetic(world, cut):
    frame, leader, full = world
    assert_prefix_equal(full.prefix(cut), build(frame.iloc[:cut].reset_index(drop=True), leader.iloc[:cut].reset_index(drop=True)))


# ----------------------------------------------------------------------------- candidates
@pytest.mark.parametrize("fam", FAMS)
def test_truncation_invariance_synthetic(world, fam):
    _, _, full = world
    train = full.prefix(6000)
    total = 0
    for spec in sample_specs(fam):
        thr = R.fit_thresholds(train, spec)
        ref = R.generate_candidates(full, spec, thr)
        total += len(ref.decision_idx)
        for cut in (3000, 7013, 9500):
            part = R.generate_candidates(full.prefix(cut), spec, thr)
            assert same(part, upto(ref, cut - 2)), (spec.to_dict(), cut)
    assert total > 20, fam  # non-vacuous


@pytest.mark.parametrize("fam", [f for f in FAMS if f != "LEADLAG"])
def test_future_perturbation_synthetic(world, fam):
    frame, leader, full = world
    for t in (4210, 7777):
        pert = build(perturb_future(frame, t), leader)
        for spec in sample_specs(fam):
            thr = R.fit_thresholds(full.prefix(3000), spec)
            a = R.generate_candidates(full, spec, thr)
            b = R.generate_candidates(pert, spec, thr)
            assert same(upto(a, t), upto(b, t)), (spec.to_dict(), t)


def test_leader_future_perturbation(world):
    """LEADLAG: perturbing the LEADER's bars after t never changes follower candidates with decision <= t."""
    frame, leader, full = world
    specs = sample_specs("LEADLAG", 10)
    assert specs
    for t in (4210, 7777):
        pert_leader = perturb_future(leader, t)
        pert = build(frame, pert_leader)
        for spec in specs:
            thr = R.fit_thresholds(full.prefix(3000), spec)
            assert same(upto(R.generate_candidates(full, spec, thr), t), upto(R.generate_candidates(pert, spec, thr), t)), (spec.to_dict(), t)


def test_perturbation_changes_later_candidates(world):
    """Sanity of the harness: perturbing the future does change candidates after t (the tests can fail)."""
    frame, leader, full = world
    pert = build(perturb_future(frame, 4210), leader)
    changed = 0
    for fam in ("ORB", "VOLREV", "ROUND"):
        for spec in sample_specs(fam):
            thr = R.fit_thresholds(full.prefix(3000), spec)
            a, b = R.generate_candidates(full, spec, thr), R.generate_candidates(pert, spec, thr)
            changed += not same(a, b)
    assert changed > 0


def test_align_markets_uses_only_completed_leader_bars(world):
    """Alignment rule behind LEADLAG: leader bar open <= follower bar open (completed by the follower's close)."""
    from alpha.common.market_data import align_markets

    frame, leader, _ = world
    lead2 = leader.iloc[3::2].reset_index(drop=True)  # sparser leader grid
    al = align_markets({"F": frame, "L": lead2}, ref="F")["L"]
    ft = pd.DatetimeIndex(frame["ts"]).as_unit("s").asi8
    lt = pd.DatetimeIndex(lead2["ts"]).as_unit("s").asi8
    v = al.valid
    assert v.any()
    assert (lt[al.idx[v]] <= ft[v]).all()  # never a leader bar that closes after the follower's close


# ----------------------------------------------------------------------------- real GER40 slice
HAVE_REAL = (ROOT / "data/ar1_ger40").exists()


@pytest.fixture(scope="module")
def real_slice():
    from alpha.common.market_data import load_dev_market_frame
    from markets.spec import load_market_spec

    spec = load_market_spec("GER40")
    frame = load_dev_market_frame(spec, source="ar1")
    frame = frame.iloc[:22000].reset_index(drop=True)
    return frame, build_family_data(frame, CAL, name="GER40", point_size=spec.point_size, tick_size=spec.tick_size, asset_class=spec.asset_class)


@pytest.mark.skipif(not HAVE_REAL, reason="AR1 GER40 dataset not present")
@pytest.mark.parametrize("cut", [5000, 9000])
def test_real_features_prefix_stable(real_slice, cut):
    frame, full = real_slice
    assert_prefix_equal(full.prefix(cut), build_family_data(frame.iloc[:cut].reset_index(drop=True), CAL, name="GER40", point_size=0.01, tick_size=0.01))


@pytest.mark.skipif(not HAVE_REAL, reason="AR1 GER40 dataset not present")
@pytest.mark.parametrize("fam", [f for f in FAMS if f != "LEADLAG"])
def test_real_truncation_and_perturbation(real_slice, fam):
    frame, full = real_slice
    train = full.prefix(11000)
    total = 0
    for spec in sample_specs(fam, 5):
        thr = R.fit_thresholds(train, spec)
        assert thr.ok, spec.to_dict()  # 11000 bars ~ 44 days: enough to fit every Train quantile
        ref = R.generate_candidates(full, spec, thr)
        total += len(ref.decision_idx)
        for cut in (14000, 19111):
            assert same(R.generate_candidates(full.prefix(cut), spec, thr), upto(ref, cut - 2)), (spec.to_dict(), cut)
        t = 16000
        pert = build_family_data(perturb_future(frame, t), CAL, name="GER40", point_size=0.01, tick_size=0.01)
        assert same(upto(ref, t), upto(R.generate_candidates(pert, spec, thr), t)), (spec.to_dict(), "perturb")
    assert total > 20, fam  # non-vacuous: the real slice actually produces candidates
