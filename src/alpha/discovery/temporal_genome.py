# ruff: noqa: E501
"""TemporalGenome: the LONG-frame genotype of the V2 temporal search (design section 5).

Research only.  A genome is frozen, hashable and JSON-serialisable.  It carries *genes* only:

* anchor clause genes (``spec.Clause``; event / state / feature),
* 1..5 step genes ``(event gene, within, guard preset id, invalidate preset id)``,
* context clause genes, ``expires_after``, a stop gene, a target gene, a time window, lineage.

Register captures are NOT genes.  ``temporal_compile`` derives them from the event registry
(``produces`` / ``exposes``) and lets every bound clause / stop read the register it needs by
role (``lvl`` = a captured level, ``ext`` = a captured extreme), so the typed register dataflow
of ``spec.validate`` holds by construction for every genome the operators can produce.

The module holds no Validation reference (structural test in tests/test_temporal_discovery_seal.py).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from alpha.events import schema as ev
from alpha.temporal import spec as sp

# ---- bounds / grids --------------------------------------------------------------------------
MAX_STEPS = sp.MAX_TRANSITIONS  # 5 transitions (anchor is state 0)
MAX_ANCHOR = sp.MAX_STATE_CLAUSES
MAX_CONTEXT = sp.MAX_CONTEXT
WITHIN_GRID = sp.WITHIN_GRID
EXPIRES_GRID: tuple[int, ...] = (6, 8, 12, 16, 24, 32, 48, 64, 96)
HOLD_ARGS: tuple[int, ...] = (2, 3, 5, 8, 12)
BEFORE_ARGS: tuple[int, ...] = (3, 5, 8, 12)
BOUND_KS: tuple[int, ...] = (3, 6)
Q_STEP = sp.Q_RANGE[2]
MIN_WINDOW_MIN = 60
CANON_DOMAIN = b"temporal-v1:"
PICKS = ("lvl", "ext", "first")
STOP_KINDS = ("register", "zone_edge", "swing", "atr")
STOP_SWING_TFS = ("M5", "M15")
TARGET_KINDS = sp.TARGET_KINDS
FEATURE_TF = "M5"
GUARD_Q_CHOICES: tuple[float, ...] = (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)

# ---- LONG-frame event catalogue --------------------------------------------------------------
# name -> allowed variants restriction (None = every registry variant).  Only LONG-frame names
# are legal in a genome; SHORT is produced by ``spec.mirror`` at compile time.
LONG_PULSES: dict[str, tuple[str, ...] | None] = {
    "SWEEP_LOW": None,
    "SWING_LOW_CONF": None,
    "SWING_HIGH_CONF": None,
    "BOS_UP": None,
    "CHOCH_UP": None,
    "ZONE_ENTER": None,
    "ZONE_EXIT": None,
    "TRENDLINE_TOUCH": None,
    "TRENDLINE_BREAK": None,
    "PATTERN_COMPLETE": ("double_bottom", "inside_bar_break_up"),
    "MOMENTUM_RESUME_UP": None,
}
LONG_BOUND = ("TOUCH", "BREAK_UP", "BREAK_DN", "RECLAIM_UP", "RETEST_HOLD_UP")
LONG_STATES = ("TREND_UP",)
TOL_BOUND = ("TOUCH", "BREAK_UP", "BREAK_DN", "RETEST_HOLD_UP", "RETEST_HOLD_DN")
K_BOUND = ("RECLAIM_UP", "RECLAIM_DN", "HOLD_ABOVE", "HOLD_BELOW")
GUARD_EVENTS = ("BOS_UP", "MOMENTUM_RESUME_UP", "SWING_LOW_CONF", "CHOCH_UP")
INVALIDATE_EVENTS = ("CHOCH_DN", "BOS_DN")


class GenomeError(ValueError):
    """A genome violates a structural rule or does not compile to a valid spec."""


def long_variants(name: str) -> tuple[str, ...]:
    """Legal variants of ``name`` in the LONG frame (registry order)."""
    d = ev.get(name)
    allowed = LONG_PULSES.get(name)
    vs = d.variants()
    return vs if allowed is None else tuple(v for v in vs if v in allowed)


def _long_names() -> tuple[str, ...]:
    return (*LONG_PULSES, *LONG_BOUND, *LONG_STATES)


@dataclass(frozen=True)
class EventPool:
    """Which (event, tf, variant) triples a search may use (the arrays that really exist)."""

    arrays: frozenset[str] | None = None  # None = the whole registry
    tfs: tuple[str, ...] = ev.TIMEFRAMES

    @classmethod
    def full(cls, tfs: tuple[str, ...] = ev.TIMEFRAMES) -> EventPool:
        return cls(None, tuple(tfs))

    @classmethod
    def from_array_names(cls, names, tfs: tuple[str, ...] = ev.TIMEFRAMES) -> EventPool:
        return cls(frozenset(names), tuple(tfs))

    def available(self, name: str, tf: str, variant: str = "") -> bool:
        if name not in _long_names() and name not in (*GUARD_EVENTS, *INVALIDATE_EVENTS, "TREND_DN"):
            return False
        d = ev.get(name)
        if tf not in self.tfs or tf not in d.tfs:
            return False
        if name in _long_names() and variant not in long_variants(name):
            return False
        if name not in _long_names() and variant not in d.variants():
            return False
        if d.has_arrays and self.arrays is not None:
            try:
                return all(a in self.arrays for a in ev.array_names(name, tf, variant))
            except ValueError:
                return False
        return True

    def feature_ok(self, name: str) -> bool:
        return name in ev.FEATURE_MIRROR


# ---- genes -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class EventGene:
    """A step's trigger: a pulse event, a bound (register-relative) event or a state clause."""

    name: str
    tf: str
    variant: str = ""
    tol: float = 0.0  # ATR tolerance, bound events in TOL_BOUND only
    op: str = "IS"  # IS (pulse/bound/state) or HOLD (state only)
    arg: int = 0

    @property
    def is_bound(self) -> bool:
        return ev.get(self.name).bound_only

    @property
    def kind(self) -> str:
        d = ev.get(self.name)
        return "bound" if d.bound_only else ("event" if d.kind == "pulse" else "state")

    def clause(self, reg: str = "") -> sp.Clause:
        if self.is_bound:
            return sp.Clause("bound", self.name, self.tf, "IS", 0, reg, self.tol, variant=self.variant)
        return sp.Clause(self.kind, self.name, self.tf, self.op, self.arg, variant=self.variant)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "tf": self.tf, "variant": self.variant, "tol": self.tol,
                "op": self.op, "arg": self.arg}

    @classmethod
    def from_dict(cls, d: dict) -> EventGene:
        return cls(**d)


