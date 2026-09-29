"""Post-search filtering stages C-E for the discovery pipeline.  Research only; NEVER touches OOS.

Stage A (Train screening) is done inside the search.  This module runs, over survivors only:

* Stage C  Validation gate, NO parameter change (uses ``validation_gate_view``: this module is the
           only non-test place allowed to call it).
* Stage D  cost stress on Train+Validation POOLED (embargo respected, OOS never selected).
* Stage E  stability: parameter neighbourhood, regime/month dependence, concentration/risk.

Every stage returns a dataclass carrying every number, a ``passed`` flag and ``reasons``.
All thresholds live in ``PipelineConfig`` (single place, overridable from JSON).  The Stage-E
concentration bounds are SANITY GATES, not optimised values.

Costs are re-simulated per candidate (``PooledSim``); the evaluator cache stores no per-trade
arrays.  Neighbour evaluations are recorded on the evaluator ledger as ``param`` trials; the
bookkeeping re-evaluation of pool candidates is ledger-neutral.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from alpha.common.sim import COST_SCENARIOS
from alpha.discovery.catalog import (
    Q_GRID,
    STOP_MULT_GRID,
    STOP_OFFSET_GRID,
    TARGET_R_GRID,
    TIME_GRID,
)
from alpha.discovery.compile import canonical_hash, canonicalize, compile_genome
from alpha.discovery.evaluate import (
    ADVERSE_COST,
    BASE_COST,
    GenomeEval,
    GenomeEvaluator,
    cluster_se,
    validation_gate_view,
)
from alpha.discovery.genome import Genome, GenomeError
from alpha.discovery.search import _quantile_clauses, param_space, with_params
from alpha.fast.screen import _metrics, _subset
from alpha.fast.sim import CandidateArrays, TradeArrays, simulate_fast
from alpha.fast.spec import evaluate_spec

STRESS_COSTS = (BASE_COST, "SPREAD_STRESS", "SLIPPAGE_STRESS", ADVERSE_COST)


# --------------------------------------------------------------------------- configuration
@dataclass(frozen=True)
class PipelineConfig:
    """Every stage threshold in ONE place.  Defaults are the pre-registered brief values."""

    seed: int = 20260930
    # Stage C (Validation, post-embargo, no parameter change)
    # Validation sample guards (from standard error, not tuned to results): with per-trade
    # sd(R) ~ 1..1.5 a mean is only resolved to ~0.2-0.3 R at n = 25; t >= 1.0 asks that the
    # day-clustered (CR1) COMBINED_ADVERSE Validation expectancy is at least one SE above 0.
    c_min_trades: int = 25
    validation_min_t: float = 1.0
    c_top3_share_max: float = 0.6  # top-3 winners' share of positive R (BASE, Validation)
    c_min_profit_factor: float = 1.0  # strictly greater, BASE
    # Stage D (pooled Train+Validation, COMBINED_ADVERSE)
    d_lb_se_mult: float = 1.0  # lower bound = mean - mult * SE (day-clustered, CR1)
    d_lb_min: float = -0.02  # R
    # Stage E (i) parameter neighbourhood
    e_joint_perturbations: int = 30
    e_neighbor_min_positive_frac: float = 0.60
    e_neighbor_worst_min: float = -0.20  # R
    # Stage E (ii) regime / month dependence
    e_bucket_max_positive_share: float = 0.80
    e_month_min_trades: int = 5
    e_month_min_nonneg_frac: float = 0.60
    # Stage E (iii) concentration / risk: SANITY GATES, not optimised values
    e_top3_share_max: float = 0.45
    e_loss_streak_max: int = 12
    e_max_dd_r_max: float = 25.0
    # Stage E (iv) entry-timing robustness: decisions delayed by k M5 bars (original stop and
    # target R kept, fill at the next bar open after the delayed decision), COMBINED_ADVERSE,
    # pooled Train+Validation.  Required: pooled expectancy > e_timing_min_expectancy for every
    # k in e_timing_required and for the seeded random-jitter variant (delay ~ U{0..jitter_max}).
    e_timing_delays: tuple[int, ...] = (1, 2, 3)
    e_timing_required: tuple[int, ...] = (1, 2)
    e_timing_jitter_max: int = 3
    e_timing_min_expectancy: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> PipelineConfig:
        raw = raw or {}
        names = {f.name for f in dataclasses.fields(cls)}
        kw = {k: (tuple(v) if isinstance(v, list) else v) for k, v in raw.items() if k in names}
        return cls(**kw)


# --------------------------------------------------------------------------- pooled simulation
def delay_candidates(cands: CandidateArrays, delays: int | np.ndarray, n_bars: int
                     ) -> CandidateArrays:
    """Shift every decision by ``delays`` M5 bars, keeping stop price, target R and direction.

    Candidates whose shifted index is >= ``n_bars`` are dropped.  The simulator requires strictly
    increasing decision indices, so shifted candidates are stably re-ordered and, if several land
    on the same bar (only possible with per-candidate delays), the earliest original is kept.
    """
    idx = np.asarray(cands.decision_idx, dtype=np.int64)
    shifted = idx + np.asarray(delays, dtype=np.int64)
    keep = np.flatnonzero(shifted < n_bars)
    order = keep[np.argsort(shifted[keep], kind="stable")]
    s = shifted[order]
    if len(s):
        first = np.concatenate(([True], s[1:] != s[:-1]))
        order, s = order[first], s[first]
    return CandidateArrays(s, cands.direction[order], cands.stop[order], cands.target[order],
                           cands.target_r[order], cands.exit_kind[order])


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


@dataclass(frozen=True)
class PooledStats:
    n_trades: int
    expectancy_r: float | None
    se_r: float | None
    lower_bound_r: float | None  # mean - 1.0 * SE (multiplier from the config)
    t_stat: float | None
    profit_factor: float | None
    top3_share: float | None
    max_dd_r: float | None
    max_loss_streak: int
    trades_per_day: float | None
    zero_trade_day_frac: float | None
    cost_burden_r: float | None


def stats_from_r(r: np.ndarray, days: np.ndarray, *, screen: Any = None,
                 lb_mult: float = 1.0) -> PooledStats:
    """Reduce a pooled per-trade R array (chronological) to ``PooledStats``."""
    n = len(r)
    if n == 0:
        return PooledStats(0, None, None, None, None, None, None, None, 0,
                           getattr(screen, "trades_per_day", None),
                           getattr(screen, "zero_trade_day_frac", None), None)
    mean = float(r.mean())
    se = cluster_se(r, days)
    return PooledStats(
        n, mean, se, None if se is None else mean - lb_mult * se,
        None if not se else mean / se,
        _f(screen.profit_factor) if screen is not None else None,
        _f(screen.top_3_positive_r_share) if screen is not None else None,
        _f(screen.max_drawdown_r) if screen is not None else None,
        int(screen.max_loss_streak) if screen is not None else 0,
        _f(screen.trades_per_day) if screen is not None else None,
        _f(screen.zero_trade_day_frac) if screen is not None else None,
        _f(screen.cost_burden) if screen is not None else None,
    )


@dataclass
class PooledTrades:
    """Train+Validation trades (embargo respected) of one genome under one cost scenario."""

    trades: TradeArrays | None
    is_validation: np.ndarray
    stats: PooledStats

    @property
    def r(self) -> np.ndarray:
        return np.zeros(0) if self.trades is None else self.trades.r_multiple

    @property
    def days(self) -> np.ndarray:
        return np.zeros(0, int) if self.trades is None else self.trades.entry_day


class PooledSim:
    """Re-simulates genomes on Train+Validation; memoises per (canonical hash, cost)."""

    def __init__(self, ev: GenomeEvaluator, cfg: PipelineConfig | None = None) -> None:
        self.ev = ev
        self.cfg = cfg or PipelineConfig()
        s = ev.split
        self._train_bars = s.mask(ev.dates, s.train)
        self._val_bars = s.mask(ev.dates, s.validation)
        pooled_bars = self._train_bars | self._val_bars
        self._n_days = len(np.unique(ev.market.day[pooled_bars]))
        self._memo: dict[tuple[str, str], PooledTrades] = {}
        self._cands: dict[str, Any] = {}
        self.simulations = 0

    def _candidates(self, canon: Genome, ghash: str, keep: bool) -> Any:
        if ghash in self._cands:
            return self._cands[ghash]
        spec = compile_genome(canon, self.ev.resolver, snap=False)
        cands = evaluate_spec(self.ev.store, spec)
        if keep:
            self._cands[ghash] = cands
        return cands

    def pooled(self, genome: Genome, cost: str, *, keep: bool = True) -> PooledTrades:
        canon = canonicalize(genome)
        ghash = canonical_hash(canon)
        key = (ghash, cost)
        if keep and key in self._memo:
            return self._memo[key]
        out = self.pooled_from_candidates(self._candidates(canon, ghash, keep), cost)
        if keep:
            self._memo[key] = out
        return out

    def pooled_from_candidates(self, cands: Any, cost: str) -> PooledTrades:
        """Simulate ``cands`` (a CandidateArrays) and reduce to pooled Train+Validation trades."""
        ev = self.ev
        if len(cands.decision_idx):
            trades = simulate_fast(ev.market, cands, COST_SCENARIOS[cost], ev.sizing, ev.rules)
            self.simulations += 1
        else:
            trades = None
        if trades is None or not len(trades):
            out = PooledTrades(None, np.zeros(0, bool), stats_from_r(np.zeros(0), np.zeros(0)))
        else:
            entry_dates = ev.dates[trades.entry_idx]
            is_val = ev.split.mask(entry_dates, ev.split.validation)
            mask = ev.split.mask(entry_dates, ev.split.train) | is_val
            sub = _subset(trades, mask)
            screen = _metrics(sub, self._n_days, contract_size=ev.sizing.contract_size)
            stats = stats_from_r(sub.r_multiple, sub.entry_day, screen=screen,
                                 lb_mult=self.cfg.d_lb_se_mult)
            out = PooledTrades(sub, is_val[mask], stats)
        return out

    def pooled_delayed(self, genome: Genome, cost: str, delays: int | np.ndarray) -> PooledTrades:
        """Pooled trades with every decision delayed (scalar k or per-candidate array)."""
        canon = canonicalize(genome)
        cands = self._candidates(canon, canonical_hash(canon), True)
        delayed = delay_candidates(cands, delays, len(self.ev.market.o))
        return self.pooled_from_candidates(delayed, cost)


def peek_evaluation(ev: GenomeEvaluator, genome: Genome) -> GenomeEval:
    """Evaluate a pool candidate WITHOUT changing the trial ledger (bookkeeping re-read)."""
    led = ev.ledger
    snap = (led.total_trials, led.param_trials, led.structural_trials, led.duplicate_rejects,
            led.invalid_rejects, led.cache_hits, set(led.seen), ev.evaluations)
    try:
        return ev.evaluate(genome, kind="structural")
    finally:
        (led.total_trials, led.param_trials, led.structural_trials, led.duplicate_rejects,
         led.invalid_rejects, led.cache_hits, led.seen, ev.evaluations) = snap


# --------------------------------------------------------------------------- results
@dataclass
class StageCResult:
    passed: bool
    reasons: list[str]
    n_trades: int
    expectancy_base: float | None
    expectancy_adverse: float | None
    profit_factor_base: float | None
    top3_share_base: float | None
    se_adverse: float | None
    t_adverse: float | None  # Validation-only day-clustered t under COMBINED_ADVERSE


@dataclass
class StageDResult:
    passed: bool
    reasons: list[str]
    by_cost: dict[str, PooledStats]
    expectancy_adverse: float | None
    lower_bound_adverse: float | None
    cost_burden_adverse_r: float | None
    r_lost_base_to_adverse: float | None


@dataclass
class NeighborResult:
    name: str  # gene name, or "joint#k"
    step: str  # "-1" | "+1" | "joint"
    expectancy_r: float | None
    n_trades: int


@dataclass
class NeighborhoodResult:
    passed: bool
    reasons: list[str]
    n_neighbors: int
    n_skipped: int
    frac_positive: float | None
    worst_expectancy_r: float | None
    median_expectancy_r: float | None
    neighbors: list[NeighborResult]


@dataclass
class RegimeResult:
    passed: bool
    reasons: list[str]
    tables: dict[str, dict[str, dict[str, float]]]  # dimension -> bucket -> {n, sum_r, mean_r}
    max_positive_share: dict[str, float | None]
    month_nonneg_frac: float | None
    n_months_eligible: int


@dataclass
class ConcentrationResult:
    passed: bool
    reasons: list[str]
    top3_share: float | None
    max_dd_r: float | None
    max_loss_streak: int
    trades_per_day: float | None
    zero_trade_day_frac: float | None


@dataclass
class TimingResult:
    passed: bool
    reasons: list[str]
    undelayed_expectancy: float | None
    by_delay: dict[str, dict[str, float | int | None]]  # "k=1" -> {n_trades, expectancy_r, t_stat}
    jitter: dict[str, float | int | None]


@dataclass
class StageEResult:
    passed: bool
    reasons: list[str]
    neighborhood: NeighborhoodResult
    regime: RegimeResult
    concentration: ConcentrationResult
    timing: TimingResult | None = None
    timing_ok: bool = True  # entry-timing robustness (delays + jitter); part of `passed`


# --------------------------------------------------------------------------- Stage C
def _fmt_t(t: float | None) -> str:
    return "n/a" if t is None else f"{t:.3f}"


def judge_stage_c(ev_result: GenomeEval, cfg: PipelineConfig) -> StageCResult:
    """Pure judgement over a GenomeEval (the only reader of Validation numbers)."""
    view = validation_gate_view(ev_result)
    base, adv = view.base, view.adverse
    n = base.screen.n_trades
    e_base, e_adv = base.screen.expectancy_r, adv.screen.expectancy_r
    pf, top3 = base.screen.profit_factor, base.screen.top_3_positive_r_share
    reasons: list[str] = []
    if ev_result.rejected:
        reasons.append(f"stage A reject: {ev_result.reject}")
    if n < cfg.c_min_trades:
        reasons.append(f"validation trades {n} < {cfg.c_min_trades}")
    if e_base is None or e_base <= 0:
        reasons.append(f"validation expectancy BASE {e_base} <= 0")
    if e_adv is None or e_adv <= 0:
        reasons.append(f"validation expectancy COMBINED_ADVERSE {e_adv} <= 0")
    se = adv.se_r
    t = None if (se is None or se <= 0 or e_adv is None) else e_adv / se
    if t is None or t < cfg.validation_min_t:
        reasons.append(f"validation adverse t {_fmt_t(t)} < {cfg.validation_min_t}")
    if n and pf is not None and pf <= cfg.c_min_profit_factor:  # None = no losing trade -> inf
        reasons.append(f"validation PF BASE {pf:.3f} <= {cfg.c_min_profit_factor}")
    if top3 is not None and top3 > cfg.c_top3_share_max:
        reasons.append(f"validation top-3 share {top3:.3f} > {cfg.c_top3_share_max}")
    return StageCResult(not reasons, reasons, n, _f(e_base), _f(e_adv), _f(pf), _f(top3),
                        _f(se), _f(t))


def stage_c_validation(candidate: Genome | GenomeEval, ev: GenomeEvaluator,
                       cfg: PipelineConfig | None = None) -> StageCResult:
    """VALIDATION gate with no parameter change; accepts a genome or its evaluation."""
    cfg = cfg or PipelineConfig()
    result = candidate if isinstance(candidate, GenomeEval) else peek_evaluation(ev, candidate)
    return judge_stage_c(result, cfg)


# --------------------------------------------------------------------------- Stage D
def judge_stage_d(by_cost: dict[str, PooledStats], cfg: PipelineConfig) -> StageDResult:
    adv, base = by_cost[ADVERSE_COST], by_cost[BASE_COST]
    reasons: list[str] = []
    if adv.expectancy_r is None or adv.expectancy_r <= 0:
        reasons.append(f"pooled expectancy COMBINED_ADVERSE {adv.expectancy_r} <= 0")
    lb = adv.lower_bound_r
    if lb is None or lb <= cfg.d_lb_min:
        reasons.append(f"pooled lower bound COMBINED_ADVERSE {lb} <= {cfg.d_lb_min}")
    lost = (None if base.expectancy_r is None or adv.expectancy_r is None
            else base.expectancy_r - adv.expectancy_r)
    return StageDResult(not reasons, reasons, by_cost, adv.expectancy_r, lb, adv.cost_burden_r,
                        lost)


def stage_d_cost_stress(candidate: Genome, ev: GenomeEvaluator, cfg: PipelineConfig | None = None,
                        sim: PooledSim | None = None) -> StageDResult:
    cfg = cfg or PipelineConfig()
    sim = sim or PooledSim(ev, cfg)
    return judge_stage_d({c: sim.pooled(candidate, c).stats for c in STRESS_COSTS}, cfg)


# --------------------------------------------------------------------------- Stage E (i)
def _step_of(name: str) -> float:
    if name.startswith("q_"):
        return Q_GRID
    return {"stop_mult": STOP_MULT_GRID, "stop_offset": STOP_OFFSET_GRID,
            "target_r": TARGET_R_GRID, "tw_start": float(TIME_GRID),
            "tw_end": float(TIME_GRID)}[name]


def _current_values(genome: Genome) -> dict[str, float]:
    values: dict[str, float] = {}
    for name, _g, _i, clause in _quantile_clauses(genome):
        values[name] = float(clause.q)
    if genome.stop.kind == "atr_multiple":
        values["stop_mult"] = float(genome.stop.multiple)
    elif genome.stop.kind == "session_level":
        values["stop_offset"] = float(genome.stop.offset)
    values["target_r"] = float(genome.target_r)
    if genome.time_window is not None:
        values["tw_start"], values["tw_end"] = map(float, genome.time_window)
    return values


def neighbor_genomes(genome: Genome, cfg: PipelineConfig, rng: np.random.Generator
                     ) -> tuple[list[tuple[str, str, Genome]], int]:
    """One-gene +-1 grid-step neighbours plus seeded joint +-1 perturbations.

    Neighbours identical to the base (boundary clamp) or to each other, or invalid, are dropped;
    the second return value counts what was dropped.
    """
    base = canonicalize(genome)
    base_hash = canonical_hash(base)
    space = param_space(base)
    current = _current_values(base)
    names = [s[0] for s in space if s[0] in current]
    seen = {base_hash}
    out: list[tuple[str, str, Genome]] = []
    skipped = 0

    def add(label: str, step: str, params: dict[str, float]) -> None:
        nonlocal skipped
        try:
            g = with_params(base, {**current, **params})
        except GenomeError:
            skipped += 1
            return
        h = canonical_hash(canonicalize(g))
        if h in seen:
            skipped += 1
            return
        seen.add(h)
        out.append((label, step, g))

    for name in names:
        for sign in (-1, 1):
            add(name, f"{sign:+d}", {name: current[name] + sign * _step_of(name)})
    for k in range(cfg.e_joint_perturbations):
        signs = rng.choice((-1, 1), size=len(names))
        add(f"joint#{k}", "joint",
            {n: current[n] + int(s) * _step_of(n) for n, s in zip(names, signs, strict=True)})
    return out, skipped


def judge_neighborhood(neighbors: list[NeighborResult], n_skipped: int,
                       cfg: PipelineConfig) -> NeighborhoodResult:
    reasons: list[str] = []
    n = len(neighbors)
    if n == 0:
        return NeighborhoodResult(False, ["no evaluable neighbours"], 0, n_skipped, None, None,
                                  None, [])
    exps = [x.expectancy_r for x in neighbors]
    positive = sum(1 for e in exps if e is not None and e > 0)
    frac = positive / n
    known = [e for e in exps if e is not None]
    worst = min(known) if known else None
    median = float(np.median(known)) if known else None
    if frac < cfg.e_neighbor_min_positive_frac:
        reasons.append(f"neighbours positive {frac:.2f} < {cfg.e_neighbor_min_positive_frac}")
    if worst is not None and worst < cfg.e_neighbor_worst_min:
        reasons.append(f"worst neighbour {worst:.3f} R < {cfg.e_neighbor_worst_min}")
    return NeighborhoodResult(not reasons, reasons, n, n_skipped, frac, worst, median, neighbors)


def _neighborhood(genome: Genome, ev: GenomeEvaluator, cfg: PipelineConfig, sim: PooledSim,
                  ghash: str) -> NeighborhoodResult:
    rng = np.random.default_rng([cfg.seed, int(ghash[:12], 16)])
    neighbors, skipped = neighbor_genomes(genome, cfg, rng)
    results: list[NeighborResult] = []
    for label, step, g in neighbors:
        ev.ledger.record(g, "param")  # neighbour evaluations are trials
        p = sim.pooled(g, ADVERSE_COST, keep=False)
        results.append(NeighborResult(label, step, p.stats.expectancy_r, p.stats.n_trades))
    return judge_neighborhood(results, skipped, cfg)


# --------------------------------------------------------------------------- Stage E (ii)
def bucket_table(labels: np.ndarray, r: np.ndarray) -> dict[str, dict[str, float]]:
    table: dict[str, dict[str, float]] = {}
    for lab in sorted({str(x) for x in labels}):
        sel = labels == lab
        table[lab] = {"n": int(sel.sum()), "sum_r": float(r[sel].sum()),
                      "mean_r": float(r[sel].mean())}
    return table


def max_positive_share(table: dict[str, dict[str, float]]) -> float | None:
    """Largest bucket's share of the summed POSITIVE bucket totals (None if < 2 buckets)."""
    if len(table) < 2:
        return None  # single populated bucket: by construction of the strategy, not evaluated
    pos = [v["sum_r"] for v in table.values() if v["sum_r"] > 0]
    return max(pos) / sum(pos) if pos else None


