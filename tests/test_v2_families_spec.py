# ruff: noqa: E501
"""FamilySpec contract: frozen / hashable / JSON / canonical hash / grids / complexity / effective windows / round scale."""

from __future__ import annotations

import dataclasses

import pytest

from alpha.families import registry as R
from alpha.families.data import round_steps
from alpha.families.eod import EODSpec, session_end
from alpha.families.gap import GAPSpec
from alpha.families.orb import ORBSpec
from alpha.families.roundnum import ROUNDSpec
from alpha.families.spec import MarketCalendar, thin_grid, tod_bounds
from alpha.families.volrev import VOLREVSpec

NAS = MarketCalendar("America/New_York", 570, 960, 570, 900, 955)
GER = MarketCalendar()


@pytest.mark.parametrize("fam", R.FAMILY_NAMES)
def test_grids_are_bounded_unique_and_deterministic(fam):
    market = "NAS100" if fam == "LEADLAG" else "GER40"
    g = R.grid_for(fam, market, 400)
    assert 20 <= len(g) <= 400
    hashes = [s.canonical_hash() for s in g]
    assert len(set(hashes)) == len(g)
    assert [s.canonical_hash() for s in R.grid_for(fam, market, 400)] == hashes  # deterministic
    assert hashes == sorted(hashes)  # hash order = the thinning order
    small = R.grid_for(fam, market, 25)
    assert [s.canonical_hash() for s in small] == hashes[:25]  # a smaller budget is a strict prefix
    for s in g:
        assert isinstance(s.complexity, int) and 3 <= s.complexity <= 9
        assert fam == s.FAMILY


@pytest.mark.parametrize("fam", R.FAMILY_NAMES)
def test_json_roundtrip_frozen_hashable(fam):
    market = "NAS100" if fam == "LEADLAG" else "GER40"
    for s in R.grid_for(fam, market, 30):
        again = R.spec_from_json(s.to_json())
        assert again == s and hash(again) == hash(s) and again.canonical_hash() == s.canonical_hash()
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.side = "long"  # type: ignore[misc]
        assert {s: 1}[again] == 1


def test_canonical_hash_ignores_parameters_a_mode_does_not_use():
    a = ORBSpec("fade", 15, 0.1, 0.5, "r", 1.0, 240)
    b = dataclasses.replace(a, stop_frac=1.0)
    assert a != b and a.canonical_hash() == b.canonical_hash()
    assert ORBSpec("breakout", 15, 0.1, 0.5).canonical_hash() != ORBSpec("breakout", 15, 0.1, 1.0).canonical_hash()
    v1 = VOLREVSpec("fade", "open", 2.0, 0.75, 24)
    v2 = VOLREVSpec("fade", "open", 2.0, 0.75, 48)
    assert v1.canonical_hash() == v2.canonical_hash()
    e1 = VOLREVSpec("expand", "vwap", 3.0, 0.15, 12, target_kind="r")
    e2 = VOLREVSpec("expand", "open", 1.5, 0.15, 12, target_kind="r")
    assert e1.canonical_hash() == e2.canonical_hash()
    assert GAPSpec("fade", 0.5, 1.0, 3, True, 1.5, "fill", 1.0).canonical_hash() == GAPSpec("fade", 0.5, 1.0, 3, True, 1.5, "fill", 2.0).canonical_hash()
    r1, r2 = ROUNDSpec("break", "major", 0.1, 0.3, 3), ROUNDSpec("break", "major", 0.1, 0.6, 3)
    assert r1.canonical_hash() == r2.canonical_hash()
    assert thin_grid([a, b], None) == [a.canonical()]


def test_invalid_specs_are_refused():
    for bad in (lambda: ORBSpec(range_min=7), lambda: ORBSpec(mode="x"), lambda: GAPSpec("go", target_kind="fill"),
                lambda: GAPSpec(bucket_lo_q=0.9, bucket_hi_q=0.5), lambda: ROUNDSpec("break", hold=0), lambda: EODSpec(window_min=7),
                lambda: VOLREVSpec("expand", n_range=1), lambda: ORBSpec(side="x")):
        with pytest.raises(ValueError):
            bad()


def test_effective_window_uses_the_market_calendar():
    s = VOLREVSpec("fade", "open", 2.0, 0.75, exit_clock="close")
    w = s.effective_window(NAS)
    assert (w.entry_start_min, w.entry_end_min, w.exit_min) == (570, 900, 955)  # min(cash close 960, flat 955)
    w2 = s.effective_window(GER)
    assert (w2.entry_start_min, w2.entry_end_min, w2.exit_min) == (540, 17 * 60 + 30, 17 * 60 + 30)  # clock exit at the cash close
    assert VOLREVSpec("fade", "open", 2.0, 0.75).effective_window(GER).sim_window().flat_min == GER.flat_min
    assert session_end(NAS) == 900 and session_end(GER) == 17 * 60 + 30
    e = EODSpec(60, exit_clock="T").effective_window(NAS)
    assert (e.entry_end_min, e.exit_min) == (900, 900)


def test_time_of_day_windows_stay_inside_the_entry_window():
    lo, hi = 570, 900
    am, pm, al = tod_bounds(lo, hi, "am"), tod_bounds(lo, hi, "pm"), tod_bounds(lo, hi, "all")
    assert al == (lo, hi) and am[0] == lo and pm[1] == hi and am[1] == pm[0] and lo < am[1] < hi and am[1] % 5 == 0


def test_round_number_scale_is_derived_from_the_market_spec():
    assert round_steps("index_cfd", 0.01) == pytest.approx((50.0, 100.0))
    assert round_steps("metal_cfd", 0.01) == pytest.approx((5.0, 10.0))
    assert round_steps("fx_cfd", 1e-5) == pytest.approx((0.005, 0.01))
    with pytest.raises(ValueError):
        round_steps("crypto", 0.01)
    from markets.spec import load_market_spec

    for name, expect in (("GER40", (50.0, 100.0)), ("NAS100", (50.0, 100.0)), ("XAUUSD", (5.0, 10.0)), ("EURUSD", (0.005, 0.01))):
        sp = load_market_spec(name)
        assert round_steps(sp.asset_class, sp.tick_size) == pytest.approx(expect), name


def test_leadlag_pairs_only_for_overlapping_index_sessions():
    from alpha.families.leadlag import PAIRS

    assert set(PAIRS) == {"GER40", "NAS100", "SPX500"} and R.grid_for("LEADLAG", "XAUUSD") == [] and R.grid_for("LEADLAG", "EURUSD") == []
    for follower, leaders in PAIRS.items():
        assert follower not in leaders