@dataclass(frozen=True)
class StepGene:
    event: EventGene
    within: int = 5
    guard: str = "none"  # preset id (see ``parse_preset``)
    invalidate: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event.to_dict(), "within": self.within, "guard": self.guard,
                "invalidate": self.invalidate}

    @classmethod
    def from_dict(cls, d: dict) -> StepGene:
        return cls(EventGene.from_dict(d["event"]), d["within"], d["guard"], d["invalidate"])


@dataclass(frozen=True)
class StopGene:
    kind: str = "register"  # register | zone_edge | swing | atr
    pick: str = "ext"  # register role for kind=register: lvl | ext | first ('' otherwise)
    buffer_atr: float = 0.1
    atr_mult: float = 0.0
    max_risk_atr: float = 3.0
    tf: str = "M5"  # swing tf

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "pick": self.pick, "buffer_atr": self.buffer_atr,
                "atr_mult": self.atr_mult, "max_risk_atr": self.max_risk_atr, "tf": self.tf}

    @classmethod
    def from_dict(cls, d: dict) -> StopGene:
        return cls(**d)


@dataclass(frozen=True)
class TargetGene:
    kind: str = "fixed_r"
    r: float = 2.0
    levels: tuple[str, ...] = ()
    fallback_r: float = 0.0
    min_space_r: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "r": self.r, "levels": list(self.levels),
                "fallback_r": self.fallback_r, "min_space_r": self.min_space_r}

    @classmethod
    def from_dict(cls, d: dict) -> TargetGene:
        return cls(**{**d, "levels": tuple(d.get("levels", ()))})


