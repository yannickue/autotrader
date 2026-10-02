# ruff: noqa: E501
"""Position-thesis monitor, opposing-signal research and hypothetical exit variants (RESEARCH / OFFLINE ONLY).

No trade authority, never imported by production code. Design: docs/market_thesis_architecture_v1.md section 5 + section 9.

THE LIVE SITUATION this module models (verified in code, nothing here changes it): ``demo/execution/live.py`` rejects an opposite-side
signal while a position is open (``gates.R_OPPOSITE`` = ``OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1``). Persisted funnel rows
(``otherwise_valid`` + reject code) therefore reconstruct a rejected OPPOSING_EVENT; OPPOSING_SETUP / THESIS_AT_RISK are never persisted
and are derived OFFLINE by this module.

FROZEN CLASSIFICATION RULES (``observe``; evidence is evaluated at ONE decision time T using only data <= T)
  ``opp`` = ``direction.opposite()``. ``status`` = ``premise_status`` (Condition.name -> bool | None; a MISSING key means "not supplied" and is
  never evidence; an explicit ``None`` means "evaluated, no longer observable").
  * OPPOSING_EVENT   an ``OpposingEvent`` with direction == opp, ``entry_ts <= ts_ns == T`` that is not a setup-sourced record. A lone
                     opposing trigger is information only: it never invalidates and never exits.
  * OPPOSING_SETUP   an INDEPENDENT, COMPLETE opposite ``SetupThesis`` (market equal, direction == opp, state ARMED or TRIGGERED,
                     ``updated_at_ns <= T``, every required condition ``observed is True``). Recorded once as an ``OpposingEvent`` with
                     source ``SETUP:<archetype>:<thesis_id>``.
  * THESIS_AT_RISK   any ONE of: a required premise condition is False; a required premise condition that was ``observed is True`` at entry is
                     now explicitly None (evidence lost); the M15 structure turns against the position (LONG: label starts with DOWN, SHORT: UP);
                     acceptance against the position BEGINS (``acceptance_state`` keyed ``"<opp>:<level_id>"`` in ACCEPTING_VALUES).
  * THESIS_INVALIDATED  (a required premise condition is False) AND (acceptance against the position is ESTABLISHED, i.e. a key
                     ``"<opp>:<level_id>"`` in ACCEPTED_VALUES). "Premise broke and price was accepted beyond it". Absorbing except CLOSED.
  * Precedence (max severity wins): INVALIDATED > AT_RISK > OPPOSING_SETUP > OPPOSING_EVENT > HEALTHY. When the target is HIGHER than the
    current state every active evidence class above the current severity is stepped through in ascending order (all stamped T) so no class
    is hidden by precedence (e.g. HEALTHY -> OPPOSING_SETUP -> THESIS_AT_RISK). When it is lower the state fades directly.
  * ``observe`` must be called once per decision bar (events are active only at ``ts_ns == T``).

Exit != reverse: ``should_remain_open`` (decision A, about the existing position) and ``valid_new_setup_in_opposite_direction`` (decision B,
a fresh opposite setup) are independent pure functions; no function in this module returns a combined "close and reverse" action.

HYPOTHETICAL VARIANTS (same immutable ``EntryCohort``): CONTROL = the entry's own stop/target outcome; A exit at the first recorded opposing
event (any); B at the first setup-sourced opposing event; C at the first THESIS_AT_RISK; D only at THESIS_INVALIDATED. Bar semantics (the repo's
``demo.labeling`` convention): decision at the bar CLOSE, fill at the NEXT bar's open (short exits at ask = open + spread). A stop/target that
resolves on or before the decision bar wins (the variant never applied). Path math is ``demo.labeling.simulate_hypothetical``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, NamedTuple

from demo.labeling import Bar, simulate_hypothetical
from demo.store import parse_utc
from research_workbench.thesis.contracts import (
    POSITION_SEVERITY,
    Condition,
    Direction,
    MarketMap,
    OpposingEvent,
    PositionThesis,
    PositionThesisState,
    PositionThesisTransition,
    SetupState,
    SetupThesis,
    legal_position_transition,
)

POSITION_THESIS_VERSION = "position-thesis-1"
SETUP_EVENT_PREFIX = "SETUP:"
ACCEPTED_VALUES = frozenset({"ACCEPTED", "RETEST_HELD"})  # MarketMap vocabulary (marketmap-1)
ACCEPTING_VALUES = frozenset({"BROKEN"})  # closes beyond the level, not yet accepted
ADVERSE_MOVE_R = 0.5  # frozen: time_to_adverse = first bar with an adverse excursion >= this many R from the event reference
RECOVERY_R = 0.5  # frozen: time_to_recovery = first bar with a favourable excursion >= this many R from the event reference
MIN_SAMPLE = 30  # frozen: below this many finite complete rows NO probability claim is made (INSUFFICIENT_SAMPLE)
M5_NS = 300_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ARMED = (SetupState.ARMED, SetupState.TRIGGERED)
_AT_RISK = PositionThesisState.THESIS_AT_RISK
_INVALID = PositionThesisState.THESIS_INVALIDATED
_CLOSED = PositionThesisState.CLOSED


@dataclass(frozen=True)
class ObservedPositionThesis(PositionThesis):
    """A PositionThesis that also remembers the last decision time it was observed at (contracts.py stays untouched)."""

    last_observed_ns: int | None = None


def _as_observed(pt: PositionThesis) -> ObservedPositionThesis:
    if isinstance(pt, ObservedPositionThesis):
        return pt
    return ObservedPositionThesis(**{f.name: getattr(pt, f.name) for f in fields(PositionThesis)})


# ------------------------------------------------------------------------------------------------ opening
def open_position_thesis(
    position_id: str,
    market: str,
    direction: Direction,
    entry_ts_ns: int,
    setup_thesis: SetupThesis | None,
    family_trigger: str | None,
    premise: Sequence[Condition],
) -> ObservedPositionThesis:
    """The entry thesis of a filled trade; keeps the setup identity after the fill. Starts HEALTHY with an empty history."""
    if setup_thesis is None and family_trigger is None:
        raise ValueError("a position thesis needs a setup_thesis or a family_trigger")
    if setup_thesis is not None and (
        setup_thesis.direction is not direction or setup_thesis.market != market
    ):
        raise ValueError("setup_thesis market/direction must equal the position's")
    return ObservedPositionThesis(
        position_id=position_id,
        market=market,
        direction=direction,
        entry_ts_ns=entry_ts_ns,
        setup_thesis=setup_thesis,
        family_trigger=family_trigger,
        premise=tuple(premise),
    )


# ------------------------------------------------------------------------------------------------ independent decisions
class RemainOpenDecision(NamedTuple):
    remain_open: bool
    reason: str


def should_remain_open(pt: PositionThesis) -> RemainOpenDecision:
    """Decision A, about the EXISTING position only: stay open unless the causal premise is objectively invalidated.

    Says nothing about any new position; an opposing event / setup / at-risk state alone never closes anything."""
    if pt.state is _CLOSED:
        return RemainOpenDecision(False, "already CLOSED")
    if pt.state is _INVALID:
        return RemainOpenDecision(
            False, "THESIS_INVALIDATED: causal premise failed and was accepted against"
        )
    return RemainOpenDecision(True, f"state {pt.state.value}: premise not objectively invalidated")


def is_complete_opposite_setup(
    setup: SetupThesis, position_direction: Direction, market: str, ts_ns: int
) -> bool:
    """A new opposite setup counts only if it satisfies ALL its own entry requirements: ARMED/TRIGGERED with every required
    condition observed True (and updated at/before ``ts_ns``)."""
    return (
        setup.market == market
        and setup.direction is position_direction.opposite()
        and setup.state in _ARMED
        and setup.updated_at_ns <= ts_ns
        and all(c.observed is True for c in setup.conditions if c.required)
    )


def valid_new_setup_in_opposite_direction(
    setups: Sequence[SetupThesis], position_direction: Direction, market: str, decision_ts_ns: int
) -> SetupThesis | None:
    """Decision B, independent of any PositionThesis: the earliest valid (complete, ARMED/TRIGGERED) setup opposite to the position's
    direction at ``decision_ts_ns`` or None. Takes no position state on purpose."""
    valid = [
        s
        for s in setups
        if is_complete_opposite_setup(s, position_direction, market, decision_ts_ns)
    ]
    if not valid:
        return None
    return min(valid, key=lambda s: (s.updated_at_ns, s.thesis_id))


# ------------------------------------------------------------------------------------------------ observe
def _structure_against(label: str | None, direction: Direction) -> bool:
    if label is None:
        return False
    head = label.upper()
    return head.startswith("DOWN") if direction is Direction.LONG else head.startswith("UP")


def _acceptance_against(market_map: MarketMap, direction: Direction) -> tuple[bool, bool]:
    """(established, beginning) acceptance against the position. Keys are ``"<DIR>:<level_id>"``; against = the OPPOSITE direction."""
    prefix = f"{direction.opposite().value}:"
    established = beginning = False
    for key in sorted(market_map.acceptance_state):
        value = market_map.acceptance_state[key]
        if not key.startswith(prefix) or value is None:
            continue
        up = value.upper()
        established = established or up in ACCEPTED_VALUES
        beginning = beginning or up in ACCEPTING_VALUES
    return established, beginning


def _event_key(e: OpposingEvent) -> tuple[int, str, str]:
    return (e.ts_ns, e.direction.value, e.source)


def observe(
    position_thesis: PositionThesis,
    market_map: MarketMap,
    opposing_events: Sequence[OpposingEvent] = (),
    opposing_setups: Sequence[SetupThesis] = (),
    premise_status: Mapping[str, bool | None] | None = None,
    closed: bool = False,
) -> PositionThesis:
    """PURE causal step: the position thesis as of ``market_map.decision_ts_ns``. Data stamped after that time is ignored."""
    pt = _as_observed(position_thesis)
    if pt.state is _CLOSED:
        return pt
    if market_map.market != pt.market:
        raise ValueError(f"market mismatch: {market_map.market} != {pt.market}")
    t = market_map.decision_ts_ns
    if t < pt.entry_ts_ns:
        raise ValueError("decision time must be >= entry time")
    if pt.last_observed_ns is not None and t <= pt.last_observed_ns:
        raise ValueError(
            "observe is exactly once per decision bar: decision time must strictly increase"
        )
    status = premise_status or {}
    opp = pt.direction.opposite()

    # 1. record opposing events visible AT this decision time (ts_ns == T). An event supplied later than its own decision bar was not
    # visible then and is ignored (no retroactive exits); setups are recorded once as SETUP:* events
    seen = {_event_key(e) for e in pt.opposing_events}
    new_events: list[OpposingEvent] = []
    for e in sorted(opposing_events, key=_event_key):
        if e.direction is opp and e.ts_ns == t and _event_key(e) not in seen:
            seen.add(_event_key(e))
            new_events.append(e)
    event_now = any(
        e.direction is opp and e.ts_ns == t and not e.source.startswith(SETUP_EVENT_PREFIX)
        for e in (*pt.opposing_events, *new_events)
    )
    complete_setups = sorted(
        (s for s in opposing_setups if is_complete_opposite_setup(s, pt.direction, pt.market, t)),
        key=lambda s: (s.updated_at_ns, s.thesis_id),
    )
    recorded_sources = {e.source for e in (*pt.opposing_events, *new_events)}
    for s in complete_setups:
        src = f"{SETUP_EVENT_PREFIX}{s.archetype}:{s.thesis_id}"
        if src not in recorded_sources:
            new_events.append(OpposingEvent(t, opp, src, None, False))
            recorded_sources.add(src)

    # 2. premise / structure / acceptance evidence
    req = [c for c in pt.premise if c.required]
    failed = [c.name for c in req if status.get(c.name, True) is False]
    lost = [
        c.name for c in req if c.name in status and status[c.name] is None and c.observed is True
    ]
    accepted, accepting = _acceptance_against(market_map, pt.direction)
    struct_against = _structure_against(market_map.m15_structure, pt.direction)
    active: dict[PositionThesisState, str] = {}
    if event_now:
        active[PositionThesisState.OPPOSING_EVENT] = f"opposing {opp.value} event at T"
    if complete_setups:
        active[PositionThesisState.OPPOSING_SETUP] = (
            f"complete opposite setup {complete_setups[0].thesis_id}"
        )
    risk = []
    if failed:
        risk.append("premise False: " + ",".join(failed))
    if lost:
        risk.append("premise evidence lost: " + ",".join(lost))
    if struct_against:
        risk.append(f"m15 structure against: {market_map.m15_structure}")
    if accepting:
        risk.append("acceptance against beginning")
    if failed and accepted:
        active[_INVALID] = "premise failed and acceptance against established: " + ",".join(failed)
        risk.append("premise failed + acceptance established")
    if risk:
        active[_AT_RISK] = "; ".join(risk)

    # 3. transitions (every one validated)
    cur = pt.state
    transitions: list[PositionThesisTransition] = []
    if cur is not _INVALID and active:
        target = max(active, key=POSITION_SEVERITY.__getitem__)
        if POSITION_SEVERITY[target] > POSITION_SEVERITY[cur]:
            steps = sorted(
                (s for s in active if POSITION_SEVERITY[s] > POSITION_SEVERITY[cur]),
                key=POSITION_SEVERITY.__getitem__,
            )
        elif POSITION_SEVERITY[target] < POSITION_SEVERITY[cur]:
            steps = [target]
        else:
            steps = []
        for dst in steps:
            transitions.append(_step(cur, dst, t, active[dst]))
            cur = dst
    elif cur is not _INVALID and cur is not PositionThesisState.HEALTHY:
        transitions.append(
            _step(cur, PositionThesisState.HEALTHY, t, "no active opposing evidence")
        )
        cur = PositionThesisState.HEALTHY
    worst = pt.worst_state
    for tr in transitions:
        if POSITION_SEVERITY[tr.dst] > POSITION_SEVERITY[worst]:
            worst = tr.dst
    if closed:
        transitions.append(_step(cur, _CLOSED, t, "position closed"))
        cur = _CLOSED
    return replace(
        pt,
        state=cur,
        worst_state=worst,
        opposing_events=pt.opposing_events + tuple(new_events),
        history=pt.history + tuple(transitions),
        last_observed_ns=t,
    )


def _step(
    src: PositionThesisState, dst: PositionThesisState, ts_ns: int, reason: str
) -> PositionThesisTransition:
    if not legal_position_transition(src, dst):
        raise ValueError(f"illegal position-thesis transition {src.value} -> {dst.value}")
    return PositionThesisTransition(ts_ns, src, dst, reason)


def mark_future_path_complete(pt: PositionThesis, complete: bool = True) -> PositionThesis:
    """Flag every recorded opposing event's future path as complete/incomplete (setup-sourced records start PENDING)."""
    return replace(
        pt,
        opposing_events=tuple(
            replace(e, future_path_complete=complete) for e in pt.opposing_events
        ),
    )


