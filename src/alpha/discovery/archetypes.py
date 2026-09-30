"""The 15 archetype templates and the random genome sampler (deterministic per numpy seed).

Every template is a function ``(rng, pool) -> Genome`` written in the LONG frame.  A slot lists
alternative catalog features in order of preference; the first one present in the
``FeaturePool`` is used, so templates degrade gracefully while the new bar/level features are
not yet in the FeatureSet.  ``random_genome`` jitters quantiles, so no two draws share a grid
cell by construction, only by chance (measured as the duplicate rate).
"""

# ruff: noqa: E501  (the template DSL reads best as one slot per line)
from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np

from alpha.discovery.catalog import (
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
    Clause,
    Genome,
    StopGene,
    is_valid,
)

# slot alternative: (feature, op, (q_lo, q_hi) | None, labels | None)
Alt = tuple[str, str, tuple[float, float] | None, tuple[str, ...] | None]
Slot = tuple[Sequence[Alt], float]  # (alternatives, probability the slot is used)

HYBRID_SHARE = 0.25


def _clause(pool: FeaturePool, rng: np.random.Generator, alt: Alt) -> Clause | None:
    name, op, qrange, labels = alt
    if not pool.has(name):
        return None
    entry = pool.entries[name]
    if entry.kind == "label":
        return Clause(name, "in", None, tuple(labels or entry.labels[:1]))
    if entry.kind in ("flag", "fixed", "level"):
        return Clause(name, op if op in entry.ops else entry.ops[0], None, ())
    lo, hi = qrange or (entry.q_lo, entry.q_hi)
    lo, hi = max(lo, entry.q_lo), min(hi, entry.q_hi)
    if lo > hi:
        lo, hi = entry.q_lo, entry.q_hi
    op = op if op in entry.ops else entry.ops[0]
    return Clause(name, op, float(rng.uniform(lo, hi)), ())


def _group(pool: FeaturePool, rng: np.random.Generator, slots: Sequence[Slot], cap: int) -> tuple[Clause, ...]:
    out: list[Clause] = []
    for alts, prob in slots:
        if len(out) >= cap or (prob < 1.0 and rng.random() >= prob):
            continue
        for alt in alts:
            clause = _clause(pool, rng, alt)
            if clause is not None:
                out.append(clause)
                break
    return tuple(out)


def _window(rng: np.random.Generator, starts: Sequence[int], lengths: Sequence[int]) -> tuple[int, int]:
    start = int(rng.choice(starts))
    return start, min(start + int(rng.choice(lengths)), TIME_DOMAIN[1])


SAFE_STOP_LEVELS = ("previous_day_low", "session_low")  # LONG frame: below price by construction


def _stop(rng: np.random.Generator, weights: tuple[float, float, float] = (0.75, 0.1, 0.15),
          levels: Sequence[str] = SAFE_STOP_LEVELS) -> StopGene:
    kind = rng.choice(["atr_multiple", "last_swing", "session_level"], p=np.asarray(weights) / sum(weights))
    if kind == "atr_multiple":
        return StopGene("atr_multiple", float(rng.uniform(*STOP_MULT_DOMAIN)))
    if kind == "last_swing":
        return StopGene("last_swing", None, None, 0.0)
    return StopGene("session_level", None, str(rng.choice(list(levels))),
                    float(rng.uniform(*STOP_OFFSET_DOMAIN)))


def _target(rng: np.random.Generator) -> float:
    return float(rng.uniform(*TARGET_R_DOMAIN))


def _direction(rng: np.random.Generator) -> str:
    return "LONG" if rng.random() < 0.5 else "SHORT"


def _build(name: str, pool: FeaturePool, rng: np.random.Generator, *, regime: Sequence[Slot] = (),
           context: Sequence[Slot] = (), trigger: Sequence[Slot] = (),
           window: tuple[int, int] | None = None, stop: StopGene | None = None,
           or_slots: Sequence[Slot] = ()) -> Genome:
    trig = _group(pool, rng, trigger, MAX_TRIGGER)
    reg = _group(pool, rng, regime, MAX_REGIME)
    ctx = _group(pool, rng, context, MAX_CONTEXT)
    or_group = _group(pool, rng, or_slots, MAX_OR)
    if len(or_group) != MAX_OR:
        or_group = ()
    if not trig:  # graceful degradation: no template trigger available -> random one
        trig = (_random_clause(pool, rng, ("TRIGGER", "LEVEL")),)
    if len(reg) + len(ctx) + len(trig) + len(or_group) > MAX_TOTAL_CLAUSES:
        or_group = ()  # trim optional structure first, then the weakest context/regime clause
    while len(reg) + len(ctx) + len(trig) > MAX_TOTAL_CLAUSES:
        if len(ctx) > 1:
            ctx = ctx[:-1]
        else:
            reg = reg[:-1]
    return Genome(_direction(rng), reg, ctx, trig, or_group, window, stop or _stop(rng),
                  _target(rng), name)


