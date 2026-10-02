# ruff: noqa: E501
"""Main thesis: frozen rule table, NO_CLEAR_THESIS default, alignment, mirror symmetry, history/first-observed freezing, prefix invariance."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from research_workbench.thesis import marketmap as MM
from research_workbench.thesis.contracts import (
    Alignment,
    Direction,
    EvidenceClass,
    MainThesisState,
    MarketMap,
    MarketPhase,
    MarketThesis,
)
from research_workbench.thesis.main_thesis import alignment, derive_main_thesis
from tests.unit.research_workbench.thesis._obs_bars import synth

STEP = 300 * 1_000_000_000
S = MainThesisState
P = MarketPhase


def mk(i: int = 10, **kw: Any) -> MarketMap:
    base: dict[str, Any] = {
        "market": "SYN", "decision_ts_ns": (i + 1) * STEP, "bar_index": i, "session": "CASH", "market_phase": P.TREND, "h1_context": "UP",
        "m15_structure": "UP_SEQUENCE", "m5_structure": "UP_SEQUENCE", "nearest_support": 99.0, "nearest_resistance": 101.0, "second_support": 98.0,
        "second_resistance": 102.0, "active_support_zone": None, "active_resistance_zone": None, "role_reversal_zones": (),
        "balance_state": "DIRECTIONAL", "acceptance_state": {}, "participation_state": "NORMAL", "volatility_context": "NORMAL", "marketmap_version": "mm-test",
    }  # fmt: skip
    base.update(kw)
    return MarketMap(**base)


def state_of(m: MarketMap) -> tuple[MainThesisState, Direction | None]:
    t = derive_main_thesis(m)
    return t.state, t.direction_hint


# ------------------------------------------------------------------------------------------------ rule table
@pytest.mark.parametrize(
    "kw,expected",
    [
        (dict(market_phase=P.TREND, h1_context="UP", m15_structure="UP_SEQUENCE"), (S.BULLISH_CONTINUATION, Direction.LONG)),
        (dict(market_phase=P.PULLBACK, h1_context="UP", m15_structure="UP_SEQUENCE", m5_structure="DOWN_SEQUENCE"), (S.BULLISH_CONTINUATION, Direction.LONG)),
        (dict(market_phase=P.TREND, h1_context="DOWN", m15_structure="DOWN_SEQUENCE", m5_structure="DOWN_SEQUENCE"), (S.BEARISH_CONTINUATION, Direction.SHORT)),
        (dict(market_phase=P.TREND, h1_context="NEUTRAL"), (S.NO_CLEAR_THESIS, None)),  # an H1-neutral M15 trend does not force a bias
        (dict(market_phase=P.TREND, h1_context=None), (S.NO_CLEAR_THESIS, None)),  # not evaluable
        (dict(market_phase=P.TREND, h1_context="DOWN", m15_structure="UP_SEQUENCE"), (S.TRANSITION, None)),  # timeframes disagree
        (dict(market_phase=P.BREAKOUT, acceptance_state={"LONG:z1": "ACCEPTED", "SHORT:z2": None}), (S.BREAKOUT_EXPANSION, Direction.LONG)),
        (dict(market_phase=P.BREAKOUT, acceptance_state={"SHORT:z2": "BROKEN"}), (S.BREAKOUT_EXPANSION, Direction.SHORT)),
        (dict(market_phase=P.BREAKOUT, acceptance_state={"LONG:z1": "ACCEPTED", "SHORT:z2": "BROKEN"}), (S.NO_CLEAR_THESIS, None)),  # ambiguous side
        (dict(market_phase=P.BREAKOUT, acceptance_state={}), (S.NO_CLEAR_THESIS, None)),
        (dict(market_phase=P.EXPANSION, volatility_context="HIGH"), (S.BREAKOUT_EXPANSION, None)),
        (dict(market_phase=P.FAILED_BREAK), (S.FAILED_BREAK_REVERSAL, None)),
        (dict(market_phase=P.BALANCE, balance_state="BALANCE"), (S.RANGE_ROTATION, None)),
        (dict(market_phase=P.RANGE, balance_state="MIXED"), (S.RANGE_ROTATION, None)),
        (dict(market_phase=P.COMPRESSION, balance_state="BALANCE"), (S.RANGE_ROTATION, None)),
        (dict(market_phase=P.REVERSAL_ATTEMPT), (S.TRANSITION, None)),
        (dict(market_phase=P.TRANSITION), (S.TRANSITION, None)),
        (dict(market_phase=P.UNDEFINED), (S.NO_CLEAR_THESIS, None)),
    ],
)  # fmt: skip
def test_rule_table(kw: dict[str, Any], expected: tuple[MainThesisState, Direction | None]) -> None:
    assert state_of(mk(**kw)) == expected


def test_every_state_is_producible() -> None:
    produced = {
        state_of(mk(market_phase=P.TREND))[0], state_of(mk(market_phase=P.TREND, m15_structure="DOWN_SEQUENCE", h1_context="DOWN"))[0],
        state_of(mk(market_phase=P.BALANCE))[0], state_of(mk(market_phase=P.EXPANSION))[0], state_of(mk(market_phase=P.FAILED_BREAK))[0],
        state_of(mk(market_phase=P.TRANSITION))[0], state_of(mk(market_phase=P.UNDEFINED))[0],
    }  # fmt: skip
    assert produced == set(MainThesisState)


def test_no_clear_thesis_is_the_default_when_nothing_is_known() -> None:
    m = mk(
        market_phase=P.UNDEFINED,
        h1_context=None,
        m15_structure=None,
        m5_structure=None,
        balance_state=None,
        volatility_context=None,
    )
    t = derive_main_thesis(m)
    assert t.state is S.NO_CLEAR_THESIS and t.direction_hint is None
    assert any(c.name == "phase_defined" and c.observed is False for c in t.basis)


# ------------------------------------------------------------------------------------------------ basis (explainable, no opaque score)
def test_basis_is_an_explainable_tuple_of_conditions() -> None:
    t = derive_main_thesis(mk(market_phase=P.TREND))
    assert isinstance(t.basis, tuple) and t.basis
    names = {c.name for c in t.basis}
    assert {
        "phase_defined",
        "m15_trend_sequence",
        "h1_context_available",
        "h1_supports_m15_trend",
    } <= names
    assert all(
        c.required and c.observed is True and c.first_observed_ns == t.decision_ts_ns
        for c in t.basis
    )
    classes = {c.evidence_class for c in t.basis}
    assert (
        classes <= set(EvidenceClass)
        and EvidenceClass.CONTEXT in classes
        and EvidenceClass.STRUCTURE in classes
    )
    assert all(c.detail for c in t.basis)
    blocked = derive_main_thesis(mk(market_phase=P.TREND, h1_context="NEUTRAL"))
    assert any(
        c.name == "h1_supports_m15_trend" and c.observed is False for c in blocked.basis
    )  # the missing condition is named
    unknown = derive_main_thesis(mk(market_phase=P.TREND, h1_context=None))
    assert any(
        c.name == "h1_supports_m15_trend" and c.observed is None and c.first_observed_ns is None
        for c in unknown.basis
    )


def test_thesis_carries_market_decision_time_and_versions() -> None:
    m = mk(i=42)
    t = derive_main_thesis(m)
    assert (t.market, t.decision_ts_ns, t.marketmap_version) == (
        m.market,
        m.decision_ts_ns,
        "mm-test",
    )
    assert t.thesis_contract_version


# ------------------------------------------------------------------------------------------------ alignment
@pytest.mark.parametrize(
    "state,hint,long,short",
    [
        (S.BULLISH_CONTINUATION, Direction.LONG, Alignment.ALIGNED, Alignment.OPPOSED),
        (S.BEARISH_CONTINUATION, Direction.SHORT, Alignment.OPPOSED, Alignment.ALIGNED),
        (S.BREAKOUT_EXPANSION, Direction.LONG, Alignment.ALIGNED, Alignment.OPPOSED),
        (S.BREAKOUT_EXPANSION, None, Alignment.NEUTRAL, Alignment.NEUTRAL),
        (S.FAILED_BREAK_REVERSAL, None, Alignment.NEUTRAL, Alignment.NEUTRAL),
        (S.RANGE_ROTATION, None, Alignment.NEUTRAL, Alignment.NEUTRAL),
        (S.TRANSITION, None, Alignment.NEUTRAL, Alignment.NEUTRAL),
        (S.NO_CLEAR_THESIS, None, Alignment.NEUTRAL, Alignment.NEUTRAL),
    ],
)
def test_alignment_matrix(
    state: MainThesisState, hint: Direction | None, long: Alignment, short: Alignment
) -> None:
    t = MarketThesis("SYN", 1, state, hint, (), "mm-test")
    assert alignment(Direction.LONG, t) is long and alignment(Direction.SHORT, t) is short


def test_alignment_never_trusts_a_hint_on_a_neutral_state() -> None:
    for st in (S.NO_CLEAR_THESIS, S.TRANSITION, S.RANGE_ROTATION):
        t = MarketThesis("SYN", 1, st, Direction.LONG, (), "mm-test")
        assert (
            alignment(Direction.LONG, t) is Alignment.NEUTRAL
            and alignment(Direction.SHORT, t) is Alignment.NEUTRAL
        )


# ------------------------------------------------------------------------------------------------ mirror symmetry
_FLIP = {"UP_SEQUENCE": "DOWN_SEQUENCE", "DOWN_SEQUENCE": "UP_SEQUENCE", "UP": "DOWN", "DOWN": "UP"}


def mirror_map(m: MarketMap) -> MarketMap:
    acc = {
        f"{'SHORT' if k.startswith('LONG') else 'LONG'}:{k.split(':', 1)[1]}": v
        for k, v in m.acceptance_state.items()
    }
    neg = lambda p: None if p is None else -p  # noqa: E731
    zone = lambda z: None if z is None else (-z[1], -z[0])  # noqa: E731
    return replace(
        m, h1_context=_FLIP.get(m.h1_context, m.h1_context), m15_structure=_FLIP.get(m.m15_structure, m.m15_structure),
        m5_structure=_FLIP.get(m.m5_structure, m.m5_structure), nearest_support=neg(m.nearest_resistance), nearest_resistance=neg(m.nearest_support),
        second_support=neg(m.second_resistance), second_resistance=neg(m.second_support), active_support_zone=zone(m.active_resistance_zone),
        active_resistance_zone=zone(m.active_support_zone), acceptance_state=acc,
    )  # fmt: skip


_FLIP_STATE = {
    S.BULLISH_CONTINUATION: S.BEARISH_CONTINUATION,
    S.BEARISH_CONTINUATION: S.BULLISH_CONTINUATION,
}


@pytest.mark.parametrize(
    "kw",
    [
        dict(market_phase=P.TREND), dict(market_phase=P.PULLBACK, m5_structure="DOWN_SEQUENCE"), dict(market_phase=P.TREND, h1_context="NEUTRAL"),
        dict(market_phase=P.TREND, h1_context="DOWN"), dict(market_phase=P.BREAKOUT, acceptance_state={"LONG:z1": "RETEST_HELD"}),
        dict(market_phase=P.BALANCE), dict(market_phase=P.FAILED_BREAK), dict(market_phase=P.UNDEFINED), dict(market_phase=P.TRANSITION),
    ],
)  # fmt: skip
def test_long_short_mirror_symmetry_of_state_hint_and_alignment(kw: dict[str, Any]) -> None:
    m = mk(**kw)
    a, b = derive_main_thesis(m), derive_main_thesis(mirror_map(m))
    assert b.state is _FLIP_STATE.get(a.state, a.state)
    assert b.direction_hint is (None if a.direction_hint is None else a.direction_hint.opposite())
    for d in Direction:
        assert alignment(d, a) is alignment(d.opposite(), b)
    assert [(c.evidence_class, c.name, c.required, c.observed) for c in a.basis] == [
        (c.evidence_class, c.name, c.required, c.observed) for c in b.basis
    ]


# ------------------------------------------------------------------------------------------------ history / frozen first-observed
def test_first_observed_is_frozen_from_the_unbroken_run_in_history() -> None:
    hist = [mk(i=1), mk(i=2), mk(i=3, h1_context="NEUTRAL"), mk(i=4), mk(i=5)]
    cur = mk(i=6)
    t = derive_main_thesis(cur, hist)
    first = {c.name: c.first_observed_ns for c in t.basis}
    assert (
        first["h1_supports_m15_trend"] == hist[3].decision_ts_ns
    )  # run restarted after the NEUTRAL bar (i=3)
    assert first["m15_trend_sequence"] == hist[0].decision_ts_ns  # never interrupted
    # a later bar never rewrites an earlier first observation: the thesis at i=5 (history = the prefix) stamps the same first-observed time
    t5 = derive_main_thesis(hist[4], hist[:4])
    assert {c.name: c.first_observed_ns for c in t5.basis}["h1_supports_m15_trend"] == hist[
        3
    ].decision_ts_ns
    assert (
        first["h1_supports_m15_trend"]
        == {c.name: c.first_observed_ns for c in t5.basis}["h1_supports_m15_trend"]
    )


def test_history_does_not_change_the_state_only_the_timestamps() -> None:
    hist = [mk(i=1, market_phase=P.UNDEFINED), mk(i=2, h1_context="DOWN")]
    cur = mk(i=3)
    assert derive_main_thesis(cur, hist).state is derive_main_thesis(cur).state
    assert derive_main_thesis(cur, hist).direction_hint is derive_main_thesis(cur).direction_hint


def test_history_from_the_future_or_other_market_or_unordered_is_rejected() -> None:
    cur = mk(i=5)
    with pytest.raises(ValueError):
        derive_main_thesis(cur, [mk(i=5)])  # same decision time
    with pytest.raises(ValueError):
        derive_main_thesis(cur, [mk(i=9)])  # look-ahead
    with pytest.raises(ValueError):
        derive_main_thesis(cur, [mk(i=3), mk(i=2)])  # not ascending
    with pytest.raises(ValueError):
        derive_main_thesis(cur, [replace(mk(i=2), market="OTHER")])


def test_determinism() -> None:
    m = mk()
    assert derive_main_thesis(m) == derive_main_thesis(m)
    h = [mk(i=1), mk(i=2)]
    assert derive_main_thesis(m, h) == derive_main_thesis(m, h)


# ------------------------------------------------------------------------------------------------ prefix invariance on real maps
@pytest.fixture(scope="module")
def up_bars():
    return synth("up", 700)


def test_prefix_invariance_of_the_thesis_with_prefix_built_history(up_bars) -> None:
    """thesis(map(bars, i), history of maps(bars, j<i)) == thesis(map(prefix, i), history of maps(prefix_j, j)) for the same i."""
    idxs = [640, 645, 650]
    full = [MM.build_market_map(up_bars, j) for j in idxs]
    pref = [MM.build_market_map(up_bars.prefix(j + 1), j) for j in idxs]
    assert [m.content_hash() for m in full] == [m.content_hash() for m in pref]
    t_full = derive_main_thesis(full[-1], full[:-1])
    t_pref = derive_main_thesis(pref[-1], pref[:-1])
    assert t_full == t_pref
    # the thesis at an earlier bar is unchanged by the later bars it was never allowed to see
    assert derive_main_thesis(full[1], full[:1]) == derive_main_thesis(pref[1], pref[:1])
    assert t_full.state in set(MainThesisState)


def test_uptrend_bars_never_give_a_bearish_thesis(up_bars) -> None:
    states = {derive_main_thesis(MM.build_market_map(up_bars, j)).state for j in (640, 660, 680)}
    assert S.BEARISH_CONTINUATION not in states