# ------------------------------------------------------------------------------------------------ cohort + hypothetical exits
class Variant(StrEnum):
    CONTROL = "CONTROL"
    A = "A"  # first ANY opposing event
    B = "B"  # first independent OPPOSING_SETUP
    C = "C"  # first THESIS_AT_RISK
    D = "D"  # only THESIS_INVALIDATED


@dataclass(frozen=True)
class EntryPath:
    entry_id: str
    market: str
    direction: Direction
    entry_ts_ns: int
    entry: float
    stop: float
    target: float | None = None

    def __post_init__(self) -> None:
        risk = abs(self.entry - self.stop)
        if not (risk > 0 and math.isfinite(risk)):
            raise ValueError("initial risk distance must be > 0")
        if (self.direction is Direction.LONG) != (self.stop < self.entry):
            raise ValueError("stop must be on the adverse side of entry")

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)


@dataclass(frozen=True)
class EntryCohort:
    """The immutable entry set; built ONCE, every variant is evaluated on exactly these entries."""

    entries: tuple[EntryPath, ...]

    @property
    def entry_ids(self) -> tuple[str, ...]:
        return tuple(e.entry_id for e in self.entries)


def build_cohort(entries: Sequence[EntryPath]) -> EntryCohort:
    ids = [e.entry_id for e in entries]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate entry ids in cohort")
    return EntryCohort(tuple(entries))


