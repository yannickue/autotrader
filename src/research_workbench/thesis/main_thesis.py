# ruff: noqa: E501
"""Main (daily) market thesis: a frozen, explainable rule table over ONE ``MarketMap`` (RESEARCH / OFFLINE ONLY, no trade authority).

``derive_main_thesis(market_map, history=())`` is a pure function of the map at T and of EARLIER maps (decision_ts < T). It never forces
a bias: ``NO_CLEAR_THESIS`` is a valid and common result. There is no score; the result carries a tuple of ``Condition`` (evidence class,
required, observed True/False/None, frozen ``first_observed_ns``) that explains exactly which facts led to the state.

``MAIN_THESIS_VERSION`` documents the rule table; any change of a rule bumps it.

RULES (first applicable; d = LONG for ``UP_SEQUENCE`` on M15, SHORT for ``DOWN_SEQUENCE``; everything is sign-symmetric)

=============================  =====================================================================================================
MarketPhase                    MainThesisState (direction_hint)
=============================  =====================================================================================================
TREND, PULLBACK                M15 sequence gives d. H1 context == d  -> BULLISH/BEARISH_CONTINUATION (d).
                               H1 NEUTRAL -> NO_CLEAR_THESIS (an H1-neutral M15 trend is not enough for a daily bias);
                               H1 opposite -> TRANSITION (timeframes disagree); H1 None -> NO_CLEAR_THESIS (not evaluable).
BREAKOUT                       BREAKOUT_EXPANSION (d) with d = the single side whose acceptance_state is BROKEN/ACCEPTED/RETEST_HELD;
                               no single side -> NO_CLEAR_THESIS.
EXPANSION                      BREAKOUT_EXPANSION (no direction: the MarketMap carries no expansion direction).
FAILED_BREAK                   FAILED_BREAK_REVERSAL (no direction: the contract has no failed-break direction field - reported gap).
BALANCE, RANGE, COMPRESSION    RANGE_ROTATION (no direction hint: a range has no daily bias).
REVERSAL_ATTEMPT, TRANSITION   TRANSITION (no direction hint).
UNDEFINED                      NO_CLEAR_THESIS.
=============================  =====================================================================================================

IMPLEMENTED states: all seven ``MainThesisState`` members are produced (FAILED_BREAK_REVERSAL and the EXPANSION flavour of
BREAKOUT_EXPANSION without a direction hint).

``alignment(setup_direction, thesis)``: NO_CLEAR_THESIS / TRANSITION / RANGE_ROTATION / any thesis without a direction hint -> NEUTRAL;
otherwise ALIGNED iff the hint equals the setup direction, else OPPOSED.

HISTORY: only used to FREEZE ``first_observed_ns`` (the decision time of the first map of the unbroken run, ending at T, in which the
condition was observed True). Entries must be maps of the same market with strictly earlier, ascending decision times (a map from the
future raises ``ValueError``: no look-ahead). Without history ``first_observed_ns`` is T for an observed condition.
"""

from __future__ import annotations

from collections.abc import Sequence

from research_workbench.thesis.contracts import (
    Alignment,
    Condition,
    Direction,
    EvidenceClass,
    MainThesisState,
    MarketMap,
    MarketPhase,
    MarketThesis,
)

MAIN_THESIS_VERSION = "main-thesis-1"

EC = EvidenceClass
_ACC_BROKEN = ("BROKEN", "ACCEPTED", "RETEST_HELD")
_SEQ_DIR = {"UP_SEQUENCE": Direction.LONG, "DOWN_SEQUENCE": Direction.SHORT}
_H1_OF = {Direction.LONG: "UP", Direction.SHORT: "DOWN"}

_Cond = tuple[
    EvidenceClass, str, bool, bool | None, str
]  # (class, name, required, observed, detail)
_Eval = tuple[MainThesisState, Direction | None, list[_Cond]]


def _acc_sides(mm: MarketMap) -> set[Direction]:
    out: set[Direction] = set()
    for key, val in mm.acceptance_state.items():
        side = key.split(":", 1)[0]
        if val in _ACC_BROKEN and side in ("LONG", "SHORT"):
            out.add(Direction(side))
    return out


