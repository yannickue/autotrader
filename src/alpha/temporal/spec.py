# ruff: noqa: E501
"""State-machine strategy spec for the V2 temporal engine (Phase 0, frozen interface).

Design: docs/V2_TEMPORAL_ENGINE.md section 2 (spec) and section 5 (canonical form).
Pure data + validation + hashing; no evaluation. All names resolve through
``alpha.events.schema``. Specs are authored in the LONG frame; SHORT specs exist only as
``mirror()`` output (they carry ``mirrored_from`` metadata).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Any

from alpha.events import schema as ev

# ---- bounds (design section 2) -------------------------------------------------------------
MAX_STATES = 6
MAX_STATE_CLAUSES = 3  # trigger + guards per transition, also anchor clauses
MAX_INVALIDATE = 2
MAX_TF = 4
MAX_CONTEXT = 3
MAX_REG = 4
MAX_WITHIN = 48
MAX_EXPIRES = 96
MAX_TRANSITIONS = MAX_STATES - 1  # anchor is state 0

SCHEMA = "temporal"
SCHEMA_VERSION = 1
CANONICAL_DOMAIN = b"temporal-v1:"

# ---- grids ---------------------------------------------------------------------------------
WITHIN_GRID: tuple[int, ...] = (2, 3, 5, 8, 12, 24, 48)
TOL_GRID: tuple[float, ...] = ev.TOL_GRID
Q_RANGE = (0.05, 0.95, 0.05)
BUFFER_RANGE = (0.0, 1.0, 0.05)
ATR_MULT_RANGE = (0.25, 4.0, 0.25)
MAX_RISK_RANGE = (0.25, 6.0, 0.25)
R_RANGE = (1.0, 4.0, 0.25)
MIN_SPACE_RANGE = (0.5, 3.0, 0.25)
SESSION_STEP = 5

REGS: tuple[str, ...] = tuple(f"R{i}" for i in range(MAX_REG))
CLAUSE_KINDS = ("event", "state", "feature", "bound")
OPS = ("IS", "NOT", "HOLD", "BEFORE", "SINCE_ENTER")
CAPTURE_SOURCES = (
    "evl",
    "evx",
    "bar_low",
    "bar_high",
    "close",
    "min_low_since_enter",
    "max_high_since_enter",
    "lv",
)
STOP_KINDS = ("register", "swing", "zone_edge", "atr")
TARGET_KINDS = ("fixed_r", "next_structure")
DIRECTIONS = ("LONG", "SHORT")
_SOURCE_MIRROR = {
    "bar_low": "bar_high",
    "bar_high": "bar_low",
    "min_low_since_enter": "max_high_since_enter",
    "max_high_since_enter": "min_low_since_enter",
}
_MIRROR_SUFFIX = "~S"
_MIRRORED_KEY = "mirrored_from"


# ---- helpers -------------------------------------------------------------------------------
def snap(v: float, step: float) -> float:
    return round(round(v / step) * step, 6)


def snap_within(v: int) -> int:
    return min(WITHIN_GRID, key=lambda g: (abs(g - v), g))


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _num(x: object, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise ValueError(f"{what} must be a finite number, got {x!r}")
    return float(x)


def _on_grid(v: float, rng: tuple[float, float, float], what: str) -> None:
    lo, hi, step = rng
    if not (lo - 1e-9 <= v <= hi + 1e-9):
        raise ValueError(f"{what}={v} outside [{lo}, {hi}]")
    if abs(v - snap(v, step)) > 1e-9:
        raise ValueError(f"{what}={v} not on grid step {step}")


def _tuple_of(x: object, cls: type, what: str) -> tuple:
    if not isinstance(x, tuple) or not all(isinstance(i, cls) for i in x):
        raise ValueError(f"{what} must be a tuple of {cls.__name__}")
    return x


def _only(d: dict, allowed: set[str], what: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise ValueError(f"{what}: unknown keys {sorted(extra)}")


def _set(obj: object, name: str, value: object) -> None:
    object.__setattr__(obj, name, value)


# ---- Clause --------------------------------------------------------------------------------
@dataclass(frozen=True)
class Clause:
    kind: str
    name: str
    tf: str
    op: str = "IS"
    arg: int = 0
    reg: str = ""
    tol_atr: float = 0.0
    cmp: str = ""
    q: float = 0.0
    variant: str = ""
    # feature clauses only: compare against -thr(q) instead of thr(q) (exact PRICE mirror of an
    # antisymmetric feature: LONG ``gt thr(q)`` <-> SHORT ``lt -thr(q)``; the threshold key stays (name, q)).
    neg: bool = False

    def __post_init__(self) -> None:
        if self.kind not in CLAUSE_KINDS:
            raise ValueError(f"clause kind {self.kind!r}")
        if self.op not in OPS:
            raise ValueError(f"clause op {self.op!r}")
        if self.tf not in ev.TIMEFRAMES:
            raise ValueError(f"clause tf {self.tf!r}")
        if not _is_int(self.arg):
            raise ValueError("clause arg must be int")
        _set(self, "tol_atr", _num(self.tol_atr, "tol_atr"))
        _set(self, "q", _num(self.q, "q"))
        if not isinstance(self.neg, bool):
            raise ValueError("neg must be a bool")
        if self.kind == "feature":
            self._check_feature()
            return
        if self.cmp != "" or self.q != 0.0 or self.neg:
            raise ValueError("cmp/q/neg are only valid on feature clauses")
        try:
            d = ev.get(self.name)
        except KeyError as exc:
            raise ValueError(str(exc)) from None
        if self.kind == "bound":
            if not d.bound_only:
                raise ValueError(f"{self.name} cannot be a bound clause")
            if self.reg not in REGS:
                raise ValueError(f"bound clause needs a register in {REGS}, got {self.reg!r}")
            if self.tol_atr not in TOL_GRID:
                raise ValueError(f"tol_atr {self.tol_atr} not in {TOL_GRID}")
        else:
            if d.bound_only or d.kind != {"event": "pulse", "state": "state"}[self.kind]:
                raise ValueError(f"{self.name} is not a resolvable {self.kind} clause")
            if self.reg != "" or self.tol_atr != 0.0:
                raise ValueError("reg/tol_atr are only valid on bound clauses")
        if self.tf not in d.tfs:
            raise ValueError(f"{self.name} not available on tf {self.tf}")
        d.parse_variant(self.variant)
        if d.kind == "pulse" and self.op == "HOLD":
            raise ValueError("pulse + HOLD is invalid")
        if d.kind == "state" and self.op in ("BEFORE", "SINCE_ENTER"):
            raise ValueError(f"state + {self.op} is invalid")
        if self.op in ("HOLD", "BEFORE"):
            if not 1 <= self.arg <= MAX_WITHIN:
                raise ValueError(f"{self.op} arg must be in [1, {MAX_WITHIN}]")
        elif self.arg != 0:
            raise ValueError(f"op {self.op} takes no arg")

    def _check_feature(self) -> None:
        if self.name not in ev.FEATURE_MIRROR:
            raise ValueError(f"unknown feature {self.name!r}")
        if self.cmp not in ("gt", "lt"):
            raise ValueError("feature clause needs cmp in gt/lt")
        _on_grid(self.q, Q_RANGE, "q")
        if self.neg and ev.feature_mirror(self.name) != "neg":
            raise ValueError(f"neg is only valid on antisymmetric features, not {self.name!r}")
        if self.op != "IS" or self.arg != 0 or self.reg != "" or self.tol_atr != 0.0:
            raise ValueError("feature clause: op IS, no arg/reg/tol_atr")
        if self.variant != "":
            raise ValueError("feature clause takes no variant")

    def key(self) -> tuple:
        return (self.kind, self.name, self.tf, self.op, self.arg, self.reg, self.tol_atr,
                self.cmp, self.q, self.variant, self.neg)

    def to_dict(self) -> dict[str, Any]:
        d = {"kind": self.kind, "name": self.name, "tf": self.tf, "op": self.op,
             "arg": self.arg, "reg": self.reg, "tol_atr": self.tol_atr, "cmp": self.cmp,
             "q": self.q, "variant": self.variant}
        if self.neg:  # omitted when False: hashes of every non-negated clause are unchanged
            d["neg"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Clause:
        _only(d, {f for f in cls.__dataclass_fields__}, "Clause")
        return cls(**d)


# ---- Capture -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Capture:
    reg: str
    source: str
    of: str = ""

    def __post_init__(self) -> None:
        if self.reg not in REGS:
            raise ValueError(f"capture reg {self.reg!r}")
        if self.source not in CAPTURE_SOURCES:
            raise ValueError(f"capture source {self.source!r}")
        if self.source in ("evl", "evx"):
            try:
                d = ev.get(self.of)
            except KeyError:
                raise ValueError(f"capture of {self.of!r} unknown") from None
            if d.bound_only or self.source not in d.produces:
                raise ValueError(f"{self.of} does not produce {self.source}")
        elif self.source == "lv":
            try:
                d = ev.get(self.of)
            except KeyError:
                raise ValueError(f"capture of {self.of!r} unknown") from None
            if d.kind != "level":
                raise ValueError(f"{self.of} is not a level event")
        elif self.of != "":
            raise ValueError(f"source {self.source} takes no 'of'")

    def to_dict(self) -> dict[str, Any]:
        return {"reg": self.reg, "source": self.source, "of": self.of}

    @classmethod
    def from_dict(cls, d: dict) -> Capture:
        _only(d, {"reg", "source", "of"}, "Capture")
        return cls(**d)


# ---- Transition ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Transition:
    trigger: Clause
    within: int | None = None
    min_gap: int = 1
    guards: tuple[Clause, ...] = ()
    invalidate: tuple[Clause, ...] = ()
    capture: tuple[Capture, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, Clause):
            raise ValueError("trigger must be a Clause")
        if self.trigger.kind == "feature" or self.trigger.op == "NOT":
            raise ValueError("trigger must be an event/state/bound clause and not NOT")
        _tuple_of(self.guards, Clause, "guards")
        _tuple_of(self.invalidate, Clause, "invalidate")
        _tuple_of(self.capture, Capture, "capture")
        if self.within is not None and (
            not _is_int(self.within) or not 1 <= self.within <= MAX_WITHIN
        ):
            raise ValueError(f"within must be None or in [1, {MAX_WITHIN}]")
        if self.min_gap != 1 or not _is_int(self.min_gap):
            raise ValueError("min_gap must be 1 (no same-bar chaining)")
        if 1 + len(self.guards) > MAX_STATE_CLAUSES:
            raise ValueError(f"trigger + guards must be <= {MAX_STATE_CLAUSES}")
        if len(self.invalidate) > MAX_INVALIDATE:
            raise ValueError(f"invalidate must be <= {MAX_INVALIDATE}")
        if len(self.capture) > MAX_REG or len({c.reg for c in self.capture}) != len(self.capture):
            raise ValueError("captures: at most MAX_REG, distinct registers")

    def to_dict(self) -> dict[str, Any]:
        return {"trigger": self.trigger.to_dict(), "within": self.within, "min_gap": self.min_gap,
                "guards": [c.to_dict() for c in self.guards],
                "invalidate": [c.to_dict() for c in self.invalidate],
                "capture": [c.to_dict() for c in self.capture]}

    @classmethod
    def from_dict(cls, d: dict) -> Transition:
        _only(d, {f for f in cls.__dataclass_fields__}, "Transition")
        return cls(
            trigger=Clause.from_dict(d["trigger"]), within=d.get("within"),
            min_gap=d.get("min_gap", 1),
            guards=tuple(Clause.from_dict(c) for c in d.get("guards", ())),
            invalidate=tuple(Clause.from_dict(c) for c in d.get("invalidate", ())),
            capture=tuple(Capture.from_dict(c) for c in d.get("capture", ())),
        )


# ---- StopRule / TargetRule -----------------------------------------------------------------
@dataclass(frozen=True)
class StopRule:
    kind: str
    reg: str = ""
    of: str = ""
    buffer_atr: float = 0.0
    atr_mult: float = 0.0
    max_risk_atr: float = 3.0
    tf: str = "M5"  # only meaningful for kind=swing (design gap: swing tf was unspecified)

    def __post_init__(self) -> None:
        if self.kind not in STOP_KINDS:
            raise ValueError(f"stop kind {self.kind!r}")
        for f in ("buffer_atr", "atr_mult", "max_risk_atr"):
            _set(self, f, _num(getattr(self, f), f))
        if self.tf not in ev.TIMEFRAMES:
            raise ValueError(f"stop tf {self.tf!r}")
        _on_grid(self.max_risk_atr, MAX_RISK_RANGE, "max_risk_atr")
        if self.kind in ("register", "zone_edge"):
            if self.reg not in REGS or self.of != "" or self.atr_mult != 0.0 or self.tf != "M5":
                raise ValueError(f"{self.kind} stop: reg only")
        elif self.kind == "swing":
            if self.reg != "" or self.atr_mult != 0.0:
                raise ValueError("swing stop: of/tf/buffer only")
            try:
                d = ev.get(self.of)
            except KeyError:
                raise ValueError(f"swing stop of {self.of!r} unknown") from None
            if self.of not in ("SWING_LOW_LVL", "SWING_HIGH_LVL") or self.tf not in d.tfs:
                raise ValueError("swing stop needs SWING_*_LVL on an allowed tf")
        else:  # atr
            if self.reg != "" or self.of != "" or self.buffer_atr != 0.0 or self.tf != "M5":
                raise ValueError("atr stop: atr_mult only")
            _on_grid(self.atr_mult, ATR_MULT_RANGE, "atr_mult")
        if self.kind != "atr":
            _on_grid(self.buffer_atr, BUFFER_RANGE, "buffer_atr")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reg": self.reg, "of": self.of, "buffer_atr": self.buffer_atr,
                "atr_mult": self.atr_mult, "max_risk_atr": self.max_risk_atr, "tf": self.tf}

    @classmethod
    def from_dict(cls, d: dict) -> StopRule:
        _only(d, {f for f in cls.__dataclass_fields__}, "StopRule")
        return cls(**d)


@dataclass(frozen=True)
class TargetRule:
    kind: str
    r: float = 0.0
    levels: tuple[str, ...] = ()
    fallback_r: float = 0.0
    min_space_r: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in TARGET_KINDS:
            raise ValueError(f"target kind {self.kind!r}")
        for f in ("r", "fallback_r", "min_space_r"):
            _set(self, f, _num(getattr(self, f), f))
        if not isinstance(self.levels, tuple) or not all(isinstance(x, str) for x in self.levels):
            raise ValueError("levels must be a tuple of str")
        if self.kind == "fixed_r":
            if self.levels or self.fallback_r != 0.0 or self.min_space_r != 0.0:
                raise ValueError("fixed_r target: r only")
            _on_grid(self.r, R_RANGE, "r")
        else:
            if self.r != 0.0:
                raise ValueError("next_structure target: r must be 0")
            if not 1 <= len(self.levels) <= 4 or len(set(self.levels)) != len(self.levels):
                raise ValueError("next_structure needs 1..4 distinct levels")
            if list(self.levels) != sorted(self.levels):
                raise ValueError("levels must be sorted")
            for lv in self.levels:
                if lv not in ev.TARGET_LEVELS:
                    raise ValueError(f"unknown target level {lv!r}")
            _on_grid(self.fallback_r, R_RANGE, "fallback_r")
            _on_grid(self.min_space_r, MIN_SPACE_RANGE, "min_space_r")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "r": self.r, "levels": list(self.levels),
                "fallback_r": self.fallback_r, "min_space_r": self.min_space_r}

    @classmethod
    def from_dict(cls, d: dict) -> TargetRule:
        _only(d, {f for f in cls.__dataclass_fields__}, "TargetRule")
        return cls(**{**d, "levels": tuple(d.get("levels", ()))})


# ---- Spec ----------------------------------------------------------------------------------
Pairs = tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class StateMachineStrategySpec:
    strategy_id: str
    version: int
    direction: str
    anchor: tuple[Clause, ...]
    anchor_capture: tuple[Capture, ...]
    states: tuple[Transition, ...]  # transitions T1..Tn; state 0 is the anchor
    context: tuple[Clause, ...]
    expires_after: int
    session_window: tuple[int, int] | None
    stop: StopRule
    target: TargetRule
    params: Pairs = ()  # fitted parameters (name, finite float), sorted by name
    metadata: Pairs = ()  # (key, str) bookkeeping, sorted by key

    def __post_init__(self) -> None:
        validate(self)

    # -- serialisation --
    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id, "version": self.version, "direction": self.direction,
            "anchor": [c.to_dict() for c in self.anchor],
            "anchor_capture": [c.to_dict() for c in self.anchor_capture],
            "states": [t.to_dict() for t in self.states],
            "context": [c.to_dict() for c in self.context],
            "expires_after": self.expires_after,
            "session_window": None if self.session_window is None else list(self.session_window),
            "stop": self.stop.to_dict(), "target": self.target.to_dict(),
            "params": {k: v for k, v in self.params},
            "metadata": {k: v for k, v in self.metadata},
        }

    @classmethod
    def from_dict(cls, d: dict) -> StateMachineStrategySpec:
        _only(d, {f for f in cls.__dataclass_fields__}, "StateMachineStrategySpec")
        sw = d.get("session_window")
        return cls(
            strategy_id=d["strategy_id"], version=d["version"], direction=d["direction"],
            anchor=tuple(Clause.from_dict(c) for c in d["anchor"]),
            anchor_capture=tuple(Capture.from_dict(c) for c in d.get("anchor_capture", ())),
            states=tuple(Transition.from_dict(t) for t in d["states"]),
            context=tuple(Clause.from_dict(c) for c in d.get("context", ())),
            expires_after=d["expires_after"],
            session_window=None if sw is None else (sw[0], sw[1]),
            stop=StopRule.from_dict(d["stop"]), target=TargetRule.from_dict(d["target"]),
            params=tuple(sorted((k, float(v)) for k, v in d.get("params", {}).items())),
            metadata=tuple(sorted(d.get("metadata", {}).items())),
        )

    def _payload(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, **self.to_dict()}

    def to_json(self) -> str:
        return json.dumps(self._payload(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    def spec_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()


# ---- validate ------------------------------------------------------------------------------
def _clause_tfs(spec: StateMachineStrategySpec) -> set[str]:
    tfs = {c.tf for c in (*spec.anchor, *spec.context)}
    for t in spec.states:
        tfs.update(c.tf for c in (t.trigger, *t.guards, *t.invalidate))
    if spec.stop.kind == "swing":
        tfs.add(spec.stop.tf)
    if spec.target.kind == "next_structure":
        tfs.update(ev.TARGET_LEVELS[lv][0] for lv in spec.target.levels)
    return tfs


def _check_capture_source(
    cap: Capture, clauses: tuple[Clause, ...], *, allow_since_enter: bool, where: str
) -> None:
    """``of`` must be backed by an event clause that is true on the capture bar."""
    if cap.source in ("min_low_since_enter", "max_high_since_enter") and not allow_since_enter:
        raise ValueError(f"{where}: {cap.source} needs an enter bar")
    if cap.source in ("evl", "evx"):
        if not any(c.kind == "event" and c.name == cap.of and c.op == "IS" for c in clauses):
            raise ValueError(f"{where}: {cap.source} of {cap.of} not backed by an IS event clause")
    elif cap.source == "lv":
        ok = any(
            c.kind == "event" and c.op == "IS" and cap.of in ev.get(c.name).exposes for c in clauses
        )
        if not ok:
            raise ValueError(f"{where}: lv {cap.of} not exposed by an IS event clause")


def validate(spec: StateMachineStrategySpec) -> None:
    """Enforce all section-2 rules. Raises ValueError on any violation."""
    if not isinstance(spec.strategy_id, str) or not spec.strategy_id:
        raise ValueError("strategy_id must be a non-empty str")
    if not _is_int(spec.version) or spec.version < 1:
        raise ValueError("version must be an int >= 1")
    if spec.direction not in DIRECTIONS:
        raise ValueError(f"direction {spec.direction!r}")
    for f in ("anchor", "context"):
        _tuple_of(getattr(spec, f), Clause, f)
    _tuple_of(spec.anchor_capture, Capture, "anchor_capture")
    _tuple_of(spec.states, Transition, "states")
    for f in ("params", "metadata"):
        if not isinstance(getattr(spec, f), tuple):
            raise ValueError(f"{f} must be a tuple of pairs")
    meta = dict(spec.metadata)
    if len(meta) != len(spec.metadata) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in spec.metadata
    ):
        raise ValueError("metadata must be unique (str, str) pairs")
    if spec.direction == "SHORT" and _MIRRORED_KEY not in meta:
        raise ValueError("SHORT specs exist only as mirror() output (LONG-frame names only)")
    names = [k for k, _ in spec.params]
    if len(set(names)) != len(names) or not all(
        isinstance(k, str) and isinstance(v, float) and math.isfinite(v) for k, v in spec.params
    ):
        raise ValueError("params must be unique (str, finite float) pairs")
    if not 1 <= len(spec.anchor) <= MAX_STATE_CLAUSES:
        raise ValueError(f"anchor needs 1..{MAX_STATE_CLAUSES} clauses")
    for c in spec.anchor:
        if c.kind == "bound" or c.op == "SINCE_ENTER":
            raise ValueError("anchor clauses cannot be bound or SINCE_ENTER")
    if not 1 <= len(spec.states) <= MAX_TRANSITIONS:
        raise ValueError(f"need 1..{MAX_TRANSITIONS} transitions")
    if len(spec.context) > MAX_CONTEXT:
        raise ValueError(f"context must be <= {MAX_CONTEXT}")
    for c in spec.context:
        if c.kind == "bound" or c.op == "SINCE_ENTER":
            raise ValueError("context clauses cannot be bound or SINCE_ENTER")
    n_states = len(spec.states) + 1
    if not _is_int(spec.expires_after) or not n_states <= spec.expires_after <= MAX_EXPIRES:
        raise ValueError(f"expires_after must be in [{n_states}, {MAX_EXPIRES}]")
    sw = spec.session_window
    if sw is not None and (
        not isinstance(sw, tuple) or len(sw) != 2 or not all(_is_int(x) for x in sw)
        or not 0 <= sw[0] < sw[1] <= 1440 or sw[0] % SESSION_STEP or sw[1] % SESSION_STEP
    ):
        raise ValueError("session_window must be (start, end) minutes, step 5, 0<=s<e<=1440")
    if len(_clause_tfs(spec)) > MAX_TF:
        raise ValueError(f"more than {MAX_TF} distinct timeframes")

    # typed register dataflow (single type: price; single assignment)
    captured: dict[str, Capture] = {}

    def add_captures(caps: tuple[Capture, ...], clauses: tuple[Clause, ...], *, enter: bool, where: str):
        for cap in caps:
            _check_capture_source(cap, clauses, allow_since_enter=enter, where=where)
        for cap in caps:
            if cap.reg in captured:
                raise ValueError(f"{where}: register {cap.reg} captured twice")
            captured[cap.reg] = cap
        if len(captured) > MAX_REG:
            raise ValueError(f"more than {MAX_REG} registers")

    add_captures(spec.anchor_capture, spec.anchor, enter=False, where="anchor")
    for i, t in enumerate(spec.states, start=1):
        where = f"T{i}"
        for c in (t.trigger, *t.guards, *t.invalidate):
            if c.kind == "bound" and c.reg not in captured:
                raise ValueError(f"{where}: reads register {c.reg} before it is captured")
        same_bar = tuple(c for c in (t.trigger, *t.guards))
        add_captures(t.capture, same_bar, enter=True, where=where)

    stop = spec.stop
    if stop.kind in ("register", "zone_edge") and stop.reg not in captured:
        raise ValueError(f"stop reads register {stop.reg} that is never captured")
    if stop.kind == "zone_edge":
        cap = captured[stop.reg]
        want = "ZONE_LO" if spec.direction == "LONG" else "ZONE_HI"
        if cap.source != "lv" or cap.of != want:
            raise ValueError(f"zone_edge stop needs a register captured from lv {want}")
    if stop.kind == "swing":
        want = "SWING_LOW_LVL" if spec.direction == "LONG" else "SWING_HIGH_LVL"
        if stop.of != want:
            raise ValueError(f"swing stop must use {want} for a {spec.direction} spec")
    tgt = spec.target
    if tgt.kind == "next_structure":
        suffix = ("high", "pdh") if spec.direction == "LONG" else ("low", "pdl")
        if not all(lv.endswith(suffix) for lv in tgt.levels):
            raise ValueError(f"target levels must be on the {spec.direction} side")


# ---- mirror --------------------------------------------------------------------------------
def _mirror_clause(c: Clause) -> Clause:
    if c.kind == "feature":
        if ev.feature_mirror(c.name) == "self":  # positive-only feature: same test in both directions
            return c
        # antisymmetric feature: exact price mirror  gt thr(q) <-> lt -thr(q)  (same q key, sign flipped)
        return replace(c, cmp="lt" if c.cmp == "gt" else "gt", neg=not c.neg)
    return replace(c, name=ev.mirror_event(c.name), variant=ev.mirror_variant(c.name, c.variant))


def _mirror_capture(c: Capture) -> Capture:
    if c.source in _SOURCE_MIRROR:
        return replace(c, source=_SOURCE_MIRROR[c.source])
    if c.source in ("evl", "evx", "lv"):
        return replace(c, of=ev.mirror_event(c.of))
    return c


def mirror(spec: StateMachineStrategySpec) -> StateMachineStrategySpec:
    """LONG <-> SHORT twin via the registry mirror table. Involutive; raises if a mirror is missing."""
    meta = dict(spec.metadata)
    if spec.direction == "LONG":
        direction, sid = "SHORT", spec.strategy_id + _MIRROR_SUFFIX
        meta[_MIRRORED_KEY] = spec.spec_hash()
    else:
        direction = "LONG"
        sid = spec.strategy_id.removesuffix(_MIRROR_SUFFIX)
        meta.pop(_MIRRORED_KEY, None)
    stop = spec.stop
    if stop.kind == "swing":
        stop = replace(stop, of=ev.mirror_event(stop.of))
    tgt = spec.target
    if tgt.kind == "next_structure":
        tgt = replace(tgt, levels=tuple(sorted(ev.target_level(lv)[1] for lv in tgt.levels)))
    return StateMachineStrategySpec(
        strategy_id=sid, version=spec.version, direction=direction,
        anchor=tuple(_mirror_clause(c) for c in spec.anchor),
        anchor_capture=tuple(_mirror_capture(c) for c in spec.anchor_capture),
        states=tuple(
            Transition(
                trigger=_mirror_clause(t.trigger), within=t.within, min_gap=t.min_gap,
                guards=tuple(_mirror_clause(c) for c in t.guards),
                invalidate=tuple(_mirror_clause(c) for c in t.invalidate),
                capture=tuple(_mirror_capture(c) for c in t.capture),
            )
            for t in spec.states
        ),
        context=tuple(_mirror_clause(c) for c in spec.context),
        expires_after=spec.expires_after, session_window=spec.session_window,
        stop=stop, target=tgt, params=spec.params, metadata=tuple(sorted(meta.items())),
    )


# ---- complexity / canonical form -----------------------------------------------------------
def complexity(spec: StateMachineStrategySpec) -> int:
    """states + clauses + guards + invalidates + distinct tf + fitted params."""
    states = len(spec.states) + 1
    clauses = len(spec.anchor) + len(spec.context) + len(spec.states)
    guards = sum(len(t.guards) for t in spec.states)
    invalidates = sum(len(t.invalidate) for t in spec.states)
    return states + clauses + guards + invalidates + len(_clause_tfs(spec)) + len(spec.params)


def _dedup_sorted(cs: tuple[Clause, ...]) -> tuple[Clause, ...]:
    return tuple(sorted({c.key(): c for c in cs}.values(), key=Clause.key))


def _rename_clause(c: Clause, m: dict[str, str]) -> Clause:
    return replace(c, reg=m.get(c.reg, c.reg)) if c.kind == "bound" else c


def _snap_clause(c: Clause) -> Clause:
    if c.kind == "feature":
        return replace(c, q=snap(min(max(c.q, Q_RANGE[0]), Q_RANGE[1]), Q_RANGE[2]))
    if c.kind == "bound":
        return replace(c, tol_atr=min(TOL_GRID, key=lambda g: (abs(g - c.tol_atr), g)))
    return c


def _canon_once(spec: StateMachineStrategySpec) -> StateMachineStrategySpec:
    exp = spec.expires_after
    # 1) per-transition: snap, clamp within, drop redundant guards, sort sets, dedup captures
    alias: dict[str, str] = {}  # dropped duplicate reg -> kept reg
    anchor_caps = _dedup_captures(spec.anchor_capture, alias)
    trans: list[Transition] = []
    for t in spec.states:
        caps = _dedup_captures(t.capture, alias)
        within = t.within
        if within is not None:
            within = min(snap_within(within), exp)
        elif exp <= MAX_WITHIN:
            within = exp
        trig = _snap_clause(t.trigger)
        guards = tuple(g for g in map(_snap_clause, t.guards) if g.key() != trig.key())
        trans.append(Transition(
            trigger=trig, within=within, min_gap=1, guards=_dedup_sorted(guards),
            invalidate=_dedup_sorted(tuple(map(_snap_clause, t.invalidate))), capture=caps,
        ))
    trans = [
        replace(t, trigger=_rename_clause(t.trigger, alias),
                guards=tuple(_rename_clause(c, alias) for c in t.guards),
                invalidate=tuple(_rename_clause(c, alias) for c in t.invalidate))
        for t in trans
    ]
    stop = spec.stop
    if stop.reg:
        stop = replace(stop, reg=alias.get(stop.reg, stop.reg))
    # 2) drop captures nobody reads
    reads = {c.reg for t in trans for c in (t.trigger, *t.guards, *t.invalidate) if c.kind == "bound"}
    if stop.reg:
        reads.add(stop.reg)
    anchor_caps = tuple(c for c in anchor_caps if c.reg in reads)
    trans = [replace(t, capture=tuple(c for c in t.capture if c.reg in reads)) for t in trans]
    # 3) rename registers in first-capture order
    order = [c.reg for c in anchor_caps] + [c.reg for t in trans for c in t.capture]
    rename = {old: REGS[i] for i, old in enumerate(order)}

    def rc(c: Capture) -> Capture:
        return replace(c, reg=rename[c.reg])

    anchor_caps = tuple(map(rc, anchor_caps))
    trans = [
        Transition(
            trigger=_rename_clause(t.trigger, rename), within=t.within, min_gap=1,
            guards=_dedup_sorted(tuple(_rename_clause(c, rename) for c in t.guards)),
            invalidate=_dedup_sorted(tuple(_rename_clause(c, rename) for c in t.invalidate)),
            capture=tuple(map(rc, t.capture)),
        )
        for t in trans
    ]
    if stop.reg:
        stop = replace(stop, reg=rename[stop.reg])
    sw = spec.session_window
    if sw == (0, 1440):
        sw = None
    return replace(
        spec, anchor=_dedup_sorted(tuple(map(_snap_clause, spec.anchor))),
        anchor_capture=anchor_caps, states=tuple(trans),
        context=_dedup_sorted(tuple(map(_snap_clause, spec.context))),
        session_window=sw, stop=stop, params=tuple(sorted(spec.params)),
        metadata=tuple(sorted(spec.metadata)),
    )


def _dedup_captures(caps: tuple[Capture, ...], alias: dict[str, str]) -> tuple[Capture, ...]:
    """Sort by (source, of); merge captures reading the same quantity on the same bar."""
    out: dict[tuple[str, str], Capture] = {}
    for c in sorted(caps, key=lambda c: (c.source, c.of, c.reg)):
        k = (c.source, c.of)
        if k in out:
            alias[c.reg] = out[k].reg
        else:
            out[k] = c
    return tuple(out.values())


def canonicalize(spec: StateMachineStrategySpec) -> StateMachineStrategySpec:
    """Canonical form (fixed point of ``_canon_once``); idempotent."""
    cur = spec
    for _ in range(16):
        nxt = _canon_once(cur)
        if nxt == cur:
            return cur
        cur = nxt
    raise RuntimeError("canonicalize did not converge")  # pragma: no cover


def canonical_payload(spec: StateMachineStrategySpec) -> dict[str, Any]:
    d = canonicalize(spec).to_dict()
    for k in ("strategy_id", "version", "metadata", "params"):
        d.pop(k)  # identity/bookkeeping, not behaviour
    return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, **d}


def canonical_hash(spec: StateMachineStrategySpec) -> str:
    blob = json.dumps(canonical_payload(spec), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(CANONICAL_DOMAIN + blob.encode()).hexdigest()