@dataclass(frozen=True)
class HypotheticalExit:
    entry_id: str
    trigger_ts_ns: int | None
    kind: str  # VARIANT_EXIT | STOP | STOP_GAP | TARGET | HORIZON | PENDING
    triggered: bool  # True iff the variant trigger really ended the trade
    complete: bool  # False => PENDING (the exit fill bar is not available yet)
    r: float  # NaN when PENDING
    exit_price: float | None
    mfe_r: float
    mae_r: float
    bars_used: int


def _open_ns(bar: Bar) -> int:
    return (parse_utc(bar.ts_utc) - _EPOCH) // timedelta(microseconds=1) * 1000


def _decision_index(bars: Sequence[Bar], trigger_ts_ns: int, bar_ns: int) -> int:
    """Index of the last bar whose CLOSE (open + bar_ns) is <= the decision time, -1 if none."""
    k = -1
    for i, b in enumerate(bars):
        if _open_ns(b) + bar_ns <= trigger_ts_ns:
            k = i
        else:
            break
    return k


def _r(entry: EntryPath, price: float) -> float:
    return entry.direction.sign * (price - entry.entry) / entry.risk


def hypothetical_exit(
    entry: EntryPath, trigger_ts_ns: int | None, bars: Sequence[Bar], *, bar_ns: int = M5_NS
) -> HypotheticalExit:
    """Evaluate one management variant on one entry. ``bars`` = causal bid bars from the entry on (time ordered).

    ``trigger_ts_ns None`` = CONTROL (no early exit). Otherwise exit at the OPEN of the first bar after the decision bar."""
    d = entry.direction.sign
    if trigger_ts_ns is None:
        res = simulate_hypothetical(
            direction=d, entry=entry.entry, stop=entry.stop, target=entry.target, bars=bars
        )
        return HypotheticalExit(
            entry.entry_id,
            None,
            res.exit_kind,
            False,
            True,
            res.r,
            None,
            res.mfe_r,
            res.mae_r,
            res.bars_used,
        )
    k = _decision_index(bars, trigger_ts_ns, bar_ns)
    pre = (
        simulate_hypothetical(
            direction=d, entry=entry.entry, stop=entry.stop, target=entry.target, bars=bars[: k + 1]
        )
        if k >= 0
        else None
    )
    if (
        pre is not None and pre.exit_kind != "HORIZON"
    ):  # resolved on/before the decision bar: the variant never applied
        return HypotheticalExit(
            entry.entry_id,
            trigger_ts_ns,
            pre.exit_kind,
            False,
            True,
            pre.r,
            None,
            pre.mfe_r,
            pre.mae_r,
            pre.bars_used,
        )
    mfe, mae, used = (pre.mfe_r, pre.mae_r, pre.bars_used) if pre is not None else (0.0, 0.0, 0)
    if k + 1 >= len(bars):
        return HypotheticalExit(
            entry.entry_id, trigger_ts_ns, "PENDING", False, False, math.nan, None, mfe, mae, used
        )
    nxt = bars[k + 1]
    px = nxt.open if d > 0 else nxt.open + nxt.spread
    return HypotheticalExit(
        entry.entry_id,
        trigger_ts_ns,
        "VARIANT_EXIT",
        True,
        True,
        _r(entry, px),
        px,
        mfe,
        mae,
        used + 1,
    )


