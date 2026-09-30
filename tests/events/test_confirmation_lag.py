# ruff: noqa: RUF059
"""Confirmation-lag rule: pulses sit at >= p + order; origin arrays are never read for logic."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from alpha.events import kernels_pattern as kp
from alpha.events import kernels_structure as kst
from alpha.events.store import EventParams, build_events
from tests.events._helpers import build_features, slice_frame

EVENTS_DIR = Path(__file__).resolve().parents[2] / "src" / "alpha" / "events"
ORDER = EventParams().swing_order
BARS_PER_M5 = {"m5": 1, "m15": 3, "h1": 12, "d1": 1}


def test_static_no_module_reads_evo_arrays():
    """The origin prefix may be WRITTEN by the store (one schema-driven branch) but no module may
    name an ``evo_`` array; kernels never even mention the origin prefix."""
    for path in sorted(EVENTS_DIR.glob("*.py")):
        if path.name == "schema.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"evo_", text), path.name
        if path.name.startswith("kernels_") or path.name == "htf.py":
            assert not re.search(r'["\']evo["\']', text), path.name
    store = (EVENTS_DIR / "store.py").read_text(encoding="utf-8")
    assert len(re.findall(r'["\']evo["\']', store)) == 1  # the single write branch


@pytest.mark.parametrize("order", [1, 2, 3, 5])
def test_pivots_never_confirmed_before_p_plus_order_and_never_repainted(order):
    rng = np.random.default_rng(order)
    n = 400
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0, 1, n)
    low = close - rng.uniform(0, 1, n)
    ph, ph_o, pl, pl_o = kst.pivot_confirmations(high, low, order)
    for conf in np.flatnonzero(np.isfinite(ph)):
        assert conf - ph_o[conf] == order
    for conf in np.flatnonzero(np.isfinite(pl)):
        assert conf - pl_o[conf] == order
    # no repaint: pivots / structure computed on any prefix equal the full-series arrays
    up, dn, cu, cd, lu, ld, ou, od, st = kst.bos_choch(close, ph, ph_o, pl, pl_o)
    for t in (60, 137, 250, 399):
        ph2, po2, pl2, plo2 = kst.pivot_confirmations(high[:t], low[:t], order)
        assert np.array_equal(ph2, ph[:t], equal_nan=True) and np.array_equal(po2, ph_o[:t])
        assert np.array_equal(pl2, pl[:t], equal_nan=True) and np.array_equal(plo2, pl_o[:t])
        r = kst.bos_choch(close[:t], ph2, po2, pl2, plo2)
        for got, want in zip(r, (up, dn, cu, cd, lu, ld, ou, od, st), strict=True):
            assert np.array_equal(got, want[:t], equal_nan=True)
        assert (np.flatnonzero(up) >= 2 * order + 1).all()


def test_double_bottom_is_not_stamped_before_second_pivot_confirmation():
    rng = np.random.default_rng(5)
    n = 600
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0, 1, n)
    low = close - rng.uniform(0, 1, n)
    _, _, pl, pl_o = kst.pivot_confirmations(high, low, 3)
    pulse, _, _, org = kp.double_bottom(high, low, close, np.full(n, 1.0), pl, pl_o, 1.0, 60, 24)
    idx = np.flatnonzero(pulse)
    assert len(idx) > 0
    assert (idx - org[idx] > 3).all()  # second pivot alone lies >= order+1 after the first


@pytest.fixture(scope="module")
def real():
    frame = slice_frame("2025-03-24", "2025-04-11")
    feats = build_features(frame)
    return feats, build_events(feats)


def _pulses(ev, name):
    pulse = ev[f"ev_{name}"].astype(bool)
    return np.flatnonzero(pulse)


def test_real_swing_and_structure_lag(real):
    feats, ev = real
    for tf, k in BARS_PER_M5.items():
        if tf == "d1":
            continue
        for name in ("swing_low_conf", "swing_high_conf"):
            idx = _pulses(ev, f"{tf}_{name}")
            org = ev[f"evo_{tf}_{name}"][idx]
            assert len(idx) > 0
            if tf == "m5":
                assert (idx - org == ORDER).all()
            else:
                # the confirming HTF bar closes >= order HTF bars after the pivot bar opens
                assert (idx - org >= ORDER * k).all(), (tf, name)
        for name in ("bos_up", "bos_dn", "choch_up", "choch_dn"):
            idx = _pulses(ev, f"{tf}_{name}")
            org = ev[f"evo_{tf}_{name}"][idx]
            assert (idx - org > ORDER * k).all(), (tf, name)  # strictly after the confirmation


def test_real_bos_exactly_one_pulse_per_swing(real):
    feats, ev = real
    c = feats["c"]
    for tf in ("m5",):
        for up in (True, False):
            name = f"{tf}_bos_{'up' if up else 'dn'}"
            idx = _pulses(ev, name)
            org = ev[f"evo_{name}"][idx]
            lvl = ev[f"evl_{name}"][idx]
            assert len(idx) > 20
            assert len(set(org.tolist())) == len(idx)  # one pulse per swing (origin unique)
            for i, p, level in zip(idx, org, lvl, strict=True):
                conf = p + ORDER
                assert (c[i] > level) if up else (c[i] < level)
                between = c[conf : i]  # closes since the swing was confirmed
                assert (between <= level).all() if up else (between >= level).all()
                # V1 arrays: the broken level IS the last confirmed swing at the previous bar
                v1 = feats["last_swing_high" if up else "last_swing_low"]
                assert v1[i - 1] == level


def test_real_zone_and_trendline_stamps_follow_their_object(real):
    feats, ev = real
    for kind in ("swing_cluster", "prior_range", "m15_range"):
        for ev_name in ("zone_enter", "zone_exit"):
            idx = _pulses(ev, f"m5_{ev_name}_{kind}")
            org = ev[f"evo_m5_{ev_name}_{kind}"][idx]
            # swing clusters are formed at a pivot confirmation strictly before the stamp bar; day /
            # range zones are known from the bar's own open (born <= stamp)
            assert (org < idx).all() if kind == "swing_cluster" else (org <= idx).all()
        zid = ev[f"zid_m5_zone_lo_{kind}"]
        assert (ev[f"zid_m5_zone_hi_{kind}"] == zid).all()
    zid = ev["zid_m5_trendline_value"]
    for name in ("touch", "break"):
        for suffix in ("t0", "t10", "t25"):
            idx = _pulses(ev, f"m5_trendline_{name}_{suffix}")
            assert len(idx) > 0
            for i in idx:
                first = int(np.flatnonzero(zid == zid[i])[0])  # first bar the line is current
                assert zid[i] > 0 and first <= i


def test_real_pattern_and_sweep_lag(real):
    feats, ev = real
    for src in ("double_bottom", "double_top"):
        idx = _pulses(ev, f"m5_pattern_complete_{src}")
        org = ev[f"evo_m5_pattern_complete_{src}"][idx]
        assert len(idx) > 0 and (idx - org > 2 * ORDER).all()
    for src, low_side in (("swing_low", True), ("swing_high", False)):
        name = f"m5_sweep_{'low' if low_side else 'high'}_{src}"
        idx = _pulses(ev, name)
        org = ev[f"evo_{name}"][idx]
        assert len(idx) > 0
        assert (idx - org > ORDER).all()  # level (pivot) confirmed before the sweeping bar
