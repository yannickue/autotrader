# ruff: noqa: E501
"""Train-only evaluation of family specs: simulate, V1 Train fitness, BH, day-shifted-label null, baselines.

Everything here operates on a TRAIN VIEW (``FamilyData`` / ``MarketArrays`` that physically contain no later
bar).  Numbers: ``simulate_fast`` per market (``SimWindow`` from the spec's effective window, ``market_costs``
sizing/costs), V1 ``train_fitness`` over a V1 ``TrainView`` (COMBINED_ADVERSE, day-clustered SE, 3 calendar
chunks), day-clustered t, cost burden.  Nothing from a later partition exists in this module; the later-fold stage
lives in a separate, sealed module.

Day-shifted-label NULL.  The candidates of a spec are re-mapped to the same clock bar of the day ``s`` days later
(circular over the Train days) and simulated there with the original stop/target DISTANCES in price units: signal
structure, direction, time-of-day and risk size are preserved while the price path that decides the outcome comes
from a different day.  The best-of-grid statistic under this null (repeated for many shifts) is the reference for
"best of N specs" (a family whose real best is inside the null band has found nothing).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from alpha.common.sim import CostScenario, SimRules, SizingSpec
from alpha.discovery.evaluate import N_CHUNKS, SideMetrics, TrainView, _side
from alpha.discovery.fitness import INVALID_FLOOR, train_fitness
from alpha.discovery.folds import berlin_dates_from_ts_ns
from alpha.discovery.temporal_evaluate import TemporalTrialLedger
from alpha.families.common import Thr, empty_candidates
from alpha.families.data import FamilyData
from alpha.families.registry import fit_thresholds, generate_candidates
from alpha.families.spec import FamilySpec, MarketCalendar
from alpha.fast.screen import screen_partition_trades
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, MarketArrays, TradeArrays, simulate_fast

BASE = "BASE"
ADVERSE = "COMBINED_ADVERSE"
MIN_TRAIN_TRADES = 40


# --------------------------------------------------------------------------- context
@dataclass(frozen=True)
class SimContext:
    market: MarketArrays
    sizing: SizingSpec
    rules: SimRules
    costs: dict[str, CostScenario]  # BASE, COMBINED_ADVERSE
    cal: MarketCalendar

    @classmethod
    def default(cls, data: FamilyData) -> SimContext:
        """V1 GER40 sizing / rules / costs (synthetic tests, GER40)."""
        from alpha.common.sim import COST_SCENARIOS, DEFAULT_RULES, DEFAULT_SIZING

        return cls(data.market_arrays(), DEFAULT_SIZING, DEFAULT_RULES, {n: COST_SCENARIOS[n] for n in (BASE, ADVERSE)}, data.cal)

    @classmethod
    def for_market(cls, data: FamilyData, market_spec: Any, *, research_equity_eur: float = 10000.0,
                   risk_fraction: float = 0.005, max_trades_per_day: int = 6) -> SimContext:
        """Spec-correct wiring (same as ``research/runners/v2_probe.py::market_sim_params``): costs / sizing from
        ``alpha.common.market_costs``, spread cap = the spec's PRICE-unit cap, local-clock window."""
        from alpha.common.market_costs import cost_scenarios_for, sizing_for

        sizing = sizing_for(market_spec, account_eur=research_equity_eur, risk_fraction=risk_fraction)
        rules = SimRules(max_trades_per_day=max_trades_per_day, max_entry_spread_pts=market_spec.max_entry_spread_price)
        costs = {n: c for n, c in cost_scenarios_for(market_spec).items() if n in (BASE, ADVERSE)}
        return cls(data.market_arrays(), sizing, rules, costs, MarketCalendar.from_market_spec(market_spec))


def simulate(ctx: SimContext, spec: FamilySpec, cands: CandidateArrays, cost: str) -> TradeArrays:
    win = spec.effective_window(ctx.cal).sim_window()
    return simulate_fast(ctx.market, cands, ctx.costs[cost], ctx.sizing, ctx.rules, win)