def variant_trigger_ts(pt: PositionThesis, variant: Variant) -> int | None:
    """Decision time at which the variant would have exited, from the monitor's own record (None = never)."""
    if variant is Variant.CONTROL:
        return None
    if variant is Variant.A:
        ts = [e.ts_ns for e in pt.opposing_events]
    elif variant is Variant.B:
        ts = [e.ts_ns for e in pt.opposing_events if e.source.startswith(SETUP_EVENT_PREFIX)]
    else:
        dst = _AT_RISK if variant is Variant.C else _INVALID
        ts = [h.ts_ns for h in pt.history if h.dst is dst]
    return min(ts) if ts else None


def run_variants(
    cohort: EntryCohort,
    theses: Mapping[str, PositionThesis],
    bars_by_entry: Mapping[str, Sequence[Bar]],
    *,
    bar_ns: int = M5_NS,
) -> dict[Variant, tuple[HypotheticalExit, ...]]:
    """CONTROL and A-D on the SAME cohort. Every variant result lists exactly the cohort's entry ids (asserted)."""
    out: dict[Variant, tuple[HypotheticalExit, ...]] = {}
    for v in Variant:
        rows = tuple(
            hypothetical_exit(
                e,
                variant_trigger_ts(theses[e.entry_id], v) if e.entry_id in theses else None,
                bars_by_entry[e.entry_id],
                bar_ns=bar_ns,
            )
            for e in cohort.entries
        )
        if tuple(r.entry_id for r in rows) != cohort.entry_ids:
            raise AssertionError("variant evaluated on a different entry set")
        out[v] = rows
    return out


