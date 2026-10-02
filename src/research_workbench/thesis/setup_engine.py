# ruff: noqa: E501
"""Generic setup engine (RESEARCH / OFFLINE ONLY): ONE state machine driven by declarative ``SetupSpec`` objects.

``advance(prev, market_map, events, spec, direction, ...)`` is a PURE function of (previous thesis, MarketMap at T, events at T, optional
alignment/geometry at T). It never sees later data, so a state at T can never change retroactively; first-observed timestamps are frozen in
``Condition.first_observed_ns``. Adding a hypothesis = adding a ``SetupSpec`` (see ``specs.py``) and, if needed, registering predicates.

Predicate registries (resolved BY NAME from the spec; an unknown name on an implemented spec raises ``UnknownPredicate``):
* ``PREDICATES``           ``(MarketMap, Direction, params) -> bool | None``  CONTEXT / LOCATION / STRUCTURE / LEVEL_BEHAVIOUR / invalidation /
                           optional evidence. ``None`` = not evaluable (warm-up / missing facts) and never satisfies a requirement.
* ``GEOMETRY_PREDICATES``  ``(geometry | None, Direction, params) -> bool | None``  GEOMETRY availability flags (caller supplies geometry).
* ``EVENT_PREDICATES``     ``(events, Direction, params) -> bool``                    TRIGGER (existing Family / observer event names).

FROZEN edge -> evidence-class mapping (all required conditions of the listed classes must be observed True ON THE DECISION BAR; the state
itself is the memory of earlier edges):

    CANDIDATE        -> FORMING           CONTEXT
    FORMING          -> LOCATION_REACHED  LOCATION
    LOCATION_REACHED -> CONFIRMING        STRUCTURE  +  LEVEL_BEHAVIOUR "started" names
    CONFIRMING       -> ARMED             ALL LEVEL_BEHAVIOUR names (started + confirmed)  +  GEOMETRY availability
    ARMED            -> TRIGGERED         TRIGGER (events at T)

LEVEL_BEHAVIOUR "started" names = ``spec.params["level_behaviour_started"]`` (a subset of ``requirements[LEVEL_BEHAVIOUR]``; default = all of
them, i.e. started == confirmed). The remaining names are the "confirmed" tier.

Per-bar precedence (contracts.SETUP_PRECEDENCE): any spec.invalidation predicate True -> INVALIDATED; else expiry (bars since creation >=
``spec.expiry_bars``, measured in ``MarketMap.bar_index``) -> EXPIRED; else forward edges chain in order (several may fire on one bar, all
stamped with the bar's decision ts, each checked with ``legal_setup_transition``; an illegal one raises ``IllegalSetupTransition``).
Terminal states are absorbing: a later bar returns the unchanged thesis with status TERMINAL.

Creation: with ``prev is None`` a CANDIDATE is created at the first bar whose ``market_phase`` is in ``spec.allowed_market_phases`` and where
no invalidation predicate is True (the new thesis may immediately chain forward on that same bar). Otherwise status NO_SETUP. After a terminal
state the replayers do NOT re-arm; a new setup lifecycle is a new replay (caller decides where).

Result type: ``StepResult(status, evaluation)``; the frozen ``SetupEvaluation`` contract cannot represent "no thesis", so the no-setup and
NOT_EVALUATED outcomes live in ``status``. NOT_EVALUATED (spec.implemented False) is explicit and never a silent zero.

Condition semantics: ``observed`` is the value on the latest bar (``None`` = not evaluable); ``first_observed_ns`` is the decision ts of the
first bar on which it was True and is never rewritten. ``newly_observed`` = first time True this bar; ``newly_missing`` = required conditions
that were True on the previous bar and are not True now.

The expiry reference is stored in ``SetupThesis.expiry_condition`` as ``"bars>=N since bar_index=B"`` (contract has no bar-index field).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from research_workbench.thesis.contracts import (
    SETUP_FORWARD_EDGES,
    TERMINAL_SETUP_STATES,
    Alignment,
    Condition,
    Direction,
    EvidenceClass,
    MarketMap,
    SetupEvaluation,
    SetupSpec,
    SetupState,
    SetupThesis,
    Transition,
    legal_setup_transition,
)

Params = Mapping[str, Any]
PredicateFn = Callable[[MarketMap, Direction, Params], "bool | None"]
GeometryPredicateFn = Callable[[Any, Direction, Params], "bool | None"]
EventPredicateFn = Callable[[Sequence[str], Direction, Params], bool]

PREDICATES: dict[str, PredicateFn] = {}
GEOMETRY_PREDICATES: dict[str, GeometryPredicateFn] = {}
EVENT_PREDICATES: dict[str, EventPredicateFn] = {}

# frozen edge -> evidence classes (LEVEL_BEHAVIOUR is split by the spec's "started" names; see module docstring)
EDGE_EVIDENCE: dict[tuple[SetupState, SetupState], tuple[EvidenceClass, ...]] = {
    (SetupState.CANDIDATE, SetupState.FORMING): (EvidenceClass.CONTEXT,),
    (SetupState.FORMING, SetupState.LOCATION_REACHED): (EvidenceClass.LOCATION,),
    (SetupState.LOCATION_REACHED, SetupState.CONFIRMING): (
        EvidenceClass.STRUCTURE,
        EvidenceClass.LEVEL_BEHAVIOUR,
    ),
    (SetupState.CONFIRMING, SetupState.ARMED): (
        EvidenceClass.LEVEL_BEHAVIOUR,
        EvidenceClass.GEOMETRY,
    ),
    (SetupState.ARMED, SetupState.TRIGGERED): (EvidenceClass.TRIGGER,),
}
EDGE_FROM = frozenset(SETUP_FORWARD_EDGES)
SETUP_NEXT = SETUP_FORWARD_EDGES
_CLASS_ORDER = {c: i for i, c in enumerate(EvidenceClass)}


class IllegalSetupTransition(RuntimeError):
    """The engine attempted (or was asked to apply) an edge not declared in ``contracts.SETUP_FORWARD_EDGES``."""


class UnknownPredicate(KeyError):
    """A spec references a predicate name that is not registered for its evidence class."""


class EvaluationStatus(StrEnum):
    EVALUATED = "EVALUATED"  # a live / just-terminated thesis was processed on this bar
    TERMINAL = "TERMINAL"  # prev was already terminal; absorbed, nothing changed
    NO_SETUP = "NO_SETUP"  # no thesis exists and the creation gate is closed on this bar
    NOT_EVALUATED = "NOT_EVALUATED"  # spec.implemented is False (explicit, never a silent zero)


@dataclass(frozen=True)
class StepResult:
    status: EvaluationStatus
    evaluation: SetupEvaluation | None
    note: str = ""

    @property
    def thesis(self) -> SetupThesis | None:
        return None if self.evaluation is None else self.evaluation.thesis


class GeometryLike(Protocol):
    """Minimal geometry the engine needs (T1's ``geometry_for_entry`` result may satisfy it; a plain dict with the same keys works too)."""

    entry_zone: tuple[float, float] | None
    structural_stop: float | None
    opposition_1: float | None
    opposition_2: float | None


# ------------------------------------------------------------------------------------------------ registration
def register_predicate(name: str, fn: PredicateFn) -> PredicateFn:
    if name in PREDICATES:
        raise ValueError(f"predicate already registered: {name}")
    PREDICATES[name] = fn
    return fn


def register_geometry_predicate(name: str, fn: GeometryPredicateFn) -> GeometryPredicateFn:
    if name in GEOMETRY_PREDICATES:
        raise ValueError(f"geometry predicate already registered: {name}")
    GEOMETRY_PREDICATES[name] = fn
    return fn


def register_event_predicate(name: str, fn: EventPredicateFn) -> EventPredicateFn:
    if name in EVENT_PREDICATES:
        raise ValueError(f"event predicate already registered: {name}")
    EVENT_PREDICATES[name] = fn
    return fn


def _registry_for(ec: EvidenceClass) -> Mapping[str, Any]:
    if ec is EvidenceClass.GEOMETRY:
        return GEOMETRY_PREDICATES
    if ec is EvidenceClass.TRIGGER:
        return EVENT_PREDICATES
    return PREDICATES


def validate_spec(spec: SetupSpec) -> None:
    """Raise ``UnknownPredicate`` if an implemented spec references an unregistered name; check the LEVEL_BEHAVIOUR split."""
    if not spec.implemented:
        return
    for ec, names in spec.requirements.items():
        reg = _registry_for(ec)
        for n in names:
            if n not in reg:
                raise UnknownPredicate(
                    f"{spec.archetype}: {ec.value} predicate not registered: {n}"
                )
    for n in (*spec.invalidation, *spec.optional_evidence):
        if n not in PREDICATES:
            raise UnknownPredicate(f"{spec.archetype}: predicate not registered: {n}")
    started = spec.params.get("level_behaviour_started")
    if started is not None and not set(started) <= set(
        spec.requirements.get(EvidenceClass.LEVEL_BEHAVIOUR, ())
    ):
        raise ValueError(
            f"{spec.archetype}: level_behaviour_started must be a subset of the LEVEL_BEHAVIOUR requirements"
        )
    for src, dst in EDGE_EVIDENCE:
        if not _edge_names(spec, src, dst):
            raise ValueError(
                f"{spec.archetype}: edge {src.value} -> {dst.value} has no evidence predicate (fail closed)"
            )


# ------------------------------------------------------------------------------------------------ helpers
def thesis_id_for(
    market: str, direction: Direction, archetype: str, spec_hash: str, created_at_ns: int
) -> str:
    raw = f"{market}|{direction.value}|{archetype}|{spec_hash}|{created_at_ns}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


_EXPIRY_RE = re.compile(r"^bars>=(\d+) since bar_index=(-?\d+)$")


def _expiry_text(spec: SetupSpec, bar_index: int) -> str | None:
    return f"bars>={spec.expiry_bars} since bar_index={bar_index}" if spec.expiry_bars > 0 else None


def _expired(thesis: SetupThesis, bar_index: int) -> bool:
    if thesis.expiry_condition is None:
        return False
    m = _EXPIRY_RE.match(thesis.expiry_condition)
    if m is None:
        raise ValueError(f"unparseable expiry_condition: {thesis.expiry_condition!r}")
    return bar_index - int(m.group(2)) >= int(m.group(1))


def _geo_get(geometry: Any, key: str) -> Any:
    if geometry is None:
        return None
    if isinstance(geometry, Mapping):
        return geometry.get(key)
    return getattr(geometry, key, None)


def _ordered_names(spec: SetupSpec) -> list[tuple[EvidenceClass, str, bool]]:
    out: list[tuple[EvidenceClass, str, bool]] = []
    for ec in sorted(spec.requirements, key=lambda c: _CLASS_ORDER[c]):
        out.extend((ec, n, True) for n in spec.requirements[ec])
    out.extend((EvidenceClass.PARTICIPATION, n, False) for n in spec.optional_evidence)
    return out


def _evaluate(
    spec: SetupSpec, mm: MarketMap, direction: Direction, events: Sequence[str], geometry: Any
) -> dict[tuple[EvidenceClass, str], bool | None]:
    params = spec.params
    values: dict[tuple[EvidenceClass, str], bool | None] = {}
    for ec, name, _req in _ordered_names(spec):
        if ec is EvidenceClass.GEOMETRY:
            v = GEOMETRY_PREDICATES[name](geometry, direction, params)
        elif ec is EvidenceClass.TRIGGER:
            v = EVENT_PREDICATES[name](tuple(events), direction, params)
        else:
            v = PREDICATES[name](mm, direction, params)
        values[(ec, name)] = None if v is None else bool(v)
    return values


def _edge_names(
    spec: SetupSpec, src: SetupState, dst: SetupState
) -> list[tuple[EvidenceClass, str]]:
    classes = EDGE_EVIDENCE[(src, dst)]
    lb_all = spec.requirements.get(EvidenceClass.LEVEL_BEHAVIOUR, ())
    started = tuple(spec.params.get("level_behaviour_started", lb_all))
    out: list[tuple[EvidenceClass, str]] = []
    for ec in classes:
        names = spec.requirements.get(ec, ())
        if ec is EvidenceClass.LEVEL_BEHAVIOUR and dst is SetupState.CONFIRMING:
            names = tuple(n for n in lb_all if n in started)
        out.extend((ec, n) for n in names)
    return out


def _all_true(
    values: Mapping[tuple[EvidenceClass, str], bool | None],
    keys: Sequence[tuple[EvidenceClass, str]],
) -> bool:
    return bool(keys) and all(
        values.get(k) is True for k in keys
    )  # empty edge = NOT satisfied (fail closed)


def _build_conditions(
    spec: SetupSpec,
    values: Mapping[tuple[EvidenceClass, str], bool | None],
    prev: SetupThesis | None,
    ts: int,
) -> tuple[tuple[Condition, ...], tuple[str, ...], tuple[str, ...]]:
    prev_by_key = {(c.evidence_class, c.name): c for c in (prev.conditions if prev else ())}
    conds: list[Condition] = []
    newly_obs: list[str] = []
    newly_missing: list[str] = []
    for ec, name, req in _ordered_names(spec):
        v = values[(ec, name)]
        old = prev_by_key.get((ec, name))
        first = old.first_observed_ns if old is not None else None
        if v is True and first is None:
            first = ts
            newly_obs.append(f"{ec.value}:{name}")
        if req and old is not None and old.observed is True and v is not True:
            newly_missing.append(f"{ec.value}:{name}")
        conds.append(
            Condition(
                ec,
                name,
                req,
                v,
                first,
                detail={True: "observed", False: "not observed", None: "not evaluable"}[v],
            )
        )
    return tuple(conds), tuple(newly_obs), tuple(newly_missing)


def _trigger_family_events(
    spec: SetupSpec,
    prev: SetupThesis | None,
    values: Mapping[tuple[EvidenceClass, str], bool | None],
    events: Sequence[str],
    direction: Direction,
) -> tuple[str, ...]:
    seen = list(prev.family_events) if prev else []
    if any(
        values.get((EvidenceClass.TRIGGER, n)) is True
        for n in spec.requirements.get(EvidenceClass.TRIGGER, ())
    ):
        wanted = spec.params.get("trigger_events", {}).get(direction.value, ())
        for e in events:
            if e in wanted and e not in seen:
                seen.append(e)
    return tuple(seen)


# ------------------------------------------------------------------------------------------------ advance
def advance(
    prev: SetupThesis | None,
    market_map: MarketMap,
    events: Sequence[str],
    spec: SetupSpec,
    direction: Direction,
    *,
    alignment: Alignment = Alignment.NEUTRAL,
    geometry: Any = None,
) -> StepResult:
    """One causal step. See the module docstring for precedence, creation and the edge -> evidence mapping."""
    if not spec.implemented:
        return StepResult(
            EvaluationStatus.NOT_EVALUATED, None, f"{spec.archetype}: spec.implemented is False"
        )
    validate_spec(spec)
    ts = market_map.decision_ts_ns

    if prev is not None:
        if prev.archetype != spec.archetype or prev.spec_hash != spec.spec_hash:
            raise ValueError("prev thesis does not belong to this spec")
        if prev.direction is not direction or prev.market != market_map.market:
            raise ValueError("prev thesis direction/market mismatch")
        if ts <= prev.updated_at_ns:
            raise ValueError(
                f"decision ts must strictly increase (prev {prev.updated_at_ns}, got {ts})"
            )
        if prev.state in TERMINAL_SETUP_STATES:
            ev = SetupEvaluation(prev, (), (), ())
            return StepResult(EvaluationStatus.TERMINAL, ev, "absorbing terminal state")

    values = _evaluate(spec, market_map, direction, events, geometry)
    inval_hit = next(
        (n for n in spec.invalidation if PREDICATES[n](market_map, direction, spec.params) is True),
        None,
    )

    if prev is None:
        if market_map.market_phase not in spec.allowed_market_phases or inval_hit is not None:
            return StepResult(EvaluationStatus.NO_SETUP, None, "creation gate closed")
        thesis = SetupThesis(
            thesis_id=thesis_id_for(
                market_map.market, direction, spec.archetype, spec.spec_hash, ts
            ),
            market=market_map.market,
            direction=direction,
            archetype=spec.archetype,
            spec_hash=spec.spec_hash,
            state=SetupState.CANDIDATE,
            created_at_ns=ts,
            updated_at_ns=ts,
            conditions=(),
            invalidation_condition="|".join(spec.invalidation) or None,
            expiry_condition=_expiry_text(spec, market_map.bar_index),
            entry_zone=None,
            structural_stop=None,
            opposition_1=None,
            opposition_2=None,
            family_events=(),
            daily_thesis_alignment=alignment,
            marketmap_version=market_map.marketmap_version,
            history=(),
        )
        cur = thesis
        base_for_conditions: SetupThesis | None = None
    else:
        cur = prev
        base_for_conditions = prev

    transitions: list[Transition] = []

    def _move(dst: SetupState, reason: str) -> None:
        nonlocal cur
        if not legal_setup_transition(cur.state, dst):
            raise IllegalSetupTransition(f"{cur.state.value} -> {dst.value}")
        transitions.append(Transition(ts, cur.state, dst, reason))
        cur = replace(cur, state=dst)

    if inval_hit is not None:
        _move(SetupState.INVALIDATED, f"invalidation:{inval_hit}")
    elif _expired(cur, market_map.bar_index):
        _move(SetupState.EXPIRED, f"expiry:{cur.expiry_condition}")
    else:
        while cur.state in EDGE_FROM:
            dst = SETUP_NEXT[cur.state]
            keys = _edge_names(spec, cur.state, dst)
            if not _all_true(values, keys):
                break
            _move(dst, "edge:" + ",".join(f"{ec.value}[{n}]" for ec, n in keys))

    conds, newly_obs, newly_missing = _build_conditions(spec, values, base_for_conditions, ts)
    updates: dict[str, Any] = {
        "conditions": conds,
        "updated_at_ns": ts,
        "daily_thesis_alignment": alignment,
        "marketmap_version": market_map.marketmap_version,
        "family_events": _trigger_family_events(
            spec, base_for_conditions, values, events, direction
        ),
        "history": (base_for_conditions.history if base_for_conditions else ())
        + tuple(transitions),
    }
    if geometry is not None:
        updates.update(
            entry_zone=_geo_get(geometry, "entry_zone"),
            structural_stop=_geo_get(geometry, "structural_stop"),
            opposition_1=_geo_get(geometry, "opposition_1"),
            opposition_2=_geo_get(geometry, "opposition_2"),
        )
    cur = replace(cur, **updates)
    return StepResult(
        EvaluationStatus.EVALUATED,
        SetupEvaluation(cur, tuple(transitions), newly_obs, newly_missing),
    )


# ------------------------------------------------------------------------------------------------ replay
class SetupReplayer:
    """Incremental driver: ``step(market_map, events)`` feeds one bar at a time; holds only the previous thesis."""

    def __init__(self, spec: SetupSpec, direction: Direction, market: str | None = None) -> None:
        self.spec = spec
        self.direction = direction
        self.market = market
        self.thesis: SetupThesis | None = None
        self._last_ts: int | None = None

    def step(
        self,
        market_map: MarketMap,
        events: Sequence[str] = (),
        *,
        alignment: Alignment = Alignment.NEUTRAL,
        geometry: Any = None,
    ) -> StepResult:
        if self.market is None:
            self.market = market_map.market
        if market_map.market != self.market:
            raise ValueError("market changed inside one replay")
        if self._last_ts is not None and market_map.decision_ts_ns <= self._last_ts:
            raise ValueError("decision ts must strictly increase")
        self._last_ts = market_map.decision_ts_ns
        res = advance(
            self.thesis,
            market_map,
            events,
            self.spec,
            self.direction,
            alignment=alignment,
            geometry=geometry,
        )
        if res.thesis is not None:
            self.thesis = res.thesis
        return res


def replay(
    market_maps: Sequence[MarketMap],
    spec: SetupSpec,
    direction: Direction,
    events_by_ts: Mapping[int, Sequence[str]] | None = None,
    *,
    alignment_by_ts: Mapping[int, Alignment] | None = None,
    geometry_by_ts: Mapping[int, Any] | None = None,
) -> tuple[StepResult, ...]:
    """Batch replay: the same pure ``advance`` applied bar by bar over a prefix-closed list (one result per MarketMap)."""
    events_by_ts = events_by_ts or {}
    alignment_by_ts = alignment_by_ts or {}
    geometry_by_ts = geometry_by_ts or {}
    out: list[StepResult] = []
    prev: SetupThesis | None = None
    last_ts: int | None = None
    for mm in market_maps:
        if last_ts is not None and mm.decision_ts_ns <= last_ts:
            raise ValueError("decision ts must strictly increase")
        last_ts = mm.decision_ts_ns
        res = advance(
            prev,
            mm,
            events_by_ts.get(mm.decision_ts_ns, ()),
            spec,
            direction,
            alignment=alignment_by_ts.get(mm.decision_ts_ns, Alignment.NEUTRAL),
            geometry=geometry_by_ts.get(mm.decision_ts_ns),
        )
        if res.thesis is not None:
            prev = res.thesis
        out.append(res)
    return tuple(out)


__all__ = [
    "EDGE_EVIDENCE",
    "EVENT_PREDICATES",
    "GEOMETRY_PREDICATES",
    "PREDICATES",
    "EvaluationStatus",
    "GeometryLike",
    "IllegalSetupTransition",
    "SetupReplayer",
    "StepResult",
    "UnknownPredicate",
    "advance",
    "register_event_predicate",
    "register_geometry_predicate",
    "register_predicate",
    "replay",
    "thesis_id_for",
    "validate_spec",
]
