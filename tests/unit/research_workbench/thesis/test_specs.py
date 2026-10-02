"""Spec catalog + CONTINUATION_RETEST predicates."""

from __future__ import annotations

from dataclasses import replace

import pytest

from research_workbench.thesis.contracts import EvidenceClass, MarketPhase
from research_workbench.thesis.setup_engine import GEOMETRY_PREDICATES, PREDICATES, validate_spec
from research_workbench.thesis.specs import CONTINUATION_RETEST, catalog, get_spec

from ._synth import GEO_LONG, LONG, SHORT, mirror_geo, mirror_map, mm

NINE = [
    "BREAKOUT_ACCEPTANCE",
    "FAILED_BREAK_RECLAIM",
    "RANGE_EDGE_REJECTION",
    "TREND_PULLBACK",
    "ROLE_REVERSAL_RETEST",
    "COMPRESSION_EXPANSION",
    "OPENING_DRIVE_PULLBACK",
    "PRIOR_LEVEL_BREAK_RETEST",
    "EXHAUSTION_FAILED_EXPANSION_REVERSAL",
]


def test_catalog_has_ten_specs_one_implemented():
    cat = catalog()
    assert [s.archetype for s in cat] == ["CONTINUATION_RETEST", *NINE]
    assert [s.archetype for s in cat if s.implemented] == ["CONTINUATION_RETEST"]
    for s in cat:
        assert s.allowed_market_phases and MarketPhase.UNDEFINED not in s.allowed_market_phases
        assert set(s.requirements) >= {
            EvidenceClass.CONTEXT,
            EvidenceClass.LOCATION,
            EvidenceClass.STRUCTURE,
            EvidenceClass.LEVEL_BEHAVIOUR,
            EvidenceClass.TRIGGER,
            EvidenceClass.GEOMETRY,
        }
        assert s.invalidation and s.expiry_bars > 0 and s.spec_version


def test_get_spec_and_unknown():
    assert get_spec("CONTINUATION_RETEST") is CONTINUATION_RETEST
    with pytest.raises(KeyError):
        get_spec("NOPE")


def test_spec_hash_stable_unique_and_sensitive():
    hashes = [s.spec_hash for s in catalog()]
    assert len(set(hashes)) == 10 and hashes == [s.spec_hash for s in catalog()]
    assert replace(CONTINUATION_RETEST, expiry_bars=49).spec_hash != CONTINUATION_RETEST.spec_hash
    assert (
        replace(CONTINUATION_RETEST, implemented=False).spec_hash != CONTINUATION_RETEST.spec_hash
    )


def test_continuation_spec_validates_and_all_predicates_registered():
    validate_spec(CONTINUATION_RETEST)
    names = {n for ns in CONTINUATION_RETEST.requirements.values() for n in ns}
    assert names - {
        "structural_stop_known",
        "nearest_opposition_known",
        "any_trigger_event",
    } <= set(PREDICATES)
    assert {"structural_stop_known", "nearest_opposition_known"} <= set(GEOMETRY_PREDICATES)