def _s(*alts: Alt, p: float = 1.0) -> Slot:
    return (alts, p)


def _c(name: str, op: str = ">", q: tuple[float, float] | None = None,
       labels: tuple[str, ...] | None = None) -> Alt:
    return (name, op, q, labels)


def trend_continuation(rng, pool):
    return _build(
        "TREND_CONTINUATION", pool, rng,
        regime=[_s(_c("regime_direction", labels=("UP",)), _c("h1_ema_slope", ">", (0.5, 0.85))),
                _s(_c("h1_adx14", ">", (0.4, 0.8)), _c("h1_efficiency_ratio", ">", (0.4, 0.8)), p=0.8)],
        context=[_s(_c("context_trend_continuation"), _c("m15_ema_slope", ">", (0.5, 0.85)))],
        trigger=[_s(_c("mom_6_atr", ">", (0.5, 0.85)), _c("m5_ema_slope", ">", (0.5, 0.85))),
                 _s(_c("brk_up_20"), _c("m5_adx14", ">", (0.4, 0.8)), p=0.7)],
    )


def trend_pullback(rng, pool):
    return _build(
        "TREND_PULLBACK", pool, rng,
        regime=[_s(_c("regime_direction", labels=("UP",)), _c("h1_ema_slope", ">", (0.5, 0.85))),
                _s(_c("h1_adx14", ">", (0.4, 0.8)), p=0.5)],
        context=[_s(_c("context_pullback_up"), p=0.5)],
        trigger=[_s(_c("from_high_24_atr", ">", (0.35, 0.65)), _c("m5_rsi14", "<", (0.25, 0.55))),
                 _s(_c("mom_3_atr", ">", (0.5, 0.8)), _c("m5_normalized_return", ">", (0.5, 0.85))),
                 _s(_c("from_high_24_atr", "<", (0.8, 0.95)), p=0.4)],
    )


def momentum_continuation(rng, pool):
    return _build(
        "MOMENTUM_CONTINUATION", pool, rng,
        regime=[_s(_c("h1_ema_slope", ">", (0.5, 0.85)), _c("regime_direction", labels=("UP",)), p=0.7)],
        context=[_s(_c("context_momentum_continuation_up"), _c("m15_ema_slope", ">", (0.6, 0.9)))],
        trigger=[_s(_c("mom_6_atr", ">", (0.6, 0.9)), _c("m5_ema_slope", ">", (0.6, 0.9))),
                 _s(_c("m5_efficiency_ratio", ">", (0.5, 0.85))),
                 _s(_c("m5_adx14", ">", (0.5, 0.85)), p=0.6)],
    )


def opening_drive(rng, pool):
    return _build(
        "OPENING_DRIVE", pool, rng,
        context=[_s(_c("m15_adx14", ">", (0.3, 0.6)), _c("m15_efficiency_ratio", ">", (0.4, 0.7)), p=0.4)],
        trigger=[_s(_c("bar_range_atr", ">", (0.4, 0.8)), _c("m5_volatility_percentile", ">", (0.4, 0.8))),
                 _s(_c("mom_6_atr", ">", (0.5, 0.85)), _c("m5_ema_slope", ">", (0.5, 0.85))),
                 _s(_c("brk_up_20", ">", (0.6, 0.9)), _c("lvl_c_sess_high", ">="), p=0.5)],
        window=_window(rng, (540, 555, 570), (60, 90, 120)),
    )


