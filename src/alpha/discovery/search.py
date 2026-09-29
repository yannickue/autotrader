"""Search-space helpers for later Optuna (param_space/with_params) and DEAP (mutate/crossover).

Deliberately free of optuna/deap imports.  Structural operators always return genomes that pass
``validate()`` (bounded retries; on exhaustion the parent is returned unchanged).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

import numpy as np

from alpha.discovery.archetypes import random_clause
from alpha.discovery.catalog import (
    CATALOG,
    STOP_LEVELS,
    STOP_MULT_DOMAIN,
    STOP_OFFSET_DOMAIN,
    TARGET_R_DOMAIN,
    TIME_DOMAIN,
    FeaturePool,
)
from alpha.discovery.compile import canonicalize
from alpha.discovery.genome import (
    MAX_CONTEXT,
    MAX_OR,
    MAX_REGIME,
    MAX_TOTAL_CLAUSES,
    MAX_TRIGGER,
    MIN_WINDOW_MIN,
    Genome,
    StopGene,
    is_valid,
    total_clauses,
    validate,
)

ParamSpec = tuple[str, float, float, str]  # (name, low, high, "float" | "int")
_GROUPS = (("regime", ("REGIME",), MAX_REGIME), ("context", ("CONTEXT",), MAX_CONTEXT),
           ("trigger", ("TRIGGER", "LEVEL"), MAX_TRIGGER))


def _quantile_clauses(g: Genome):
    for group in ("regime", "context", "trigger", "or_group"):
        for i, c in enumerate(getattr(g, group)):
            if CATALOG[c.feature].kind in ("continuous", "signed"):
                yield f"q_{group}{i}", group, i, c


def param_space(genome: Genome) -> list[ParamSpec]:
    """Ordered numeric genes of a FIXED-structure genome (names are stable per structure)."""
    space: list[ParamSpec] = []
    for name, _group, _i, c in _quantile_clauses(genome):
        entry = CATALOG[c.feature]
        space.append((name, entry.q_lo, entry.q_hi, "float"))
    if genome.stop.kind == "atr_multiple":
        space.append(("stop_mult", *STOP_MULT_DOMAIN, "float"))
    elif genome.stop.kind == "session_level":
        space.append(("stop_offset", *STOP_OFFSET_DOMAIN, "float"))
    space.append(("target_r", *TARGET_R_DOMAIN, "float"))
    if genome.time_window is not None:
        space.append(("tw_start", TIME_DOMAIN[0], TIME_DOMAIN[1] - MIN_WINDOW_MIN, "int"))
        space.append(("tw_end", TIME_DOMAIN[0] + MIN_WINDOW_MIN, TIME_DOMAIN[1], "int"))
    return space


def with_params(genome: Genome, values: Mapping[str, float] | Sequence[float]) -> Genome:
    """Return the genome with numeric genes replaced (clamped to their domains)."""
    space = param_space(genome)
    if not isinstance(values, Mapping):
        if len(values) != len(space):
            raise ValueError(f"expected {len(space)} values, got {len(values)}")
        values = {spec[0]: v for spec, v in zip(space, values, strict=True)}
    bounds = {name: (lo, hi, kind) for name, lo, hi, kind in space}

    def get(name: str, default: float) -> float:
        if name not in values:
            return default
        lo, hi, kind = bounds[name]
        v = min(max(float(values[name]), lo), hi)
        return round(v) if kind == "int" else v

    groups = {g: list(getattr(genome, g)) for g in ("regime", "context", "trigger", "or_group")}
    for name, group, i, c in _quantile_clauses(genome):
        groups[group][i] = replace(c, q=get(name, c.q))
    stop = genome.stop
    if stop.kind == "atr_multiple":
        stop = replace(stop, multiple=get("stop_mult", stop.multiple))
    elif stop.kind == "session_level":
        stop = replace(stop, offset=get("stop_offset", stop.offset))
    window = genome.time_window
    if window is not None:
        start = int(get("tw_start", window[0]))
        end = int(get("tw_end", window[1]))
        end = max(end, start + MIN_WINDOW_MIN)
        if end > TIME_DOMAIN[1]:
            end = TIME_DOMAIN[1]
            start = min(start, end - MIN_WINDOW_MIN)
        window = (start, end)
    out = replace(
        genome, regime=tuple(groups["regime"]), context=tuple(groups["context"]),
        trigger=tuple(groups["trigger"]), or_group=tuple(groups["or_group"]), stop=stop,
        target_r=get("target_r", genome.target_r), time_window=window,
    )
    return validate(out)


# --------------------------------------------------------------------------- structure operators
def _root(lineage: str) -> str:
    return lineage[4:] if lineage.startswith("MUT:") else lineage


def _random_window(rng: np.random.Generator) -> tuple[int, int]:
    start = int(rng.choice(range(480, 1140, 30)))
    return start, min(start + int(rng.choice((60, 90, 120, 180, 240))), TIME_DOMAIN[1])


def _mutate_once(g: Genome, rng: np.random.Generator, pool: FeaturePool) -> Genome | None:
    op = str(rng.choice(["add", "remove", "replace", "stop", "time", "or"]))
    name, layers, cap = _GROUPS[int(rng.integers(len(_GROUPS)))]
    group = list(getattr(g, name))
    if op == "add":
        if len(group) >= cap or total_clauses(g) >= MAX_TOTAL_CLAUSES or not pool.layer(*layers):
            return None
        group.append(random_clause(pool, rng, layers))
    elif op == "remove":
        if len(group) <= (1 if name == "trigger" else 0):
            return None
        group.pop(int(rng.integers(len(group))))
    elif op == "replace":
        if not group or not pool.layer(*layers):
            return None
        group[int(rng.integers(len(group)))] = random_clause(pool, rng, layers)
    elif op == "stop":
        kinds = [k for k in ("atr_multiple", "last_swing", "session_level") if k != g.stop.kind]
        kind = str(rng.choice(kinds))
        stop = (StopGene("atr_multiple", float(rng.uniform(*STOP_MULT_DOMAIN)))
                if kind == "atr_multiple"
                else StopGene("last_swing", None, None, 0.0) if kind == "last_swing"
                else StopGene("session_level", None, str(rng.choice(STOP_LEVELS)),
                              float(rng.uniform(*STOP_OFFSET_DOMAIN))))
        return replace(g, stop=stop)
    elif op == "time":
        window = None if (g.time_window is not None and rng.random() < 0.4) else _random_window(rng)
        return replace(g, time_window=window)
    else:  # or-group toggle
        if g.or_group:
            return replace(g, or_group=())
        if total_clauses(g) + MAX_OR > MAX_TOTAL_CLAUSES:
            return None
        alts = tuple(random_clause(pool, rng, ("TRIGGER", "LEVEL")) for _ in range(MAX_OR))
        return replace(g, or_group=alts)
    return replace(g, **{name: tuple(group)})


def mutate_structure(genome: Genome, rng: np.random.Generator, pool: FeaturePool,
                     *, max_tries: int = 25) -> Genome:
    """Add/remove/replace a clause, flip the stop kind, switch the time window or toggle an OR
    group.  Returns a canonical, validate()-passing genome (lineage ``MUT:<root>``)."""
    for _ in range(max_tries):
        child = _mutate_once(genome, rng, pool)
        if child is None:
            continue
        child = canonicalize(replace(child, lineage=f"MUT:{_root(genome.lineage)}"))
        if is_valid(child) and child != canonicalize(genome):
            return child
    return genome


_SWAPPABLE = ("regime", "context", "trigger", "or_group", "time_window", "stop", "target_r")


def crossover(
    a: Genome, b: Genome, rng: np.random.Generator, *, max_tries: int = 25
) -> tuple[Genome, Genome]:
    """Uniform crossover over clause groups / stop / window / target; children validate()."""
    lineage = f"X:{_root(a.lineage)}|{_root(b.lineage)}"
    for _ in range(max_tries):
        swap = [bool(x) for x in rng.random(len(_SWAPPABLE) + 1) < 0.5]
        fields_a = {f: getattr(a, f) for f in _SWAPPABLE}
        fields_b = {f: getattr(b, f) for f in _SWAPPABLE}
        for flag, f in zip(swap, _SWAPPABLE, strict=False):
            if flag:
                fields_a[f], fields_b[f] = fields_b[f], fields_a[f]
        dir_a, dir_b = (b.direction, a.direction) if swap[-1] else (a.direction, b.direction)
        c1 = canonicalize(Genome(direction=dir_a, lineage=lineage, **fields_a))
        c2 = canonicalize(Genome(direction=dir_b, lineage=lineage, **fields_b))
        if is_valid(c1) and is_valid(c2):
            return c1, c2
    return a, b


__all__ = ("crossover", "mutate_structure", "param_space", "with_params")
