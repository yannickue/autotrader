# ruff: noqa: E501
"""MarketMap semantics: phase rule table, acceptance vocabulary, field invariants and LONG/SHORT mirror symmetry."""

from __future__ import annotations

import pytest

from market_observer import swings as S
from market_observer.schema import ObserverBars
from research_workbench.thesis import marketmap as MM
from research_workbench.thesis.contracts import MarketMap, MarketPhase
from tests.unit.research_workbench.thesis._obs_bars import mirror_bars, synth

CFG = MM.MARKETMAP_CONFIG
N = 760
FIRST = 603


# ------------------------------------------------------------------------------------------------ phase rule table (fake swing states)
def sw(
    seq: str = "RANGE_OR_UNDEFINED",
    *,
    up: float | None = 0.0,
    dn: float | None = 0.0,
    since_up: int | None = None,
    since_dn: int | None = None,
) -> S.SwingStructureState:
    return S.SwingStructureState(
        timeframe="M5", decision_ts_ns=0, last_high_price=101.0, prev_high_price=100.0, last_low_price=99.0, prev_low_price=98.0,
        last_high_confirmed_at_ts_ns=1, last_low_confirmed_at_ts_ns=1, high_label=None, low_label=None, sequence=S.SwingSequence(seq),
        sequence_length=2, structure_age_bars=3, structure_age_minutes=15.0, high_delta_atr=None, low_delta_atr=None, confirmed_at_ts_ns=1,
        close_beyond_last_swing_atr_high=up, close_beyond_last_swing_atr_low=dn, bars_since_beyond_high=since_up, bars_since_beyond_low=since_dn,
    )  # fmt: skip


_FLIP_SEQ = {"UP_SEQUENCE": "DOWN_SEQUENCE", "DOWN_SEQUENCE": "UP_SEQUENCE"}


def mirror_state(s: S.SwingStructureState | None) -> S.SwingStructureState | None:
    if s is None:
        return None
    flip = S.SwingSequence(_FLIP_SEQ.get(s.sequence.value, s.sequence.value))
    neg = lambda v: None if v is None else (0.0 if v == 0 else -v)  # noqa: E731
    from dataclasses import replace

    return replace(
        s, sequence=flip, close_beyond_last_swing_atr_high=neg(s.close_beyond_last_swing_atr_low),
        close_beyond_last_swing_atr_low=neg(s.close_beyond_last_swing_atr_high), bars_since_beyond_high=s.bars_since_beyond_low,
        bars_since_beyond_low=s.bars_since_beyond_high,
    )  # fmt: skip