def breakout(rng, pool):
    return _build(
        "BREAKOUT", pool, rng,
        regime=[_s(_c("h1_volatility_percentile", ">", (0.3, 0.7)), p=0.4)],
        context=[_s(_c("context_breakout_setup_up"), _c("context_consolidation"), p=0.3)],
        trigger=[_s(_c("brk_up_20", ">", (0.85, 0.95)), _c("brk_up_48", ">", (0.8, 0.95)),
                    _c("lvl_c_pdh"), _c("lvl_c_sess_high", ">=")),
                 _s(_c("bar_body_ratio", ">", (0.4, 0.75)), _c("m5_volatility_percentile", ">", (0.4, 0.8))),
                 _s(_c("bar_close_loc", ">", (0.6, 0.85)), _c("m5_range_position", ">", (0.6, 0.85)), p=0.5)],
    )


def breakout_retest(rng, pool):
    return _build(
        "BREAKOUT_RETEST", pool, rng,
        regime=[_s(_c("regime_direction", labels=("UP",)), _c("h1_ema_slope", ">", (0.5, 0.85)), p=0.7)],
        context=[_s(_c("context_retest_up"))],
        trigger=[_s(_c("dist_pdh_atr", "<", (0.5, 0.8)), _c("dist_sess_high_atr", "<", (0.5, 0.8)),
                    _c("lvl_c_pdh"), _c("lvl_c_sess_open")),
                 _s(_c("dist_pdh_atr", ">", (0.3, 0.5)), _c("dist_swing_high_atr", ">", (0.3, 0.5)),
                    _c("m5_range_position", ">", (0.4, 0.7))),
                 _s(_c("bar_dir", ">"), _c("mom_3_atr", ">", (0.5, 0.85)), _c("m5_normalized_return", ">", (0.5, 0.85)))],
        stop=_stop(rng, (0.5, 0.2, 0.3)),
    )


def compression_expansion(rng, pool):
    return _build(
        "COMPRESSION_EXPANSION", pool, rng,
        regime=[_s(_c("regime_vol_state", labels=("COMPRESSION",)),  # both labels = always true
                   _c("h1_bollinger_width", "<", (0.2, 0.6)), p=0.3)],
        context=[_s(_c("range_ratio_12_48", "<", (0.25, 0.6)), _c("compression_expansion_ratio", "<", (0.3, 0.6)),
                    _c("context_compression"))],
        trigger=[_s(_c("brk_up_20", ">", (0.8, 0.95)), _c("bar_range_atr", ">", (0.6, 0.9)),
                    _c("m5_volatility_percentile", ">", (0.6, 0.95))),
                 _s(_c("bar_body_ratio", ">", (0.4, 0.75)), _c("m5_range_position", ">", (0.6, 0.9)), p=0.7)],
    )


def failed_breakout(rng, pool):
    # LONG frame = failed breakdown that reclaims; SHORT mirror = failed breakout (sweep_hi/pdh)
    sweep = ("sweep_lo_20", "sweep_pdl") if rng.random() < 0.6 else ("sweep_pdl", "sweep_lo_20")
    return _build(
        "FAILED_BREAKOUT", pool, rng,
        context=[_s(_c("context_failed_breakout_up"), _c("context_reversal_up"), p=0.25)],
        trigger=[_s(_c(sweep[0]), _c(sweep[1])),
                 _s(_c("bar_close_loc", ">", (0.4, 0.75)), _c("lower_wick_ratio", ">", (0.3, 0.7)),
                    _c("m5_range_position", ">", (0.3, 0.6))),
                 _s(_c("lower_wick_ratio", ">", (0.3, 0.6)), _c("m5_rsi14", "<", (0.2, 0.5)), p=0.4)],
        stop=_stop(rng, (0.6, 0.15, 0.25), ("session_low", "previous_day_low")),
    )


def mean_reversion(rng, pool):
    return _build(
        "MEAN_REVERSION", pool, rng,
        regime=[_s(_c("regime_trend_strength", labels=("RANGE_LIKE",)), _c("h1_adx14", "<", (0.2, 0.5))),
                _s(_c("h1_efficiency_ratio", "<", (0.2, 0.5)), p=0.5)],
        context=[_s(_c("context_range_extreme_bottom"), _c("m15_range_position", "<", (0.05, 0.3)))],
        trigger=[_s(_c("m5_rsi14", "<", (0.05, 0.25))),
                 _s(_c("m5_range_position", "<", (0.05, 0.3)), _c("dist_sess_low_atr", "<", (0.1, 0.35)), p=0.7)],
    )


