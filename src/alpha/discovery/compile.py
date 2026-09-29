"""Genome -> StrategySpec compiler, canonicalisation, TRAIN-only threshold resolver, ledger."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from alpha.discovery.catalog import (
    ALIAS_GROUPS,
    CATALOG,
    Q_GRID,
    STOP_LEVEL_MIRROR,
    STOP_MULT_DOMAIN,
    STOP_MULT_GRID,
    STOP_OFFSET_DOMAIN,
    STOP_OFFSET_GRID,
    TARGET_R_DOMAIN,
    TARGET_R_GRID,
    TIME_DOMAIN,
    TIME_GRID,
    CatalogEntry,
    flip_op,
)
from alpha.discovery.genome import (
    MAX_TRIGGER,
    MIN_WINDOW_MIN,
    Clause,
    Genome,
    GenomeError,
    StopGene,
    complexity,
    validate,
)
from alpha.fast.spec import Rule, StopSpec, StrategySpec, TargetSpec

SPEC_VERSION = "grammar-v1"


# --------------------------------------------------------------------------- resolver
class ThresholdResolver:
    """Resolves quantiles to values from TRAIN-partition bars only.

    Only ``features[name][train_mask]`` is ever read, so Validation/OOS values cannot leak
    into a threshold.  Sorted train values are cached per feature.
    """

    def __init__(self, features: Any, train_mask: np.ndarray) -> None:
        self._features = features
        self._mask = np.asarray(train_mask, dtype=bool)
        self._sorted: dict[str, np.ndarray] = {}

    @classmethod
    def from_plan(cls, features: Any, plan: Any, dates: np.ndarray) -> ThresholdResolver:
        return cls(features, plan.mask(np.asarray(dates), plan.train))

    def _train_values(self, feature: str) -> np.ndarray:
        cached = self._sorted.get(feature)
        if cached is None:
            values = np.asarray(self._features[feature])[self._mask].astype(float)
            cached = np.sort(values[np.isfinite(values)])
            self._sorted[feature] = cached
        return cached

    def value(self, feature: str, q: float, floor: float | None = None) -> float:
        """TRAIN quantile of ``feature``; ``floor`` (catalog domain) lower-bounds the result."""
        values = self._train_values(feature)
        if not len(values):
            raise ValueError(f"no finite TRAIN values for {feature}")
        v = float(np.quantile(values, q))
        if floor is not None:
            v = max(v, float(floor))
        return float(round(v, 6))


# --------------------------------------------------------------------------- canonicalisation
def _snap(x: float, grid: float, lo: float, hi: float) -> float:
    x = min(max(float(x), lo), hi)
    return round(round(x / grid) * grid, 10)


def _clause_key(c: Clause) -> tuple:
    return (c.feature, c.op, -1.0 if c.q is None else c.q, c.labels)


def _snap_clause(c: Clause) -> Clause:
    entry = CATALOG[c.feature]
    if entry.kind == "label":
        return Clause(c.feature, "in", None, tuple(sorted(set(c.labels))))
    if c.q is None:
        return Clause(c.feature, c.op, None, ())
    q = _snap(c.q, Q_GRID, entry.q_lo, entry.q_hi)
    q = min(q, entry.q_hi)  # grid rounding must not leave the domain (e.g. q_hi = 0.995)
    return Clause(c.feature, c.op, round(q, 3), ())


def _is_lower_bound(op: str) -> bool:
    return op in (">", ">=")


def _dedupe_feature(clauses: list[Clause]) -> list[Clause]:
    """Same feature repeated: drop duplicates, keep the tighter bound, drop contradictions."""
    entry = CATALOG[clauses[0].feature]
    clauses = sorted(clauses, key=_clause_key)
    if entry.kind == "label":
        labels = set(clauses[0].labels)
        for c in clauses[1:]:
            if labels & set(c.labels):
                labels &= set(c.labels)
        return [Clause(clauses[0].feature, "in", None, tuple(sorted(labels)))]
    if entry.kind in ("flag", "level", "fixed"):
        first = clauses[0]
        return [first] if entry.kind == "flag" else [
            c for c in clauses if c.op == first.op][:1]
    lows = [c for c in clauses if _is_lower_bound(c.op)]
    highs = [c for c in clauses if not _is_lower_bound(c.op)]
    low = max(lows, key=lambda c: c.q) if lows else None  # tighter lower bound = larger q
    high = min(highs, key=lambda c: c.q) if highs else None  # tighter upper bound = smaller q
    if low and high and not low.q < high.q:  # empty band: keep the first in sorted order
        keep = clauses[0]
        return [keep]
    return [c for c in (low, high) if c is not None]


def _canon_group(clauses: tuple[Clause, ...]) -> tuple[Clause, ...]:
    snapped = [_snap_clause(c) for c in clauses]
    by_feature: dict[str, list[Clause]] = {}
    for c in snapped:
        by_feature.setdefault(c.feature, []).append(c)
    merged = [c for cs in by_feature.values() for c in _dedupe_feature(cs)]
    # alias groups: near-duplicate features carry the same information; keep the first by name
    for members in ALIAS_GROUPS.values():
        present = sorted({c.feature for c in merged if c.feature in members})
        for drop in present[1:]:
            merged = [c for c in merged if c.feature != drop]
    return tuple(sorted(merged, key=_clause_key))


def _canon_stop(stop: StopGene) -> StopGene:
    if stop.kind == "atr_multiple":
        lo, hi = STOP_MULT_DOMAIN
        mult = _snap(stop.multiple or 1.5, STOP_MULT_GRID, lo, hi)
        return StopGene("atr_multiple", round(mult, 2))
    if stop.kind == "last_swing":
        return StopGene("last_swing", None, None, 0.0)
    lo, hi = STOP_OFFSET_DOMAIN
    offset = _snap(stop.offset, STOP_OFFSET_GRID, lo, hi)
    return StopGene("session_level", None, stop.level, round(offset, 2))


def _canon_window(window: tuple[int, int] | None) -> tuple[int, int] | None:
    if window is None:
        return None
    lo, hi = TIME_DOMAIN
    start = int(_snap(min(window), TIME_GRID, lo, hi - MIN_WINDOW_MIN))
    end = int(_snap(max(window), TIME_GRID, lo + MIN_WINDOW_MIN, hi))
    if end - start < MIN_WINDOW_MIN:
        end = min(hi, start + MIN_WINDOW_MIN)
        start = end - MIN_WINDOW_MIN
    return (start, end)


def canonicalize(genome: Genome) -> Genome:
    """Return the canonical form: logically identical genomes map to identical values."""
    regime = _canon_group(genome.regime)
    context = _canon_group(genome.context)
    trigger = _canon_group(genome.trigger)
    or_group = tuple(sorted({_snap_clause(c) for c in genome.or_group}, key=_clause_key))
    if len(or_group) == 1:  # both alternatives identical -> a plain conjunct
        if len(trigger) < MAX_TRIGGER and or_group[0] not in trigger:
            trigger = tuple(sorted(trigger + or_group, key=_clause_key))
        or_group = ()
    return Genome(
        direction=genome.direction,
        regime=regime,
        context=context,
        trigger=trigger,
        or_group=or_group,
        time_window=_canon_window(genome.time_window),
        stop=_canon_stop(genome.stop),
        target_r=round(_snap(genome.target_r, TARGET_R_GRID, *TARGET_R_DOMAIN), 2),
        lineage=genome.lineage,
    )


def canonical_hash(genome: Genome) -> str:
    canon = canonicalize(genome)
    payload = json.dumps(canon.to_dict(with_lineage=False), sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


# --------------------------------------------------------------------------- mirror + compile
def _entry(name: str) -> CatalogEntry:
    return CATALOG[name]


def mirror_rule_parts(
    clause: Clause, resolver: ThresholdResolver | None, short: bool
) -> tuple[str, str, float | bool | None, str | None]:
    """Return (feature, op, threshold, other_feature) of the clause for the given direction."""
    entry = _entry(clause.feature)
    op = clause.op
    if entry.kind == "level":
        other = entry.other_feature
        if short:
            op = flip_op(op)
            if entry.mirror.partner:
                other = _entry(entry.mirror.partner).other_feature
        return "c", op, None, other
    if entry.kind == "flag":
        feature = clause.feature
        if short and entry.mirror.kind == "pair":
            feature = _entry(entry.mirror.partner).feature
        return feature, "==", True, None
    if entry.kind == "fixed":
        value = float(entry.fixed_value)
    else:
        if resolver is None:
            raise ValueError("resolver required for quantile clauses")
        value = resolver.value(entry.feature, float(clause.q), entry.floor)
    feature = entry.feature
    if short:
        kind, center = entry.mirror.kind, entry.mirror.center
        if kind in ("reflect", "pair_reflect"):
            op, value = flip_op(op), 2 * center - value + 0.0
        if kind in ("pair", "pair_reflect"):
            feature = _entry(entry.mirror.partner).feature
    return feature, op, float(round(value, 6)) + 0.0, None


def _rule(clause: Clause, resolver: ThresholdResolver | None, short: bool) -> Rule:
    feature, op, threshold, other = mirror_rule_parts(clause, resolver, short)
    return Rule(feature, op, threshold=threshold, other_feature=other)


def compile_genome(
    genome: Genome, resolver: ThresholdResolver, *, snap: bool = True
) -> StrategySpec:
    """Compile to the declarative spec.  ``snap`` canonicalises first (same hash -> same spec)."""
    g = canonicalize(genome) if snap else genome
    validate(g)
    short = g.direction == "SHORT"
    rules: list[Rule] = []
    regime_filters: dict[str, tuple[str, ...]] = {}
    context_filters: list[str] = []
    for clause in g.regime + g.context + g.trigger:
        entry = _entry(clause.feature)
        if entry.kind == "label":
            labels = clause.labels
            if short and entry.mirror.labels:
                mapping = dict(entry.mirror.labels)
                labels = tuple(mapping.get(x, x) for x in labels)
            labels = tuple(sorted(labels))
            if entry.dimension in regime_filters:
                labels = tuple(sorted(set(labels) & set(regime_filters[entry.dimension])))
                if not labels:
                    raise GenomeError(f"contradictory regime labels for {entry.dimension}")
            regime_filters[entry.dimension] = labels
        elif entry.kind == "flag" and entry.feature.startswith("context_"):
            context_filters.append(entry.feature[len("context_"):].upper())
        else:
            rules.append(_rule(clause, resolver, short))
    if g.time_window is not None:
        rules.append(Rule("berlin_minute", ">=", threshold=int(g.time_window[0])))
        rules.append(Rule("berlin_minute", "<", threshold=int(g.time_window[1])))
    or_groups = (tuple(_rule(c, resolver, short) for c in g.or_group),) if g.or_group else ()
    stop = g.stop
    if stop.kind == "atr_multiple":
        stop_spec = StopSpec("atr_multiple", feature="m5_atr14", multiple=float(stop.multiple))
    elif stop.kind == "last_swing":
        stop_spec = StopSpec("last_swing")
    else:
        level = STOP_LEVEL_MIRROR[stop.level] if short else stop.level
        stop_spec = StopSpec("session_level", level=level, offset=float(stop.offset))
    ghash = canonical_hash(g)
    return StrategySpec(
        strategy_id=f"grammar_{ghash[:12]}",
        version=SPEC_VERSION,
        direction=g.direction,
        entry_rules=tuple(rules),
        stop=stop_spec,
        target=TargetSpec("fixed_r", r=float(g.target_r)),
        regime_filters=regime_filters,
        context_filters=tuple(context_filters),
        or_groups=or_groups,
        params={"target_r": float(g.target_r)},
        metadata={
            "genome_hash": ghash,
            "lineage": g.lineage,
            "complexity": complexity(g),
            "clause_features": sorted(c.feature for c in g.clauses),
        },
    )


def behavior_key(spec: StrategySpec) -> str:
    """Hash of everything that affects candidates (metadata/strategy_id stripped)."""
    stripped = replace(spec, strategy_id="x", metadata={})
    return stripped.spec_hash()


# --------------------------------------------------------------------------- trial ledger
@dataclass
class TrialLedger:
    seen: set[str] = field(default_factory=set)
    total_trials: int = 0
    param_trials: int = 0
    structural_trials: int = 0
    duplicate_rejects: int = 0
    invalid_rejects: int = 0
    cache_hits: int = 0  # evaluations served from the result cache (still counted as trials)

    def record(self, genome: Genome, kind: str = "structural") -> str:
        """Count one trial; returns 'new', 'duplicate' or 'invalid'."""
        if kind not in ("param", "structural"):
            raise ValueError("kind must be 'param' or 'structural'")
        self.total_trials += 1
        if kind == "param":
            self.param_trials += 1
        else:
            self.structural_trials += 1
        try:
            validate(genome)
        except GenomeError:
            self.invalid_rejects += 1
            return "invalid"
        digest = canonical_hash(genome)
        if digest in self.seen:
            self.duplicate_rejects += 1
            return "duplicate"
        self.seen.add(digest)
        return "new"

    @property
    def unique(self) -> int:
        return len(self.seen)

    def to_json(self) -> str:
        return json.dumps(
            {
                "seen": sorted(self.seen), "total_trials": self.total_trials,
                "param_trials": self.param_trials, "structural_trials": self.structural_trials,
                "duplicate_rejects": self.duplicate_rejects,
                "invalid_rejects": self.invalid_rejects, "unique": self.unique,
                "unique_specs": self.unique,  # alias: pool meta / stages / report read this key
                "cache_hits": self.cache_hits,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> TrialLedger:
        raw = json.loads(text)
        return cls(
            set(raw["seen"]), raw["total_trials"], raw["param_trials"], raw["structural_trials"],
            raw["duplicate_rejects"], raw["invalid_rejects"], raw.get("cache_hits", 0),
        )


__all__ = (
    "ThresholdResolver", "TrialLedger", "behavior_key", "canonical_hash", "canonicalize",
    "compile_genome", "mirror_rule_parts",
)
