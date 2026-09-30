# ruff: noqa: E501
"""Registry consistency tests for alpha.events.schema (Phase 0 interface freeze)."""

from __future__ import annotations

import dataclasses

import pytest

from alpha.events import schema as ev

REQUIRED = {
    "TOUCH", "BREAK_UP", "BREAK_DN", "RECLAIM_UP", "RECLAIM_DN", "RETEST_HOLD_UP", "RETEST_HOLD_DN",
    "HOLD_ABOVE", "HOLD_BELOW", "SWEEP_LOW", "SWEEP_HIGH", "SWING_LOW_CONF", "SWING_HIGH_CONF",
    "BOS_UP", "BOS_DN", "CHOCH_UP", "CHOCH_DN", "ZONE_ENTER", "ZONE_EXIT", "TRENDLINE_TOUCH",
    "TRENDLINE_BREAK", "PATTERN_COMPLETE", "MOMENTUM_RESUME_UP", "MOMENTUM_RESUME_DN", "TREND_UP",
    "TREND_DN",
}


def test_catalogue_complete():
    assert {d.name for d in ev.all_events()} >= REQUIRED


def test_defs_well_formed():
    for d in ev.all_events():
        assert d.kind in ev.KINDS
        assert d.tfs and set(d.tfs) <= set(ev.TIMEFRAMES)
        assert d.roles and set(d.roles) <= set(ev.ROLES)
        assert d.causality
        assert d.lag_rule in ev.LAG_RULES  # every event has an availability rule
        if d.lag_bars is not None:
            assert d.lag_bars >= 0
        if d.kind == "pulse":
            assert d.lag_rule and d.causality
        for t in d.tols:
            assert t in ev.TOL_GRID
        for k in d.ks:
            assert k in ev.K_GRID


def test_lag_rules_causal_semantics():
    for n in ("BOS_UP", "BOS_DN", "SWEEP_LOW", "SWEEP_HIGH", "RECLAIM_UP"):
        assert ev.get(n).lag_rule == "close" and ev.get(n).lag_bars == 0
    for n in ("SWING_LOW_CONF", "SWING_HIGH_CONF"):
        assert ev.get(n).lag_rule == "pivot_order" and ev.get(n).lag_bars is None
    assert ev.get("TRENDLINE_BREAK").lag_rule == "second_pivot"


def test_mirror_is_involution_and_variants_mirror():
    for d in ev.all_events():
        m = ev.mirror_event(d.name)
        assert ev.get(m)  # exists
        assert ev.mirror_event(m) == d.name
        for v in d.variants():
            mv = ev.mirror_variant(d.name, v)
            assert mv in ev.get(m).variants()
            assert ev.mirror_variant(m, mv) == v


def test_mirror_pairs_specific():
    assert ev.mirror_event("BOS_UP") == "BOS_DN"
    assert ev.mirror_event("ZONE_LO") == "ZONE_HI"
    assert ev.mirror_event("TOUCH") == "TOUCH"
    assert ev.mirror_variant("SWEEP_LOW", "pdl") == "pdh"
    assert ev.mirror_variant("SWEEP_LOW", "prior20") == "prior20"


def test_missing_mirror_raises(monkeypatch):
    monkeypatch.setitem(ev._REGISTRY, "BOS_UP", dataclasses.replace(ev.get("BOS_UP"), mirror=None))
    with pytest.raises(ValueError):
        ev.mirror_event("BOS_UP")


def test_array_names_pattern_and_dtypes():
    assert ev.array_names("SWEEP_LOW", "M5", "prior20") == (
        "ev_m5_sweep_low_prior20", "evl_m5_sweep_low_prior20",
        "evx_m5_sweep_low_prior20", "evo_m5_sweep_low_prior20",
    )
    assert ev.array_names("TREND_UP", "H1") == ("st_h1_trend_up",)
    assert ev.array_names("ZONE_LO", "M15", "swing_cluster") == (
        "lv_m15_zone_lo_swing_cluster", "zid_m15_zone_lo_swing_cluster",
    )
    dt = ev.array_dtypes("SWEEP_LOW", "M5", "prior20")
    assert dt["ev_m5_sweep_low_prior20"] == "uint8"
    assert dt["evl_m5_sweep_low_prior20"] == "float64"
    assert dt["evo_m5_sweep_low_prior20"] == "int32"
    assert ev.array_dtypes("TREND_UP", "H1")["st_h1_trend_up"] == "int8"
    assert ev.valid_array_name("D1") == "valid_d1"
    assert "sf_m15_bar_progress" in ev.sf_array_names("M15")


def test_variant_sets():
    assert ev.get("SWEEP_LOW").srcs == ("prior20", "prior48", "pdl", "session_low", "swing_low")
    assert set(ev.get("RECLAIM_UP").variants()) == {"k3", "k6"}
    assert set(ev.get("TRENDLINE_TOUCH").variants()) == {"t0", "t10", "t25"}
    assert ev.get("BOS_UP").variants() == ("",)


def test_misuse_raises():
    with pytest.raises(ValueError):
        ev.array_names("SWEEP_LOW", "M5", "")  # variant required
    with pytest.raises(ValueError):
        ev.array_names("SWEEP_LOW", "M5", "nope")
    with pytest.raises(ValueError):
        ev.array_names("SWEEP_LOW", "D1", "prior20")  # tf not allowed
    with pytest.raises(ValueError):
        ev.array_names("BREAK_UP", "M5")  # bound only
    with pytest.raises(ValueError):
        ev.array_names("BOS_UP", "M5", "x")
    with pytest.raises(KeyError):
        ev.get("NOPE")


def test_array_name_uniqueness():
    names = ev.all_array_names()
    assert len(names) == len(set(names))


def test_fingerprint_stable_and_sensitive(monkeypatch):
    base = ev.registry_fingerprint()
    assert base == ev.registry_fingerprint()
    d = ev.get("SWEEP_LOW")
    monkeypatch.setitem(ev._REGISTRY, "SWEEP_LOW", dataclasses.replace(d, srcs=d.srcs[:-1]))
    assert ev.registry_fingerprint() != base
    monkeypatch.setitem(ev._REGISTRY, "SWEEP_LOW", d)
    assert ev.registry_fingerprint() == base
    monkeypatch.setitem(ev._REGISTRY, "RECLAIM_UP", dataclasses.replace(ev.get("RECLAIM_UP"), ks=(3,)))
    assert ev.registry_fingerprint() != base
    monkeypatch.setattr(ev, "EVENT_SET_VERSION", "events-v9.9-test")
    assert ev.registry_fingerprint() != base