# ------------------------------------------------------------------------------------------------ opposing-event metrics
@dataclass(frozen=True)
class EventOutcome:
    entry_id: str
    event_ts_ns: int
    status: str  # COMPLETE | PENDING | NOT_OPEN (position already resolved at the event)
    finite: bool
    r_at_event: float
    future_mfe_r: float
    future_mae_r: float
    target_hit_after: bool
    stop_hit_after: bool
    realized_r: float
    giveback_r: float
    capture: float | None
    time_to_adverse_bars: int | None
    time_to_recovery_bars: int | None
    recovered: bool


def _pending(entry: EntryPath, ts: int, status: str) -> EventOutcome:
    n = math.nan
    return EventOutcome(
        entry.entry_id, ts, status, False, n, n, n, False, False, n, n, None, None, None, False
    )


def _finite_bars(bars: Sequence[Bar]) -> bool:
    return all(math.isfinite(x) for b in bars for x in (b.open, b.high, b.low, b.close, b.spread))


def event_outcome(
    entry: EntryPath,
    event_ts_ns: int,
    bars: Sequence[Bar],
    *,
    future_path_complete: bool = True,
    bar_ns: int = M5_NS,
) -> EventOutcome:
    """Everything that happened AFTER an opposing event, computed only from bars after the event's decision bar (R vs the entry risk).

    PENDING (not missing, not negative) when the future path is flagged incomplete or no bar follows the decision bar."""
    d = entry.direction.sign
    k = _decision_index(bars, event_ts_ns, bar_ns)
    pre = (
        simulate_hypothetical(
            direction=d, entry=entry.entry, stop=entry.stop, target=entry.target, bars=bars[: k + 1]
        )
        if k >= 0
        else None
    )
    if pre is not None and pre.exit_kind != "HORIZON":
        return _pending(entry, event_ts_ns, "NOT_OPEN")
    if not future_path_complete or k + 1 >= len(bars):
        return _pending(entry, event_ts_ns, "PENDING")
    future = bars[k + 1 :]
    fut = simulate_hypothetical(
        direction=d, entry=entry.entry, stop=entry.stop, target=entry.target, bars=future
    )
    if pre is not None:
        c = bars[k]
        r_event = _r(entry, c.close if d > 0 else c.close + c.spread)
        ref = c.close if d > 0 else c.close + c.spread
    else:
        r_event, ref = 0.0, entry.entry
    best_mfe = max(pre.mfe_r if pre is not None else 0.0, fut.mfe_r)
    stopped = fut.exit_kind in ("STOP", "STOP_GAP")
    t_adv = t_rec = None
    for i, b in enumerate(future[: fut.bars_used], start=1):
        sp = 0.0 if d > 0 else b.spread
        adv = (ref - b.low) if d > 0 else (b.high + sp - ref)
        fav = (b.high - ref) if d > 0 else (ref - (b.low + sp))
        if t_adv is None and adv / entry.risk >= ADVERSE_MOVE_R:
            t_adv = i
        stop_bar = stopped and i == fut.bars_used
        if t_rec is None and not stop_bar and fav / entry.risk >= RECOVERY_R:
            t_rec = i
    finite = _finite_bars(bars) and all(
        math.isfinite(x) for x in (fut.r, fut.mfe_r, fut.mae_r, r_event)
    )
    return EventOutcome(
        entry_id=entry.entry_id,
        event_ts_ns=event_ts_ns,
        status="COMPLETE",
        finite=finite,
        r_at_event=r_event,
        future_mfe_r=fut.mfe_r,
        future_mae_r=fut.mae_r,
        target_hit_after=fut.target_before_stop is True,
        stop_hit_after=stopped,
        realized_r=fut.r,
        giveback_r=r_event - fut.r,
        capture=(fut.r / best_mfe) if best_mfe > 0 and math.isfinite(fut.r) else None,
        time_to_adverse_bars=t_adv,
        time_to_recovery_bars=t_rec,
        recovered=t_rec is not None,
    )