@dataclass(frozen=True)
class TemporalGenome:
    direction: str  # LONG | SHORT (SHORT = mirror of the LONG-frame genes)
    anchor: tuple[sp.Clause, ...]
    steps: tuple[StepGene, ...]
    context: tuple[sp.Clause, ...] = ()
    expires_after: int = 24
    stop: StopGene = StopGene()
    target: TargetGene = TargetGene()
    window: tuple[int, int] | None = None
    lineage: str = "HYBRID"  # provenance only; excluded from the canonical hash

    def to_dict(self, *, with_lineage: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "direction": self.direction,
            "anchor": [c.to_dict() for c in self.anchor],
            "steps": [s.to_dict() for s in self.steps],
            "context": [c.to_dict() for c in self.context],
            "expires_after": self.expires_after,
            "stop": self.stop.to_dict(), "target": self.target.to_dict(),
            "window": None if self.window is None else list(self.window),
        }
        if with_lineage:
            out["lineage"] = self.lineage
        return out

    @classmethod
    def from_dict(cls, d: dict) -> TemporalGenome:
        w = d.get("window")
        return cls(
            direction=d["direction"],
            anchor=tuple(sp.Clause.from_dict(c) for c in d["anchor"]),
            steps=tuple(StepGene.from_dict(s) for s in d["steps"]),
            context=tuple(sp.Clause.from_dict(c) for c in d.get("context", ())),
            expires_after=d["expires_after"], stop=StopGene.from_dict(d["stop"]),
            target=TargetGene.from_dict(d["target"]),
            window=None if w is None else (int(w[0]), int(w[1])),
            lineage=d.get("lineage", "HYBRID"),
        )

    def to_json(self, *, with_lineage: bool = True) -> str:
        return json.dumps(self.to_dict(with_lineage=with_lineage), sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, text: str) -> TemporalGenome:
        return cls.from_dict(json.loads(text))


# ---- presets (guard / invalidate genes) ------------------------------------------------------
@dataclass(frozen=True)
class Preset:
    kind: str  # none | feature | state | event | bound
    name: str = ""
    tf: str = FEATURE_TF
    op: str = "IS"
    arg: int = 0
    cmp: str = ""
    q: float = 0.0
    variant: str = ""
    tol: float = 0.0
    pick: str = ""

    def pid(self) -> str:
        k = self.kind
        if k == "none":
            return "none"
        if k == "feature":
            return f"F:{self.name}:{self.cmp}:{self.q:.2f}"
        if k == "state":
            tag = {"IS": "S", "NOT": "SN", "HOLD": "SH"}[self.op]
            return f"{tag}:{self.name}:{self.tf}" + (f":{self.arg}" if self.op == "HOLD" else "")
        if k == "event":
            tag = {"IS": "E", "BEFORE": "EB", "SINCE_ENTER": "ES"}[self.op]
            return f"{tag}:{self.name}:{self.tf}" + (f":{self.arg}" if self.op == "BEFORE" else "")
        if self.name == "HOLD_ABOVE":
            return f"HA:{self.variant[1:]}:{self.pick}"
        return f"BD:{ev.tol_code(self.tol)}:{self.pick}"

    def signature(self) -> tuple:
        """Comparable to ``trigger_signature`` (kind/name/tf/op/arg) for redundancy checks."""
        return (self.kind, self.name, self.tf, self.op, self.arg, self.variant)


NONE_PRESET = Preset("none")


def _bad(pid: str, why: str = "") -> GenomeError:
    return GenomeError(f"invalid preset {pid!r}" + (f": {why}" if why else ""))


