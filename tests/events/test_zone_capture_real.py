"""ZONE_ENTER/EXIT capture the zone TESTED at the pulse bar (audit defect A), real dev frame."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.events import schema as ev
from alpha.temporal.reference import level_arrays
from alpha.temporal.spec import Clause
from scripts.bench_temporal_real import load_real


@pytest.fixture(scope="module")
def real():
    return load_real()


@pytest.mark.parametrize("event", ["ZONE_ENTER", "ZONE_EXIT"])
def test_captured_zone_is_the_tested_zone_on_real_frame(real, event):
    close = np.asarray(real.features["c"], dtype=float)
    a = real.frame.arrays
    total = 0
    bad: list[str] = []
    for tf in ("M5", "M15", "H1"):
        for kind in ev.ZONE_KINDS:
            cl = Clause("event", event, tf, "IS", variant=kind)
            pulse = np.flatnonzero(np.asarray(a[ev.array_names(event, tf, kind)[0]]) > 0)
            lo_n, zid_n = level_arrays("ZONE_LO", cl)
            hi_n, zid_h = level_arrays("ZONE_HI", cl)
            assert zid_n is not None and zid_h is not None
            lo, hi, zid = (np.asarray(a[n])[pulse] for n in (lo_n, hi_n, zid_n))
            c = close[pulse]
            total += len(pulse)
            inside = (lo <= c) & (c <= hi)
            viol = ~inside if event == "ZONE_ENTER" else inside
            viol |= ~np.isfinite(lo) | ~np.isfinite(hi) | (zid <= 0)
            # one zid = one box
            seen: dict[int, tuple[float, float]] = {}
            for z, l_, h_ in zip(zid, lo, hi, strict=True):
                if seen.setdefault(int(z), (float(l_), float(h_))) != (float(l_), float(h_)):
                    viol[:] = True
            if viol.any():
                bad.append(f"{tf}/{kind}: {int(viol.sum())}/{len(pulse)}")
    assert total > 1000
    assert not bad, bad