def false_early_exit(variant_exit: HypotheticalExit, control: HypotheticalExit) -> bool:
    """The variant exited early, then price continued to the original target (the control hit TARGET)."""
    return variant_exit.triggered and control.kind == "TARGET"


def whipsaw(variant_exit: HypotheticalExit, control: HypotheticalExit) -> bool:
    """The variant exited early and the control outcome was strictly better (the opposing information was noise)."""
    return (
        variant_exit.triggered
        and math.isfinite(variant_exit.r)
        and math.isfinite(control.r)
        and control.r > variant_exit.r
    )


def conditional_stat(
    outcomes: Sequence[EventOutcome], hit: str, *, min_n: int = MIN_SAMPLE
) -> dict[str, Any]:
    """P(hit | event) over COMPLETE + finite outcomes only. ``hit`` in target_hit_after | stop_hit_after | recovered.

    Fields: n_total / n_finite / n_excluded (non-finite) / n_pending / n_not_open / k / p. Below ``min_n`` finite rows: status
    INSUFFICIENT_SAMPLE and p is None (no claim). PENDING outcomes are neither successes nor failures."""
    if hit not in ("target_hit_after", "stop_hit_after", "recovered"):
        raise ValueError(f"unknown outcome flag {hit}")
    complete = [o for o in outcomes if o.status == "COMPLETE"]
    finite = [o for o in complete if o.finite]
    k = sum(1 for o in finite if getattr(o, hit))
    n = len(finite)
    if n == 0:
        status = "NO_DATA"
    elif n < min_n:
        status = "INSUFFICIENT_SAMPLE"
    else:
        status = "OK"
    return {
        "n_total": len(outcomes),
        "n_finite": n,
        "n_excluded": len(complete) - n,
        "n_pending": sum(1 for o in outcomes if o.status == "PENDING"),
        "n_not_open": sum(1 for o in outcomes if o.status == "NOT_OPEN"),
        "k": k,
        "min_n": min_n,
        "p": (k / n) if status == "OK" else None,
        "status": status,
    }


