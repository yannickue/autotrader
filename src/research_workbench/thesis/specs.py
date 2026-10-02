# ruff: noqa: E501
"""Setup spec catalog (RESEARCH / OFFLINE ONLY). Importing this module registers the CONTINUATION_RETEST predicates in the engine registries.

CONTINUATION_RETEST v0 (design §4) is the only fully evaluable spec; the other nine archetypes are REPRESENTABLE specs with
``implemented=False`` (the engine returns an explicit NOT_EVALUATED, never a silent zero). Thresholds live in ``SetupSpec.params`` and are frozen
(changing anything changes ``spec_hash``; no tuning before a result exists).

Predicate -> MarketMap field mapping (every predicate takes a Direction; ``own``/``against`` are sign-relative so SHORT mirrors LONG exactly):
  CONTEXT
    h1_not_against           h1_context ("UP"/"DOWN"/"NEUTRAL"): False iff == against-label (LONG: "DOWN", SHORT: "UP"); None if h1_context None
    m15_sequence_compatible  m15_structure: True for own sequence (LONG "UP_SEQUENCE" / SHORT "DOWN_SEQUENCE") or "MIXED_TRANSITION"; False for the
                             opposite sequence or "RANGE_OR_UNDEFINED"; None if m15_structure None
  LOCATION
    at_active_zone           LONG: active_support_zone is not None; SHORT: active_resistance_zone is not None (interpretation: the zone price is
                             currently at/in; None = price not at a zone). bool, never None
  STRUCTURE
    break_accepted_with_direction
                             acceptance_state values of the keys "<DIR>:<level_id>" (DIR = own direction = direction of the BREAK): True iff one is
                             "ACCEPTED" or "RETEST_HELD" (break -> acceptance observed). Pullback depth is NOT a MarketMap fact; being back at the
                             zone (LOCATION) after an accepted break is the pullback/retest context.
  LEVEL_BEHAVIOUR  (started tier = no_acceptance_against; confirmed tier = retest_confirmed)
    no_acceptance_against    False iff an acceptance_state key "<OPPOSITE>:<level_id>" has a value in ACCEPTED_VALUES (ACCEPTED | RETEST_HELD)
    retest_confirmed         True iff a "<DIR>:*" key is "RETEST_HELD" (hold / rejection of the zone) OR (at_active_zone AND m5_structure is the
                             own sequence = reclaim / rejection by M5 structure); None if neither and m5_structure is None
  INVALIDATION
    structural_break_against m15_structure == opposite sequence (None if m15_structure None)
    acceptance_against       an opposite-direction key has a value in ACCEPTED_VALUES
    (+ time expiry: expiry_bars)
  GEOMETRY (caller-supplied geometry, engine registry GEOMETRY_PREDICATES)
    structural_stop_known    geometry.structural_stop not None (and on the protective side of entry_zone when entry_zone is given)
    nearest_opposition_known geometry.opposition_1 not None
  TRIGGER (EVENT_PREDICATES)
    any_trigger_event        any name in params["trigger_events"][<DIR>] is in the bar's events
  OPTIONAL (non-gating)
    flipped_zone_overlap     any role_reversal_zones entry overlaps the active zone of the setup side
    participation_not_low    participation_state != "LOW" (None if None)

acceptance_state vocabulary expected from the MarketMap builder: "BROKEN" | "ACCEPTED" | "RECLAIMED" | "RETEST_HELD" | None, keyed
"<LONG|SHORT>:<level_id>" where LONG = break above the level.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from research_workbench.thesis.contracts import (
    Direction,
    EvidenceClass,
    MarketMap,
    MarketPhase,
    SetupSpec,
)
from research_workbench.thesis.setup_engine import (
    register_event_predicate,
    register_geometry_predicate,
    register_predicate,
)

EC = EvidenceClass
MP = MarketPhase

# established acceptance in the MarketMap vocabulary (marketmap-1); mirrors position_thesis.ACCEPTED_VALUES (pinned by a test)
ACCEPTED_VALUES = frozenset({"ACCEPTED", "RETEST_HELD"})

_UP, _DOWN = "UP_SEQUENCE", "DOWN_SEQUENCE"


def _own_seq(d: Direction) -> str:
    return _UP if d is Direction.LONG else _DOWN


def _opp_seq(d: Direction) -> str:
    return _DOWN if d is Direction.LONG else _UP


def _against_h1(d: Direction) -> str:
    return "DOWN" if d is Direction.LONG else "UP"


def _acc_values(mm: MarketMap, d: Direction) -> list[str | None]:
    prefix = f"{d.value}:"
    return [v for k, v in sorted(mm.acceptance_state.items()) if k.startswith(prefix)]


def _active_zone(mm: MarketMap, d: Direction) -> tuple[float, float] | None:
    return mm.active_support_zone if d is Direction.LONG else mm.active_resistance_zone


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


# ------------------------------------------------------------------------------------------------ MarketMap predicates
def h1_not_against(mm: MarketMap, d: Direction, params: Mapping[str, Any]) -> bool | None:
    if mm.h1_context is None:
        return None
    return mm.h1_context != _against_h1(d)


def m15_sequence_compatible(mm: MarketMap, d: Direction, params: Mapping[str, Any]) -> bool | None:
    if mm.m15_structure is None:
        return None
    return mm.m15_structure in (_own_seq(d), "MIXED_TRANSITION")


def at_active_zone(mm: MarketMap, d: Direction, params: Mapping[str, Any]) -> bool | None:
    return _active_zone(mm, d) is not None


def break_accepted_with_direction(
    mm: MarketMap, d: Direction, params: Mapping[str, Any]
) -> bool | None:
    return any(v in ACCEPTED_VALUES for v in _acc_values(mm, d))


def no_acceptance_against(mm: MarketMap, d: Direction, params: Mapping[str, Any]) -> bool | None:
    return not any(v in ACCEPTED_VALUES for v in _acc_values(mm, d.opposite()))


def retest_confirmed(mm: MarketMap, d: Direction, params: Mapping[str, Any]) -> bool | None:
    if any(v == "RETEST_HELD" for v in _acc_values(mm, d)):
        return True
    if mm.m5_structure is None:
        return None
    return _active_zone(mm, d) is not None and mm.m5_structure == _own_seq(d)


def structural_break_against(mm: MarketMap, d: Direction, params: Mapping[str, Any]) -> bool | None:
    if mm.m15_structure is None:
        return None
    return mm.m15_structure == _opp_seq(d)


def acceptance_against(mm: MarketMap, d: Direction, params: Mapping[str, Any]) -> bool | None:
    return any(v in ACCEPTED_VALUES for v in _acc_values(mm, d.opposite()))


def flipped_zone_overlap(mm: MarketMap, d: Direction, params: Mapping[str, Any]) -> bool | None:
    zone = _active_zone(mm, d)
    if zone is None:
        return False
    return any(_overlap(zone, z) for z in mm.role_reversal_zones)


def participation_not_low(mm: MarketMap, d: Direction, params: Mapping[str, Any]) -> bool | None:
    if mm.participation_state is None:
        return None
    return mm.participation_state != "LOW"


# ------------------------------------------------------------------------------------------------ geometry / event predicates
def _geo(g: Any, key: str) -> Any:
    if g is None:
        return None
    return g.get(key) if isinstance(g, Mapping) else getattr(g, key, None)


def structural_stop_known(geometry: Any, d: Direction, params: Mapping[str, Any]) -> bool | None:
    if geometry is None:
        return None
    stop = _geo(geometry, "structural_stop")
    if stop is None:
        return False
    zone = _geo(geometry, "entry_zone")
    if zone is not None:
        # protective side: LONG stop below the zone, SHORT stop above it (sign-symmetric)
        edge = min(zone) if d is Direction.LONG else max(zone)
        return d.sign * (edge - stop) > 0
    return True


def nearest_opposition_known(geometry: Any, d: Direction, params: Mapping[str, Any]) -> bool | None:
    if geometry is None:
        return None
    return _geo(geometry, "opposition_1") is not None


def any_trigger_event(events: Sequence[str], d: Direction, params: Mapping[str, Any]) -> bool:
    wanted = params.get("trigger_events", {}).get(d.value, ())
    return any(e in wanted for e in events)


for _name, _fn in (
    ("h1_not_against", h1_not_against),
    ("m15_sequence_compatible", m15_sequence_compatible),
    ("at_active_zone", at_active_zone),
    ("break_accepted_with_direction", break_accepted_with_direction),
    ("no_acceptance_against", no_acceptance_against),
    ("retest_confirmed", retest_confirmed),
    ("structural_break_against", structural_break_against),
    ("acceptance_against", acceptance_against),
    ("flipped_zone_overlap", flipped_zone_overlap),
    ("participation_not_low", participation_not_low),
):
    register_predicate(_name, _fn)
register_geometry_predicate("structural_stop_known", structural_stop_known)
register_geometry_predicate("nearest_opposition_known", nearest_opposition_known)
register_event_predicate("any_trigger_event", any_trigger_event)


# ------------------------------------------------------------------------------------------------ catalog
SPEC_VERSION = "setup-spec-1"

CONTINUATION_RETEST = SetupSpec(
    archetype="CONTINUATION_RETEST",
    spec_version=SPEC_VERSION,
    allowed_market_phases=(MP.TREND, MP.PULLBACK, MP.BREAKOUT, MP.TRANSITION),
    requirements={
        EC.CONTEXT: ("h1_not_against", "m15_sequence_compatible"),
        EC.LOCATION: ("at_active_zone",),
        EC.STRUCTURE: ("break_accepted_with_direction",),
        EC.LEVEL_BEHAVIOUR: ("no_acceptance_against", "retest_confirmed"),
        EC.TRIGGER: ("any_trigger_event",),
        EC.GEOMETRY: ("structural_stop_known", "nearest_opposition_known"),
    },
    optional_evidence=("flipped_zone_overlap", "participation_not_low"),
    invalidation=("structural_break_against", "acceptance_against"),
    expiry_bars=48,  # M5 bars (4h); frozen
    semantic_exit_profile="STRUCTURAL_TRAIL_REFERENCE",
    params={
        "level_behaviour_started": ("no_acceptance_against",),
        "trigger_events": {
            "LONG": ("STRUCT_RETEST_LONG", "M5_STRUCTURE_TRANSITION_UP"),
            "SHORT": ("STRUCT_RETEST_SHORT", "M5_STRUCTURE_TRANSITION_DOWN"),
        },
    },
    implemented=True,
)


def _stub(
    archetype: str,
    phases: tuple[MarketPhase, ...],
    req: dict[EvidenceClass, tuple[str, ...]],
    inval: tuple[str, ...],
    expiry: int,
) -> SetupSpec:
    return SetupSpec(
        archetype=archetype,
        spec_version=SPEC_VERSION,
        allowed_market_phases=phases,
        requirements=req,
        invalidation=inval,
        expiry_bars=expiry,
        implemented=False,
    )


_STUBS: tuple[SetupSpec, ...] = (
    _stub(
        "BREAKOUT_ACCEPTANCE",
        (MP.BREAKOUT, MP.EXPANSION, MP.TRANSITION),
        {
            EC.CONTEXT: ("h1_not_against",),
            EC.LOCATION: ("beyond_level_edge",),
            EC.STRUCTURE: ("fresh_break_found",),
            EC.LEVEL_BEHAVIOUR: ("acceptance_beyond_level",),
            EC.TRIGGER: ("any_trigger_event",),
            EC.GEOMETRY: ("structural_stop_known", "nearest_opposition_known"),
        },
        ("reclaim_through_level",),
        36,
    ),
    _stub(
        "FAILED_BREAK_RECLAIM",
        (MP.FAILED_BREAK, MP.REVERSAL_ATTEMPT, MP.RANGE),
        {
            EC.CONTEXT: ("not_strong_trend_against",),
            EC.LOCATION: ("at_broken_level",),
            EC.STRUCTURE: ("break_then_reclaim",),
            EC.LEVEL_BEHAVIOUR: ("no_reacceptance_beyond",),
            EC.TRIGGER: ("any_trigger_event",),
            EC.GEOMETRY: ("structural_stop_known", "nearest_opposition_known"),
        },
        ("reacceptance_beyond_level",),
        36,
    ),
    _stub(
        "RANGE_EDGE_REJECTION",
        (MP.RANGE, MP.BALANCE),
        {
            EC.CONTEXT: ("balance_confirmed",),
            EC.LOCATION: ("at_range_edge",),
            EC.STRUCTURE: ("edge_tested",),
            EC.LEVEL_BEHAVIOUR: ("rejection_at_edge",),
            EC.TRIGGER: ("any_trigger_event",),
            EC.GEOMETRY: ("structural_stop_known", "nearest_opposition_known"),
        },
        ("acceptance_beyond_edge",),
        48,
    ),
    _stub(
        "TREND_PULLBACK",
        (MP.TREND, MP.PULLBACK),
        {
            EC.CONTEXT: ("h1_not_against", "m15_sequence_compatible"),
            EC.LOCATION: ("at_pullback_zone",),
            EC.STRUCTURE: ("pullback_depth_in_bounds",),
            EC.LEVEL_BEHAVIOUR: ("pullback_not_accepted_through",),
            EC.TRIGGER: ("any_trigger_event",),
            EC.GEOMETRY: ("structural_stop_known", "nearest_opposition_known"),
        },
        ("structural_break_against",),
        48,
    ),
    _stub(
        "ROLE_REVERSAL_RETEST",
        (MP.TREND, MP.PULLBACK, MP.BREAKOUT, MP.TRANSITION),
        {
            EC.CONTEXT: ("h1_not_against",),
            EC.LOCATION: ("at_role_reversal_zone",),
            EC.STRUCTURE: ("role_flip_observed",),
            EC.LEVEL_BEHAVIOUR: ("flip_zone_held",),
            EC.TRIGGER: ("any_trigger_event",),
            EC.GEOMETRY: ("structural_stop_known", "nearest_opposition_known"),
        },
        ("role_flip_failed",),
        48,
    ),
    _stub(
        "COMPRESSION_EXPANSION",
        (MP.COMPRESSION, MP.EXPANSION, MP.BALANCE),
        {
            EC.CONTEXT: ("compression_confirmed",),
            EC.LOCATION: ("at_compression_edge",),
            EC.STRUCTURE: ("range_contracting",),
            EC.LEVEL_BEHAVIOUR: ("expansion_bar_with_acceptance",),
            EC.TRIGGER: ("any_trigger_event",),
            EC.GEOMETRY: ("structural_stop_known", "nearest_opposition_known"),
        },
        ("expansion_failed",),
        36,
    ),
    _stub(
        "OPENING_DRIVE_PULLBACK",
        (MP.EXPANSION, MP.PULLBACK, MP.TREND),
        {
            EC.CONTEXT: ("session_open_window",),
            EC.LOCATION: ("at_drive_pullback_zone",),
            EC.STRUCTURE: ("opening_drive_observed",),
            EC.LEVEL_BEHAVIOUR: ("pullback_held",),
            EC.TRIGGER: ("any_trigger_event",),
            EC.GEOMETRY: ("structural_stop_known", "nearest_opposition_known"),
        },
        ("drive_origin_broken",),
        24,
    ),
    _stub(
        "PRIOR_LEVEL_BREAK_RETEST",
        (MP.BREAKOUT, MP.PULLBACK, MP.TRANSITION),
        {
            EC.CONTEXT: ("h1_not_against",),
            EC.LOCATION: ("at_prior_level",),
            EC.STRUCTURE: ("prior_level_break_accepted",),
            EC.LEVEL_BEHAVIOUR: ("retest_confirmed",),
            EC.TRIGGER: ("any_trigger_event",),
            EC.GEOMETRY: ("structural_stop_known", "nearest_opposition_known"),
        },
        ("reclaim_through_prior_level",),
        48,
    ),
    _stub(
        "EXHAUSTION_FAILED_EXPANSION_REVERSAL",
        (MP.EXPANSION, MP.REVERSAL_ATTEMPT, MP.FAILED_BREAK),
        {
            EC.CONTEXT: ("extended_move",),
            EC.LOCATION: ("at_extension_extreme",),
            EC.STRUCTURE: ("expansion_stalled",),
            EC.LEVEL_BEHAVIOUR: ("rejection_after_expansion",),
            EC.TRIGGER: ("any_trigger_event",),
            EC.GEOMETRY: ("structural_stop_known", "nearest_opposition_known"),
        },
        ("expansion_resumed",),
        36,
    ),
)

_CATALOG: dict[str, SetupSpec] = {s.archetype: s for s in (CONTINUATION_RETEST, *_STUBS)}


def catalog() -> tuple[SetupSpec, ...]:
    """All ten archetype specs in a stable order (CONTINUATION_RETEST first)."""
    return tuple(_CATALOG.values())


def get_spec(name: str) -> SetupSpec:
    try:
        return _CATALOG[name]
    except KeyError:
        raise KeyError(f"unknown setup archetype: {name}") from None


__all__ = ["CONTINUATION_RETEST", "SPEC_VERSION", "catalog", "get_spec"]