PHASE_CASES = [
    # name, sw5, sw15, balance, vol, acc, expected
    ("undefined_no_m5", None, sw("UP_SEQUENCE"), "MIXED", "NORMAL", {}, MarketPhase.UNDEFINED),
    ("undefined_no_balance", sw(), sw(), None, "NORMAL", {}, MarketPhase.UNDEFINED),
    (
        "failed_break_up",
        sw("UP_SEQUENCE", up=0.0, since_up=5),
        sw("UP_SEQUENCE"),
        "MIXED",
        "NORMAL",
        {},
        MarketPhase.FAILED_BREAK,
    ),
    (
        "failed_break_too_old",
        sw("UP_SEQUENCE", up=0.0, since_up=40),
        sw("UP_SEQUENCE"),
        "MIXED",
        "NORMAL",
        {},
        MarketPhase.TREND,
    ),
    (
        "reversal_attempt",
        sw("DOWN_SEQUENCE"),
        sw("UP_SEQUENCE", dn=-0.6),
        "MIXED",
        "NORMAL",
        {},
        MarketPhase.REVERSAL_ATTEMPT,
    ),
    (
        "breakout_up",
        sw("UP_SEQUENCE", up=0.8, since_up=3),
        sw("RANGE_OR_UNDEFINED"),
        "DIRECTIONAL",
        "NORMAL",
        {1: "BROKEN"},
        MarketPhase.BREAKOUT,
    ),
    (
        "break_without_acceptance_is_no_breakout",
        sw("UP_SEQUENCE", up=0.8, since_up=3),
        sw("RANGE_OR_UNDEFINED"),
        "MIXED",
        "NORMAL",
        {1: None},
        MarketPhase.RANGE,
    ),
    (
        "break_in_existing_uptrend_is_trend",
        sw("UP_SEQUENCE", up=0.8, since_up=3),
        sw("UP_SEQUENCE"),
        "DIRECTIONAL",
        "NORMAL",
        {1: "ACCEPTED"},
        MarketPhase.TREND,
    ),
    (
        "trend_up",
        sw("UP_SEQUENCE"),
        sw("UP_SEQUENCE"),
        "DIRECTIONAL",
        "NORMAL",
        {},
        MarketPhase.TREND,
    ),
    (
        "pullback",
        sw("DOWN_SEQUENCE"),
        sw("UP_SEQUENCE"),
        "MIXED",
        "NORMAL",
        {},
        MarketPhase.PULLBACK,
    ),
    (
        "trend_unclear_m5_is_transition",
        sw("RANGE_OR_UNDEFINED"),
        sw("UP_SEQUENCE"),
        "MIXED",
        "NORMAL",
        {},
        MarketPhase.TRANSITION,
    ),
    ("expansion", sw(), sw("RANGE_OR_UNDEFINED"), "DIRECTIONAL", "HIGH", {}, MarketPhase.EXPANSION),
    ("compression", sw(), sw("RANGE_OR_UNDEFINED"), "BALANCE", "LOW", {}, MarketPhase.COMPRESSION),
    ("balance", sw(), sw("MIXED_TRANSITION"), "BALANCE", "NORMAL", {}, MarketPhase.BALANCE),
    ("range", sw(), sw("RANGE_OR_UNDEFINED"), "MIXED", "NORMAL", {}, MarketPhase.RANGE),
    (
        "transition_mixed_m15",
        sw(),
        sw("MIXED_TRANSITION"),
        "MIXED",
        "NORMAL",
        {},
        MarketPhase.TRANSITION,
    ),
    (
        "nothing_applies",
        sw(),
        sw("RANGE_OR_UNDEFINED"),
        "DIRECTIONAL",
        "NORMAL",
        {},
        MarketPhase.UNDEFINED,
    ),
]


@pytest.mark.parametrize(
    "name,sw5,sw15,bal,vol,acc,expected", PHASE_CASES, ids=[c[0] for c in PHASE_CASES]
)
def test_phase_rule_table_and_its_mirror(name, sw5, sw15, bal, vol, acc, expected) -> None:
    assert MM.derive_phase(sw5, sw15, bal, vol, acc, CFG) is expected
    mirrored_acc = {-d: v for d, v in acc.items()}
    assert (
        MM.derive_phase(mirror_state(sw5), mirror_state(sw15), bal, vol, mirrored_acc, CFG)
        is expected
    )


def test_failed_break_on_both_sides_is_not_a_failed_break() -> None:
    both = sw("RANGE_OR_UNDEFINED", up=0.0, dn=0.0, since_up=3, since_dn=4)
    assert (
        MM.derive_phase(both, sw("RANGE_OR_UNDEFINED"), "MIXED", "NORMAL", {}, CFG)
        is MarketPhase.RANGE
    )


def test_every_marketphase_member_is_producible_by_the_rule_table() -> None:
    produced = {c[-1] for c in PHASE_CASES}
    assert produced == set(MarketPhase)


# ------------------------------------------------------------------------------------------------ acceptance vocabulary
def _acc(
    found: bool | None,
    held: int | None = None,
    depth: float | None = None,
    reclaim: bool | None = None,
) -> dict:
    return {
        "break_found": found,
        "time_held_beyond_level_bars": held,
        "max_reentry_depth_atr": depth,
        "reclaim_occurred": reclaim,
    }


