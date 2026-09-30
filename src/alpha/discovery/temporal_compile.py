# ruff: noqa: E501
"""TemporalGenome -> StateMachineStrategySpec (design section 5).  Research only.

Register dataflow is DERIVED here, never encoded in genes:

* every ``IS`` event clause of the anchor / a step captures what the registry says it
  ``produces`` (``evx`` = extreme = role ``ext``, ``evl`` = level = role ``lvl``) or, for zones, the
  ``ZONE_LO`` level it ``exposes``; the anchor falls back to ``bar_low`` when nothing is captured;
  bound / momentum / state triggers additionally capture ``min_low_since_enter`` (role ``ext``);
* a bound clause, a bound guard/invalidate preset and a ``register`` stop read the register
  chosen by ROLE (``lvl`` / ``ext`` / ``first``) among the registers captured strictly earlier, so
  the "no read before capture" rule of ``spec.validate`` holds by construction;
* at most ``MAX_REG`` registers are allocated; later captures are simply not made.

SHORT genomes compile to ``spec.mirror(long_spec)`` (registry mirror table; antisymmetric features
become ``lt -thr(q)`` via ``Clause.neg``, positive-only ones keep the same test).  Feature clauses keep
their quantile ``q`` in the spec (the kernel resolves ``frame.thresholds[(name, q)]``); the injected
threshold ``resolver`` is used to (a) fail closed on unresolvable / non-finite thresholds and
(b) record the resolved numbers in ``metadata['thr']`` so ``behavior_key`` can identify two
genomes whose q values resolve to the same thresholds as ONE behaviour.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from alpha.discovery.temporal_genome import (
    GenomeError,
    Preset,
    TemporalGenome,
    canonical_hash,
    canonicalize,
    parse_preset,
)
from alpha.events import schema as ev
from alpha.temporal import spec as sp

Resolver = Callable[[str, float], float]
BEHAVIOR_DOMAIN = b"temporal-behavior-v1:"
_EXTRA_EXT = ("MOMENTUM_RESUME_UP",)


def _event_caps(name: str) -> list[tuple[str, str, str]]:
    d = ev.get(name)
    out: list[tuple[str, str, str]] = []
    if "evx" in d.produces:
        out.append(("evx", name, "ext"))
    if "evl" in d.produces:
        out.append(("evl", name, "lvl"))
    if not out and "ZONE_LO" in d.exposes:
        out.append(("lv", "ZONE_LO", "lvl"))
    return out


class _Regs:
    """Register allocator + role-based reader (typed dataflow by construction)."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, sp.Capture]] = []  # (reg, role, capture)

    def alloc(self, wanted: list[tuple[str, str, str]]) -> tuple[sp.Capture, ...]:
        out: list[sp.Capture] = []
        seen: set[tuple[str, str]] = set()
        for source, of, role in wanted:
            if (source, of) in seen or len(self.items) >= sp.MAX_REG:
                continue
            seen.add((source, of))
            cap = sp.Capture(sp.REGS[len(self.items)], source, of)
            self.items.append((cap.reg, role, cap))
            out.append(cap)
        return tuple(out)

    def pick(self, role: str) -> str:
        if not self.items:
            raise GenomeError("no register captured yet")
        if role == "first":
            return self.items[0][0]
        for reg, r, _ in reversed(self.items):
            if r == role:
                return reg
        return self.items[-1][0]

    def zone_lo(self) -> str:
        for reg, _r, cap in self.items:
            if cap.source == "lv" and cap.of == "ZONE_LO":
                return reg
        raise GenomeError("zone_edge stop needs a captured ZONE_LO")


def _preset_clause(p: Preset, regs: _Regs) -> sp.Clause | None:
    if p.kind == "none":
        return None
    if p.kind == "feature":
        return sp.Clause("feature", p.name, p.tf, cmp=p.cmp, q=p.q)
    if p.kind == "state":
        return sp.Clause("state", p.name, p.tf, p.op, p.arg)
    if p.kind == "event":
        return sp.Clause("event", p.name, p.tf, p.op, p.arg)
    return sp.Clause("bound", p.name, p.tf, "IS", 0, regs.pick(p.pick), p.tol, variant=p.variant)


