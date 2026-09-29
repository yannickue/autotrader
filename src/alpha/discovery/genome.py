"""Plain-data, hashable, JSON-serialisable strategy genome with hard complexity limits.

A genome is written in the LONG frame.  ``direction == "SHORT"`` means "the mirror of these
clauses" (see ``catalog.Mirror``); compilation applies the mirror so both directions share
identical semantics.  Quantiles ``q`` are resolved against TRAIN values only at compile time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from alpha.discovery.catalog import (
    CATALOG,
    STOP_LEVELS,
    STOP_MULT_DOMAIN,
    STOP_OFFSET_DOMAIN,
    TARGET_R_DOMAIN,
    TIME_DOMAIN,
)

MAX_REGIME = 2
MAX_CONTEXT = 2
MAX_TRIGGER = 3
MAX_OR = 2
MAX_TOTAL_CLAUSES = 6
MIN_WINDOW_MIN = 30
_EPS = 1e-9


class GenomeError(ValueError):
    """Raised by ``validate`` for a structurally invalid genome."""


@dataclass(frozen=True)
class Clause:
    feature: str  # catalog entry name
    op: str
    q: float | None = None  # TRAIN quantile for continuous/signed entries
    labels: tuple[str, ...] = ()  # regime label clauses

    def to_dict(self) -> dict[str, Any]:
        return {"feature": self.feature, "op": self.op, "q": self.q, "labels": list(self.labels)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Clause:
        return cls(raw["feature"], raw["op"], raw.get("q"), tuple(raw.get("labels", ())))


@dataclass(frozen=True)
class StopGene:
    kind: str = "atr_multiple"  # atr_multiple | last_swing | session_level
    multiple: float | None = 1.5
    level: str | None = None  # LONG-frame level (mirrored for SHORT)
    offset: float = 0.0  # points beyond the level

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "multiple": self.multiple, "level": self.level,
                "offset": self.offset}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StopGene:
        return cls(raw["kind"], raw.get("multiple"), raw.get("level"), raw.get("offset", 0.0))


@dataclass(frozen=True)
class Genome:
    direction: str  # LONG | SHORT
    regime: tuple[Clause, ...] = ()
    context: tuple[Clause, ...] = ()
    trigger: tuple[Clause, ...] = ()
    or_group: tuple[Clause, ...] = ()
    time_window: tuple[int, int] | None = None  # [start, end) Berlin minutes
    stop: StopGene = field(default_factory=StopGene)
    target_r: float = 2.0
    lineage: str = "HYBRID"  # provenance only; excluded from the canonical hash

    def to_dict(self, *, with_lineage: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "direction": self.direction,
            "regime": [c.to_dict() for c in self.regime],
            "context": [c.to_dict() for c in self.context],
            "trigger": [c.to_dict() for c in self.trigger],
            "or_group": [c.to_dict() for c in self.or_group],
            "time_window": list(self.time_window) if self.time_window else None,
            "stop": self.stop.to_dict(),
            "target_r": self.target_r,
        }
        if with_lineage:
            out["lineage"] = self.lineage
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Genome:
        window = raw.get("time_window")
        return cls(
            raw["direction"],
            tuple(Clause.from_dict(c) for c in raw.get("regime", ())),
            tuple(Clause.from_dict(c) for c in raw.get("context", ())),
            tuple(Clause.from_dict(c) for c in raw.get("trigger", ())),
            tuple(Clause.from_dict(c) for c in raw.get("or_group", ())),
            (int(window[0]), int(window[1])) if window else None,
            StopGene.from_dict(raw["stop"]),
            float(raw["target_r"]),
            raw.get("lineage", "HYBRID"),
        )

    @classmethod
    def from_json(cls, text: str) -> Genome:
        return cls.from_dict(json.loads(text))

    @property
    def clauses(self) -> tuple[Clause, ...]:
        return self.regime + self.context + self.trigger + self.or_group


def total_clauses(g: Genome) -> int:
    return len(g.regime) + len(g.context) + len(g.trigger) + len(g.or_group)


def complexity(g: Genome) -> int:
    """clauses + 1 for an OR group + 1 for a non-default (non-ATR) stop kind."""
    return total_clauses(g) + (1 if g.or_group else 0) + (1 if g.stop.kind != "atr_multiple" else 0)


def _check_clause(c: Clause, layers: tuple[str, ...], where: str) -> None:
    entry = CATALOG.get(c.feature)
    if entry is None or entry.kind == "time":
        raise GenomeError(f"{where}: unknown feature {c.feature!r}")
    if entry.layer not in layers:
        raise GenomeError(f"{where}: {c.feature} is layer {entry.layer}, expected {layers}")
    if entry.kind == "label":
        if c.op != "in" or not c.labels or c.q is not None:
            raise GenomeError(f"{where}: label clause needs op 'in' and labels only")
        if not set(c.labels) <= set(entry.labels):
            raise GenomeError(f"{where}: unknown labels {c.labels}")
        return
    if c.labels:
        raise GenomeError(f"{where}: labels only valid for regime label features")
    if c.op not in entry.ops:
        raise GenomeError(f"{where}: op {c.op!r} not allowed for {c.feature}")
    if entry.kind in ("continuous", "signed"):
        if c.q is None or not (entry.q_lo - _EPS <= c.q <= entry.q_hi + _EPS):
            raise GenomeError(f"{where}: q {c.q} outside [{entry.q_lo},{entry.q_hi}]")
    elif c.q is not None:
        raise GenomeError(f"{where}: {entry.kind} clause takes no quantile")


def validate(g: Genome) -> Genome:
    """Raise ``GenomeError`` unless the genome respects every hard limit; return it."""
    if g.direction not in ("LONG", "SHORT"):
        raise GenomeError("direction must be LONG or SHORT")
    if len(g.regime) > MAX_REGIME:
        raise GenomeError(f"more than {MAX_REGIME} regime clauses")
    if len(g.context) > MAX_CONTEXT:
        raise GenomeError(f"more than {MAX_CONTEXT} context clauses")
    if len(g.trigger) > MAX_TRIGGER:
        raise GenomeError(f"more than {MAX_TRIGGER} trigger clauses")
    if not g.trigger:
        raise GenomeError("at least one trigger clause is required")
    if len(g.or_group) not in (0, MAX_OR):
        raise GenomeError("OR group must be empty or hold exactly 2 alternatives")
    if total_clauses(g) > MAX_TOTAL_CLAUSES:
        raise GenomeError(f"more than {MAX_TOTAL_CLAUSES} clauses in total")
    for c in g.regime:
        _check_clause(c, ("REGIME",), "regime")
    for c in g.context:
        _check_clause(c, ("CONTEXT",), "context")
    for c in g.trigger:
        _check_clause(c, ("TRIGGER", "LEVEL"), "trigger")
    for c in g.or_group:
        _check_clause(c, ("TRIGGER", "LEVEL"), "or_group")
        if CATALOG[c.feature].kind == "label":
            raise GenomeError("or_group takes rule clauses only")
    if g.time_window is not None:
        start, end = g.time_window
        lo, hi = TIME_DOMAIN
        if not (lo <= start < end <= hi) or end - start < MIN_WINDOW_MIN:
            raise GenomeError(f"invalid time window {g.time_window}")
    stop = g.stop
    if stop.kind == "atr_multiple":
        lo, hi = STOP_MULT_DOMAIN
        if stop.multiple is None or not (lo - _EPS <= stop.multiple <= hi + _EPS):
            raise GenomeError("atr stop multiple outside domain")
    elif stop.kind == "session_level":
        lo, hi = STOP_OFFSET_DOMAIN
        if stop.level not in STOP_LEVELS or not (lo - _EPS <= stop.offset <= hi + _EPS):
            raise GenomeError("session_level stop needs a known level and offset in domain")
    elif stop.kind != "last_swing":
        raise GenomeError(f"unknown stop kind {stop.kind!r}")
    lo, hi = TARGET_R_DOMAIN
    if not (lo - _EPS <= g.target_r <= hi + _EPS):
        raise GenomeError("target_r outside domain")
    return g


def is_valid(g: Genome) -> bool:
    try:
        validate(g)
    except GenomeError:
        return False
    return True


__all__ = (
    "MAX_CONTEXT",
    "MAX_OR",
    "MAX_REGIME",
    "MAX_TOTAL_CLAUSES",
    "MAX_TRIGGER",
    "Clause",
    "Genome",
    "GenomeError",
    "StopGene",
    "complexity",
    "is_valid",
    "total_clauses",
    "validate",
)