def judge_regime(tables: dict[str, dict[str, dict[str, float]]], cfg: PipelineConfig
                 ) -> RegimeResult:
    reasons: list[str] = []
    shares: dict[str, float | None] = {}
    for dim, table in tables.items():
        share = max_positive_share(table)
        shares[dim] = share
        if share is not None and share > cfg.e_bucket_max_positive_share:
            top = max(table, key=lambda b: table[b]["sum_r"])
            reasons.append(f"{dim}: bucket {top} carries {share:.2f} of positive R "
                           f"> {cfg.e_bucket_max_positive_share}")
    months = [v for k, v in tables.get("month", {}).items() if v["n"] >= cfg.e_month_min_trades]
    frac = None
    if not months:
        reasons.append(f"no calendar month with >= {cfg.e_month_min_trades} trades")
    else:
        frac = sum(1 for v in months if v["mean_r"] >= 0) / len(months)
        if frac < cfg.e_month_min_nonneg_frac:
            reasons.append(f"non-negative months {frac:.2f} < {cfg.e_month_min_nonneg_frac}")
    return RegimeResult(not reasons, reasons, tables, shares, frac, len(months))


def _label_names(store: Any, group: str, key: str | None = None) -> dict[int, str]:
    maps = getattr(store, "metadata", {}).get("maps", {})
    raw = maps.get(group, {})
    if key is not None:
        raw = raw.get(key, {})
    return {int(k): str(v) for k, v in raw.items()}


