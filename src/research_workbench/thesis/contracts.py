"""Thesis / setup / position-thesis CONTRACTS (RESEARCH / OFFLINE ONLY).

Frozen data contracts + the legal transition tables. Nothing here has trade authority and nothing in this package may be imported by
production code (src/demo, scripts/demo_trader.py, scripts/autostart). Design: docs/market_thesis_architecture_v1.md (revised after
CODEX-2/3). Authority map: docs/authority_map_research_v1.md.

Timing convention (CODEX-2 finding): ``ObserverBars.ts_ns`` is the bar OPEN. A decision at bar ``i`` is taken at its CLOSE
(``bars.decision_ts_ns(i)``). Every timestamp stored in these contracts is a DECISION-time (close-based) timestamp, and every level
confirmation / role event / baseline window / trigger referenced by a contract must be <= ``decision_ts_ns``. Never compare against
``ts_ns[i]`` alone.

Retroactivity rule: the first time a state / condition is observed its timestamp is FROZEN (``first_observed_ns``); a later bar can add
transitions but never rewrites what the state was at an earlier decision time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from research_speed.artifact import config_hash

THESIS_CONTRACT_VERSION = "thesis-contract-1"


# ------------------------------------------------------------------------------------------------ enums
class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    def opposite(self) -> Direction:
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class EvidenceClass(StrEnum):
    """Evidence classes (no voting machine: correlated information is not independent evidence)."""

    CONTEXT = "CONTEXT"
    LOCATION = "LOCATION"
    STRUCTURE = "STRUCTURE"
    LEVEL_BEHAVIOUR = "LEVEL_BEHAVIOUR"
    TRIGGER = "TRIGGER"
    GEOMETRY = "GEOMETRY"
    PARTICIPATION = "PARTICIPATION"


class MarketPhase(StrEnum):
    TREND = "TREND"
    PULLBACK = "PULLBACK"
    BALANCE = "BALANCE"
    RANGE = "RANGE"
    COMPRESSION = "COMPRESSION"
    EXPANSION = "EXPANSION"
    BREAKOUT = "BREAKOUT"
    FAILED_BREAK = "FAILED_BREAK"
    REVERSAL_ATTEMPT = "REVERSAL_ATTEMPT"
    TRANSITION = "TRANSITION"
    UNDEFINED = "UNDEFINED"


class MainThesisState(StrEnum):
    BULLISH_CONTINUATION = "BULLISH_CONTINUATION"
    BEARISH_CONTINUATION = "BEARISH_CONTINUATION"
    RANGE_ROTATION = "RANGE_ROTATION"
    BREAKOUT_EXPANSION = "BREAKOUT_EXPANSION"
    FAILED_BREAK_REVERSAL = "FAILED_BREAK_REVERSAL"
    TRANSITION = "TRANSITION"
    NO_CLEAR_THESIS = "NO_CLEAR_THESIS"  # valid; a daily bias is never forced


class Alignment(StrEnum):
    ALIGNED = "ALIGNED"
    NEUTRAL = "NEUTRAL"
    OPPOSED = "OPPOSED"


class SetupState(StrEnum):
    CANDIDATE = "CANDIDATE"
    FORMING = "FORMING"
    LOCATION_REACHED = "LOCATION_REACHED"
    CONFIRMING = "CONFIRMING"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class PositionThesisState(StrEnum):
    HEALTHY = "HEALTHY"
    OPPOSING_EVENT = "OPPOSING_EVENT"
    OPPOSING_SETUP = "OPPOSING_SETUP"
    THESIS_AT_RISK = "THESIS_AT_RISK"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    CLOSED = "CLOSED"


class ResearchClass(StrEnum):
    """Result classification. There is deliberately NO live / production member."""

    REJECT = "REJECT"
    INCONCLUSIVE = "INCONCLUSIVE"
    PROMISING_FOR_FIDELITY = "PROMISING_FOR_FIDELITY"
    FORWARD_SHADOW_CANDIDATE = "FORWARD_SHADOW_CANDIDATE"


# ------------------------------------------------------------------------------------------------ legal transitions
TERMINAL_SETUP_STATES = frozenset({SetupState.TRIGGERED, SetupState.INVALIDATED, SetupState.EXPIRED})
# the ONLY legal forward edges (besides INVALIDATED / EXPIRED from any non-terminal state). A single bar may chain several edges in
# order (all stamped with the same decision ts); skipping an edge is illegal.
SETUP_FORWARD_EDGES: dict[SetupState, SetupState] = {
    SetupState.CANDIDATE: SetupState.FORMING,
    SetupState.FORMING: SetupState.LOCATION_REACHED,
    SetupState.LOCATION_REACHED: SetupState.CONFIRMING,
    SetupState.CONFIRMING: SetupState.ARMED,
    SetupState.ARMED: SetupState.TRIGGERED,
}
# precedence when several outcomes coincide on one decision bar (first match wins): invalidation, then expiry, then progress
SETUP_PRECEDENCE = (SetupState.INVALIDATED, SetupState.EXPIRED, "FORWARD")


def legal_setup_transition(src: SetupState, dst: SetupState) -> bool:
    """True iff ``src -> dst`` is a declared edge. Terminal states are absorbing (no outgoing edge)."""
    if src in TERMINAL_SETUP_STATES:
        return False
    if dst in (SetupState.INVALIDATED, SetupState.EXPIRED):
        return True
    return SETUP_FORWARD_EDGES.get(src) == dst


# Position thesis: severity ordering (worse = higher). A position thesis keeps the CURRENT state and the WORST state seen so far.
POSITION_SEVERITY = {
    PositionThesisState.HEALTHY: 0,
    PositionThesisState.OPPOSING_EVENT: 1,
    PositionThesisState.OPPOSING_SETUP: 2,
    PositionThesisState.THESIS_AT_RISK: 3,
    PositionThesisState.THESIS_INVALIDATED: 4,
}


def legal_position_transition(src: PositionThesisState, dst: PositionThesisState) -> bool:
    """CLOSED is terminal; THESIS_INVALIDATED is absorbing except for CLOSED; the other states may move to any other non-CLOSED state
    (evidence can appear and fade) and to CLOSED. A transition to the same state is not a transition."""
    if src is dst or src is PositionThesisState.CLOSED:
        return False
    if dst is PositionThesisState.CLOSED:
        return True
    if src is PositionThesisState.THESIS_INVALIDATED:
        return False
    return True


# ------------------------------------------------------------------------------------------------ dataclasses
@dataclass(frozen=True)
class Provenance:
    """Where a MarketMap field came from (module + version) so a result is auditable and caches are version-exact."""

    source: str  # e.g. "market_observer.swings.swing_state"
    version: str
    definition_hash: str = "-"


@dataclass(frozen=True)
class Condition:
    """One required/observed/missing condition of a thesis (explainable instead of an opaque score)."""

    evidence_class: EvidenceClass
    name: str
    required: bool
    observed: bool | None  # None = not evaluable yet (warm-up / missing facts)
    first_observed_ns: int | None  # frozen decision-time stamp of the first observation
    detail: str = ""


@dataclass(frozen=True)
class MarketMap:
    """Causal, deterministic, versioned snapshot of market facts at ONE decision time. NO trade authority."""

    market: str
    decision_ts_ns: int  # bars.decision_ts_ns(i) = bar close
    bar_index: int
    session: str | None
    market_phase: MarketPhase
    h1_context: str | None  # e.g. "UP" | "DOWN" | "NEUTRAL" | None (warm-up)
    m15_structure: str | None  # swing sequence label, e.g. "UP_SEQUENCE"
    m5_structure: str | None
    nearest_support: float | None
    nearest_resistance: float | None
    second_support: float | None
    second_resistance: float | None
    active_support_zone: tuple[float, float] | None
    active_resistance_zone: tuple[float, float] | None
    role_reversal_zones: tuple[tuple[float, float], ...]
    balance_state: str | None
    acceptance_state: dict[str, str | None] = field(default_factory=dict)  # acceptance is level- and direction-specific
    participation_state: str | None = None  # MT5 tick-activity PROXY, not exchange volume
    volatility_context: str | None = None
    provenance: dict[str, Provenance] = field(default_factory=dict)
    marketmap_version: str = "-"
    observer_version: str = "-"
    definition_hashes: dict[str, str] = field(default_factory=dict)
    code_sha: str = "-"

    def content_hash(self) -> str:
        return config_hash(self)


@dataclass(frozen=True)
class MarketThesis:
    market: str
    decision_ts_ns: int
    state: MainThesisState
    direction_hint: Direction | None
    basis: tuple[Condition, ...]
    marketmap_version: str
    thesis_contract_version: str = THESIS_CONTRACT_VERSION


@dataclass(frozen=True)
class SetupSpec:
    """Declarative setup definition. Adding a hypothesis = adding a spec, never an engine."""

    archetype: str
    spec_version: str
    allowed_market_phases: tuple[MarketPhase, ...]
    requirements: dict[EvidenceClass, tuple[str, ...]]  # named predicates over MarketMap fields (resolved by the engine's registry)
    optional_evidence: tuple[str, ...] = ()
    invalidation: tuple[str, ...] = ()
    expiry_bars: int = 0  # 0 = no time expiry
    semantic_exit_profile: str | None = None  # reference only
    params: dict[str, Any] = field(default_factory=dict)  # frozen thresholds (no tuning before a result exists)
    implemented: bool = True  # False => representable but NOT_EVALUATED

    @property
    def spec_hash(self) -> str:
        return config_hash(self)


@dataclass(frozen=True)
class Transition:
    ts_ns: int  # decision time of the bar on which the transition was OBSERVED (frozen)
    src: SetupState
    dst: SetupState
    reason: str


@dataclass(frozen=True)
class SetupThesis:
    thesis_id: str
    market: str
    direction: Direction
    archetype: str
    spec_hash: str
    state: SetupState
    created_at_ns: int
    updated_at_ns: int
    conditions: tuple[Condition, ...]
    invalidation_condition: str | None
    expiry_condition: str | None
    entry_zone: tuple[float, float] | None
    structural_stop: float | None
    opposition_1: float | None
    opposition_2: float | None
    family_events: tuple[str, ...]
    daily_thesis_alignment: Alignment
    marketmap_version: str
    history: tuple[Transition, ...] = ()


@dataclass(frozen=True)
class SetupEvaluation:
    """Result of one ``advance`` step (pure function of previous state + MarketMap at T + events at T)."""

    thesis: SetupThesis
    transitions: tuple[Transition, ...]
    newly_observed: tuple[str, ...]
    newly_missing: tuple[str, ...]


@dataclass(frozen=True)
class OpposingEvent:
    ts_ns: int  # decision time
    direction: Direction  # the OPPOSING direction
    source: str  # Family event name or setup archetype
    rejected_by_stack_gate: bool | None  # OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1 (None = unknown)
    future_path_complete: bool  # False => PENDING (never "missing")


@dataclass(frozen=True)
class PositionThesisTransition:
    ts_ns: int
    src: PositionThesisState
    dst: PositionThesisState
    reason: str


@dataclass(frozen=True)
class PositionThesis:
    """The entry thesis of a FILLED trade. Observation only: nothing here exits, reduces or reverses a position."""

    position_id: str  # intent_id / opportunity_id of the trade
    market: str
    direction: Direction
    entry_ts_ns: int
    setup_thesis: SetupThesis | None  # None when the entry came from a bare Family trigger
    family_trigger: str | None
    premise: tuple[Condition, ...]  # the causal premise of the trade (e.g. FLIPPED_TO_SUPPORT holds)
    state: PositionThesisState = PositionThesisState.HEALTHY
    worst_state: PositionThesisState = PositionThesisState.HEALTHY
    opposing_events: tuple[OpposingEvent, ...] = ()
    history: tuple[PositionThesisTransition, ...] = ()


__all__ = [name for name in dir() if not name.startswith("_")]
