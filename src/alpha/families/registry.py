# ruff: noqa: E501
"""Family registry: name -> (spec class, grid, Train-fit, generate)."""

from __future__ import annotations

import json
from types import ModuleType
from typing import Any

from alpha.families import eod, gap, leadlag, orb, overnight, roundnum, structbrk, volrev
from alpha.families.common import Thr
from alpha.families.data import FamilyData
from alpha.families.spec import FamilySpec
from alpha.fast.sim import CandidateArrays

FAMILY_MODULES: dict[str, ModuleType] = {
    "ORB": orb, "GAP": gap, "OVERNIGHT": overnight, "VOLREV": volrev, "ROUND": roundnum, "LEADLAG": leadlag, "EOD": eod,
    "STRUCT": structbrk,
}
SPEC_CLASSES: dict[str, type[FamilySpec]] = {
    "ORB": orb.ORBSpec, "GAP": gap.GAPSpec, "OVERNIGHT": overnight.OVERNIGHTSpec, "VOLREV": volrev.VOLREVSpec,
    "ROUND": roundnum.ROUNDSpec, "LEADLAG": leadlag.LEADLAGSpec, "EOD": eod.EODSpec, "STRUCT": structbrk.STRUCTSpec,
}
FAMILY_NAMES = tuple(FAMILY_MODULES)


def spec_from_dict(raw: dict[str, Any]) -> FamilySpec:
    raw = dict(raw)
    cls = SPEC_CLASSES[raw.pop("family")]
    return cls(**raw)


def spec_from_json(text: str) -> FamilySpec:
    return spec_from_dict(json.loads(text))


def grid_for(family: str, market: str, max_n: int | None = 400) -> list[FamilySpec]:
    """Deterministic (hash-ordered) parameter grid of one family for one market, at most ``max_n`` specs."""
    if family == "LEADLAG":
        leaders = leadlag.PAIRS.get(market, ())
        return leadlag.grid(leaders, max_n) if leaders else []
    return FAMILY_MODULES[family].grid(max_n)


def fit_thresholds(train: FamilyData, spec: FamilySpec) -> Thr:
    """Train-fitted numbers of ``spec`` from the Train view ONLY (the caller passes the Train slice)."""
    return FAMILY_MODULES[spec.FAMILY].fit(train, spec)


def generate_candidates(data: FamilyData, spec: FamilySpec, thr: Thr) -> CandidateArrays:
    return FAMILY_MODULES[spec.FAMILY].generate(data, spec, thr)


def describe_candidate(data: FamilyData, spec: FamilySpec, decision_idx: int, direction: int) -> dict[str, Any]:
    """Optional additive per-candidate metadata (e.g. STRUCT ``structure_levels``); ``{}`` for families without it."""
    fn = getattr(FAMILY_MODULES[spec.FAMILY], "structure_levels", None)
    return fn(data, spec, decision_idx, direction) if fn is not None else {}


__all__ = (
    "FAMILY_MODULES", "FAMILY_NAMES", "SPEC_CLASSES", "describe_candidate", "fit_thresholds", "generate_candidates", "grid_for",
    "spec_from_dict", "spec_from_json",
)