def regime_tables(pt: PooledTrades, ev: GenomeEvaluator) -> dict[str, dict[str, dict]]:
    """Pooled R by H1 regime (at the decision bar), Berlin phase/hour and calendar month."""
    tr, store = pt.trades, ev.store
    r, dec = tr.r_multiple, tr.decision_idx
    tables: dict[str, dict[str, dict[str, float]]] = {}
    for dim, key in (("regime_direction", "DIRECTION"), ("regime_vol_state", "VOL_STATE")):
        names = _label_names(store, "regime", key)
        codes = np.asarray(store[dim])[dec]
        tables[dim] = bucket_table(np.asarray([names.get(int(c), str(int(c))) for c in codes]), r)
    names = _label_names(store, "phase")
    codes = np.asarray(store["phase_code"])[dec]
    tables["phase"] = bucket_table(np.asarray([names.get(int(c), str(int(c))) for c in codes]), r)
    hours = np.asarray(store["berlin_minute"])[dec] // 60
    tables["hour"] = bucket_table(np.asarray([f"H{int(h):02d}" for h in hours]), r)
    months = ev.dates[tr.entry_idx].astype("datetime64[M]").astype(str)
    tables["month"] = bucket_table(np.asarray(months), r)
    return tables


# --------------------------------------------------------------------------- Stage E (iii)
def judge_concentration(stats: PooledStats, cfg: PipelineConfig) -> ConcentrationResult:
    reasons: list[str] = []
    if stats.top3_share is not None and stats.top3_share > cfg.e_top3_share_max:
        reasons.append(f"pooled top-3 share {stats.top3_share:.3f} > {cfg.e_top3_share_max}")
    if stats.max_loss_streak > cfg.e_loss_streak_max:
        reasons.append(f"loss streak {stats.max_loss_streak} > {cfg.e_loss_streak_max}")
    if stats.max_dd_r is not None and stats.max_dd_r > cfg.e_max_dd_r_max:
        reasons.append(f"max drawdown {stats.max_dd_r:.1f} R > {cfg.e_max_dd_r_max}")
    return ConcentrationResult(not reasons, reasons, stats.top3_share, stats.max_dd_r,
                               stats.max_loss_streak, stats.trades_per_day,
                               stats.zero_trade_day_frac)


