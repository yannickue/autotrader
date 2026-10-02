# ruff: noqa: E501
"""Synthetic MarketMap builders + mirror helper shared by the setup-engine / spec tests (no dependency on marketmap.py)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from research_workbench.thesis.contracts import Direction, MarketMap, MarketPhase

STEP_NS = 300_000_000_000
LONG_TRIG = "STRUCT_RETEST_LONG"
SHORT_TRIG = "STRUCT_RETEST_SHORT"
GEO_LONG = {
    "entry_zone": (100.0, 101.0),
    "structural_stop": 99.0,
    "opposition_1": 105.0,
    "opposition_2": 108.0,
}


def mm(i: int, **kw: Any) -> MarketMap:
    base: dict[str, Any] = {
        "market": "EURUSD",
        "decision_ts_ns": (i + 1) * STEP_NS,
        "bar_index": i,
        "session": "LONDON",
        "market_phase": MarketPhase.TREND,
        "h1_context": "UP",
        "m15_structure": "UP_SEQUENCE",
        "m5_structure": "DOWN_SEQUENCE",
        "nearest_support": None,
        "nearest_resistance": None,
        "second_support": None,
        "second_resistance": None,
        "active_support_zone": None,
        "active_resistance_zone": None,
        "role_reversal_zones": (),
        "balance_state": None,
        "acceptance_state": {},
        "participation_state": "NORMAL",
        "marketmap_version": "mm-test",
    }
    base.update(kw)
    return MarketMap(**base)


def ts(i: int) -> int:
    return (i + 1) * STEP_NS


_FLIP = {"UP_SEQUENCE": "DOWN_SEQUENCE", "DOWN_SEQUENCE": "UP_SEQUENCE", "UP": "DOWN", "DOWN": "UP"}


def _mz(z: tuple[float, float] | None) -> tuple[float, float] | None:
    return None if z is None else (-z[1], -z[0])


def _mp(p: float | None) -> float | None:
    return None if p is None else -p


def _mkey(k: str) -> str:
    d, rest = k.split(":", 1)
    return f"{'SHORT' if d == 'LONG' else 'LONG'}:{rest}"


def mirror_map(m: MarketMap) -> MarketMap:
    """Price/direction mirror: support<->resistance, UP<->DOWN, LONG<->SHORT keys, p -> -p."""
    return replace(
        m,
        h1_context=_FLIP.get(m.h1_context, m.h1_context) if m.h1_context else None,
        m15_structure=_FLIP.get(m.m15_structure, m.m15_structure) if m.m15_structure else None,
        m5_structure=_FLIP.get(m.m5_structure, m.m5_structure) if m.m5_structure else None,
        nearest_support=_mp(m.nearest_resistance),
        nearest_resistance=_mp(m.nearest_support),
        second_support=_mp(m.second_resistance),
        second_resistance=_mp(m.second_support),
        active_support_zone=_mz(m.active_resistance_zone),
        active_resistance_zone=_mz(m.active_support_zone),
        role_reversal_zones=tuple(_mz(z) for z in m.role_reversal_zones),  # type: ignore[misc]
        acceptance_state={_mkey(k): v for k, v in m.acceptance_state.items()},
    )


def mirror_geo(g: dict[str, Any] | None) -> dict[str, Any] | None:
    if g is None:
        return None
    return {
        "entry_zone": _mz(g["entry_zone"]),
        "structural_stop": _mp(g["structural_stop"]),
        "opposition_1": _mp(g["opposition_1"]),
        "opposition_2": _mp(g["opposition_2"]),
    }


def mirror_event(e: str) -> str:
    return e.replace("LONG", "SHORT") if "LONG" in e else e.replace("SHORT", "LONG")


ZONE = (100.0, 101.0)


def full_progression() -> tuple[
    list[MarketMap], dict[int, tuple[str, ...]], dict[int, dict[str, Any]]
]:
    """LONG: FORMING@0 -> LOCATION_REACHED+CONFIRMING@1 -> ARMED@2 -> TRIGGERED@3, one more absorbed bar @4."""
    maps = [
        mm(0),
        mm(1, active_support_zone=ZONE, acceptance_state={"LONG:L1": "ACCEPTED"}),
        mm(
            2,
            active_support_zone=ZONE,
            acceptance_state={"LONG:L1": "ACCEPTED"},
            m5_structure="UP_SEQUENCE",
        ),
        mm(
            3,
            active_support_zone=ZONE,
            acceptance_state={"LONG:L1": "RETEST_HELD"},
            m5_structure="UP_SEQUENCE",
        ),
        mm(4, m5_structure="UP_SEQUENCE"),
    ]
    events = {ts(3): (LONG_TRIG,)}
    geo = {ts(i): GEO_LONG for i in range(2, 5)}
    return maps, events, geo


def invalidated_midway() -> tuple[
    list[MarketMap], dict[int, tuple[str, ...]], dict[int, dict[str, Any]]
]:
    maps = [
        mm(0),
        mm(1, active_support_zone=ZONE, acceptance_state={"LONG:L1": "ACCEPTED"}),
        mm(2, active_support_zone=ZONE, m15_structure="DOWN_SEQUENCE"),
        mm(3, active_support_zone=ZONE, m15_structure="UP_SEQUENCE", m5_structure="UP_SEQUENCE"),
    ]
    return maps, {ts(3): (LONG_TRIG,)}, {ts(i): GEO_LONG for i in range(4)}


def acceptance_invalidation() -> tuple[
    list[MarketMap], dict[int, tuple[str, ...]], dict[int, dict[str, Any]]
]:
    maps = [
        mm(0),
        mm(1, active_support_zone=ZONE, acceptance_state={"LONG:L1": "ACCEPTED"}),
        mm(
            2,
            active_support_zone=ZONE,
            acceptance_state={"LONG:L1": "ACCEPTED", "SHORT:L1": "ACCEPTED"},
        ),
    ]
    return maps, {}, {}


def stalls_then_expires() -> tuple[
    list[MarketMap], dict[int, tuple[str, ...]], dict[int, dict[str, Any]]
]:
    maps = [mm(i) for i in range(6)]  # context only, never reaches a location
    return maps, {}, {}


def no_setup() -> tuple[list[MarketMap], dict[int, tuple[str, ...]], dict[int, dict[str, Any]]]:
    maps = [mm(i, market_phase=MarketPhase.RANGE) for i in range(4)]
    return maps, {}, {}


SCENARIOS = {
    "full_progression": full_progression,
    "invalidated_midway": invalidated_midway,
    "acceptance_invalidation": acceptance_invalidation,
    "stalls_then_expires": stalls_then_expires,
    "no_setup": no_setup,
}

LONG, SHORT = Direction.LONG, Direction.SHORT