def range_rejection(rng, pool):
    return _build(
        "RANGE_REJECTION", pool, rng,
        regime=[_s(_c("regime_trend_strength", labels=("RANGE_LIKE", "WEAK")), _c("h1_adx14", "<", (0.2, 0.55)), p=0.7)],
        context=[_s(_c("context_consolidation"), _c("context_range_extreme_bottom")),
                 _s(_c("m15_range_position", "<", (0.05, 0.3)))],
        trigger=[_s(_c("lower_wick_ratio", ">", (0.5, 0.9)), _c("bar_close_loc", ">", (0.5, 0.85)), _c("m5_rsi14", "<", (0.1, 0.35))),
                 _s(_c("m5_range_position", "<", (0.1, 0.4)), p=0.6)],
    )


def prev_day_level_reaction(rng, pool):
    return _build(
        "PREV_DAY_LEVEL", pool, rng,
        regime=[_s(_c("regime_direction", labels=("UP",)), _c("h1_ema_slope", ">", (0.4, 0.8)), p=0.4)],
        trigger=[_s(_c("dist_pdl_atr", "<", (0.1, 0.3)), _c("lvl_c_pdl", ">")),
                 _s(_c("bar_close_loc", ">", (0.5, 0.8)), _c("lower_wick_ratio", ">", (0.3, 0.6)),
                    _c("mom_3_atr", ">", (0.5, 0.8))),
                 _s(_c("sweep_pdl"), _c("lower_wick_ratio", ">", (0.4, 0.8)), p=0.35)],
        stop=_stop(rng, (0.6, 0.15, 0.25), ("previous_day_low", "session_low")),
    )


def session_sweep_reclaim(rng, pool):
    sweep = ("sweep_pdl", "sweep_lo_20") if rng.random() < 0.4 else ("sweep_lo_20", "sweep_pdl")
    return _build(
        "SESSION_SWEEP", pool, rng,
        regime=[_s(_c("regime_direction", labels=("UP",)), _c("h1_ema_slope", ">", (0.4, 0.8)), p=0.6)],
        trigger=[_s(_c(sweep[0]), _c(sweep[1]), _c("lvl_c_sess_low", "<=")),
                 _s(_c("bar_close_loc", ">", (0.4, 0.75)), _c("m5_range_position", ">", (0.3, 0.7))),
                 _s(_c("dist_sess_low_atr", "<", (0.2, 0.5)), _c("m5_rsi14", "<", (0.2, 0.5)), p=0.4)],
        stop=_stop(rng, (0.6, 0.15, 0.25), ("session_low", "previous_day_low")),
    )


def volatility_transition(rng, pool):
    return _build(
        "VOL_TRANSITION", pool, rng,
        regime=[_s(_c("regime_vol_state", labels=("EXPANSION",)), _c("h1_volatility_percentile", ">", (0.5, 0.85))),
                _s(_c("regime_direction", labels=("UP",)), p=0.4)],
        context=[_s(_c("m15_volatility_percentile", ">", (0.4, 0.8)), _c("compression_expansion_ratio", ">", (0.5, 0.85)))],
        trigger=[_s(_c("m5_volatility_percentile", ">", (0.5, 0.9)), _c("bar_range_atr", ">", (0.5, 0.9))),
                 _s(_c("mom_3_atr", ">", (0.5, 0.85)), _c("m5_ema_slope", ">", (0.5, 0.85)), p=0.8)],
    )


def price_action_continuation(rng, pool):
    return _build(
        "PA_CONTINUATION", pool, rng,
        regime=[_s(_c("regime_direction", labels=("UP",)), _c("h1_ema_slope", ">", (0.5, 0.85)), p=0.7)],
        context=[_s(_c("context_trend_continuation"), _c("m15_ema_slope", ">", (0.5, 0.85)), p=0.7)],
        trigger=[_s(_c("bar_dir", ">"), _c("m5_normalized_return", ">", (0.5, 0.85))),
                 _s(_c("bar_body_ratio", ">", (0.5, 0.85)), _c("m5_efficiency_ratio", ">", (0.5, 0.85))),
                 _s(_c("bar_close_loc", ">", (0.6, 0.9)), _c("m5_range_position", ">", (0.6, 0.9)))],
    )


def price_action_reversal(rng, pool):
    return _build(
        "PA_REVERSAL", pool, rng,
        context=[_s(_c("context_reversal_up"), _c("m15_range_position", "<", (0.1, 0.4)), p=0.4)],
        trigger=[_s(_c("lower_wick_ratio", ">", (0.4, 0.8)), _c("m5_rsi14", "<", (0.2, 0.45))),
                 _s(_c("bar_close_loc", ">", (0.5, 0.8)), _c("m5_range_position", ">", (0.3, 0.6))),
                 _s(_c("from_high_24_atr", ">", (0.6, 0.9)), _c("bar_dir", ">"), p=0.6)],
    )