def parse_preset(pid: str) -> Preset:
    """Parse a preset id; raises GenomeError.  ``parse_preset(p).pid()`` is the canonical id."""
    if not isinstance(pid, str):
        raise _bad(str(pid))
    if pid == "none":
        return NONE_PRESET
    parts = pid.split(":")
    tag = parts[0]
    try:
        if tag == "F" and len(parts) == 4:
            name, cmp_, q = parts[1], parts[2], float(parts[3])
            if name not in ev.FEATURE_MIRROR or cmp_ not in ("gt", "lt"):
                raise _bad(pid, "feature/cmp")
            q = sp.snap(min(max(q, sp.Q_RANGE[0]), sp.Q_RANGE[1]), Q_STEP)
            return Preset("feature", name, FEATURE_TF, cmp=cmp_, q=q)
        if tag in ("S", "SN", "SH") and len(parts) == (4 if tag == "SH" else 3):
            name, tf = parts[1], parts[2]
            if name not in LONG_STATES or tf not in ev.get(name).tfs:
                raise _bad(pid, "state/tf")
            if tag == "SH":
                arg = min(HOLD_ARGS, key=lambda g: (abs(g - int(parts[3])), g))
                return Preset("state", name, tf, "HOLD", arg)
            return Preset("state", name, tf, "IS" if tag == "S" else "NOT")
        if tag in ("E", "EB", "ES") and len(parts) == (4 if tag == "EB" else 3):
            name, tf = parts[1], parts[2]
            legal = INVALIDATE_EVENTS if tag == "E" else GUARD_EVENTS
            if name not in legal or tf not in ev.get(name).tfs or ev.get(name).variants() != ("",):
                raise _bad(pid, "event/tf")
            if tag == "EB":
                arg = min(BEFORE_ARGS, key=lambda g: (abs(g - int(parts[3])), g))
                return Preset("event", name, tf, "BEFORE", arg)
            return Preset("event", name, tf, "IS" if tag == "E" else "SINCE_ENTER")
        if tag == "HA" and len(parts) == 3:
            k = int(parts[1])
            if k not in BOUND_KS or parts[2] not in PICKS:
                raise _bad(pid, "k/pick")
            return Preset("bound", "HOLD_ABOVE", "M5", variant=f"k{k}", pick=parts[2])
        if tag == "BD" and len(parts) == 3:
            tols = {ev.tol_code(t): t for t in ev.TOL_GRID}
            if parts[1] not in tols or parts[2] not in PICKS:
                raise _bad(pid, "tol/pick")
            return Preset("bound", "BREAK_DN", "M5", tol=tols[parts[1]], pick=parts[2])
    except (ValueError, KeyError):
        raise _bad(pid) from None
    raise _bad(pid, "unknown form")


def feature_preset(name: str, cmp_: str, q: float) -> str:
    return Preset("feature", name, FEATURE_TF, cmp=cmp_, q=sp.snap(q, Q_STEP)).pid()


def guard_preset_ids(pool: EventPool) -> tuple[str, ...]:
    out = ["none"]
    out += [feature_preset(f, c, q) for f in ev.FEATURE_MIRROR for c in ("gt", "lt")
            for q in GUARD_Q_CHOICES]
    for tf in ("M15", "H1", "D1"):
        if pool.available("TREND_UP", tf):
            out.append(f"S:TREND_UP:{tf}")
            if tf != "D1":
                out += [f"SH:TREND_UP:{tf}:{k}" for k in (2, 3, 5)]
    out += [f"HA:{k}:{p}" for k in BOUND_KS for p in PICKS]
    for name in GUARD_EVENTS:
        if pool.available(name, "M5"):
            out += [f"EB:{name}:M5:{k}" for k in (3, 5, 8)]
            out.append(f"ES:{name}:M5")
    return tuple(out)


def invalidate_preset_ids(pool: EventPool) -> tuple[str, ...]:
    out = ["none"]
    out += [f"BD:{ev.tol_code(t)}:{p}" for t in ev.TOL_GRID for p in PICKS]
    for name in INVALIDATE_EVENTS:
        for tf in ("M5", "M15"):
            if pool.available(name, tf):
                out.append(f"E:{name}:{tf}")
    for tf in ("M15", "H1"):
        if pool.available("TREND_UP", tf):
            out.append(f"SN:TREND_UP:{tf}")
    return tuple(out)


# ---- canonical form --------------------------------------------------------------------------
def _snap_clause(c: sp.Clause) -> sp.Clause:
    if c.kind == "feature":
        return replace(c, q=sp.snap(min(max(c.q, sp.Q_RANGE[0]), sp.Q_RANGE[1]), Q_STEP))
    return c