@pytest.mark.parametrize(
    "vals,expected",
    [
        (_acc(None), None),
        (_acc(False), None),
        (_acc(True, 0, 0.4, True), "RECLAIMED"),
        (_acc(True, 1, 0.0, False), "BROKEN"),
        (_acc(True, 2, 0.2, False), "BROKEN"),
        (_acc(True, 3, 0.0, False), "ACCEPTED"),
        (_acc(True, 9, 0.0, False), "ACCEPTED"),
        (_acc(True, 9, 0.3, False), "RETEST_HELD"),
        (
            _acc(True, 9, 0.3, True),
            "ACCEPTED",
        ),  # a close went back through the edge before: not a clean hold
        (_acc(True, None, None, None), None),
    ],
)
def test_acceptance_label_vocabulary(vals: dict, expected: str | None) -> None:
    assert MM.acceptance_label(vals, 3) == expected


# ------------------------------------------------------------------------------------------------ real-bar maps (replay fixtures)
def _replay(bars: ObserverBars) -> dict[int, MarketMap]:
    rep = MM.MarketMapReplay()
    out: dict[int, MarketMap] = {}
    for j in range(len(bars)):
        if j < FIRST:
            rep.advance(bars, j)
        else:
            out[j] = rep.step(bars, j)
    return out


@pytest.fixture(scope="module")
def up_bars() -> ObserverBars:
    return synth("up", N)


@pytest.fixture(scope="module")
def flat_bars() -> ObserverBars:
    return synth("flat", N)


@pytest.fixture(scope="module")
def up_maps(up_bars: ObserverBars) -> dict[int, MarketMap]:
    return _replay(up_bars)


@pytest.fixture(scope="module")
def flat_maps(flat_bars: ObserverBars) -> dict[int, MarketMap]:
    return _replay(flat_bars)


def test_support_resistance_ordering_invariants(
    up_bars: ObserverBars, up_maps: dict[int, MarketMap]
) -> None:
    for j, m in up_maps.items():
        close = float(up_bars.c[j])
        if m.nearest_support is not None:
            assert m.nearest_support < close
            if m.second_support is not None:
                assert m.second_support < m.nearest_support
        if m.nearest_resistance is not None:
            assert m.nearest_resistance > close
            if m.second_resistance is not None:
                assert m.second_resistance > m.nearest_resistance
        for zone in (m.active_support_zone, m.active_resistance_zone, *m.role_reversal_zones):
            if zone is not None:
                assert zone[0] <= zone[1]
        if m.active_support_zone is not None:  # "at or inside": never entirely above the close
            assert m.active_support_zone[0] <= close
        if m.active_resistance_zone is not None:
            assert m.active_resistance_zone[1] >= close


def test_uptrend_series_gives_trend_with_h1_up_and_a_bullish_structure(
    up_maps: dict[int, MarketMap],
) -> None:
    phases = [m.market_phase for m in up_maps.values()]
    assert phases.count(MarketPhase.TREND) > len(phases) // 3
    assert {m.h1_context for m in up_maps.values()} == {"UP"}
    assert all(
        m.m15_structure == "UP_SEQUENCE"
        for m in up_maps.values()
        if m.market_phase is MarketPhase.TREND
    )
    assert all(
        m.m5_structure == "UP_SEQUENCE"
        for m in up_maps.values()
        if m.market_phase is MarketPhase.TREND
    )
    assert all(
        m.m5_structure == "DOWN_SEQUENCE"
        for m in up_maps.values()
        if m.market_phase is MarketPhase.PULLBACK
    )


def test_flat_series_is_mostly_ranging_not_trending(flat_maps: dict[int, MarketMap]) -> None:
    phases = [m.market_phase for m in flat_maps.values()]
    assert phases.count(MarketPhase.RANGE) > phases.count(MarketPhase.TREND)
    assert phases.count(MarketPhase.RANGE) > len(phases) // 3