def build_long_spec(genome: TemporalGenome, *, canonical: bool = False) -> sp.StateMachineStrategySpec:
    """LONG-frame spec of ``genome`` (SHORT genomes use the same LONG-frame genes)."""
    c = genome if canonical else canonicalize(genome)
    regs = _Regs()
    wanted: list[tuple[str, str, str]] = []
    for cl in c.anchor:
        if cl.kind == "event" and cl.op == "IS":
            wanted += _event_caps(cl.name)
    if not wanted:
        wanted = [("bar_low", "", "ext")]
    anchor_caps = regs.alloc(wanted)

    def n_event_caps(e) -> int:
        return len(_event_caps(e.name)) if e.kind == "event" and e.op == "IS" else 0

    states: list[sp.Transition] = []
    for i, s in enumerate(c.steps):
        e = s.event
        later = sum(n_event_caps(t.event) for t in c.steps[i + 1:])
        trig = e.clause(regs.pick("lvl") if e.is_bound else "")
        gp, ip = parse_preset(s.guard), parse_preset(s.invalidate)
        guard = _preset_clause(gp, regs)
        inv = _preset_clause(ip, regs)
        want: list[tuple[str, str, str]] = []
        if e.kind == "event" and e.op == "IS":
            want += _event_caps(e.name)
        caps = regs.alloc(want)
        # optional pullback-low capture: never at the expense of a later event's level/extreme capture
        if (e.is_bound or e.kind == "state" or e.name in _EXTRA_EXT) and len(regs.items) + 1 + later <= sp.MAX_REG:
            caps = (*caps, *regs.alloc([("min_low_since_enter", "", "ext")]))
        states.append(sp.Transition(
            trigger=trig, within=s.within, guards=() if guard is None else (guard,),
            invalidate=() if inv is None else (inv,), capture=caps,
        ))

    sg = c.stop
    if sg.kind == "register":
        stop = sp.StopRule("register", reg=regs.pick(sg.pick), buffer_atr=sg.buffer_atr,
                           max_risk_atr=sg.max_risk_atr)
    elif sg.kind == "zone_edge":
        stop = sp.StopRule("zone_edge", reg=regs.zone_lo(), buffer_atr=sg.buffer_atr,
                           max_risk_atr=sg.max_risk_atr)
    elif sg.kind == "swing":
        stop = sp.StopRule("swing", of="SWING_LOW_LVL", tf=sg.tf, buffer_atr=sg.buffer_atr,
                           max_risk_atr=sg.max_risk_atr)
    elif sg.kind == "atr":
        stop = sp.StopRule("atr", atr_mult=sg.atr_mult, max_risk_atr=sg.max_risk_atr)
    else:
        raise GenomeError(f"stop kind {sg.kind!r}")
    tg = c.target
    if tg.kind == "fixed_r":
        target = sp.TargetRule("fixed_r", r=tg.r)
    elif tg.kind == "next_structure":
        target = sp.TargetRule("next_structure", levels=tg.levels, fallback_r=tg.fallback_r,
                               min_space_r=tg.min_space_r)
    else:
        raise GenomeError(f"target kind {tg.kind!r}")
    fam = c.lineage.split(">")[0][:40]
    return sp.StateMachineStrategySpec(
        strategy_id=f"T{canonical_hash(c)[:16]}", version=1, direction="LONG",
        anchor=c.anchor, anchor_capture=anchor_caps, states=tuple(states), context=c.context,
        expires_after=c.expires_after, session_window=c.window, stop=stop, target=target,
        metadata=(("family", fam),),
    )


def feature_clauses(spec: sp.StateMachineStrategySpec) -> list[sp.Clause]:
    out = [c for c in (*spec.anchor, *spec.context) if c.kind == "feature"]
    for t in spec.states:
        out += [c for c in (t.trigger, *t.guards, *t.invalidate) if c.kind == "feature"]
    return out


def compile_temporal(
    genome: TemporalGenome, resolver: Resolver | None = None, *, canonical: bool = False
) -> sp.StateMachineStrategySpec:
    """Compile ``genome`` (canonicalised unless ``canonical``) to a validated spec.

    Raises ``GenomeError`` for anything that does not compile or whose thresholds are unresolvable.
    """
    c = genome if canonical else canonicalize(genome)
    try:
        spec = build_long_spec(c, canonical=True)
        if c.direction == "SHORT":
            spec = sp.mirror(spec)
        elif c.direction != "LONG":
            raise GenomeError(f"direction {c.direction!r}")
        if resolver is not None:
            thr: dict[str, float] = {}
            for cl in feature_clauses(spec):
                try:
                    v = float(resolver(cl.name, cl.q))
                except (KeyError, TypeError, ValueError) as exc:
                    raise GenomeError(f"unresolvable threshold {cl.name}@{cl.q}") from exc
                if not math.isfinite(v):
                    raise GenomeError(f"non-finite threshold {cl.name}@{cl.q}")
                thr[f"{cl.name}@{cl.q:.2f}"] = v
            meta = dict(spec.metadata)
            meta["thr"] = json.dumps(thr, sort_keys=True, separators=(",", ":"), allow_nan=False)
            spec = replace(spec, metadata=tuple(sorted(meta.items())))
    except GenomeError:
        raise
    except (ValueError, KeyError) as exc:
        raise GenomeError(str(exc)) from None
    return spec


def _swap_q(clause: dict[str, Any], thr: dict[str, float]) -> None:
    if clause["kind"] == "feature":
        v = thr[f"{clause['name']}@{clause['q']:.2f}"]
        clause["thr"] = -v if clause.pop("neg", False) else v  # effective (signed) threshold
        del clause["q"]


def behavior_key(spec: sp.StateMachineStrategySpec) -> str:
    """Hash of everything that affects candidates (strategy_id/metadata stripped, feature q replaced by
    the resolved threshold when the spec was compiled with a resolver)."""
    payload = sp.canonical_payload(spec)
    raw = dict(spec.metadata).get("thr")
    if raw is not None:
        thr = json.loads(raw)
        for c in (*payload["anchor"], *payload["context"]):
            _swap_q(c, thr)
        for t in payload["states"]:
            for c in (t["trigger"], *t["guards"], *t["invalidate"]):
                _swap_q(c, thr)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(BEHAVIOR_DOMAIN + blob.encode()).hexdigest()


__all__ = ("BEHAVIOR_DOMAIN", "Resolver", "behavior_key", "build_long_spec", "canonical_hash",
           "canonicalize", "compile_temporal", "feature_clauses")
