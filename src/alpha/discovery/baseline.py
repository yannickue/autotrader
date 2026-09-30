"""Random-entry / drift baseline for a discovered genome (research only).

Question answered: does the candidate's *entry timing* add anything over entering at random
times with the same direction, stop rule, target R, time-of-day window, trade count, entry
minute-of-day distribution and per-Berlin-day trade count?  A LONG "edge" that only harvests the
day's upward drift is indistinguishable from this baseline.

Method (per candidate, per partition Train / Validation / pooled):

* Reference stream = the candidate's own spec with every entry rule removed except the time
  window (and regime / context / OR filters dropped): all bars where the spec's stop is valid.
* Tradable pool = the subset of those decision bars that ``simulate_fast`` actually fills under
  COMBINED_ADVERSE when simulated in isolation (contiguity, spread, risk range, size).  Isolation
  is exact: candidates are simulated in passes of decision bars exactly ``SPACING`` (> one trading
  day of M5 bars) apart, so no trade blocks another.
* Each draw copies the real trades' structure: real trades are grouped by Berlin day; each group
  (its multiset of decision-bar minutes) is assigned to a random DISTINCT day of the same
  partition that has enough tradable bars and, for every real minute, the tradable bar of that
  day nearest in minute-of-day is used.  Draws are simulated with the real cost / sizing / rules
  (this may drop a few trades through overlap / day cap; the mean simulated trade count is
  reported).
* Pooled draw b = union of the Train draw b and the Validation draw b.

Statistics: baseline mean/sd of expectancy (R, mean over trades) across B draws, excess =
candidate E - baseline mean, one-sided permutation p = (1 + #{baseline >= candidate E}) /
(1 + B) (``p_raw`` without the +1).  Drift-only reference: same draws with direction forced LONG
(identical to the baseline for LONG candidates).
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from alpha.common.sim import CostScenario, SimRules, SizingSpec
from alpha.fast.sim import (
    CandidateArrays,
    MarketArrays,
    TradeArrays,
    simulate_fast,
)
from alpha.fast.spec import Rule, StrategySpec, evaluate_spec

SPACING = 160  # > 150 M5 bars of one Berlin trading day (09:00-21:30)
DEFAULT_DRAWS = 500
PARTITIONS = ("train", "validation")


# --------------------------------------------------------------------------- pool
def reference_spec(spec: StrategySpec) -> StrategySpec:
    """Spec with only the time-window rules kept (all other entry conditions removed)."""
    window = tuple(r for r in spec.entry_rules if r.feature == "berlin_minute")
    if not window:
        window = (Rule("berlin_minute", ">=", threshold=0),)
    return dataclasses.replace(spec, entry_rules=window, regime_filters={},
                               context_filters=(), or_groups=())


def tradable_mask(market: MarketArrays, cands: CandidateArrays, cost: CostScenario,
                  sizing: SizingSpec, rules: SimRules, spacing: int = SPACING) -> np.ndarray:
    """Boolean mask over ``cands``: decision bars that would be filled when simulated alone."""
    ok = np.zeros(len(cands.decision_idx), dtype=bool)
    slot = cands.decision_idx % spacing
    pos = {int(d): k for k, d in enumerate(cands.decision_idx)}
    for p in np.unique(slot):
        sel = np.flatnonzero(slot == p)
        sub = _take(cands, sel)
        trades = simulate_fast(market, sub, cost, sizing, rules)
        for d in trades.decision_idx:
            ok[pos[int(d)]] = True
    return ok


def _take(c: CandidateArrays, idx: np.ndarray) -> CandidateArrays:
    return CandidateArrays(c.decision_idx[idx], c.direction[idx], c.stop[idx], c.target[idx],
                           c.target_r[idx], c.exit_kind[idx])


@dataclass
class _DayPool:
    """Tradable decision bars of ONE partition, indexed by Berlin day."""

    cands: CandidateArrays
    day_ids: np.ndarray  # distinct day keys
    starts: np.ndarray
    counts: np.ndarray
    minute: np.ndarray  # minute-of-day per pool candidate (sorted by day then index)
    order: np.ndarray  # positions into ``cands`` sorted by (day, decision idx)


def _build_pool(market: MarketArrays, cands: CandidateArrays, keep: np.ndarray) -> _DayPool:
    sel = np.flatnonzero(keep)
    sub = _take(cands, sel)
    day = market.day[sub.decision_idx]
    order = np.lexsort((sub.decision_idx, day))
    day_sorted = day[order]
    ids, starts, counts = np.unique(day_sorted, return_index=True, return_counts=True)
    return _DayPool(sub, ids, starts, counts, market.minute[sub.decision_idx][order], order)


# --------------------------------------------------------------------------- draws
def _draw(rng: np.random.Generator, pool: _DayPool, groups: list[np.ndarray]) -> np.ndarray:
    """Positions into ``pool.cands`` for one random assignment of the real day groups."""
    used = np.zeros(len(pool.day_ids), dtype=bool)
    out: list[int] = []
    for gi in np.argsort([-len(g) + rng.random() * 0.5 for g in groups], kind="stable"):
        g = groups[gi]
        ok = np.flatnonzero(~used & (pool.counts >= len(g)))
        if len(ok) == 0:
            continue
        d = int(ok[rng.integers(len(ok))])
        used[d] = True
        s, cnt = int(pool.starts[d]), int(pool.counts[d])
        minutes = pool.minute[s:s + cnt]
        free = np.ones(cnt, dtype=bool)
        for m in g:
            gap = np.abs(minutes - m).astype(float)
            gap[~free] = np.inf
            best = np.flatnonzero(gap == gap.min())
            k = int(best[rng.integers(len(best))])
            free[k] = False
            out.append(int(pool.order[s + k]))
    return np.asarray(out, dtype=np.int64)


def _cands_from(pool_a: _DayPool, pos_a: np.ndarray, pool_b: _DayPool, pos_b: np.ndarray
                ) -> CandidateArrays:
    """Merge the draws of two partitions into one sorted CandidateArrays."""
    names = ("decision_idx", "direction", "stop", "target", "target_r", "exit_kind")
    cols = {n: np.concatenate([getattr(pool_a.cands, n)[pos_a], getattr(pool_b.cands, n)[pos_b]])
            for n in names}
    o = np.argsort(cols["decision_idx"], kind="stable")
    keep = np.ones(len(o), dtype=bool)
    idx = cols["decision_idx"][o]
    keep[1:] = idx[1:] != idx[:-1]  # partitions use distinct days: no duplicates expected
    return CandidateArrays(*(cols[n][o][keep] for n in names))


@dataclass(frozen=True)
class RealStructure:
    """What a random draw must reproduce, per partition."""

    groups: dict[str, list[np.ndarray]]  # per partition: minute arrays of real trades per day
    mean_r: dict[str, float | None]
    n: dict[str, int]
    r: dict[str, np.ndarray]


def real_structure(trades: TradeArrays, market: MarketArrays, masks: dict[str, np.ndarray]
                   ) -> RealStructure:
    groups, mean_r, n, rr = {}, {}, {}, {}
    for name, m in masks.items():
        r = trades.r_multiple[m]
        rr[name] = r
        n[name] = int(m.sum())
        mean_r[name] = float(r.mean()) if len(r) else None
        days = trades.entry_day[m]
        minutes = market.minute[trades.decision_idx[m]]
        groups[name] = [minutes[days == d] for d in np.unique(days)]
    return RealStructure(groups, mean_r, n, rr)


def _stats(cand_e: float | None, draws: np.ndarray, n_real: int, mean_n: float) -> dict[str, Any]:
    d = draws[np.isfinite(draws)]
    if cand_e is None or not len(d):
        return {"cand_E": cand_e, "base_mean": None, "base_sd": None, "excess": None,
                "p": None, "p_raw": None, "n_cand": n_real, "base_n_mean": mean_n}
    ge = int((d >= cand_e).sum())
    return {"cand_E": cand_e, "base_mean": float(d.mean()),
            "base_sd": float(d.std(ddof=1)) if len(d) > 1 else 0.0,
            "excess": float(cand_e - d.mean()), "p": (1 + ge) / (1 + len(d)),
            "p_raw": ge / len(d), "n_cand": n_real, "base_n_mean": mean_n, "n_draws": len(d)}


def baseline_draws(market: MarketArrays, pools: dict[str, _DayPool], real: RealStructure,
                   cost: CostScenario, sizing: SizingSpec, rules: SimRules,
                   entry_masks, draws: int, seed: int) -> dict[str, np.ndarray]:
    """B simulated random-entry expectancies per partition and pooled.

    ``entry_masks(trades) -> {name: bool mask}`` maps simulated trades to partitions."""
    rng = np.random.default_rng(seed)
    out = {k: np.full(draws, np.nan) for k in (*PARTITIONS, "pooled")}
    nn = {k: np.zeros(draws) for k in out}
    for b in range(draws):
        pos = {p: _draw(rng, pools[p], real.groups[p]) for p in PARTITIONS}
        cands = _cands_from(pools["train"], pos["train"], pools["validation"],
                               pos["validation"])
        if not len(cands.decision_idx):
            continue
        trades = simulate_fast(market, cands, cost, sizing, rules)
        masks = entry_masks(trades)
        for p in PARTITIONS:
            r = trades.r_multiple[masks[p]]
            nn[p][b] = len(r)
            if len(r):
                out[p][b] = r.mean()
        r_all = np.concatenate([trades.r_multiple[masks[p]] for p in PARTITIONS])
        nn["pooled"][b] = len(r_all)
        if len(r_all):
            out["pooled"][b] = r_all.mean()
    out["_n"] = {k: float(v.mean()) for k, v in nn.items()}  # type: ignore[assignment]
    return out


# --------------------------------------------------------------------------- driver
class BaselineContext:
    """Shared objects of one pipeline evaluation (same frame / embargo / cost as the search)."""

    def __init__(self, evaluator: Any) -> None:
        self.ev = evaluator
        self.market, self.dates, self.split = evaluator.market, evaluator.dates, evaluator.split
        self.cost = evaluator._costs["COMBINED_ADVERSE"]
        self.sizing, self.rules = evaluator.sizing, evaluator.rules
        self.part_masks = {"train": self.split.mask(self.dates, self.split.train),
                           "validation": self.split.mask(self.dates, self.split.validation)}

    def entry_masks(self, trades: TradeArrays) -> dict[str, np.ndarray]:
        ed = self.dates[trades.entry_idx] if len(trades) else self.dates[:0]
        return {"train": self.split.mask(ed, self.split.train),
                "validation": self.split.mask(ed, self.split.validation)}

    def pool_for(self, spec: StrategySpec) -> dict[str, _DayPool]:
        cands = evaluate_spec(self.ev.store, reference_spec(spec))
        keep_all = tradable_mask(self.market, cands, self.cost, self.sizing, self.rules)
        out = {}
        for p in PARTITIONS:
            in_p = self.part_masks[p][cands.decision_idx]
            out[p] = _build_pool(self.market, cands, keep_all & in_p)
        return out


def genome_seed(canonical_hash: str, base_seed: int) -> int:
    return int(hashlib.sha256(f"{base_seed}:{canonical_hash}".encode()).hexdigest()[:8], 16)


def drift_baseline(ctx: BaselineContext, genome: Any, draws: int = DEFAULT_DRAWS,
                   seed: int = 20260930) -> dict[str, Any]:
    """Full baseline record for one genome (schema documented in the module docstring)."""
    from alpha.discovery.compile import canonical_hash, canonicalize, compile_genome

    canon = canonicalize(genome)
    ghash = canonical_hash(canon)
    spec = compile_genome(canon, ctx.ev.resolver, snap=False)
    trades = simulate_fast(ctx.market, evaluate_spec(ctx.ev.store, spec), ctx.cost,
                           ctx.sizing, ctx.rules)
    real = real_structure(trades, ctx.market, ctx.entry_masks(trades))
    s = genome_seed(ghash, seed)
    res = baseline_draws(ctx.market, ctx.pool_for(spec), real, ctx.cost, ctx.sizing, ctx.rules,
                         ctx.entry_masks, draws, s)
    r_pool = np.concatenate([real.r[p] for p in PARTITIONS])
    cand = {**real.mean_r, "pooled": float(r_pool.mean()) if len(r_pool) else None}
    n_real = {**real.n, "pooled": len(r_pool)}
    rec: dict[str, Any] = {
        k: _stats(cand[k], res[k], n_real[k], res["_n"][k]) for k in (*PARTITIONS, "pooled")}
    rec["direction"] = canon.direction
    if canon.direction == "LONG":
        long_res = res
    else:
        lg = dataclasses.replace(canon, direction="LONG")
        lspec = compile_genome(lg, ctx.ev.resolver, snap=False)
        long_res = baseline_draws(ctx.market, ctx.pool_for(lspec), real, ctx.cost, ctx.sizing,
                                  ctx.rules, ctx.entry_masks, draws, s)
    rec["drift_ref"] = {k: {"always_long_mean": _f(long_res[k]),
                            "excess_over_always_long": (None if cand[k] is None else
                                                        float(cand[k] - np.nanmean(long_res[k])))}
                        for k in (*PARTITIONS, "pooled")}
    rec["drift_excess_p"] = rec["validation"]["p"]
    rec["canonical_hash"] = ghash
    return rec


def _f(x: np.ndarray) -> float | None:
    x = x[np.isfinite(x)]
    return float(x.mean()) if len(x) else None


__all__ = (
    "DEFAULT_DRAWS",
    "BaselineContext",
    "RealStructure",
    "baseline_draws",
    "drift_baseline",
    "genome_seed",
    "real_structure",
    "reference_spec",
    "tradable_mask",
)