def test_acceptance_is_keyed_per_direction_and_level(
    up_bars: ObserverBars, up_maps: dict[int, MarketMap]
) -> None:
    vocabulary = {None, "BROKEN", "ACCEPTED", "RECLAIMED", "RETEST_HELD"}
    saw = {"LONG": set(), "SHORT": set()}
    for m in up_maps.values():
        for key, val in m.acceptance_state.items():
            side, level_id = key.split(":", 1)
            assert side in ("LONG", "SHORT") and level_id
            assert val in vocabulary
            saw[side].add(level_id)
        assert (
            sum(1 for k in m.acceptance_state if k.startswith("LONG:")) <= 1
        )  # only the nearest level of each side
        assert sum(1 for k in m.acceptance_state if k.startswith("SHORT:")) <= 1
    assert saw["LONG"] and saw["SHORT"]


# ------------------------------------------------------------------------------------------------ LONG/SHORT mirror symmetry (price series p -> -p)
_FLIP = {
    "UP_SEQUENCE": "DOWN_SEQUENCE",
    "DOWN_SEQUENCE": "UP_SEQUENCE",
    "UP": "DOWN",
    "DOWN": "UP",
    None: None,
}


def _neg(p: float | None) -> float | None:
    return None if p is None else -p


def _neg_zone(z: tuple[float, float] | None) -> tuple[float, float] | None:
    return None if z is None else (-z[1], -z[0])


def assert_mirrored(a: MarketMap, m: MarketMap) -> None:
    """``m`` was built on the price-mirrored bars of ``a``: support<->resistance, UP<->DOWN, LONG<->SHORT; everything else identical."""
    assert m.market_phase is a.market_phase
    assert m.m15_structure == _FLIP.get(a.m15_structure, a.m15_structure)
    assert m.m5_structure == _FLIP.get(a.m5_structure, a.m5_structure)
    assert m.h1_context == _FLIP.get(a.h1_context, a.h1_context)
    for fa, fm in (
        ("nearest_support", "nearest_resistance"),
        ("nearest_resistance", "nearest_support"),
        ("second_support", "second_resistance"),
        ("second_resistance", "second_support"),
    ):
        va, vm = getattr(a, fa), getattr(m, fm)
        assert (va is None) == (vm is None), (fa, va, vm)
        if va is not None:
            assert vm == pytest.approx(-va, abs=1e-9), fa
    for fa, fm in (
        ("active_support_zone", "active_resistance_zone"),
        ("active_resistance_zone", "active_support_zone"),
    ):
        za, zm = _neg_zone(getattr(a, fa)), getattr(m, fm)
        assert (za is None) == (zm is None), fa
        if za is not None:
            assert zm == pytest.approx(za, abs=1e-9), fa
    assert sorted(m.role_reversal_zones) == pytest.approx(
        sorted(_neg_zone(z) for z in a.role_reversal_zones), abs=1e-9
    )
    assert m.balance_state == a.balance_state and m.participation_state == a.participation_state
    assert m.volatility_context == a.volatility_context and m.session == a.session
    for side, other in (("LONG", "SHORT"), ("SHORT", "LONG")):
        va = sorted(str(v) for k, v in a.acceptance_state.items() if k.startswith(f"{side}:"))
        vm = sorted(str(v) for k, v in m.acceptance_state.items() if k.startswith(f"{other}:"))
        assert va == vm, (side, a.acceptance_state, m.acceptance_state)


@pytest.mark.parametrize("idx", [610, 650, 700, 755])
def test_mirror_symmetry_uptrend_vs_mirrored_series(
    up_bars: ObserverBars, up_maps: dict[int, MarketMap], idx: int
) -> None:
    mirrored = MM.build_market_map(mirror_bars(up_bars), idx)
    assert_mirrored(up_maps[idx], mirrored)


@pytest.mark.parametrize("idx", [620, 700])
def test_mirror_symmetry_range_series(
    flat_bars: ObserverBars, flat_maps: dict[int, MarketMap], idx: int
) -> None:
    assert_mirrored(flat_maps[idx], MM.build_market_map(mirror_bars(flat_bars), idx))


def test_mirror_of_mirror_is_identity(up_bars: ObserverBars, up_maps: dict[int, MarketMap]) -> None:
    twice = mirror_bars(mirror_bars(up_bars))
    assert MM.build_market_map(twice, 650).content_hash() == up_maps[650].content_hash()