PRED_CASES = [
    # (predicate, map kwargs, expected LONG)
    ("h1_not_against", {"h1_context": "DOWN"}, False),
    ("h1_not_against", {"h1_context": "NEUTRAL"}, True),
    ("h1_not_against", {"h1_context": None}, None),
    ("m15_sequence_compatible", {"m15_structure": "UP_SEQUENCE"}, True),
    ("m15_sequence_compatible", {"m15_structure": "MIXED_TRANSITION"}, True),
    ("m15_sequence_compatible", {"m15_structure": "DOWN_SEQUENCE"}, False),
    ("m15_sequence_compatible", {"m15_structure": "RANGE_OR_UNDEFINED"}, False),
    ("m15_sequence_compatible", {"m15_structure": None}, None),
    ("at_active_zone", {"active_support_zone": (1.0, 2.0)}, True),
    ("at_active_zone", {}, False),
    ("break_accepted_with_direction", {"acceptance_state": {"LONG:a": "ACCEPTED"}}, True),
    ("break_accepted_with_direction", {"acceptance_state": {"LONG:a": "BROKEN"}}, False),
    ("break_accepted_with_direction", {"acceptance_state": {"SHORT:a": "ACCEPTED"}}, False),
    ("no_acceptance_against", {"acceptance_state": {"SHORT:a": "ACCEPTED"}}, False),
    (
        "no_acceptance_against",
        {"acceptance_state": {"SHORT:a": "BROKEN", "LONG:b": "ACCEPTED"}},
        True,
    ),
    ("retest_confirmed", {"acceptance_state": {"LONG:a": "RETEST_HELD"}}, True),
    ("retest_confirmed", {"active_support_zone": (1.0, 2.0), "m5_structure": "UP_SEQUENCE"}, True),
    ("retest_confirmed", {"m5_structure": "UP_SEQUENCE"}, False),
    ("retest_confirmed", {"m5_structure": None}, None),
    ("structural_break_against", {"m15_structure": "DOWN_SEQUENCE"}, True),
    ("structural_break_against", {"m15_structure": "UP_SEQUENCE"}, False),
    ("structural_break_against", {"m15_structure": None}, None),
    ("acceptance_against", {"acceptance_state": {"SHORT:a": "ACCEPTED"}}, True),
    (
        "flipped_zone_overlap",
        {"active_support_zone": (1.0, 2.0), "role_reversal_zones": ((1.5, 3.0),)},
        True,
    ),
    (
        "flipped_zone_overlap",
        {"active_support_zone": (1.0, 2.0), "role_reversal_zones": ((5.0, 6.0),)},
        False,
    ),
    ("participation_not_low", {"participation_state": "LOW"}, False),
    ("participation_not_low", {"participation_state": None}, None),
]


@pytest.mark.parametrize(("name", "kw", "expected"), PRED_CASES)
def test_predicates_are_sign_symmetric(name, kw, expected):
    m = mm(0, **kw)
    assert PREDICATES[name](m, LONG, CONTINUATION_RETEST.params) is expected
    assert PREDICATES[name](mirror_map(m), SHORT, CONTINUATION_RETEST.params) is expected


def test_geometry_predicates_mirror():
    g = GEO_LONG
    for d, geo in ((LONG, g), (SHORT, mirror_geo(g))):
        assert GEOMETRY_PREDICATES["structural_stop_known"](geo, d, {}) is True
        assert GEOMETRY_PREDICATES["nearest_opposition_known"](geo, d, {}) is True
        bad = {**geo, "structural_stop": None}
        assert GEOMETRY_PREDICATES["structural_stop_known"](bad, d, {}) is False
        assert GEOMETRY_PREDICATES["structural_stop_known"](None, d, {}) is None
    wrong_side = {**g, "structural_stop": 100.5}  # inside the entry zone -> not protective
    assert GEOMETRY_PREDICATES["structural_stop_known"](wrong_side, LONG, {}) is False


def test_accepted_values_match_position_thesis():
    from research_workbench.thesis import position_thesis, specs

    assert (
        specs.ACCEPTED_VALUES
        == position_thesis.ACCEPTED_VALUES
        == frozenset({"ACCEPTED", "RETEST_HELD"})
    )


@pytest.mark.parametrize("val", ["ACCEPTED", "RETEST_HELD"])
def test_opposing_established_acceptance_blocks_and_invalidates(val):
    m = mm(0, acceptance_state={"SHORT:a": val})
    assert PREDICATES["no_acceptance_against"](m, LONG, {}) is False
    assert PREDICATES["acceptance_against"](m, LONG, {}) is True
    assert PREDICATES["no_acceptance_against"](mirror_map(m), SHORT, {}) is False
    for weak in ("BROKEN", "RECLAIMED", None):
        w = mm(0, acceptance_state={"SHORT:a": weak})
        assert PREDICATES["acceptance_against"](w, LONG, {}) is False