def chunk_of_bar(data: FamilyData) -> np.ndarray:
    """3 equal-CALENDAR chunks of the view (same rule as the V1 evaluator's Train chunks)."""

    def build() -> np.ndarray:
        d = berlin_dates_from_ts_ns(data.ts_ns)
        if len(d) == 0:
            return np.zeros(0, dtype=np.int64)
        total = int((d.max() - d.min()).astype(int)) + 1
        off = (d - d.min()).astype(int)
        return np.clip(off * N_CHUNKS // total, 0, N_CHUNKS - 1)

    return data.memo("chunk_of_bar", build)


def n_days_of(data: FamilyData) -> int:
    return len(np.unique(data.day))


def side_metrics(trades: TradeArrays, ctx: SimContext, data: FamilyData) -> SideMetrics:
    mask = np.ones(len(trades), dtype=bool)
    screen = screen_partition_trades(trades, mask, n_days_of(data), contract_size=ctx.sizing.contract_size)
    chunks = chunk_of_bar(data)[trades.entry_idx] if len(trades) else np.zeros(0, dtype=np.int64)
    return _side(trades, mask, screen, chunks)


# --------------------------------------------------------------------------- results
def one_sided_p(t: float | None) -> float | None:
    """P(T >= t) under N(0,1): H1 = positive expectancy."""
    return None if t is None or not math.isfinite(t) else 0.5 * math.erfc(t / math.sqrt(2.0))


def bh_adjust(p: list[float | None]) -> list[float | None]:
    """Benjamini-Hochberg step-up adjusted p-values over the non-None entries (None stays None)."""
    idx = [i for i, v in enumerate(p) if v is not None]
    out: list[float | None] = [None] * len(p)
    m = len(idx)
    if m == 0:
        return out
    order = sorted(idx, key=lambda i: p[i])  # type: ignore[arg-type,return-value]
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, p[i] * m / rank)  # type: ignore[operator]
        out[i] = prev
    return out


@dataclass
class SpecResult:
    spec: FamilySpec
    spec_hash: str
    n_candidates: int
    n_trades: int
    trades_per_day: float | None
    exp_base: float | None
    exp_adv: float | None
    pf_adv: float | None
    payoff_adv: float | None
    win_rate_adv: float | None
    t_adv: float | None
    se_adv: float | None
    cost_burden_adv: float | None  # mean (spread+slippage+commission) cost in R
    spread_cost_r_adv: float | None  # mean entry/exit spread cost in R
    fitness: float
    skips: dict[str, int]
    p_one_sided: float | None = None
    q_bh: float | None = None

    def row(self) -> dict[str, Any]:
        def r(x, k=5):
            return None if x is None else round(float(x), k)

        return {
            "hash": self.spec_hash, "spec": self.spec.to_dict(), "n_candidates": self.n_candidates, "n_trades": self.n_trades,
            "trades_per_day": r(self.trades_per_day, 4), "exp_r_base": r(self.exp_base), "exp_r_adverse": r(self.exp_adv),
            "profit_factor_adverse": r(self.pf_adv, 4), "payoff_adverse": r(self.payoff_adv, 4),
            "win_rate_adverse": r(self.win_rate_adv, 4), "t_day_clustered": r(self.t_adv, 3), "se_r": r(self.se_adv),
            "cost_burden_r": r(self.cost_burden_adv), "spread_cost_r": r(self.spread_cost_r_adv), "fitness": r(self.fitness, 6),
            "p_one_sided": r(self.p_one_sided, 6), "q_bh": r(self.q_bh, 6),
            "skips": {k: v for k, v in self.skips.items() if v},
        }


def _adverse_view(spec: FamilySpec, side_adv: SideMetrics, side_base: SideMetrics | None) -> TrainView:
    return TrainView(spec.canonical_hash(), spec.complexity, side_base, side_adv)