def canon_group(cs: tuple[sp.Clause, ...]) -> tuple[sp.Clause, ...]:
    """AND-set canonical form: snap, drop implied clauses (stricter subsumes weaker), sort."""
    cs = tuple(_snap_clause(c) for c in cs)
    keep: dict[tuple, sp.Clause] = {}
    for c in cs:
        if c.kind == "feature":
            k = ("f", c.name, c.tf, c.cmp)
            cur = keep.get(k)
            better = cur is None or (c.q > cur.q if c.cmp == "gt" else c.q < cur.q)
            if better:
                keep[k] = c
        elif c.kind == "state" and c.op in ("IS", "HOLD"):
            k = ("s", c.name, c.tf)
            cur = keep.get(k)  # HOLD(k) implies IS; larger k is stricter
            rank = (1 if c.op == "HOLD" else 0, c.arg)
            if cur is None or rank > (1 if cur.op == "HOLD" else 0, cur.arg):
                keep[k] = c
        elif c.kind == "event" and c.op == "BEFORE":
            k = ("b", c.name, c.tf, c.variant)
            cur = keep.get(k)  # BEFORE(k) implies BEFORE(k') for k' > k: smaller k is stricter
            if cur is None or c.arg < cur.arg:
                keep[k] = c
        else:
            keep[("o", *c.key())] = c
    return tuple(sorted(keep.values(), key=sp.Clause.key))


def _snap_grid(v: int, grid: tuple[int, ...]) -> int:
    return min(grid, key=lambda g: (abs(g - v), g))


def _canon_event(g: EventGene) -> EventGene:
    tol = min(ev.TOL_GRID, key=lambda t: (abs(t - g.tol), t)) if g.name in TOL_BOUND else 0.0
    if ev.get(g.name).kind == "state":
        if g.op == "HOLD":
            return replace(g, tol=tol, arg=_snap_grid(g.arg, HOLD_ARGS))
        return replace(g, tol=tol, op="IS", arg=0)
    return replace(g, tol=tol, op="IS", arg=0)


def trigger_signature(g: EventGene) -> tuple:
    return ("state" if g.kind == "state" else g.kind, g.name, g.tf, g.op, g.arg, g.variant)


def _redundant_guard(p: Preset, trig: EventGene) -> bool:
    if p.kind == "none":
        return True
    if p.kind == "state" and trig.kind == "state" and p.name == trig.name and p.tf == trig.tf:
        return p.op == "IS" or (p.op == "HOLD" and trig.op == "HOLD" and p.arg <= trig.arg)
    if p.kind == "event" and p.op == "IS" and trig.kind == "event":
        return p.signature() == trigger_signature(trig)
    return False


def _canon_step(s: StepGene, expires: int) -> StepGene:
    trig = _canon_event(s.event)
    w = _snap_grid(s.within, WITHIN_GRID)
    if w >= expires:  # a within >= expires_after never binds: all such values are equivalent
        w = min((g for g in WITHIN_GRID if g >= expires), default=WITHIN_GRID[-1])
    gp = parse_preset(s.guard)
    ip = parse_preset(s.invalidate)
    return StepGene(trig, w, "none" if _redundant_guard(gp, trig) else gp.pid(), ip.pid())


def _canon_stop(s: StopGene) -> StopGene:
    risk = sp.snap(min(max(s.max_risk_atr, sp.MAX_RISK_RANGE[0]), sp.MAX_RISK_RANGE[1]), sp.MAX_RISK_RANGE[2])
    buf = sp.snap(min(max(s.buffer_atr, 0.0), 1.0), sp.BUFFER_RANGE[2])
    if s.kind == "register":
        return StopGene("register", s.pick, buf, 0.0, risk, "M5")
    if s.kind == "zone_edge":
        return StopGene("zone_edge", "", buf, 0.0, risk, "M5")
    if s.kind == "swing":
        return StopGene("swing", "", buf, 0.0, risk, s.tf)
    mult = sp.snap(min(max(s.atr_mult, sp.ATR_MULT_RANGE[0]), sp.ATR_MULT_RANGE[1]), sp.ATR_MULT_RANGE[2])
    return StopGene("atr", "", 0.0, mult, risk, "M5")