def judge_timing(undelayed: float | None, by_delay: dict[int, PooledStats], jitter: PooledStats,
                 cfg: PipelineConfig) -> TimingResult:
    """Pure judgement: required delays and the jitter variant must keep expectancy > floor."""
    floor = cfg.e_timing_min_expectancy
    reasons: list[str] = []
    for k in cfg.e_timing_required:
        e = by_delay[k].expectancy_r if k in by_delay else None
        if e is None or e <= floor:
            reasons.append(f"delay k={k} pooled adverse expectancy {e} <= {floor}")
    if jitter.expectancy_r is None or jitter.expectancy_r <= floor:
        reasons.append(f"random-jitter pooled adverse expectancy {jitter.expectancy_r} <= {floor}")
    rows = {f"k={k}": {"n_trades": s.n_trades, "expectancy_r": s.expectancy_r,
                       "t_stat": s.t_stat} for k, s in by_delay.items()}
    jrow = {"n_trades": jitter.n_trades, "expectancy_r": jitter.expectancy_r,
            "t_stat": jitter.t_stat}
    return TimingResult(not reasons, reasons, undelayed, rows, jrow)


def stage_e_timing(candidate: Genome, cfg: PipelineConfig, sim: PooledSim) -> TimingResult:
    canon = canonicalize(candidate)
    ghash = canonical_hash(canon)
    base = sim.pooled(candidate, ADVERSE_COST).stats
    by_delay = {int(k): sim.pooled_delayed(candidate, ADVERSE_COST, int(k)).stats
                for k in cfg.e_timing_delays}
    cands = sim._candidates(canon, ghash, True)
    rng = np.random.default_rng([cfg.seed, int(ghash[:8], 16)])
    delays = rng.integers(0, cfg.e_timing_jitter_max + 1, size=len(cands.decision_idx))
    jitter = sim.pooled_delayed(candidate, ADVERSE_COST, delays).stats
    return judge_timing(base.expectancy_r, by_delay, jitter, cfg)