def evaluate_spec(data: FamilyData, ctx: SimContext, spec: FamilySpec, thr: Thr, *, min_trades: int = MIN_TRAIN_TRADES,
                  cands: CandidateArrays | None = None) -> tuple[SpecResult, CandidateArrays]:
    cands = generate_candidates(data, spec, thr) if cands is None else cands
    n_c = len(cands.decision_idx)
    if n_c == 0:
        return SpecResult(spec, spec.canonical_hash(), 0, 0, None, None, None, None, None, None, None, None, None, None,
                          INVALID_FLOOR, {}), cands
    tr_adv = simulate(ctx, spec, cands, ADVERSE)
    tr_base = simulate(ctx, spec, cands, BASE)
    adv, base = side_metrics(tr_adv, ctx, data), side_metrics(tr_base, ctx, data)
    s = adv.screen
    t = None
    if s.expectancy_r is not None and adv.se_r and adv.se_r > 0:
        t = s.expectancy_r / adv.se_r
    spread_r = float(np.mean(tr_adv.spread_cost_pts / tr_adv.risk_pts)) if len(tr_adv) else None
    fit_v = train_fitness(_adverse_view(spec, adv, base), min_trades)
    return SpecResult(
        spec, spec.canonical_hash(), n_c, s.n_trades, s.trades_per_day, base.screen.expectancy_r, s.expectancy_r,
        s.profit_factor, s.payoff, s.win_rate, t, adv.se_r, s.cost_burden, spread_r, fit_v, tr_adv.skips,
        one_sided_p(t) if s.n_trades >= min_trades else None,
    ), cands


def evaluate_grid(data: FamilyData, ctx: SimContext, specs: list[FamilySpec], *, min_trades: int = MIN_TRAIN_TRADES
                  ) -> tuple[list[SpecResult], list[CandidateArrays]]:
    """Evaluate all specs on the Train view; BH-adjusted q-values are set within THIS grid."""
    results, cand_list = [], []
    for spec in specs:
        thr = fit_thresholds(data, spec)
        res, c = evaluate_spec(data, ctx, spec, thr, min_trades=min_trades)
        results.append(res)
        cand_list.append(c)
    for res, q in zip(results, bh_adjust([r.p_one_sided for r in results]), strict=True):
        res.q_bh = q
    return results, cand_list


# --------------------------------------------------------------------------- trial ledger
@dataclass(frozen=True)
class TrialKey:
    """(spec, market) evaluation identity for the cumulative ledger."""

    spec: FamilySpec
    market: str

    def validate(self) -> None:
        self.spec.validate()

    def digest(self) -> str:
        return f"{self.market}:{self.spec.canonical_hash()}"


def make_ledger() -> TemporalTrialLedger:
    return TemporalTrialLedger(validate_fn=lambda k: k.validate(), hash_fn=lambda k: k.digest())


def load_ledger(text: str | None) -> TemporalTrialLedger:
    led = make_ledger()
    if text:
        old = TemporalTrialLedger.from_json(text)
        for f in ("seen", "total_trials", "param_trials", "structural_trials", "duplicate_rejects", "invalid_rejects", "cache_hits", "behaviors", "twins"):
            setattr(led, f, getattr(old, f))
    return led


# --------------------------------------------------------------------------- day-shifted-label null
def _day_keys(data: FamilyData) -> tuple[np.ndarray, int]:
    def build() -> tuple[np.ndarray, int]:
        rank = np.unique(data.day, return_inverse=True)[1].astype(np.int64)
        return rank * 1440 + data.minute.astype(np.int64), int(rank.max()) + 1 if len(rank) else 0

    return data.memo("day_keys", build)


