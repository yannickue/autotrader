"""Random-entry / drift baseline (research only): reproducibility, drift sensitivity, null case."""

from __future__ import annotations

import numpy as np

from alpha.common.protocol import Partition, SplitPlan
from alpha.common.sim import COST_SCENARIOS, DEFAULT_RULES, DEFAULT_SIZING
from alpha.discovery import baseline as bl
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, MarketArrays, simulate_fast

COST = COST_SCENARIOS["COMBINED_ADVERSE"]
BARS = 110  # bars per day, minute 540..1085 (entries before 20:00)
DAYS = 40


def _market(drift: float, seed: int = 3):
    rng = np.random.default_rng(seed)
    n = DAYS * BARS
    minute = np.tile(540 + 5 * np.arange(BARS), DAYS)
    day = np.repeat(np.arange(DAYS), BARS)
    step = rng.normal(drift, 3.0, n)
    c = 10000 + np.cumsum(step)
    o = np.r_[c[0], c[:-1]] + 0.0
    h = np.maximum(o, c) + 1.0
    lo = np.minimum(o, c) - 1.0
    contig_next = np.ones(n, dtype=bool)
    contig_next[BARS - 1::BARS] = False
    m = MarketArrays(o, h, lo, c, np.full(n, 1.0), minute, day, contig_next)
    dates = (np.datetime64("2025-03-03") + day).astype("datetime64[D]")
    return m, dates


def _plan() -> SplitPlan:
    return SplitPlan(Partition("train", "2025-03-03", "2025-03-30"),
                     Partition("validation", "2025-03-31", "2025-04-08"),
                     Partition("oos", "2025-04-09", "2025-04-20"), embargo_days=0)


def _cands(market: MarketArrays, idx: np.ndarray, stop_pts: float = 10.0) -> CandidateArrays:
    k = len(idx)
    return CandidateArrays(idx, np.ones(k, dtype=np.int8), market.c[idx] - stop_pts,
                           np.full(k, np.nan), np.full(k, 1.0),
                           np.full(k, EXIT_FIXED_R, dtype=np.int8))


def _setup(drift: float, real_idx: np.ndarray):
    market, dates = _market(drift)
    plan = _plan()
    pool_all = _cands(market, np.flatnonzero(market.minute < 1080))
    keep = bl.tradable_mask(market, pool_all, COST, DEFAULT_SIZING, DEFAULT_RULES)
    pools = {}
    for name, part in (("train", plan.train), ("validation", plan.validation)):
        in_p = plan.mask(dates[pool_all.decision_idx], part)
        pools[name] = bl._build_pool(market, pool_all, keep & in_p)
    trades = simulate_fast(market, _cands(market, real_idx), COST, DEFAULT_SIZING, DEFAULT_RULES)

    def masks(t):
        ed = dates[t.entry_idx]
        return {"train": plan.mask(ed, plan.train), "validation": plan.mask(ed, plan.validation)}

    real = bl.real_structure(trades, market, masks(trades))
    return market, pools, real, masks


def _run(market, pools, real, masks, draws=60, seed=11):
    return bl.baseline_draws(market, pools, real, COST, DEFAULT_SIZING, DEFAULT_RULES, masks,
                             draws, seed)


def _real_idx(rng_seed: int = 5) -> np.ndarray:
    rng = np.random.default_rng(rng_seed)
    days = rng.choice(DAYS - 2, size=30, replace=False)
    return np.sort(days * BARS + rng.integers(2, 100, size=30))


def test_reproducible_for_seed() -> None:
    market, pools, real, masks = _setup(0.3, _real_idx())
    a, b = _run(market, pools, real, masks), _run(market, pools, real, masks)
    c = _run(market, pools, real, masks, seed=12)
    assert np.array_equal(a["train"], b["train"], equal_nan=True)
    assert not np.array_equal(a["train"], c["train"], equal_nan=True)


def test_positive_drift_gives_positive_long_baseline() -> None:
    market, pools, real, masks = _setup(1.2, _real_idx())
    res = _run(market, pools, real, masks)
    assert np.nanmean(res["pooled"]) > 0.2
    flat_m, flat_p, flat_r, flat_masks = _setup(0.0, _real_idx())
    flat = _run(flat_m, flat_p, flat_r, flat_masks)
    assert np.nanmean(res["pooled"]) > np.nanmean(flat["pooled"])


def test_always_long_candidate_has_zero_excess() -> None:
    market, _ = _market(0.6)
    # the "candidate" enters at every 3rd bar of every day (no timing skill at all)
    idx = np.flatnonzero((market.minute < 1080) & (np.arange(len(market.minute)) % 7 == 0))
    market, pools, real, masks = _setup(0.6, idx)
    res = _run(market, pools, real, masks, draws=200)
    for part in ("train", "validation", "pooled"):
        base = res[part]
        cand = real.mean_r[part] if part != "pooled" else float(
            np.concatenate([real.r["train"], real.r["validation"]]).mean())
        stats = bl._stats(cand, base, 1, 1.0)
        assert abs(stats["excess"]) < 3 * stats["base_sd"] + 0.05, (part, stats)
        assert stats["p"] > 0.01