def stage_e_stability(candidate: Genome, ev: GenomeEvaluator, cfg: PipelineConfig | None = None,
                      sim: PooledSim | None = None) -> StageEResult:
    cfg = cfg or PipelineConfig()
    sim = sim or PooledSim(ev, cfg)
    ghash = canonical_hash(canonicalize(candidate))
    pt = sim.pooled(candidate, ADVERSE_COST)
    hood = _neighborhood(candidate, ev, cfg, sim, ghash)
    if pt.stats.n_trades:
        regime = judge_regime(regime_tables(pt, ev), cfg)
    else:
        regime = RegimeResult(False, ["no pooled trades"], {}, {}, None, 0)
    conc = judge_concentration(pt.stats, cfg)
    reasons = [f"neighbourhood: {x}" for x in hood.reasons]
    reasons += [f"regime: {x}" for x in regime.reasons]
    reasons += [f"concentration: {x}" for x in conc.reasons]
    timing = stage_e_timing(candidate, cfg, sim)
    reasons += [f"timing: {x}" for x in timing.reasons]
    return StageEResult(not reasons, reasons, hood, regime, conc, timing, timing.passed)


# --------------------------------------------------------------------------- pipeline
@dataclass
class CandidateResult:
    canonical_hash: str
    genome: Genome
    train_fitness: float | None
    origin: str | None
    lineage: str | None
    train_positive: bool
    validation_positive: bool
    stage_c: StageCResult | None = None
    stage_d: StageDResult | None = None
    stage_e: StageEResult | None = None
    selection: Any = None  # selection.SelectionStats, filled by run_pipeline

    @property
    def furthest_stage(self) -> str:
        if self.stage_e is not None:
            return "E"
        return "D" if self.stage_d is not None else ("C" if self.stage_c is not None else "A")

    @property
    def passed_all(self) -> bool:
        return bool(self.stage_c and self.stage_c.passed and self.stage_d and self.stage_d.passed
                    and self.stage_e and self.stage_e.passed)