def _canon_target(t: TargetGene) -> TargetGene:
    def r_(v: float, rng: tuple[float, float, float]) -> float:
        return sp.snap(min(max(v, rng[0]), rng[1]), rng[2])

    if t.kind == "fixed_r":
        return TargetGene("fixed_r", r_(t.r, sp.R_RANGE))
    levels = tuple(sorted(set(t.levels)))
    return TargetGene("next_structure", 0.0, levels, r_(t.fallback_r, sp.R_RANGE),
                      r_(t.min_space_r, sp.MIN_SPACE_RANGE))


def _canon_window(w: tuple[int, int] | None) -> tuple[int, int] | None:
    if w is None:
        return None
    lo = max(0, min(1440, round(w[0] / sp.SESSION_STEP) * sp.SESSION_STEP))
    hi = max(0, min(1440, round(w[1] / sp.SESSION_STEP) * sp.SESSION_STEP))
    if hi - lo < MIN_WINDOW_MIN:
        hi = min(1440, lo + MIN_WINDOW_MIN)
        lo = hi - MIN_WINDOW_MIN
    return None if (lo, hi) == (0, 1440) else (lo, hi)


def _canon_once(g: TemporalGenome) -> TemporalGenome:
    n_states = len(g.steps) + 1
    e = _snap_grid(g.expires_after, EXPIRES_GRID)
    if e < n_states:
        e = min(x for x in EXPIRES_GRID if x >= n_states)
    return replace(
        g, anchor=canon_group(g.anchor), context=canon_group(g.context), expires_after=e,
        steps=tuple(_canon_step(s, e) for s in g.steps), stop=_canon_stop(g.stop),
        target=_canon_target(g.target), window=_canon_window(g.window),
    )


def canonicalize(g: TemporalGenome) -> TemporalGenome:
    """Fixed point of the canonical rewrite (idempotent): sorted/snapped AND-sets, subsumed
    clauses dropped, within on the grid and clamped against expires_after, redundant guards
    removed, stop/target fields zeroed per kind.  Lineage is kept (excluded from the hash)."""
    cur = g
    for _ in range(16):
        nxt = _canon_once(cur)
        if nxt == cur:
            return cur
        cur = nxt
    raise RuntimeError("temporal canonicalize did not converge")  # pragma: no cover


def canonical_payload(g: TemporalGenome) -> dict[str, Any]:
    return {"schema": "temporal-genome", "schema_version": 1,
            **canonicalize(g).to_dict(with_lineage=False)}