ARCHETYPES: dict[str, Callable[[np.random.Generator, FeaturePool], Genome]] = {
    "TREND_CONTINUATION": trend_continuation,
    "TREND_PULLBACK": trend_pullback,
    "MOMENTUM_CONTINUATION": momentum_continuation,
    "OPENING_DRIVE": opening_drive,
    "BREAKOUT": breakout,
    "BREAKOUT_RETEST": breakout_retest,
    "COMPRESSION_EXPANSION": compression_expansion,
    "FAILED_BREAKOUT": failed_breakout,
    "MEAN_REVERSION": mean_reversion,
    "RANGE_REJECTION": range_rejection,
    "PREV_DAY_LEVEL": prev_day_level_reaction,
    "SESSION_SWEEP": session_sweep_reclaim,
    "VOL_TRANSITION": volatility_transition,
    "PA_CONTINUATION": price_action_continuation,
    "PA_REVERSAL": price_action_reversal,
}


# --------------------------------------------------------------------------- random assembly
def random_clause(pool: FeaturePool, rng: np.random.Generator, layers: Sequence[str]) -> Clause:
    return _random_clause(pool, rng, layers)


def _random_clause(pool: FeaturePool, rng: np.random.Generator, layers: Sequence[str]) -> Clause:
    names = pool.layer(*layers)
    if not names:
        raise ValueError(f"feature pool has no features for layers {tuple(layers)}")
    entry = pool.entries[names[int(rng.integers(len(names)))]]
    if entry.kind == "label":
        k = 1 if len(entry.labels) < 3 or rng.random() < 0.7 else 2
        labels = tuple(str(x) for x in rng.choice(list(entry.labels), size=k, replace=False))
        return Clause(entry.name, "in", None, labels)
    op = str(rng.choice(list(entry.ops)))
    if entry.kind in ("flag", "fixed", "level"):
        return Clause(entry.name, op, None, ())
    return Clause(entry.name, op, float(rng.uniform(entry.q_lo, entry.q_hi)), ())


def _hybrid(rng: np.random.Generator, pool: FeaturePool) -> Genome:
    n_reg = int(rng.choice([0, 1, 2], p=[0.25, 0.5, 0.25])) if pool.layer("REGIME") else 0
    n_ctx = int(rng.choice([0, 1, 2], p=[0.25, 0.5, 0.25])) if pool.layer("CONTEXT") else 0
    n_trig = int(rng.choice([1, 2, 3], p=[0.3, 0.45, 0.25]))
    use_or = rng.random() < 0.15 and n_reg + n_ctx + n_trig + MAX_OR <= MAX_TOTAL_CLAUSES
    trig_layers = ("TRIGGER", "LEVEL")
    return Genome(
        _direction(rng),
        tuple(_random_clause(pool, rng, ("REGIME",)) for _ in range(n_reg)),
        tuple(_random_clause(pool, rng, ("CONTEXT",)) for _ in range(n_ctx)),
        tuple(_random_clause(pool, rng, trig_layers) for _ in range(n_trig)),
        tuple(_random_clause(pool, rng, trig_layers) for _ in range(MAX_OR)) if use_or else (),
        _window(rng, range(480, 1140, 30), (60, 90, 120, 180, 240)) if rng.random() < 0.25 else None,
        _stop(rng),
        _target(rng),
        "HYBRID",
    )


def random_genome(rng: np.random.Generator, pool: FeaturePool) -> Genome:
    """Uniform archetype (75%) or a pure-random role-assembled HYBRID (25%); canonical form."""
    names = sorted(ARCHETYPES)
    if rng.random() < HYBRID_SHARE:
        raw = _hybrid(rng, pool)
    else:
        raw = ARCHETYPES[names[int(rng.integers(len(names)))]](rng, pool)
    genome = canonicalize(raw)
    if not is_valid(genome):  # cannot happen for pools built from the catalog; stay safe
        genome = canonicalize(_hybrid(rng, pool))
    return genome


__all__ = ("ARCHETYPES", "HYBRID_SHARE", "random_clause", "random_genome")