@dataclass
class PipelineResult:
    candidates: list[CandidateResult]
    counts: dict[str, int]
    ledger_before: dict[str, int]
    ledger_after: dict[str, int]
    sim: PooledSim = field(repr=False, default=None)  # type: ignore[assignment]
    # N used by the selection statistics: campaign totals + prior (earlier campaigns') trials
    accounting: dict[str, int] = field(default_factory=dict)

    def survivors(self, stage: str) -> list[CandidateResult]:
        attr = {"C": "stage_c", "D": "stage_d", "E": "stage_e"}[stage]
        return [c for c in self.candidates
                if getattr(c, attr) is not None and getattr(c, attr).passed]


def ledger_snapshot(ev: GenomeEvaluator) -> dict[str, int]:
    led = ev.ledger
    return {"total_trials": led.total_trials, "param_trials": led.param_trials,
            "structural_trials": led.structural_trials, "unique_specs": led.unique,
            "duplicate_rejects": led.duplicate_rejects, "invalid_rejects": led.invalid_rejects,
            "cache_hits": led.cache_hits}


def run_pipeline(pool: dict[str, Any], ev: GenomeEvaluator, cfg: PipelineConfig | None = None,
                 prior_trials: int | None = None, prior_unique_specs: int | None = None
                 ) -> PipelineResult:
    """Stage A (done in search) -> C -> D -> E over the candidate pool; only survivors advance.

    Selection statistics use N = this campaign's total trials + ``prior_trials`` (and unique
    specs + ``prior_unique_specs`` for Bonferroni); the priors default to
    ``meta.prior_trials`` / ``meta.prior_unique_specs`` of the pool, else 0.
    """
    from alpha.discovery.selection import ledger_unique, selection_stats  # selection imports stages

    cfg = cfg or PipelineConfig()
    meta = pool.get("meta", {})
    if meta.get("oos_touched") is not False:
        raise ValueError("pool meta must assert oos_touched == false; refusing to run")
    ledger_before = ledger_snapshot(ev)
    sim = PooledSim(ev, cfg)
    if prior_trials is None:
        prior_trials = int(meta.get("prior_trials") or 0)
    if prior_unique_specs is None:
        prior_unique_specs = int(meta.get("prior_unique_specs") or 0)
    camp_total = int(meta.get("ledger", {}).get("total_trials") or ev.ledger.total_trials)
    camp_unique = int(ledger_unique(meta.get("ledger")) or ev.ledger.unique)
    n_total = camp_total + int(prior_trials)
    n_unique = camp_unique + int(prior_unique_specs)
    accounting = {"campaign_total_trials": camp_total, "campaign_unique_specs": camp_unique,
                  "prior_trials": int(prior_trials), "prior_unique_specs": int(prior_unique_specs),
                  "n_total_trials": n_total, "n_unique_specs": n_unique}
    counts = {"pool": 0, "stage_a": 0, "train_positive": 0, "validation_positive": 0,
              "train_and_validation_positive": 0, "C": 0, "D": 0, "E": 0}
    seen: set[str] = set()
    out: list[CandidateResult] = []
    for entry in pool["candidates"]:
        genome = Genome.from_dict(entry["genome"])
        h = canonical_hash(canonicalize(genome))
        if h in seen:
            continue
        seen.add(h)
        counts["pool"] += 1
        res = peek_evaluation(ev, genome)
        canon = canonicalize(genome)
        cand = CandidateResult(
            h, canon, entry.get("train_fitness"), entry.get("origin"),
            entry.get("lineage", genome.lineage), False, False)
        out.append(cand)
        if res.rejected:
            continue
        counts["stage_a"] += 1
        view = validation_gate_view(res)
        cand.train_positive = (res.train.adverse.screen.expectancy_r or 0.0) > 0
        cand.validation_positive = (view.adverse.screen.expectancy_r or 0.0) > 0
        counts["train_positive"] += cand.train_positive
        counts["validation_positive"] += cand.validation_positive
        counts["train_and_validation_positive"] += cand.train_positive and cand.validation_positive
        cand.stage_c = judge_stage_c(res, cfg)
        if not cand.stage_c.passed:
            continue
        counts["C"] += 1
        cand.stage_d = stage_d_cost_stress(canon, ev, cfg, sim)
        adv = sim.pooled(canon, ADVERSE_COST)
        cand.selection = selection_stats(adv.r, adv.days, cand.stage_c.t_adverse,
                                         n_total, n_unique)
        if not cand.stage_d.passed:
            continue
        counts["D"] += 1
        cand.stage_e = stage_e_stability(canon, ev, cfg, sim)
        if cand.stage_e.passed:
            counts["E"] += 1
    return PipelineResult(out, counts, ledger_before, ledger_snapshot(ev), sim, accounting)


def _num(x: Any) -> Any:
    if isinstance(x, (np.floating, np.integer)):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def to_plain(obj: Any) -> Any:
    """Recursively convert result dataclasses/numpy scalars into JSON-safe plain data."""
    if isinstance(obj, Genome):
        return obj.to_dict()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)
                if f.name != "sim"}
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return _num(obj)


__all__ = (
    "STRESS_COSTS",
    "CandidateResult",
    "PipelineConfig",
    "PipelineResult",
    "PooledSim",
    "PooledStats",
    "PooledTrades",
    "StageCResult",
    "StageDResult",
    "StageEResult",
    "TimingResult",
    "delay_candidates",
    "judge_concentration",
    "judge_neighborhood",
    "judge_regime",
    "judge_stage_c",
    "judge_stage_d",
    "judge_timing",
    "ledger_snapshot",
    "neighbor_genomes",
    "peek_evaluation",
    "run_pipeline",
    "stage_c_validation",
    "stage_d_cost_stress",
    "stage_e_stability",
    "stage_e_timing",
    "to_plain",
)