def _first_event_ts(pt: PositionThesis, setup: bool) -> OpposingEvent | None:
    evs = [e for e in pt.opposing_events if e.source.startswith(SETUP_EVENT_PREFIX) is setup]
    return min(evs, key=lambda e: e.ts_ns) if evs else None


def conditional_probabilities(
    cohort: EntryCohort,
    theses: Mapping[str, PositionThesis],
    bars_by_entry: Mapping[str, Sequence[Bar]],
    *,
    paths_complete: Mapping[str, bool] | None = None,
    bar_ns: int = M5_NS,
    min_n: int = MIN_SAMPLE,
) -> dict[str, dict[str, Any]]:
    """The six conditional probabilities of the research question, each with N and INSUFFICIENT_SAMPLE handling."""
    complete = paths_complete or {}
    groups: dict[str, list[EventOutcome]] = {
        k: [] for k in ("event", "setup", "invalidated", "at_risk")
    }
    for e in cohort.entries:
        pt = theses.get(e.entry_id)
        if pt is None:
            continue
        bars = bars_by_entry[e.entry_id]
        for name, setup in (("event", False), ("setup", True)):
            ev = _first_event_ts(pt, setup)
            if ev is not None:
                groups[name].append(
                    event_outcome(
                        e,
                        ev.ts_ns,
                        bars,
                        future_path_complete=ev.future_path_complete,
                        bar_ns=bar_ns,
                    )
                )
        for name, dst in (("invalidated", _INVALID), ("at_risk", _AT_RISK)):
            ts = [h.ts_ns for h in pt.history if h.dst is dst]
            if ts:
                groups[name].append(
                    event_outcome(
                        e,
                        min(ts),
                        bars,
                        future_path_complete=complete.get(e.entry_id, True),
                        bar_ns=bar_ns,
                    )
                )
    return {
        "P(target|opposing_event)": conditional_stat(
            groups["event"], "target_hit_after", min_n=min_n
        ),
        "P(stop|opposing_event)": conditional_stat(groups["event"], "stop_hit_after", min_n=min_n),
        "P(target|opposing_setup)": conditional_stat(
            groups["setup"], "target_hit_after", min_n=min_n
        ),
        "P(stop|opposing_setup)": conditional_stat(groups["setup"], "stop_hit_after", min_n=min_n),
        "P(stop|thesis_invalidated)": conditional_stat(
            groups["invalidated"], "stop_hit_after", min_n=min_n
        ),
        "P(recovery|thesis_at_risk)": conditional_stat(groups["at_risk"], "recovered", min_n=min_n),
    }