def canonical_hash(g: TemporalGenome) -> str:
    blob = json.dumps(canonical_payload(g), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(CANON_DOMAIN + blob.encode()).hexdigest()


# ---- complexity / validate -------------------------------------------------------------------
def genome_tfs(g: TemporalGenome) -> tuple[str, ...]:
    tfs = {c.tf for c in (*g.anchor, *g.context)}
    for s in g.steps:
        tfs.add(s.event.tf)
        for pid in (s.guard, s.invalidate):
            p = parse_preset(pid)
            if p.kind != "none":
                tfs.add(p.tf)
    if g.stop.kind == "swing":
        tfs.add(g.stop.tf)
    if g.target.kind == "next_structure":
        tfs.update(ev.TARGET_LEVELS[lv][0] for lv in g.target.levels)
    return tuple(sorted(tfs, key=ev.TIMEFRAMES.index))


def n_fitted_params(g: TemporalGenome) -> int:
    """Feature quantiles are the fitted numbers of a temporal genome."""
    n = sum(c.kind == "feature" for c in (*g.anchor, *g.context))
    for s in g.steps:
        n += sum(parse_preset(p).kind == "feature" for p in (s.guard, s.invalidate))
    return n


def complexity(g: TemporalGenome) -> int:
    """states + clauses + guards + invalidates + distinct tf + fitted params (design section 5)."""
    states = len(g.steps) + 1
    clauses = len(g.anchor) + len(g.context) + len(g.steps)
    guards = sum(parse_preset(s.guard).kind != "none" for s in g.steps)
    invs = sum(parse_preset(s.invalidate).kind != "none" for s in g.steps)
    return states + clauses + guards + invs + len(genome_tfs(g)) + n_fitted_params(g)


def _check_clause_group(cs: tuple[sp.Clause, ...], *, anchor: bool) -> None:
    for c in cs:
        if c.kind == "bound":
            raise GenomeError("bound clauses cannot be anchor/context genes")
        if c.kind == "feature":
            if c.tf != FEATURE_TF:
                raise GenomeError("feature clauses live on M5")
            continue
        if c.name not in (*LONG_PULSES, *LONG_STATES) or c.variant not in long_variants(c.name):
            raise GenomeError(f"{c.name}/{c.variant} is not a LONG-frame anchor/context event")
        if c.op == "SINCE_ENTER" or (c.kind == "event" and c.op not in ("IS", "BEFORE")):
            raise GenomeError("anchor/context events use IS or BEFORE")
        if not anchor and c.kind == "event" and c.op == "IS":
            raise GenomeError("context events must use BEFORE")


def validate(g: TemporalGenome) -> None:
    """Structural rules + compile-to-valid-spec.  Raises GenomeError."""
    if g.direction not in sp.DIRECTIONS:
        raise GenomeError(f"direction {g.direction!r}")
    if not 1 <= len(g.anchor) <= MAX_ANCHOR:
        raise GenomeError("anchor needs 1..3 clauses")
    if not 1 <= len(g.steps) <= MAX_STEPS:
        raise GenomeError("need 1..5 steps")
    if len(g.context) > MAX_CONTEXT:
        raise GenomeError("context > 3")
    if g.expires_after not in EXPIRES_GRID or g.expires_after < len(g.steps) + 1:
        raise GenomeError("expires_after off grid or below the state count")
    _check_clause_group(g.anchor, anchor=True)
    _check_clause_group(g.context, anchor=False)
    for s in g.steps:
        e = s.event
        if s.within not in WITHIN_GRID:
            raise GenomeError(f"within {s.within} off grid")
        try:
            d = ev.get(e.name)
        except KeyError as exc:
            raise GenomeError(str(exc)) from None
        if e.name not in (*LONG_PULSES, *LONG_BOUND, *LONG_STATES) or e.variant not in long_variants(e.name):
            raise GenomeError(f"{e.name}/{e.variant} is not a LONG-frame step event")
        if e.tf not in d.tfs:
            raise GenomeError(f"{e.name} not on {e.tf}")
        if d.kind == "state":
            if e.op not in ("IS", "HOLD") or (e.op == "HOLD") != (e.arg > 0):
                raise GenomeError("state step: IS or HOLD(arg)")
            if e.op == "HOLD" and e.arg not in HOLD_ARGS:
                raise GenomeError("HOLD arg off grid")
        elif e.op != "IS" or e.arg != 0:
            raise GenomeError("pulse/bound step uses IS")
        if (e.tol != 0.0 and e.name not in TOL_BOUND) or e.tol not in ev.TOL_GRID:
            raise GenomeError("tol only on bound events, on grid")
        parse_preset(s.guard)
        parse_preset(s.invalidate)
    from alpha.discovery import temporal_compile  # local import: compile depends on this module

    try:
        temporal_compile.build_long_spec(g)
    except (ValueError, KeyError) as exc:
        raise GenomeError(str(exc)) from None


def is_valid(g: TemporalGenome) -> bool:
    try:
        validate(g)
    except GenomeError:
        return False
    return True


__all__ = (
    "BEFORE_ARGS", "EXPIRES_GRID", "GUARD_EVENTS", "HOLD_ARGS", "INVALIDATE_EVENTS", "LONG_BOUND",
    "LONG_PULSES", "LONG_STATES", "MAX_STEPS", "EventGene", "EventPool", "GenomeError",
    "Preset", "StepGene", "StopGene", "TargetGene", "TemporalGenome", "canon_group",
    "canonical_hash", "canonical_payload", "canonicalize", "complexity", "feature_preset",
    "genome_tfs", "guard_preset_ids", "invalidate_preset_ids", "is_valid", "long_variants",
    "parse_preset", "trigger_signature", "validate",
)