def shift_candidates(cands: CandidateArrays, data: FamilyData, shift_days: int) -> CandidateArrays:
    """Same clock bar ``shift_days`` days later (circular); stop/target DISTANCES in price units are preserved."""
    if len(cands.decision_idx) == 0:
        return cands
    keys, n_days = _day_keys(data)
    i = cands.decision_idx
    tgt_key = (((keys[i] // 1440) + shift_days) % n_days) * 1440 + keys[i] % 1440
    j = np.minimum(np.searchsorted(keys, tgt_key), len(keys) - 1)
    ok = keys[j] == tgt_key
    if not ok.any():
        return empty_candidates()
    jj, ii = j[ok], i[ok]
    dirn, ctgt, ctr = cands.direction[ok], cands.target[ok], cands.target_r[ok]
    d = dirn.astype(float)
    stop = data.c[jj] - d * (d * (data.c[ii] - cands.stop[ok]))
    target = np.where(np.isfinite(ctgt), data.c[jj] + d * (d * (ctgt - data.c[ii])), np.nan)
    order = np.argsort(jj, kind="stable")
    jj, dirn, stop, target, ctr = jj[order], dirn[order], stop[order], target[order], ctr[order]
    keep = np.r_[True, jj[1:] != jj[:-1]]
    return CandidateArrays(jj[keep], dirn[keep], stop[keep], target[keep], ctr[keep],
                           np.full(int(keep.sum()), EXIT_FIXED_R, dtype=np.int8))


def null_calibration(data: FamilyData, ctx: SimContext, specs: list[FamilySpec], cands: list[CandidateArrays],
                     real: list[SpecResult], *, n_shifts: int, seed: int, min_trades: int = MIN_TRAIN_TRADES) -> dict[str, Any]:
    """Best-of-grid statistics under the day-shifted-label null vs the real best-of-grid.

    Statistics: V1 ``train_fitness`` (the selection criterion) and the COMBINED_ADVERSE expectancy among specs with
    at least ``min_trades`` trades.  ``pct`` = share of null bests strictly below the real best; ``p_best`` = (1 +
    #null bests >= real best) / (1 + K)."""
    _, n_days = _day_keys(data)
    rng = np.random.default_rng(seed)
    lo, hi = max(5, n_days // 20), n_days - max(5, n_days // 20)
    pool = np.arange(lo, hi)
    shifts = np.sort(rng.choice(pool, size=min(n_shifts, len(pool)), replace=False)) if len(pool) else np.zeros(0, int)
    best_fit, best_exp = [], []
    for s in shifts:
        bf, be = INVALID_FLOOR, -np.inf
        for spec, c in zip(specs, cands, strict=True):
            sc = shift_candidates(c, data, int(s))
            if len(sc.decision_idx) == 0:
                continue
            tr = simulate(ctx, spec, sc, ADVERSE)
            adv = side_metrics(tr, ctx, data)
            f = train_fitness(_adverse_view(spec, adv, None), min_trades)
            bf = max(bf, f)
            if adv.screen.n_trades >= min_trades and adv.screen.expectancy_r is not None:
                be = max(be, adv.screen.expectancy_r)
        best_fit.append(bf)
        best_exp.append(be)
    real_fit = max((r.fitness for r in real), default=INVALID_FLOOR)
    real_exp_vals = [r.exp_adv for r in real if r.exp_adv is not None and r.n_trades >= min_trades]
    real_exp = max(real_exp_vals) if real_exp_vals else None
    nf, ne = np.asarray(best_fit), np.asarray(best_exp)
    k = len(nf)

    def stat(real_v, null_v):
        if real_v is None or k == 0:
            return {"real_best": None, "null_best_mean": None, "null_best_p95": None, "pct": None, "p_best": None}
        finite = null_v[np.isfinite(null_v)]
        return {
            "real_best": round(float(real_v), 6),
            "null_best_mean": round(float(finite.mean()), 6) if len(finite) else None,
            "null_best_p95": round(float(np.quantile(finite, 0.95)), 6) if len(finite) else None,
            "pct": round(float((null_v < real_v).mean()), 4),
            "p_best": round(float((1 + (null_v >= real_v).sum()) / (1 + k)), 4),
        }

    return {
        "method": "day-shifted labels: same clock bar of the day s days later (circular), stop/target distances kept",
        "n_shifts": int(k), "shifts_days": [int(s) for s in shifts], "n_specs": len(specs), "min_trades": min_trades,
        "fitness": stat(real_fit, nf), "expectancy_r_adverse": stat(real_exp, ne),
        "null_evaluations": int(k * len(specs)),
    }


# --------------------------------------------------------------------------- drift baselines
def drift_baselines(data: FamilyData, ctx: SimContext, seed: int, *, draws: int = 20, stop_atr: float = 1.0,
                    target_r: float = 1.5, decisions_per_day: int = 3) -> dict[str, Any]:
    """Market-level Train baselines under COMBINED_ADVERSE: always long / always short / random side / same-session random.

    Same construction as ``research/runners/v2_probe.py::drift_baselines`` (every entry-window bar is a
    candidate, ATR stop, fixed-R target; the simulator's one-position / day-cap rules thin them out)."""
    win = ctx.cal.window()
    m, atr = ctx.market, data.atr
    ok = np.isfinite(atr) & (atr > 0) & (m.minute >= win.entry_start_min) & (m.minute < win.entry_end_min)
    idx = np.flatnonzero(ok)
    nd = n_days_of(data)

    def sim(decision: np.ndarray, direction: np.ndarray) -> dict[str, Any]:
        if len(decision) == 0:
            return {"n_trades": 0, "expectancy_r": None, "trades_per_day": 0.0}
        o = np.argsort(decision, kind="stable")
        decision, direction = decision[o], direction[o].astype(np.int8)
        stop = m.c[decision] - direction * stop_atr * atr[decision]
        c = CandidateArrays(decision, direction, stop, np.full(len(decision), np.nan), np.full(len(decision), target_r),
                            np.full(len(decision), EXIT_FIXED_R, dtype=np.int8))
        tr = simulate_fast(m, c, ctx.costs[ADVERSE], ctx.sizing, ctx.rules, win)
        sc = screen_partition_trades(tr, np.ones(len(tr), bool), nd, contract_size=ctx.sizing.contract_size)
        return {"n_trades": sc.n_trades, "expectancy_r": None if sc.expectancy_r is None else round(sc.expectancy_r, 5),
                "trades_per_day": None if sc.trades_per_day is None else round(sc.trades_per_day, 4)}

    def summ(rows: list[dict]) -> dict[str, Any]:
        e = [r["expectancy_r"] for r in rows if r["expectancy_r"] is not None]
        return {"draws": len(rows), "expectancy_r_mean": round(float(np.mean(e)), 5) if e else None,
                "expectancy_r_sd": round(float(np.std(e)), 5) if len(e) > 1 else None,
                "trades_mean": round(float(np.mean([r["n_trades"] for r in rows])), 1) if rows else None}

    rng = np.random.default_rng(seed + 7)
    out: dict[str, Any] = {
        "cost": ADVERSE, "stop_atr": stop_atr, "target_r": target_r, "eligible_train_bars": len(idx), "train_days": nd,
        "always_long": sim(idx, np.ones(len(idx), np.int8)), "always_short": sim(idx, -np.ones(len(idx), np.int8)),
        "random_long_short": summ([sim(idx, rng.choice(np.array([-1, 1], np.int8), len(idx))) for _ in range(draws)]),
    }
    days, start = np.unique(m.day[idx], return_index=True)
    bounds = np.r_[start, len(idx)]
    ss = []
    for _ in range(draws):
        pick = np.concatenate([bounds[j] + rng.choice(bounds[j + 1] - bounds[j], min(decisions_per_day, bounds[j + 1] - bounds[j]), replace=False)
                               for j in range(len(days))]) if len(days) else np.zeros(0, int)
        ss.append(sim(idx[pick], rng.choice(np.array([-1, 1], np.int8), len(pick))))
    out["same_session_random"] = {**summ(ss), "decisions_per_day": decisions_per_day}
    return out


__all__ = (
    "ADVERSE", "BASE", "MIN_TRAIN_TRADES", "SimContext", "SpecResult", "TrialKey", "bh_adjust", "chunk_of_bar",
    "drift_baselines", "evaluate_grid", "evaluate_spec", "load_ledger", "make_ledger", "n_days_of", "null_calibration",
    "one_sided_p", "shift_candidates", "side_metrics", "simulate",
)