# ------------------------------------------------------------------------------------------------ report
def summary(
    position_theses: Sequence[PositionThesis],
    variants: Mapping[Variant, Sequence[HypotheticalExit]] | None = None,
) -> dict[str, Any]:
    """Compact coverage dict for the Coverage Auditor / Workbench report.

    position_thesis_assessment_complete = positions that reached CLOSED (the full path was observed); the rest are pending.
    unexpected_missing = (eligible position, variant) pairs with no variant result at all (pending ones are NOT missing)."""
    events = [e for pt in position_theses for e in pt.opposing_events]
    done = sum(1 for pt in position_theses if pt.state is _CLOSED)
    out: dict[str, Any] = {
        "open_positions_eligible": len(position_theses),
        "opposing_events_detected": len(events),
        "opposing_events_future_path_complete": sum(1 for e in events if e.future_path_complete),
        "position_thesis_assessment_complete": done,
        "hypothetical_exit_complete": 0,
        "pending": {
            "opposing_events": sum(1 for e in events if not e.future_path_complete),
            "position_thesis_assessment": len(position_theses) - done,
            "hypothetical_exit": 0,
        },
        "unexpected_missing": 0,
    }
    if variants is not None:
        ids = {pt.position_id for pt in position_theses}
        missing = 0
        for v in Variant:
            have = {r.entry_id for r in variants.get(v, ())}
            missing += len(ids - have)
        rows = [r for v in Variant for r in variants.get(v, ())]
        out["hypothetical_exit_complete"] = sum(1 for r in rows if r.complete)
        out["pending"]["hypothetical_exit"] = sum(1 for r in rows if not r.complete)
        out["unexpected_missing"] = missing
    return out
