# ruff: noqa: E501
"""NEGATIVE CONTROL of the look-ahead harness: deliberately LEAKY feature groups must be DETECTED by ``_leak_harness``.

If the harness cannot catch these, passing it proves nothing about the real groups (see test_negative_control_real_groups.py).
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from _leak_harness import (
    ARRAY_FIELDS,
    assert_no_future_dependence,
    leaked_field_names,
    leaking_fields,
    perturb_future,
)
from test_levels_support import random_walk

from market_observer import schema as S

BARS = random_walk(300, 3, bars_per_day=60, segment_breaks=(120,))
I = 150  # noqa: E741


def _nxt(arr, i, k=1):
    return float(arr[min(i + k, len(arr) - 1)])


# ---------------------------------------------------------------------------------------------- (a) c[i+1]
def leaky_next_close(bars, i):
    return {"ret_fwd": _nxt(bars.c, i) - float(bars.c[i])}


# ---------------------------------------------------------------------------------------------- (b) full-sample normalisation
def leaky_fullsample_z(bars, i):
    return {"z": (float(bars.c[i]) - float(np.nanmean(bars.c))) / float(np.nanstd(bars.c))}


def leaky_fullsample_rank(bars, i):
    return {"rank": float(np.mean(bars.c <= bars.c[i]))}  # percentile over the whole sample, not past-only


# ---------------------------------------------------------------------------------------------- (c) leaks through every ObserverBars field
LEAK_BY_FIELD = {
    "ts_ns": lambda b, i: {"x": _nxt(b.ts_ns, i) - float(b.ts_ns[i])},
    "atr": lambda b, i: {"x": _nxt(b.atr, i)},
    "segment_id": lambda b, i: {"x": _nxt(b.segment_id, i)},
    "local_minute": lambda b, i: {"x": _nxt(b.local_minute, i, 3)},
    "local_day": lambda b, i: {"x": _nxt(b.local_day, i)},
    "tick_volume": lambda b, i: {"x": _nxt(b.tick_volume, i)},
    "spread": lambda b, i: {"x": _nxt(b.spread, i)},
    "o": lambda b, i: {"x": _nxt(b.o, i)},
    "h": lambda b, i: {"x": _nxt(b.h, i)},
    "l": lambda b, i: {"x": _nxt(b.l, i)},
    "c": leaky_next_close,
}


def _noisy(bars):
    """Make every array vary so a leak cannot hide behind constant data."""
    rng = np.random.default_rng(1)
    return dataclasses.replace(bars, tick_volume=rng.uniform(10, 100, len(bars)), spread=rng.uniform(0.01, 0.05, len(bars)))


def test_the_old_scramble_blind_spot_is_real():
    """The pre-existing scramble (o/h/l/c/tick_volume/spread only, atr/ts/segment/day kept) cannot see a future-atr leak; the new harness can."""
    bars = _noisy(BARS)
    old_fields = ("o", "h", "l", "c", "tick_volume", "spread")
    fn = LEAK_BY_FIELD["atr"]
    assert leaking_fields(fn, bars, I, fields=old_fields, check_prefix=False) == {}  # old harness: green although the group leaks
    assert "atr" in leaked_field_names(leaking_fields(fn, bars, I))  # new harness: caught, and attributed to the right field


@pytest.mark.parametrize("name", sorted(LEAK_BY_FIELD))
def test_every_per_field_leak_is_detected_and_attributed(name):
    bars = _noisy(BARS)
    found = leaking_fields(LEAK_BY_FIELD[name], bars, I)
    assert found, f"harness missed a leak through {name}"
    if name not in ("segment_id", "local_day"):  # piecewise constant: the next value often equals the current one, only the perturbation sees it
        assert "prefix" in found
    assert name in leaked_field_names(found)
    with pytest.raises(AssertionError, match="look-ahead detected"):
        assert_no_future_dependence(LEAK_BY_FIELD[name], bars, [I - 3, I])


@pytest.mark.parametrize("fn", [leaky_fullsample_z, leaky_fullsample_rank], ids=["fullsample_mean_std", "fullsample_percentile"])
def test_future_dependent_normalisation_is_detected(fn):
    found = leaking_fields(fn, _noisy(BARS), I)
    assert "prefix" in found and "c:scale" in found


def test_leak_is_detected_at_early_decision_bars_too():
    for i in (0, 1, 10, 100):
        assert leaking_fields(leaky_next_close, _noisy(BARS), i), i


# ---------------------------------------------------------------------------------------------- (d) timestamp-column leak
def leaky_future_confirmation_group(bars, i):
    """A swing-like group that reports a pivot 'confirmed' at the NEXT bar's open (needs bar i+1 to exist)."""
    return S.FeatureResult("balance", "leaky-1", {"confirmed_ts_ns": int(bars.ts_ns[min(i + 1, len(bars) - 1)]), "x": 1.0})


def test_timestamp_column_leak_is_caught_by_the_causality_rule():
    bars = _noisy(BARS)
    res = leaky_future_confirmation_group(bars, I)
    with pytest.raises(S.CausalityError):
        S.DecisionFeatures.from_results(bars.decision_ts_ns(I - 1), [res])  # stamped after the decision it was used for
    ok = S.DecisionFeatures.from_results(bars.decision_ts_ns(I), [S.FeatureResult("balance", "leaky-1", {"confirmed_ts_ns": int(bars.ts_ns[I]), "x": 1.0})])
    assert ok.columns["f_balance__confirmed_ts_ns"] == int(bars.ts_ns[I])
    # ... and the harness flags the same group through the ts_ns / prefix route
    found = leaking_fields(leaky_future_confirmation_group, bars, I)
    assert "ts_ns" in leaked_field_names(found) and "prefix" in found


def test_perturbation_leaves_the_past_untouched_and_ts_ascending():
    bars = _noisy(BARS)
    rng = np.random.default_rng(0)
    p = perturb_future(bars, I, ARRAY_FIELDS, rng, "scale")
    p.validate()
    for f in ARRAY_FIELDS:
        a, b = np.asarray(getattr(bars, f)), np.asarray(getattr(p, f))
        assert np.array_equal(a[: I + 1], b[: I + 1], equal_nan=True), f
        assert not np.array_equal(a[I + 1:], b[I + 1:], equal_nan=True), f
