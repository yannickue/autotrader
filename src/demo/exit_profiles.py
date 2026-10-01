# ruff: noqa: E501
"""Lane Y: family/mode (entry thesis) -> exit PROFILE router. Pure data + pure functions; NO state, NO execution.

The ONE execution authority stays ``exits.ExitEngine`` (driven by ``demo.execution.exit_manager.StagedExitManager``).
This module only (a) maps ``(family, mode)`` to exactly one of four profiles and (b) derives, from that profile, the
EXISTING ``ExitPolicy`` / stage fractions the engine is configured with. The profile is chosen by the family's
documented ECONOMIC HYPOTHESIS (never by market), frozen at entry and persisted with the intent / registry row.

Profiles
--------
CONTINUATION  directional continuation. structural stop -> structural TP1 (partial) -> RUNNER -> structure trail
              (the ONLY trailing authority) -> thesis (structure) failure -> hard EOD flat.
REVERSION     genuine mean reversion / equilibrium return. structural stop -> ONE mean/structure target, full close
              (no runner) -> family-aware time decay (OFF unless the family documents a horizon) -> thesis failure ->
              hard EOD flat.
FAILED_MOVE   failed expansion / false breakout / reclaim (price tried to expand, failed, re-entered prior structure).
              stop behind the failure extreme -> first structural RETURN target, full close by default (TP2 is
              implemented but DORMANT / SHADOW) -> thesis failure / renewed breakout -> hard EOD flat.
FIXED_1_5R    unchanged baseline: broker SL + one fixed-R broker TP, the engine does not touch the row. Used for
              families whose hypothesis is genuinely ambiguous (TEMPORARY, listed in ``ROUTES`` + docs).

Not active in ANY profile (implemented, configured OFF): break-even as an R-threshold authority, percentage / ATR
trail, MFE give-back, late-session loser rule, momentum exit (SHADOW only), TP2 (SHADOW only).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

from exits.models import ExitPolicy

MAPPING_VERSION = "exit-profiles-v1"

PROFILE_CONTINUATION = "CONTINUATION"
PROFILE_REVERSION = "REVERSION"
PROFILE_FAILED_MOVE = "FAILED_MOVE"
PROFILE_FIXED = "FIXED_1_5R"
PROFILES = (PROFILE_CONTINUATION, PROFILE_REVERSION, PROFILE_FAILED_MOVE, PROFILE_FIXED)
ENGINE_PROFILES = (PROFILE_CONTINUATION, PROFILE_REVERSION, PROFILE_FAILED_MOVE)  # the engine manages these rows

STOP_BASIS_FAMILY = "family"  # the family's own stop already IS the thesis invalidation (kept as is)
STOP_BASIS_CHART = "chart"  # the family stop is ATR-defined -> chart structural stop (demo.structure) replaces it
NO_MODE = "-"

# Shadow-only momentum signal (never an order): same value as the E2 production momentum rule it replaces.
SHADOW_MOMENTUM_THRESHOLD = Decimal("-1.0")

D = Decimal
# Fractions of the ORIGINAL (filled) quantity per stage; the remainder is the runner.
PROFILE_FRACTIONS: dict[str, tuple[Decimal, ...]] = {
    PROFILE_CONTINUATION: (D("0.5"),),  # TP1 partial, 50 % runner (TP2 DORMANT)
    PROFILE_REVERSION: (D("1"),),  # single target, full close, no runner
    PROFILE_FAILED_MOVE: (D("1"),),  # first structural return target, full close, no runner (TP2 DORMANT)
}


@dataclass(frozen=True, slots=True)
class FamilyRoute:
    family: str
    mode: str  # NO_MODE for families without a mode
    thesis: str
    profile: str
    rationale: str
    stop_basis: str | None  # STOP_BASIS_* (None for FIXED)
    time_stop_minutes: int | None = None  # REVERSION only; None = time stop OFF (no documented family horizon)
    temporary: bool = False  # FIXED_1_5R because the hypothesis is ambiguous / undocumented


def _r(family: str, mode: str | None, thesis: str, profile: str, rationale: str, stop_basis: str | None, **kw: Any) -> FamilyRoute:
    return FamilyRoute(family, mode or NO_MODE, thesis, profile, rationale, stop_basis, **kw)


_AMBIG = "TEMPORARY benchmark: "
ROUTES: tuple[FamilyRoute, ...] = (
    # --- STRUCT (alpha.families.structbrk): the SAME structure break, four entry timings --------------------------------
    _r("STRUCT", "breakout", "break of a compressed range continues", PROFILE_CONTINUATION,
       "entry at the break close; stop = opposite range edge (family stop already structural)", STOP_BASIS_FAMILY),
    _r("STRUCT", "confirmed", "break of a compressed range continues (next bar confirms)", PROFILE_CONTINUATION,
       "same break, one-bar confirmation; continuation thesis", STOP_BASIS_FAMILY),
    _r("STRUCT", "retest", "broken level is retested and holds, the break continues", PROFILE_CONTINUATION,
       "retest of the BROKEN edge that still closes beyond it = continuation of the break (documented decision)", STOP_BASIS_FAMILY),
    _r("STRUCT", "fade", "failed break: close back inside the range, move goes the other way", PROFILE_FAILED_MOVE,
       "failed-break fade; stop = extreme of the failed excursion (family stop already structural)", STOP_BASIS_FAMILY),
    # --- ORB -------------------------------------------------------------------------------------------------------
    _r("ORB", "breakout", "close beyond the opening range = initiative flow continues", PROFILE_CONTINUATION,
       "range breakout; stop = broken side minus stop_frac*width (family stop already structural)", STOP_BASIS_FAMILY),
    _r("ORB", "fade", "breach rejected within fail_bars = trapped traders squeeze the other way", PROFILE_FAILED_MOVE,
       "failed breakout; stop = breach extreme + pad (family stop already structural)", STOP_BASIS_FAMILY),
    # --- ROUND -----------------------------------------------------------------------------------------------------
    _r("ROUND", "reject", "wick rejection at a round number: price falls back from the level", PROFILE_REVERSION,
       "level rejection / reversion (explicit user classification); family stop is ATR-based -> chart stop", STOP_BASIS_CHART),
    _r("ROUND", "break", "close beyond a round level that HOLDS: the break continues", PROFILE_CONTINUATION,
       "break-and-hold continuation; family stop is ATR-based -> chart stop", STOP_BASIS_CHART),
    # --- VOLREV ----------------------------------------------------------------------------------------------------
    _r("VOLREV", "fade", "high-vol overshoot k ATR from its anchor mean-reverts to the anchor", PROFILE_REVERSION,
       "mean reversion to the anchor (cash open / VWAP proxy); family target 'anchor' is the structural target", STOP_BASIS_CHART),
    _r("VOLREV", "expand", "compression -> break of the prior range starts a volatility expansion", PROFILE_CONTINUATION,
       "compression-breakout continuation", STOP_BASIS_CHART),
    # --- GAP -------------------------------------------------------------------------------------------------------
    _r("GAP", "fade", "cash-open gap is (partly) filled: price returns to the previous close", PROFILE_REVERSION,
       "gap fill = return to prior structure (previous close is the documented structural target)", STOP_BASIS_CHART),
    _r("GAP", "go", "cash-open gap is extended in the gap direction", PROFILE_CONTINUATION,
       "gap continuation", STOP_BASIS_CHART),
    # --- OVERNIGHT -------------------------------------------------------------------------------------------------
    _r("OVERNIGHT", "continue", "one-sided overnight flow persists into the cash session", PROFILE_CONTINUATION,
       "overnight drift persistence", STOP_BASIS_CHART),
    _r("OVERNIGHT", "reverse", "overnight flow is faded once the cash session absorbs it", PROFILE_FIXED,
       _AMBIG + "reversion hypothesis but NO documented equilibrium level/anchor (R target only) and no failed-move structure "
       "-> reversion vs failed-move cannot be decided without inventing a target", None, temporary=True),
    # --- EOD -------------------------------------------------------------------------------------------------------
    _r("EOD", "continue", "last-hour day-trend continues (closing-imbalance flow)", PROFILE_CONTINUATION,
       "continuation into the session end; the hard EOD flat stays the final authority", STOP_BASIS_CHART),
    _r("EOD", "reverse", "last-hour day move is faded (profit taking / mean reversion)", PROFILE_FIXED,
       _AMBIG + "reversion hypothesis but NO documented equilibrium level/anchor (R target only) -> no defensible "
       "mean target without inventing one", None, temporary=True),
    # --- LEADLAG ---------------------------------------------------------------------------------------------------
    _r("LEADLAG", None, "follower catches up with the leader's n-bar move (sign=-1 is the fade control)", PROFILE_FIXED,
       _AMBIG + "cross-market catch-up of seconds-to-minutes horizon, neither continuation nor reversion of "
       "the follower's own structure; the M5 caveat in the family doc says most of the effect is contemporaneous correlation", None,
       temporary=True),
)

_BY_KEY: dict[tuple[str, str], FamilyRoute] = {}
for _route in ROUTES:
    _key = (_route.family, _route.mode)
    if _key in _BY_KEY:  # exactly-one-profile determinism is enforced at import time as well as by test
        raise RuntimeError(f"duplicate exit-profile route {_key}")
    _BY_KEY[_key] = _route
del _route, _key


def _key(family: str | None, mode: str | None) -> tuple[str, str]:
    return (str(family or "").upper(), str(mode).lower() if mode not in (None, "") else NO_MODE)


def route_for(family: str | None, mode: str | None) -> FamilyRoute:
    """The route of ``(family, mode)``. An UNMAPPED pair is never guessed: it is the explicit TEMPORARY FIXED_1_5R route
    (unchanged baseline behaviour) flagged ``UNMAPPED`` so it shows up in the attribution; the coverage test fails on it."""
    key = _key(family, mode)
    fam = key[0]
    found = _BY_KEY.get(key)
    if found is not None:
        return found
    return FamilyRoute(
        fam, key[1], "UNMAPPED family/mode", PROFILE_FIXED,
        "UNMAPPED: no documented hypothesis -> unchanged fixed_1_5r baseline", None, temporary=True,
    )


def is_mapped(family: str | None, mode: str | None) -> bool:
    return _key(family, mode) in _BY_KEY


def attribution(route: FamilyRoute, *, mapping_version: str = MAPPING_VERSION) -> dict[str, Any]:
    """Per-intent profile attribution persisted in the registry context (and on the GEOMETRY shadow record)."""
    fr = PROFILE_FRACTIONS.get(route.profile)
    tp1 = None if fr is None else str(fr[0])
    runner = None if fr is None else str(Decimal(1) - sum(fr, Decimal(0)))
    return {
        "profile": route.profile, "mapping_version": mapping_version, "family": route.family, "mode": route.mode,
        "thesis": route.thesis, "stop_basis": route.stop_basis, "temporary": route.temporary,
        "tp1_fraction": tp1, "runner_fraction": runner, "tp2": "DORMANT_SHADOW" if fr is not None else None,
        "time_stop_minutes": route.time_stop_minutes,
    }


def profile_policy(route_or_profile: FamilyRoute | str, base: ExitPolicy, *, time_stop_minutes: int | None = None) -> ExitPolicy:
    """The EXISTING ``ExitPolicy`` configured for one profile (derived from ``base``; never a new engine).

    Common to every engine profile: break-even R trigger / after-TP1 / late-window break-even OFF, ATR + percentage trail OFF,
    MFE give-back OFF, late-session loser rule OFF, momentum exit OFF (shadow only), policy-ladder empty (the per-position
    ``exit_plan`` carries the stages), structural failure exit ON (thesis failure)."""
    if isinstance(route_or_profile, FamilyRoute):
        profile = route_or_profile.profile
        minutes = route_or_profile.time_stop_minutes if time_stop_minutes is None else time_stop_minutes
    else:
        profile, minutes = route_or_profile, time_stop_minutes
    if profile not in ENGINE_PROFILES:
        raise ValueError(f"{profile!r} is not an engine-managed profile")
    off = {
        "breakeven_trigger_r_multiple": Decimal("99"), "breakeven_after_first_stage": False, "breakeven_buffer_bps": Decimal(0),
        "trailing_activation_r_multiple": Decimal("99"), "momentum_deterioration_threshold": None,
        "max_giveback_fraction": None, "late_window": None, "late_loser_momentum_threshold": None,
        "take_profit_stages": (), "max_holding_duration": None, "time_stop_min_mfe_r": None,
        "structure_trailing": False, "structure_cost_floor": False, "structure_failure_exit": True,
    }
    if profile == PROFILE_CONTINUATION:
        off.update(structure_trailing=True, structure_cost_floor=True)
    elif profile == PROFILE_REVERSION and minutes is not None:
        off.update(max_holding_duration=timedelta(minutes=minutes), time_stop_min_mfe_r=Decimal("0.5"))
    suffix = "" if not minutes else f"-t{minutes}"
    return replace(base, policy_id=f"profile-{profile.lower()}-v1{suffix}", **off)


__all__ = [
    "ENGINE_PROFILES", "MAPPING_VERSION", "NO_MODE", "PROFILES", "PROFILE_CONTINUATION", "PROFILE_FAILED_MOVE",
    "PROFILE_FIXED", "PROFILE_FRACTIONS", "PROFILE_REVERSION", "ROUTES", "SHADOW_MOMENTUM_THRESHOLD",
    "STOP_BASIS_CHART", "STOP_BASIS_FAMILY", "FamilyRoute", "attribution", "is_mapped", "profile_policy", "route_for",
]
