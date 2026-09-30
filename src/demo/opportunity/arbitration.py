# ruff: noqa: E501
"""Lane S: structure-event attribution and MT5-netting arbitration of the STRUCT variants (PHASE2_DISCOVERY).

MT5 nets positions: ONE net position per symbol. Every STRUCT variant (breakout / confirmed / retest / fade) is
``ACTIVE_DISCOVERY_ELIGIBLE`` and NOT_ALPHA_VALIDATED; none has a permanent priority. Rules (implemented here / in the engine
and stack, never by variant name):

* symbol FLAT: a fresh valid signal of ANY variant may become an intent. Several variants of the same cycle: the winner is the
  one with the EARLIEST signal timestamp; remaining ties are broken by ``sha256(structure_event_id | variant)`` (a hash, never a
  fixed variant order). Losers are still persisted (snapshot + decision) with an explicit code (below).
* symbol already has a position (or an ACCEPTED/SENT registry row): no overlapping independent position and no automatic
  pyramiding. The stack gates ADDON_* / OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1 stay the enforcement; this module only
  names the situation so the funnel can count it: same direction -> ``CONCURRENT_SIGNAL`` (+ ``ADD_ON_CANDIDATE`` tag),
  opposite direction -> ``REVERSAL_CANDIDATE``. Nothing ever flips a position through zero.
* after the position closes a later fresh signal of ANY variant may trade again (no family is suppressed).

Correlated evidence: variants that react to the SAME break share one ``structure_event_id``; they are NOT independent
observations. Analyses must report event-level (clustered) counts next to raw counts (``demo.funnel.structure_event_clusters``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from demo.opportunity.production_spec import ROLE_ACTIVE_DISCOVERY

__all__ = ("ROLE_ACTIVE_DISCOVERY",)

CONCURRENT_SIGNAL = "CONCURRENT_SIGNAL"  # same-direction signal while the symbol is (or is about to be) occupied
ADD_ON_CANDIDATE = "ADD_ON_CANDIDATE"  # tag accompanying CONCURRENT_SIGNAL: would be a pyramid add-on; never executed
REVERSAL_CANDIDATE = "REVERSAL_CANDIDATE"  # opposite-direction signal while occupied; never flips through zero
ARBITRATION_CODES: tuple[str, ...] = (CONCURRENT_SIGNAL, ADD_ON_CANDIDATE, REVERSAL_CANDIDATE)


def structure_event_id_of(market: str, range_high: float, range_low: float, break_bar_ts: str) -> str:
    """Deterministic id of one structure event: market + parent range edges + break bar time (UTC ISO).
    Every variant that reacts to the same break (same ``n_range``) shares it."""
    raw = f"STRUCT_EVENT|{market}|{float(range_high)!r}|{float(range_low)!r}|{break_bar_ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def tie_hash(structure_event_id: str, variant: str) -> str:
    return hashlib.sha256(f"{structure_event_id}|{variant}".encode()).hexdigest()


def arbitration_key(signal_ts_iso: str, structure_event_id: str, variant: str) -> tuple[str, str]:
    """Sort key of the same-cycle arbitration: earliest signal first, then a deterministic hash (no name priority)."""
    return (signal_ts_iso, tie_hash(structure_event_id, variant))


def pick_winner(items: Sequence[tuple[str, str, str]]) -> int:
    """Index of the winning (signal_ts_iso, structure_event_id, variant) item."""
    return min(range(len(items)), key=lambda k: arbitration_key(*items[k]))


def concurrent_reasons(direction: int, winner_direction: int) -> tuple[str, ...]:
    """Decision reasons of a same-cycle loser relative to the winner that occupies the symbol."""
    return (CONCURRENT_SIGNAL, ADD_ON_CANDIDATE) if direction == winner_direction else (REVERSAL_CANDIDATE,)


def classify_stack_code(code: str | None) -> tuple[str, ...]:
    """Map a stack gate code (signal while a position exists) onto the arbitration classification."""
    from demo.execution import gates as G

    base = G.base_code(code) if code else ""
    if base in (G.R_ADDON, G.R_ADDON_SHARED):
        return (CONCURRENT_SIGNAL, ADD_ON_CANDIDATE)
    if base == G.R_OPPOSITE:
        return (REVERSAL_CANDIDATE,)
    return ()


def event_attribution(market: str, variant: str, signal_ts_iso: str, direction: int, price: float,
                      levels: Mapping[str, Any]) -> dict[str, Any]:
    """Per-signal attribution persisted in the snapshot's signal metadata ({} when the family has no structure event)."""
    hi, lo, brk = levels.get("range_high"), levels.get("range_low"), levels.get("break_bar_ts")
    if hi is None or lo is None or brk is None:
        return {}
    return {
        "structure_event_id": structure_event_id_of(market, hi, lo, brk),
        "variant": variant,
        "signal_timestamp": signal_ts_iso,
        "direction": int(direction),
        "price": float(price),
        "parent_range": {
            "range_high": float(hi), "range_low": float(lo), "bars": levels.get("n_range"),
            "width_atr": levels.get("range_width_atr"), "break_bar_ts": brk,
        },
    }
