# ruff: noqa: E501
"""Mirror correctness: the price-mirrored frame (p -> K - p, high <-> low) yields exactly mirrored candidates."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.families import registry as R
from alpha.families.data import LeaderFeatures, build_family_data
from alpha.families.volrev import rel_atr
from tests.test_v2_families_causality import leader_cross, sample_specs
from tests.test_v2_families_synth import CAL, mirror_frame, synth_frame

K = 40000.0  # a multiple of both round-number steps (50 / 100): the level grid maps onto itself


@pytest.fixture(scope="module")
def pair():
    frame = synth_frame(n_days=110, seed=11, plant="gap_fade", phi=0.4)
    lead, _ = leader_cross(frame)
    m_frame, m_lead = mirror_frame(frame, K), mirror_frame(lead, K)
    from alpha.families.data import build_leader_features

    orig = build_family_data(frame, CAL, name="O", point_size=0.01, tick_size=0.01, cross={"SPX500": build_leader_features(frame, lead, CAL, "SPX500")})
    mir = build_family_data(m_frame, CAL, name="M", point_size=0.01, tick_size=0.01, cross={"SPX500": build_leader_features(m_frame, m_lead, CAL, "SPX500")})
    # relative ATR (atr / close) is a SCALE measure, not a direction: share it so the comparison isolates direction logic
    ra = rel_atr(orig)
    mir._memo["rel_atr"] = ra
    return orig, mir


def test_mirrored_arrays_are_mirrored(pair):
    o, m = pair
    assert np.allclose(m.c, K - o.c) and np.allclose(m.atr, o.atr, equal_nan=True)
    assert np.allclose(m.cash["pdc_cash"], K - o.cash["pdc_cash"], equal_nan=True)
    assert np.allclose(m.vwap, K - o.vwap, equal_nan=True)
    for n, arr in o.cross["SPX500"].ret.items():
        assert np.allclose(m.cross["SPX500"].ret[n], -arr, equal_nan=True, atol=1e-9)
    assert isinstance(o.cross["SPX500"], LeaderFeatures)


@pytest.mark.parametrize("fam", R.FAMILY_NAMES)
def test_candidates_are_price_mirrored(pair, fam):
    o, m = pair
    total = 0
    for spec in sample_specs(fam, 10):
        thr = R.fit_thresholds(o.prefix(8000), spec)
        a = R.generate_candidates(o, spec, thr)
        b = R.generate_candidates(m, spec, thr)  # SAME frozen numbers: only the price direction differs
        total += len(a.decision_idx)
        assert np.array_equal(a.decision_idx, b.decision_idx), spec.to_dict()
        assert np.array_equal(a.direction, -b.direction), spec.to_dict()
        assert np.allclose(b.stop, K - a.stop, atol=1e-6)
        fin = np.isfinite(a.target)
        assert np.array_equal(fin, np.isfinite(b.target))
        assert np.allclose(b.target[fin], K - a.target[fin], atol=1e-6)
        assert np.allclose(a.target_r, b.target_r)
    assert total > 5, fam  # non-vacuous


def test_fitted_thresholds_are_mirror_invariant_where_they_should_be(pair):
    o, m = pair
    for fam in ("GAP", "OVERNIGHT", "EOD"):
        for spec in sample_specs(fam, 6):
            a, b = R.fit_thresholds(o.prefix(8000), spec).values, R.fit_thresholds(m.prefix(8000), spec).values
            assert np.allclose(a, b, equal_nan=True, rtol=1e-6), (spec.to_dict(), a, b)


def test_side_filter_keeps_only_the_requested_direction(pair):
    import dataclasses

    o, _ = pair
    spec = sample_specs("ORB", 3)[0]
    thr = R.fit_thresholds(o, spec)
    both = R.generate_candidates(o, spec, thr)
    long_ = R.generate_candidates(o, dataclasses.replace(spec, side="long"), thr)
    short = R.generate_candidates(o, dataclasses.replace(spec, side="short"), thr)
    assert (long_.direction > 0).all() and (short.direction < 0).all()
    assert len(long_.decision_idx) + len(short.decision_idx) == len(both.decision_idx) > 0
