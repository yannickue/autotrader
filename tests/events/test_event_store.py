# ruff: noqa: RUF059
"""EventSet builder, registry conformance and the on-disk cache."""

from __future__ import annotations

import json

import numpy as np
import pytest

from alpha.events import kernels_structure as kst
from alpha.events import kernels_zone as kz
from alpha.events import schema
from alpha.events.store import EventParams, build_events, cache_key, load_or_build_events
from tests.events._helpers import build_features, same_array, slice_frame


@pytest.fixture(scope="module")
def feats():
    return build_features(slice_frame("2025-03-17", "2025-03-21"))


def test_names_are_exactly_the_registry_names(feats):
    ev = build_events(feats)
    assert sorted(ev) == sorted(schema.all_array_names())
    assert len(ev) == len(set(ev)) == 385
    # bound-only events have no arrays
    assert not any(k.startswith(("ev_m5_touch", "ev_m5_break", "ev_m5_reclaim")) for k in ev)


def test_no_nan_in_flags_and_masks(feats):
    ev = build_events(feats)
    for name, arr in ev.items():
        prefix = name.split("_", 1)[0]
        if prefix in ("ev", "st", "zid", "evo", "valid"):
            assert arr.dtype.kind in "uib", name  # integer / bool arrays cannot hold NaN
    assert ev["valid_m5"].dtype == np.bool_ and ev["valid_m5"].any() and not ev["valid_m5"].all()
    assert ev["valid_d1"].sum() == 0  # < 14 complete days -> D1 stays warming up


def test_events_fire_and_every_array_is_used(feats):
    ev = build_events(feats)
    silent = [
        n
        for n, a in ev.items()
        if n.startswith("ev_m5_") and not a.any() and "pattern" not in n and "d1" not in n
    ]
    # at most a couple of rare M5 events may stay silent in one week of data
    assert len(silent) <= 6, silent


def test_cache_roundtrip_mmap_and_key_sensitivity(feats, tmp_path):
    first = load_or_build_events(feats, None, tmp_path)
    assert first.metadata["cache_hit"] is False
    second = load_or_build_events(feats, None, tmp_path)
    assert second.metadata["cache_hit"] is True
    assert second.metadata["cache_key"] == first.metadata["cache_key"]
    assert set(second) == set(first)
    assert not second.is_loaded("ev_m5_swing_low_conf")  # lazy: nothing opened yet
    assert isinstance(second["ev_m5_swing_low_conf"], np.memmap)
    assert second.is_loaded("ev_m5_swing_low_conf") and not second.is_loaded("valid_m5")
    assert all(same_array(np.asarray(second[n]), first[n]) for n in first)
    assert all(isinstance(v, np.memmap) for v in second.values())
    assert set(dict(second)) == set(first)  # plain dict copies materialise, never leak None
    # one .npy per array
    key_dir = tmp_path / first.metadata["cache_key"]
    assert len(list(key_dir.glob("*.npy"))) == len(first)
    manifest = json.loads((key_dir / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["arrays"]) == set(first)
    # parameter change -> different key, different directory
    assert cache_key(feats, EventParams(swing_order=2)) != cache_key(feats)
    # data change -> different key
    changed = dict(feats)
    changed["c"] = feats["c"] + 1.0
    assert cache_key(type(feats)(changed, feats.metadata)) != cache_key(feats)
    # registry / version bump -> different key
    old = schema.EVENT_SET_VERSION
    try:
        schema.EVENT_SET_VERSION = old + "-x"  # type: ignore[misc]
        bumped = cache_key(feats)
    finally:
        schema.EVENT_SET_VERSION = old
    assert bumped != cache_key(feats)


def test_cache_key_includes_featurestore_key(feats):
    a = type(feats)(dict(feats), {"cache_key": "aaa"})
    b = type(feats)(dict(feats), {"cache_key": "bbb"})
    assert cache_key(a) != cache_key(b)


def test_trendline_pulses_only_after_second_anchor_confirmation():
    rng = np.random.default_rng(1)
    n = 800
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0, 1, n)
    low = close - rng.uniform(0, 1, n)
    ph, ph_o, pl, pl_o = kst.pivot_confirmations(high, low, 3)
    lv, slope, side, zid, born = kz.trendline_lines(ph, ph_o, pl, pl_o, 288)
    assert (side != 0).sum() > 100
    live = side != 0
    assert (born[live] <= np.flatnonzero(live)).all()
    # the line's 2nd anchor confirmation coincides with a confirmed pivot of the right side
    for i in np.unique(born[live]):
        assert np.isfinite(pl[i]) or np.isfinite(ph[i])
    touch, brk, _, _ = kz.trendline_events(high, low, close, np.ones(n), lv, slope, side, born, 0.1)
    fired = np.flatnonzero(touch | brk)
    assert len(fired) > 10 and (born[fired] < fired).all()