def _evaluate(mm: MarketMap) -> _Eval:
    """The frozen rule table: (state, direction_hint, conditions) of ONE map (no history, no timestamps)."""
    ph = mm.market_phase
    defined = ph is not MarketPhase.UNDEFINED
    conds: list[_Cond] = [(EC.CONTEXT, "phase_defined", True, defined, f"market_phase={ph.value}")]
    if not defined:
        return MainThesisState.NO_CLEAR_THESIS, None, conds

    if ph in (MarketPhase.TREND, MarketPhase.PULLBACK):
        d = _SEQ_DIR.get(mm.m15_structure or "")
        conds.append(
            (
                EC.STRUCTURE,
                "m15_trend_sequence",
                True,
                d is not None,
                f"m15_structure={mm.m15_structure}",
            )
        )
        conds.append(
            (
                EC.CONTEXT,
                "h1_context_available",
                True,
                mm.h1_context is not None,
                f"h1_context={mm.h1_context}",
            )
        )
        if d is None:
            return MainThesisState.NO_CLEAR_THESIS, None, conds
        h1 = mm.h1_context
        conds.append(
            (
                EC.CONTEXT,
                "h1_supports_m15_trend",
                True,
                None if h1 is None else h1 == _H1_OF[d],
                f"h1_context={h1} vs m15 trend {d.value}",
            )
        )
        if h1 is None or h1 == "NEUTRAL":
            return MainThesisState.NO_CLEAR_THESIS, None, conds
        if h1 != _H1_OF[d]:
            return MainThesisState.TRANSITION, None, conds
        state = (
            MainThesisState.BULLISH_CONTINUATION
            if d is Direction.LONG
            else MainThesisState.BEARISH_CONTINUATION
        )
        return state, d, conds

    if ph is MarketPhase.BREAKOUT:
        sides = _acc_sides(mm)
        d = next(iter(sides)) if len(sides) == 1 else None
        conds.append(
            (
                EC.LEVEL_BEHAVIOUR,
                "single_side_break_acceptance",
                True,
                d is not None,
                f"sides={sorted(s.value for s in sides)}",
            )
        )
        if d is None:
            return MainThesisState.NO_CLEAR_THESIS, None, conds
        return MainThesisState.BREAKOUT_EXPANSION, d, conds

    if ph is MarketPhase.EXPANSION:
        conds.append(
            (
                EC.CONTEXT,
                "volatility_high",
                True,
                mm.volatility_context == "HIGH",
                f"volatility_context={mm.volatility_context}",
            )
        )
        conds.append(
            (
                EC.STRUCTURE,
                "balance_directional",
                True,
                mm.balance_state == "DIRECTIONAL",
                f"balance_state={mm.balance_state}",
            )
        )
        return MainThesisState.BREAKOUT_EXPANSION, None, conds

    if ph is MarketPhase.FAILED_BREAK:
        conds.append(
            (
                EC.LEVEL_BEHAVIOUR,
                "failed_break_observed",
                True,
                True,
                "m5 swing break reversed inside the event window",
            )
        )
        return MainThesisState.FAILED_BREAK_REVERSAL, None, conds

    if ph in (MarketPhase.BALANCE, MarketPhase.RANGE, MarketPhase.COMPRESSION):
        conds.append(
            (
                EC.STRUCTURE,
                "balanced_or_ranging",
                True,
                mm.balance_state in ("BALANCE", "MIXED"),
                f"balance_state={mm.balance_state}",
            )
        )
        both = mm.nearest_support is not None and mm.nearest_resistance is not None
        conds.append(
            (
                EC.LOCATION,
                "range_boundaries_known",
                False,
                both,
                f"support={mm.nearest_support} resistance={mm.nearest_resistance}",
            )
        )
        return MainThesisState.RANGE_ROTATION, None, conds

    # REVERSAL_ATTEMPT, TRANSITION
    conds.append(
        (
            EC.STRUCTURE,
            "structure_in_transition",
            True,
            True,
            f"m15={mm.m15_structure} m5={mm.m5_structure}",
        )
    )
    return MainThesisState.TRANSITION, None, conds


def _check_history(mm: MarketMap, history: Sequence[MarketMap]) -> None:
    prev_ts: int | None = None
    for h in history:
        if h.market != mm.market:
            raise ValueError(f"history map of market {h.market!r} for a thesis of {mm.market!r}")
        if h.decision_ts_ns >= mm.decision_ts_ns:
            raise ValueError(
                "history contains a map at/after the thesis decision time (look-ahead)"
            )
        if prev_ts is not None and h.decision_ts_ns <= prev_ts:
            raise ValueError("history must be strictly ascending in decision time")
        prev_ts = h.decision_ts_ns


def derive_main_thesis(market_map: MarketMap, history: Sequence[MarketMap] = ()) -> MarketThesis:
    """Pure + deterministic main thesis of ``market_map`` (see the module docstring for the frozen rule table)."""
    _check_history(market_map, history)
    state, hint, conds = _evaluate(market_map)
    past = [_evaluate(h)[2] for h in history]
    past_ts = [h.decision_ts_ns for h in history]
    basis: list[Condition] = []
    for ec, name, required, observed, detail in conds:
        first: int | None = None
        if observed is True:
            first = market_map.decision_ts_ns
            for pc, pts in zip(reversed(past), reversed(past_ts), strict=True):
                hit = next((c for c in pc if c[1] == name), None)
                if hit is None or hit[3] is not True:
                    break
                first = pts
        basis.append(Condition(ec, name, required, observed, first, detail))
    return MarketThesis(
        market=market_map.market,
        decision_ts_ns=market_map.decision_ts_ns,
        state=state,
        direction_hint=hint,
        basis=tuple(basis),
        marketmap_version=market_map.marketmap_version,
    )


def alignment(setup_direction: Direction, thesis: MarketThesis) -> Alignment:
    """ALIGNED / NEUTRAL / OPPOSED of a setup direction versus the main thesis (no clear thesis, ranges, transitions -> NEUTRAL)."""
    if thesis.state in (
        MainThesisState.NO_CLEAR_THESIS,
        MainThesisState.TRANSITION,
        MainThesisState.RANGE_ROTATION,
    ):
        return Alignment.NEUTRAL
    if thesis.direction_hint is None:
        return Alignment.NEUTRAL
    return Alignment.ALIGNED if thesis.direction_hint is setup_direction else Alignment.OPPOSED


__all__ = ["MAIN_THESIS_VERSION", "alignment", "derive_main_thesis"]
