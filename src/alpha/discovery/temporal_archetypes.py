# ruff: noqa: E501
"""Typed role-path templates + ``random_genome`` for the V2 temporal search (design section 5).

Each archetype is a role path (ANCHOR > SETUP/TRIGGER > CONFIRM ...) built ONLY from registry
events the ``EventPool`` says exist; an archetype whose slots cannot be filled is unavailable.
Everything is deterministic in the numpy ``Generator`` passed in.  Research only; no Validation.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

import numpy as np

from alpha.discovery.search import make_lineage
from alpha.discovery.temporal_genome import (
    EXPIRES_GRID,
    EventGene,
    EventPool,
    GenomeError,
    StepGene,
    StopGene,
    TargetGene,
    TemporalGenome,
    canonicalize,
    feature_preset,
    guard_preset_ids,
    invalidate_preset_ids,
    is_valid,
    long_variants,
)
from alpha.events import schema as ev
from alpha.temporal import spec as sp

LONG_TARGET_LEVELS = tuple(sorted(k for k in ev.TARGET_LEVELS if k.endswith(("high", "pdh"))))
WINDOWS = ((480, 1050), (540, 1200), (0, 720), (600, 1440), (420, 900), (720, 1320))
WITHIN_WEIGHTS = np.array([1, 3, 4, 4, 3, 2, 1], dtype=float)
WITHIN_WEIGHTS /= WITHIN_WEIGHTS.sum()


def _pick(rng: np.random.Generator, seq):
    return seq[int(rng.integers(len(seq)))]


def _within(rng: np.random.Generator) -> int:
    return int(sp.WITHIN_GRID[int(rng.choice(len(sp.WITHIN_GRID), p=WITHIN_WEIGHTS))])


@lru_cache(maxsize=32)
def _guards(pool: EventPool) -> tuple[str, ...]:
    return guard_preset_ids(pool)


@lru_cache(maxsize=32)
def _invalidates(pool: EventPool) -> tuple[str, ...]:
    return invalidate_preset_ids(pool)


def _guard(rng, pool, p: float = 0.35) -> str:
    return _pick(rng, _guards(pool)[1:]) if rng.random() < p else "none"


def _invalidate(rng, pool, p: float = 0.5) -> str:
    return _pick(rng, _invalidates(pool)[1:]) if rng.random() < p else "none"


def _feature_clause(rng, cmp_: str | None = None, name: str | None = None, q: float | None = None) -> sp.Clause:
    name = name or _pick(rng, tuple(ev.FEATURE_MIRROR))
    cmp_ = cmp_ or _pick(rng, ("gt", "lt"))
    q = q if q is not None else _pick(rng, (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8))
    return sp.Clause("feature", name, "M5", cmp=cmp_, q=sp.snap(q, sp.Q_RANGE[2]))


def _variant(rng, pool: EventPool, name: str, tf: str) -> str | None:
    opts = [v for v in long_variants(name) if pool.available(name, tf, v)]
    return _pick(rng, opts) if opts else None


def _ev_clause(rng, pool, name: str, tf: str, variant: str | None = None, **kw) -> sp.Clause | None:
    v = _variant(rng, pool, name, tf) if variant is None else variant
    if v is None or not pool.available(name, tf, v):
        return None
    return sp.Clause("event", name, tf, variant=v, **kw)


def _tf(rng, pool, name: str, tfs=("M5", "M15")) -> str | None:
    opts = [t for t in tfs if any(pool.available(name, t, v) for v in long_variants(name))]
    return _pick(rng, opts) if opts else None


def _pulse(rng, pool, name: str, tfs=("M5",), variant: str | None = None) -> EventGene | None:
    tf = _tf(rng, pool, name, tfs)
    if tf is None:
        return None
    v = _variant(rng, pool, name, tf) if variant is None else variant
    if v is None or not pool.available(name, tf, v):
        return None
    return EventGene(name, tf, v)


def _bound(rng, name: str) -> EventGene:
    if name == "RECLAIM_UP":
        return EventGene(name, "M5", variant=f"k{_pick(rng, (3, 6))}")
    return EventGene(name, "M5", tol=_pick(rng, ev.TOL_GRID))


def _trend_anchor(rng, pool, tfs=("H1", "M15")) -> sp.Clause | None:
    tf = _pick(rng, [t for t in tfs if pool.available("TREND_UP", t)] or [None])
    if tf is None:
        return None
    hold = _pick(rng, (0, 0, 3, 5))
    return sp.Clause("state", "TREND_UP", tf, "HOLD", hold) if hold else sp.Clause("state", "TREND_UP", tf)


def _steps(rng, pool, genes: list[EventGene], *, inv_first_first: bool = False) -> tuple[StepGene, ...]:
    out = []
    for i, g in enumerate(genes):
        inv = _invalidate(rng, pool)
        if inv_first_first and i == 0 and rng.random() < 0.6:
            inv = "BD:t0:first"
        out.append(StepGene(g, _within(rng), _guard(rng, pool), inv))
    return tuple(out)


# ---- archetypes: (rng, pool) -> (anchor clauses, step genes, zone?) or None -------------------
Built = tuple[list[sp.Clause], list[EventGene], bool]


def _zone_sweep_reclaim_bos_retest(rng, pool) -> Built | None:
    tf = _tf(rng, pool, "ZONE_ENTER", ("M15", "M5"))
    zone = _ev_clause(rng, pool, "ZONE_ENTER", tf) if tf else None
    sweep = _pulse(rng, pool, "SWEEP_LOW", ("M5",))
    bos = _pulse(rng, pool, "BOS_UP", ("M5",))
    if not (zone and sweep and bos):
        return None
    anchor = [zone]
    if rng.random() < 0.5 and (t := _trend_anchor(rng, pool, ("H1",))):
        anchor.append(t)
    steps = [sweep, _bound(rng, "RECLAIM_UP"), bos, _bound(rng, "RETEST_HOLD_UP")]
    return anchor, steps[: _pick(rng, (3, 4, 4))], True


def _trend_pullback_resume(rng, pool) -> Built | None:
    a = _trend_anchor(rng, pool, ("H1", "M15"))
    hold_tf = _pick(rng, [t for t in ("M15", "H1") if pool.available("TREND_UP", t)] or [None])
    mom = _pulse(rng, pool, "MOMENTUM_RESUME_UP", ("M5", "M15"))
    if not (a and hold_tf and mom):
        return None
    steps = [EventGene("TREND_UP", hold_tf, op="HOLD", arg=_pick(rng, (3, 5, 8))), mom]
    if rng.random() < 0.4 and (bos := _pulse(rng, pool, "BOS_UP", ("M5",))):
        steps.append(bos)
    return [a], steps, False


def _range_sweep_choch_retest(rng, pool) -> Built | None:
    tf = _tf(rng, pool, "ZONE_ENTER", ("M15", "M5"))
    zone = _ev_clause(rng, pool, "ZONE_ENTER", tf, "prior_range") if tf else None
    sweep = _pulse(rng, pool, "SWEEP_LOW", ("M5",))
    choch = _pulse(rng, pool, "CHOCH_UP", ("M5",))
    if not (zone and sweep and choch):
        return None
    steps = [sweep, choch, _bound(rng, "RETEST_HOLD_UP")]
    return [zone], steps[: _pick(rng, (2, 3, 3))], True


def _break_retest_momentum(rng, pool) -> Built | None:
    sh = _pulse(rng, pool, "SWING_HIGH_CONF", ("M15", "M5"))
    mom = _pulse(rng, pool, "MOMENTUM_RESUME_UP", ("M5",))
    if not sh:
        return None
    anchor = [sp.Clause("event", sh.name, sh.tf)]
    if rng.random() < 0.5 and (t := _trend_anchor(rng, pool, ("H1",))):
        anchor.append(t)
    steps = [_bound(rng, "BREAK_UP"), _bound(rng, "RETEST_HOLD_UP")]
    if mom and rng.random() < 0.6:
        steps.append(mom)
    return anchor, steps, False


def _failed_breakout(rng, pool) -> Built | None:
    if rng.random() < 0.6:
        tf = _tf(rng, pool, "ZONE_ENTER", ("M15", "M5"))
        a = _ev_clause(rng, pool, "ZONE_ENTER", tf, "prior_range") if tf else None
        zone = True
    else:
        sl = _pulse(rng, pool, "SWING_LOW_CONF", ("M15", "M5"))
        a = sp.Clause("event", sl.name, sl.tf) if sl else None
        zone = False
    if a is None:
        return None
    steps = [_bound(rng, "BREAK_DN"), _bound(rng, "RECLAIM_UP")]
    if rng.random() < 0.5 and (bos := _pulse(rng, pool, "BOS_UP", ("M5",))):
        steps.append(bos)
    return [a], steps, zone


def _compression_expansion(rng, pool) -> Built | None:
    pat = _pulse(rng, pool, "PATTERN_COMPLETE", ("M5", "M15"), "inside_bar_break_up")
    bos = _pulse(rng, pool, "BOS_UP", ("M5",))
    trig = pat or bos
    if trig is None:
        return None
    anchor = [_feature_clause(rng, "lt", "range_ratio", _pick(rng, (0.2, 0.3, 0.4)))]
    if rng.random() < 0.5 and (t := _trend_anchor(rng, pool, ("H1", "M15"))):
        anchor.append(t)
    steps = [trig, _bound(rng, "RETEST_HOLD_UP")] if rng.random() < 0.5 else [trig]
    return anchor, steps, False


def _pattern_confirm_retest(rng, pool) -> Built | None:
    pat = _pulse(rng, pool, "PATTERN_COMPLETE", ("M5", "M15"), "double_bottom")
    bos = _pulse(rng, pool, "BOS_UP", ("M5",))
    if not (pat and bos):
        return None
    steps = [bos, _bound(rng, "RETEST_HOLD_UP")]
    return [sp.Clause("event", pat.name, pat.tf, variant=pat.variant)], steps[: _pick(rng, (1, 2, 2))], False


def _trendline(rng, pool) -> Built | None:
    a = _trend_anchor(rng, pool, ("H1", "M15"))
    if a is None:
        return None
    if rng.random() < 0.5:
        tl = _pulse(rng, pool, "TRENDLINE_TOUCH", ("M5", "M15"))
        nxt = _pulse(rng, pool, "MOMENTUM_RESUME_UP", ("M5",)) or _pulse(rng, pool, "BOS_UP", ("M5",))
        if not (tl and nxt):
            return None
        return [a], [tl, nxt], False
    tb = _pulse(rng, pool, "TRENDLINE_BREAK", ("M5", "M15"))
    if not tb:
        return None
    return [a], [tb, _bound(rng, "RETEST_HOLD_UP")], False


def _swing_bos_retest(rng, pool) -> Built | None:
    sl = _pulse(rng, pool, "SWING_LOW_CONF", ("M15", "M5"))
    bos = _pulse(rng, pool, "BOS_UP", ("M5",))
    if not (sl and bos):
        return None
    anchor = [sp.Clause("event", sl.name, sl.tf)]
    if rng.random() < 0.6 and (t := _trend_anchor(rng, pool, ("H1",))):
        anchor.append(t)
    return anchor, [bos, _bound(rng, "RETEST_HOLD_UP")], False


ARCHETYPES: dict[str, Callable] = {
    "ZONE_SWEEP_RECLAIM_BOS_RETEST": _zone_sweep_reclaim_bos_retest,
    "TREND_PULLBACK_RESUME": _trend_pullback_resume,
    "RANGE_SWEEP_CHOCH_RETEST": _range_sweep_choch_retest,
    "BREAK_RETEST_MOMENTUM": _break_retest_momentum,
    "FAILED_BREAKOUT": _failed_breakout,
    "COMPRESSION_EXPANSION": _compression_expansion,
    "PATTERN_CONFIRM_RETEST": _pattern_confirm_retest,
    "TRENDLINE_BOUNCE_BREAK": _trendline,
    "SWING_BOS_RETEST": _swing_bos_retest,
}


def random_stop(rng: np.random.Generator, *, zone: bool) -> StopGene:
    r = rng.random()
    buf = float(_pick(rng, (0.0, 0.05, 0.1, 0.25, 0.5)))
    risk = float(_pick(rng, (1.5, 2.0, 3.0, 4.0, 6.0)))
    if r < 0.40:
        return StopGene("register", _pick(rng, ("ext", "ext", "lvl", "first")), buf, 0.0, risk)
    if r < 0.55 and zone:
        return StopGene("zone_edge", "", buf, 0.0, risk)
    if r < 0.70:
        return StopGene("swing", "", buf, 0.0, risk, _pick(rng, ("M5", "M15")))
    return StopGene("atr", "", 0.0, float(_pick(rng, (1.0, 1.5, 2.0, 3.0))), risk)


def random_target(rng: np.random.Generator) -> TargetGene:
    if rng.random() < 0.55:
        return TargetGene("fixed_r", float(_pick(rng, (1.0, 1.5, 2.0, 2.5, 3.0))))
    k = int(rng.integers(1, 4))
    idx = rng.choice(len(LONG_TARGET_LEVELS), size=k, replace=False)
    levels = tuple(sorted(LONG_TARGET_LEVELS[int(i)] for i in idx))
    return TargetGene("next_structure", 0.0, levels, float(_pick(rng, (1.5, 2.0, 2.5))),
                      float(_pick(rng, (0.5, 1.0, 1.5))))


def random_context(rng: np.random.Generator, pool: EventPool) -> tuple[sp.Clause, ...]:
    out: list[sp.Clause] = []
    for _ in range(_pick(rng, (0, 0, 0, 1, 1, 2))):
        r = rng.random()
        if r < 0.4 and pool.available("TREND_UP", "D1"):
            out.append(sp.Clause("state", "TREND_UP", "D1"))
        elif r < 0.6 and pool.available("TREND_UP", "H1"):
            out.append(sp.Clause("state", "TREND_UP", "H1", "HOLD", _pick(rng, (3, 5))))
        else:
            out.append(_feature_clause(rng))
    return tuple(out)


def available_archetypes(pool: EventPool, seed: int = 0) -> tuple[str, ...]:
    """Archetypes that can be filled from ``pool`` (probed with a fixed seed, several draws)."""
    rng = np.random.default_rng(seed)
    return tuple(n for n, f in ARCHETYPES.items() if any(f(rng, pool) for _ in range(12)))


def random_genome(
    rng: np.random.Generator, pool: EventPool | None = None, archetype: str | None = None,
    direction: str | None = None,
) -> TemporalGenome:
    """A canonical, validate()-passing genome (deterministic per ``rng`` state)."""
    pool = pool if pool is not None else EventPool.full()
    names = tuple(ARCHETYPES) if archetype is None else (archetype,)
    for _ in range(400):
        name = _pick(rng, names)
        built = ARCHETYPES[name](rng, pool)
        if built is None:
            continue
        anchor, genes, zone = built
        if rng.random() < 0.15 and len(anchor) < 3:
            anchor.append(_feature_clause(rng))
        steps = _steps(rng, pool, genes, inv_first_first=True)
        exp = _pick(rng, [e for e in EXPIRES_GRID if e >= len(steps) + 1])
        g = TemporalGenome(
            direction=direction or ("LONG" if rng.random() < 0.5 else "SHORT"),
            anchor=tuple(anchor), steps=steps, context=random_context(rng, pool),
            expires_after=exp, stop=random_stop(rng, zone=zone), target=random_target(rng),
            window=None if rng.random() < 0.6 else _pick(rng, WINDOWS),
            lineage=make_lineage(name),
        )
        try:
            g = canonicalize(g)
        except GenomeError:
            continue
        if is_valid(g):
            return g
    raise GenomeError(f"could not build a valid genome (archetype={archetype!r})")


__all__ = ("ARCHETYPES", "LONG_TARGET_LEVELS", "available_archetypes", "feature_preset",
           "random_context", "random_genome", "random_stop", "random_target")
