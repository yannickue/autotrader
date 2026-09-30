# ruff: noqa: E501
"""V2 PROBE runner (Phase 12 prep): a cheap, Train-only structural probe per market.

Research only.  Per market it builds the dev frame / FeatureStore / EventSet / MarketFrame, generates
``--n-candidates`` unique temporal structures (DEAP/niche evolution with a small budget + random fill
round-robin over all archetypes), evaluates them with the sealed ``TemporalEvaluator`` and writes a
compact campaign report (``probe_summary.json`` + ``probe_summary.md`` + ``ledger.json`` +
``candidate_pool.json``).

SEALING.  The search path only ever calls ``evaluate(..., need_base=False)`` (lean, Train side
only); ``ProbeEvaluator.evaluate`` refuses the full (non-lean) mode.  The fold TEST sides (see
``alpha.discovery.folds``) are the evaluator's sealed "validation" view and are never computed
here.  Nothing after 2026-08-31 can enter (forward holdout).  Every number in the summary is a
Train number of the search fold (fold 0 train side).

    python research/runners/v2_probe.py --markets GER40 --n-candidates 300 --tag smoke
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import shutil
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from alpha.common.market_costs import (  # noqa: E402
    cost_scenarios_for,
    market_cost_model,
    sizing_for,
)
from alpha.common.protocol import stable_hash  # noqa: E402
from alpha.common.sim import (  # noqa: E402
    CostScenario,
    SimRules,
    SizingSpec,
)
from alpha.discovery import temporal_compile  # noqa: E402
from alpha.discovery.disk import assert_free_space  # noqa: E402
from alpha.discovery.fitness import train_fitness  # noqa: E402
from alpha.discovery.folds import (  # noqa: E402
    SearchSplitPlan,
    berlin_dates_from_ts_ns,
    fold_report,
    make_folds,
)
from alpha.discovery.temporal_archetypes import available_archetypes, random_genome  # noqa: E402
from alpha.discovery.temporal_evaluate import (  # noqa: E402
    TemporalEval,
    TemporalEvaluator,
    TemporalTrialLedger,
    attach_min_space,
)
from alpha.discovery.temporal_genome import (  # noqa: E402
    EventPool,
    TemporalGenome,
    canonicalize,
    genome_tfs,
)
from alpha.discovery.temporal_niches import Elite, NicheArchive, niche_key, role_path  # noqa: E402
from alpha.discovery.temporal_search import (  # noqa: E402
    evolve_temporal,
    lineage_family,
    search_windows,
)
from alpha.discovery.v2_select import daily_hash, daily_vector, trade_stats  # noqa: E402
from alpha.fast.screen import screen_partition_trades  # noqa: E402
from alpha.fast.sim import (  # noqa: E402
    EXIT_FIXED_R,
    SKIP_LABELS,
    CandidateArrays,
    MarketArrays,
    SimWindow,
    simulate_fast,
)
from alpha.temporal.evaluate import evaluate_temporal_many  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/v2_probe.json"
SUMMARY_VERSION = "v2-probe-summary-v1"
ADVERSE = "COMBINED_ADVERSE"
BASE = "BASE"
SHOCK = "EXIT_SHOCK"
STATS_VERSION = "v2-probe-stats-v1"
POOL_KEEP = 100


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- small helpers
def peak_rss_mb() -> float | None:
    """Peak resident set size of this process in MiB (Windows: PeakWorkingSetSize)."""
    try:
        import resource  # type: ignore[import-not-found]

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except ImportError:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return None
        return round(pmc.PeakWorkingSetSize / 2**20, 1)
    except Exception:
        return None


def _q(values: list[float] | np.ndarray, qs=(0.05, 0.25, 0.5, 0.75, 0.95)) -> dict[str, float] | None:
    a = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if len(a) == 0:
        return None
    out = {f"p{int(q * 100):02d}": round(float(np.quantile(a, q)), 6) for q in qs}
    out["min"], out["max"], out["mean"], out["n"] = (round(float(a.min()), 6), round(float(a.max()), 6),
                                                    round(float(a.mean()), 6), len(a))
    return out


def local_market_minute(ts_ns: np.ndarray, tz: str) -> np.ndarray:
    """Minute-of-day of each UTC bar-OPEN stamp in the market's LOCAL calendar timezone (DST-correct, per bar).

    CLOCK FIX (2026-09-30): the FeatureSet's ``berlin_minute`` is ALWAYS Europe/Berlin, but
    ``SimWindow.from_spec`` and the temporal kernel's session windows are stated in the market's local
    clock (NAS100/SPX500 America/New_York, XAUUSD/EURUSD Europe/London).  Pairing them was misaligned."""
    idx = pd.DatetimeIndex(np.asarray(ts_ns, dtype="int64").view("datetime64[ns]"), tz="UTC").tz_convert(tz)
    return (idx.hour * 60 + idx.minute).to_numpy(np.int16)


def market_clock_minute(features: Any, spec: Any) -> np.ndarray:
    """Local-clock minute per bar for ``spec``; Europe/Berlin markets (GER40) return the FeatureSet's
    ``berlin_minute`` itself, so their results stay bit-identical."""
    if spec is None or spec.calendar.tz == "Europe/Berlin":
        return np.asarray(features["berlin_minute"])
    return local_market_minute(features["ts_ns"], spec.calendar.tz)


def market_arrays(features: Any, minute: np.ndarray | None = None) -> MarketArrays:
    """MarketArrays from a FeatureSet (same wiring as ``ar2_fast._market``).

    ``minute`` = the market-LOCAL minute-of-day (``market_clock_minute``); None keeps the Berlin minute
    (correct for GER40 / synthetic tests only).  ``day`` stays the Berlin day id: Berlin midnight lies
    outside the cash/entry windows of every market here (NY 09:30-15:55 = Berlin 15:30-21:55, London
    08:00-16:55 = Berlin 09:00-17:55; for the 24h markets the London/Berlin midnight differ by 1 h, which
    is irrelevant inside the entry windows), so day grouping and the forced flat agree."""
    contig = np.asarray(features["contig"], dtype=bool)
    return MarketArrays(features["o"], features["h"], features["l"], features["c"], features["spread"],
                        features["berlin_minute"] if minute is None else minute, features["berlin_day_id"],
                        np.r_[contig[1:], False])


def market_sim_params(spec: Any, cfg: dict) -> tuple[SizingSpec, SimRules, dict[str, CostScenario], SimWindow]:
    """(research sizing, rules, {BASE, COMBINED_ADVERSE}, SimWindow) for a market, from ``market_costs``.

    DISCOVERY runs on the NORMALISED research account (``cfg.sizing.research_equity_eur``, 10,000 EUR = V1
    ``DEFAULT_SIZING`` for GER40, exactly) so results are in comparable R and minimum-lot feasibility does
    not distort the search; feasibility at the real account is a separate annotation
    (``account_feasibility``).  Costs / risk band / lot / contract / leverage come from
    ``cost_scenarios_for`` / ``sizing_for``; the window is the market's local-minute entry/flat window.
    The spread cap is the spec's (PRICE units)."""
    s = cfg["sizing"]
    sizing = sizing_for(spec, account_eur=s["research_equity_eur"], risk_fraction=s["risk_fraction"])
    rules = SimRules(max_trades_per_day=cfg["rules"]["max_trades_per_day"],
                     max_entry_spread_pts=spec.max_entry_spread_price)
    costs = {n: c for n, c in cost_scenarios_for(spec).items() if n in (BASE, ADVERSE)}
    return sizing, rules, costs, SimWindow.from_spec(spec)


# --------------------------------------------------------------------------- evaluator
class ProbeEvaluator(TemporalEvaluator):
    """Sealed evaluator that also records every valid lean evaluation and sim skip totals."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.skip_totals = np.zeros(len(SKIP_LABELS), dtype=np.int64)
        self.records: dict[str, tuple[TemporalGenome, TemporalEval, str]] = {}
        self.train_cands: dict[str, CandidateArrays] = {}  # fresh computations only (Train-side candidates)
        self.stage = "init"

    def _reduce(self, trades, want_val):  # type: ignore[no-untyped-def]
        self.skip_totals += np.asarray(trades.skip_counts, dtype=np.int64)
        return super()._reduce(trades, want_val)

    def evaluate(self, genome: TemporalGenome, kind: str = "structural", need_base: bool = False
                 ) -> TemporalEval:
        if need_base:
            raise RuntimeError("the probe search path is Train-only: the full (non-lean) mode is forbidden")
        res = super().evaluate(genome, kind, need_base=False)
        if res.reject != "invalid_genome" and res.genome_hash not in self.records:
            self.records[res.genome_hash] = (canonicalize(genome), res, self.stage)
        if res.train_candidates is not None and res.genome_hash not in self.train_cands:
            self.train_cands[res.genome_hash] = res.train_candidates
        return res


# --------------------------------------------------------------------------- context
@dataclass
class ProbeContext:
    """Everything the search needs; built from real data or from a synthetic frame (tests)."""

    market_name: str
    frame_provider: Callable[[], Any]
    market: MarketArrays
    dates: np.ndarray
    plan: SearchSplitPlan
    fold_rep: dict
    pool: EventPool
    sizing: SizingSpec
    rules: SimRules
    costs: dict[str, CostScenario]
    data_fingerprint: str
    atr: np.ndarray
    meta: dict = field(default_factory=dict)
    window: SimWindow | None = None  # None = V1 GER40 constants (synthetic tests)
    market_spec: Any = None
    events_cache_key: str | None = None  # mandatory for the on-disk result cache (fail closed)
    features_cache_key: str | None = None


def build_real_context(canonical: str, cfg: dict, cache_dir: Path,
                       dev_transform: Callable[[Any], Any] | None = None) -> ProbeContext:
    """``dev_transform`` (null calibration only): maps the real dev DataFrame to a synthetic one with the
    SAME timestamps / spreads; everything downstream (features, events, folds, thresholds) is rebuilt."""
    from alpha.discovery.folds import assert_dev_only
    from alpha.events.store import load_or_build_events
    from alpha.temporal.frame import build_market_frame
    from markets.spec import load_market_spec
    from research.runners import v2_market_frame as mf

    t0 = time.perf_counter()
    spec = load_market_spec(canonical)
    source = cfg["ger40_source"] if canonical == "GER40" else "v2"
    dev = mf.build_dev_frame(spec, source)
    if dev_transform is not None:
        dev = dev_transform(dev)
    features = mf.build_feature_store(dev, spec, cache_dir)
    dates = berlin_dates_from_ts_ns(features["ts_ns"])
    assert_dev_only(dates)
    f = cfg["folds"]
    folds = make_folds(dates, f["n_folds"], f["embargo_days"], f["purge_bars"],
                       initial_train_frac=f["initial_train_frac"], min_test_days=f["min_test_days"])
    plan = SearchSplitPlan.from_folds(dates, folds)
    events = load_or_build_events(features, None, Path(cache_dir) / "events")
    ev_mb = sum(p.stat().st_size for p in (Path(cache_dir) / "events").rglob("*.npy")) / 2**20
    if ev_mb > cfg["max_event_cache_mb"]:
        raise RuntimeError(f"EventSet cache is {ev_mb:.0f} MB > {cfg['max_event_cache_mb']} MB limit")
    frame = build_market_frame(features, events, plan=plan)
    minute = market_clock_minute(features, spec)
    if spec.calendar.tz != "Europe/Berlin":  # kernel session windows compare against frame.berlin_minute
        frame = dataclasses.replace(frame, berlin_minute=minute)  # carries the market-LOCAL minute (see market_arrays)
    atr = np.asarray(features["m5_atr14"], dtype=float)
    med_atr = float(np.nanmedian(atr[plan.mask(dates, plan.train)]))
    sizing, rules, costs, window = market_sim_params(spec, cfg)
    sanity = mf.sanity_report(spec, dev, features, source)
    keep = ("calendar_status", "calendar_provisional", "n_bars", "first_bar_utc", "last_bar_utc",
            "n_local_days", "n_cash_session_days", "bars_per_day", "share_bars_in_entry_window",
            "share_entry_window_bars_over_cap", "gaps")
    meta = {
        "source": source, "sanity": {k: sanity[k] for k in keep}, "median_atr14_price_train": med_atr,
        "event_cache_mb": round(ev_mb, 1),
        "features_cache_key": getattr(features, "metadata", {}).get("cache_key"),
        "events_cache_key": getattr(events, "metadata", {}).get("cache_key"),
        "build_s": round(time.perf_counter() - t0, 1),
        "sim_window": dataclasses.asdict(window),
        "sim_window_note": "SimWindow.from_spec(spec): entry/flat minutes in the market's LOCAL calendar tz",
        "clock": {"tz": spec.calendar.tz, "minute_basis": "market-local (MarketArrays.minute and MarketFrame."
                  "berlin_minute); day id = Berlin day", "clock_fix": "2026-09-30 local-clock fix"},
        "cost_scenarios": {n: dataclasses.asdict(c) for n, c in costs.items()},
        "cost_note": "alpha.common.market_costs.cost_scenarios_for / sizing_for (research account "
                     f"{cfg['sizing']['research_equity_eur']:g} EUR)",
    }
    if sanity["calendar_provisional"]:
        log(f"[{canonical}] WARNING provisional calendar (status={sanity['calendar_status']})")
    fp = stable_hash({"f": meta["features_cache_key"], "e": meta["events_cache_key"], "m": canonical})
    return ProbeContext(canonical, lambda: frame, market_arrays(features, minute), dates, plan,
                        fold_report(dates, folds, f["embargo_days"]),
                        EventPool.from_array_names(events), sizing, rules, costs, fp, atr, meta,
                        window=window, market_spec=spec, events_cache_key=meta["events_cache_key"],
                        features_cache_key=meta["features_cache_key"])


# --------------------------------------------------------------------------- search
def run_search(ev: ProbeEvaluator, ctx: ProbeContext, cfg: dict, n_candidates: int, seed: int,
               timings: dict[str, float]) -> dict:
    s = cfg["search"]
    info: dict[str, Any] = {}
    pool = ctx.pool
    t0 = time.perf_counter()
    ev.stage = "deap"
    budget = min(int(s["deap_budget_fraction"] * n_candidates), s["deap_pop"] * (s["deap_gens"] + 1))
    if budget >= s["deap_pop"] and s["deap_pop"] >= 2:
        res = evolve_temporal(ev, pool, s["deap_pop"], s["deap_gens"], seed, s["deap_cxpb"], s["deap_mutpb"],
                              max_evaluations=budget, hof_size=max(s["deap_pop"], 50), window=ctx.window)
        info["deap"] = {"budget": budget, "unique_evaluations": res.evaluations_used,
                        "budget_exhausted": res.budget_exhausted, "generations_run": len(res.stats) - 1,
                        "niches_at_end": res.archive.n_niches, "twins_rejected": res.archive.twins_rejected}
    else:
        info["deap"] = {"budget": budget, "skipped": True}
    timings["search_deap_s"] = round(time.perf_counter() - t0, 2)

    t0 = time.perf_counter()
    ev.stage = "random"
    rng = np.random.default_rng(seed + 1)
    names = available_archetypes(pool)
    windows = search_windows(ctx.window)
    attempts, i = 0, 0
    while ev.ledger.unique < n_candidates and attempts < 8 * n_candidates:
        g = random_genome(rng, pool, archetype=names[i % len(names)], windows=windows)
        i += 1
        attempts += 1
        ev.evaluate(g, kind="structural", need_base=False)
        if attempts % 200 == 0:
            log(f"[random] attempts={attempts} unique={ev.ledger.unique}/{n_candidates}")
    info["random"] = {"attempts": attempts, "archetypes": list(names)}
    timings["search_random_s"] = round(time.perf_counter() - t0, 2)
    ev.flush()
    return info


# --------------------------------------------------------------------------- analysis (Train only)
def _train_mask(ctx: ProbeContext) -> np.ndarray:
    return ctx.plan.mask(ctx.dates, ctx.plan.train)


def cofire_stats(ev: ProbeEvaluator, ctx: ProbeContext, passers: list[tuple[TemporalGenome, TemporalEval, str]],
                 top_k: int) -> dict:
    """Same-bar same-direction overlap between the top-K passers (by Train trade count) on Train bars.

    Uses the candidate decision bars (what a SignalRecord's ``decision_idx``/``direction`` carry)."""
    top = sorted(passers, key=lambda t: (-t[1].train.adverse.screen.n_trades, t[1].genome_hash))[:top_k]
    if len(top) < 2:
        return {"n_strategies": len(top), "note": "fewer than 2 passers"}
    tm = _train_mask(ctx)
    specs = [temporal_compile.compile_temporal(g, ev.resolver, canonical=True) for g, _, _ in top]
    results = evaluate_temporal_many(specs, ev.frame, use_cache=False)
    sets = []
    for r in results:
        c = r.candidates
        keep = tm[c.decision_idx] if len(c.decision_idx) else np.zeros(0, bool)
        sets.append(set((c.decision_idx[keep].astype(np.int64) * 2 + (c.direction[keep] > 0)).tolist()))
    jac = []
    for a in range(len(sets)):
        for b in range(a + 1, len(sets)):
            u = len(sets[a] | sets[b])
            jac.append(len(sets[a] & sets[b]) / u if u else 0.0)
    cnt: Counter[int] = Counter()
    for s in sets:
        cnt.update(s)
    union = len(cnt)
    multi = sum(1 for v in cnt.values() if v >= 2)
    return {
        "n_strategies": len(sets), "pairs": len(jac), "jaccard": _q(jac, (0.5, 0.9, 0.99)),
        "share_pairs_jaccard_gt_0.1": round(float(np.mean(np.asarray(jac) > 0.1)), 4),
        "union_decision_bars": union, "bars_with_ge2_same_direction": multi,
        "share_bars_with_ge2_same_direction": round(multi / union, 4) if union else None,
        "max_concurrent_same_direction": max(cnt.values()) if cnt else 0,
        "note": "Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only",
    }


def drift_baselines(ctx: ProbeContext, ev: ProbeEvaluator, cfg: dict, seed: int,
                    tpd_median: float | None) -> dict:
    """Train-only drift baselines: always long/short, random long/short, same-session random.

    V1 ``alpha.discovery.baseline`` is bound to V1 ``StrategySpec`` genomes (reference spec of the
    candidate) and cannot be reused for temporal specs; this is a market-level equivalent with a
    fixed ATR stop and fixed-R target under the COMBINED_ADVERSE cost."""
    b = cfg["baselines"]
    m, tm = ctx.market, _train_mask(ctx)
    atr = ctx.atr
    win = ctx.window if ctx.window is not None else SimWindow()  # None = V1 GER40 constants
    ok = tm & np.isfinite(atr) & (atr > 0) & (m.minute >= win.entry_start_min) & (m.minute < win.entry_end_min)
    idx = np.flatnonzero(ok)
    n_days = len(np.unique(m.day[tm]))
    cost = ev._costs[ADVERSE]

    def sim(decision: np.ndarray, direction: np.ndarray) -> dict:
        if len(decision) == 0:
            return {"n_trades": 0, "expectancy_r": None, "trades_per_day": 0.0}
        o = np.argsort(decision, kind="stable")
        decision, direction = decision[o], direction[o]
        stop = m.c[decision] - direction * b["stop_atr_mult"] * atr[decision]
        c = CandidateArrays(decision, direction.astype(np.int8), stop, np.full(len(decision), np.nan),
                            np.full(len(decision), float(b["target_r"])),
                            np.full(len(decision), EXIT_FIXED_R, dtype=np.int8))
        tr = simulate_fast(m, c, cost, ctx.sizing, ctx.rules, ctx.window)
        mask = tm[tr.entry_idx] if len(tr) else np.zeros(0, bool)
        sc = screen_partition_trades(tr, mask, n_days, contract_size=ctx.sizing.contract_size)
        return {"n_trades": sc.n_trades,
                "expectancy_r": None if sc.expectancy_r is None else round(sc.expectancy_r, 5),
                "trades_per_day": None if sc.trades_per_day is None else round(sc.trades_per_day, 4)}

    out: dict[str, Any] = {
        "stop_atr_mult": b["stop_atr_mult"], "target_r": b["target_r"], "cost": ADVERSE,
        "eligible_train_bars": len(idx), "train_days": n_days,
        "always_long": sim(idx, np.ones(len(idx), np.int8)),
        "always_short": sim(idx, -np.ones(len(idx), np.int8)),
    }
    rng = np.random.default_rng(seed + 7)
    rl = [sim(idx, rng.choice(np.array([-1, 1], np.int8), len(idx))) for _ in range(b["draws"])]
    out["random_long_short"] = _draws(rl)
    k = max(1, round(tpd_median)) if tpd_median else 1
    day_of = m.day[idx]
    days, start = np.unique(day_of, return_index=True)
    bounds = np.r_[start, len(idx)]
    ss = []
    for _ in range(b["draws"]):
        pick = np.concatenate([bounds[i] + rng.choice(bounds[i + 1] - bounds[i],
                                                       min(k, bounds[i + 1] - bounds[i]), replace=False)
                               for i in range(len(days))]) if len(days) else np.zeros(0, int)
        ss.append(sim(idx[pick], rng.choice(np.array([-1, 1], np.int8), len(pick))))
    out["same_session_random"] = {**_draws(ss), "decisions_per_day": k}
    return out


def _draws(rows: list[dict]) -> dict:
    e = [r["expectancy_r"] for r in rows if r["expectancy_r"] is not None]
    return {"draws": len(rows), "expectancy_r_mean": round(float(np.mean(e)), 5) if e else None,
            "expectancy_r_sd": round(float(np.std(e)), 5) if len(e) > 1 else None,
            "trades_mean": round(float(np.mean([r["n_trades"] for r in rows])), 1) if rows else None}


def account_feasibility(ev: ProbeEvaluator, ctx: ProbeContext, cfg: dict,
                        rows: list[tuple[TemporalGenome, TemporalEval, str]]) -> tuple[dict, dict[str, dict]]:
    """ANNOTATION ONLY (never used by fitness / selection): feasibility at the REAL account.

    Discovery runs on the normalised research account; here the same Train candidates of each annotated
    pool candidate are re-simulated (COMBINED_ADVERSE) at ``cfg.account.real_account_eur`` with the same
    risk fraction / lot rules / leverage cap and the ``size_below_min`` skips are counted.
    ``size_below_min_skip_share`` = size_below_min skips / (booked trades + size_below_min skips) among the
    Train entries.  Market level: min-lot risk at the minimum stop vs the approved risk, min-lot leverage
    vs the research cap (30x is only the permitted ceiling)."""
    acct = float(cfg["account"]["real_account_eur"])
    real = dataclasses.replace(ctx.sizing, equity_eur=acct)
    tm = _train_mask(ctx)
    market: dict[str, Any] = {
        "real_account_eur": acct, "research_account_eur": ctx.sizing.equity_eur,
        "risk_fraction": ctx.sizing.risk_fraction, "approved_risk_eur": round(acct * ctx.sizing.risk_fraction, 6),
        "min_lot": ctx.sizing.min_lot, "lot_step": ctx.sizing.lot_step,
        "leverage_cap_research": ctx.sizing.max_leverage, "leverage_ceiling_permitted": 30.0,
        "influences_fitness_or_selection": False,
    }
    if ctx.market_spec is not None:
        m = market_cost_model(ctx.market_spec, acct, risk_fraction=ctx.sizing.risk_fraction)
        market.update(min_lot_risk_at_min_stop_eur=round(m.min_lot_risk_at_min_stop_eur, 6),
                      min_lot_feasible_at_min_stop=m.min_lot_feasible_at_min_stop,
                      min_lot_leverage=None if m.min_lot_leverage is None else round(m.min_lot_leverage, 4),
                      min_lot_leverage_within_research_cap=(
                          None if m.min_lot_leverage is None else m.min_lot_leverage <= ctx.sizing.max_leverage))
    else:
        lot_risk = ctx.sizing.min_lot * ctx.sizing.min_risk_pts * ctx.sizing.contract_size
        market.update(min_lot_risk_at_min_stop_eur=round(lot_risk, 6),
                      min_lot_feasible_at_min_stop=lot_risk <= acct * ctx.sizing.risk_fraction + 1e-12,
                      min_lot_leverage=None, min_lot_leverage_within_research_cap=None)
    per: dict[str, dict] = {}
    if rows:
        specs = [temporal_compile.compile_temporal(g, ev.resolver, canonical=True) for g, _, _ in rows]
        results = evaluate_temporal_many(specs, ev.frame, use_cache=False)
        for (_, r, _), spec, res in zip(rows, specs, results, strict=True):
            c = attach_min_space(res.candidates, spec)
            c = c.subset(tm[c.decision_idx]) if len(c.decision_idx) else c
            kw = (ctx.market, c, ev._costs[ADVERSE])
            t_res = simulate_fast(*kw, ctx.sizing, ctx.rules, ctx.window)
            t_real = simulate_fast(*kw, real, ctx.rules, ctx.window)
            below = int(t_real.skips["size_below_min"])
            attempted = len(t_real) + below
            per[r.genome_hash] = {
                "real_account_eur": acct, "train_trades_research": len(t_res), "train_trades_at_account": len(t_real),
                "size_below_min_skips": below,
                "size_below_min_skip_share": round(below / attempted, 6) if attempted else None,
                "leverage_capped_trades_at_account": int(np.sum(t_real.leverage_capped)),
                "feasible": below == 0 and len(t_real) > 0,
            }
    shares = [v["size_below_min_skip_share"] for v in per.values() if v["size_below_min_skip_share"] is not None]
    market.update(
        candidates_annotated=len(per), share_size_below_min_of_train_entries=_q(shares, (0.25, 0.5, 0.75)),
        candidates_fully_feasible=sum(1 for v in per.values() if v["feasible"]),
        candidates_majority_skipped=sum(1 for x in shares if x > 0.5),
        note="annotation only; discovery/fitness/selection use the research account (see market_sim_params)")
    return market, per


def summarize(ev: ProbeEvaluator, ctx: ProbeContext, cfg: dict, n_candidates: int, timings: dict[str, float],
              search_info: dict, prior: dict) -> tuple[dict, list[dict]]:
    led = ev.ledger
    recs = list(ev.records.values())
    n_days_train = len(np.unique(ctx.market.day[_train_mask(ctx)]))
    reasons = Counter(r.reject or "pass" for _, r, _ in recs)
    passers = [t for t in recs if not t[1].rejected]
    dec = [r.n_train_candidates for _, r, _ in recs]
    zero = sum(1 for d in dec if d == 0)
    fam: dict[str, dict] = {}
    for g, r, _ in recs:
        f = fam.setdefault(lineage_family(r.lineage or g.lineage), {"n": 0, "pass": 0, "zero": 0, "e": []})
        f["n"] += 1
        f["zero"] += r.n_train_candidates == 0
        if not r.rejected:
            f["pass"] += 1
            f["e"].append(r.train.adverse.screen.expectancy_r)
    families = {k: {"n": v["n"], "passed_min_trades": v["pass"],
                    "zero_train_decision_share": round(v["zero"] / v["n"], 4),
                    "train_expectancy_r_adverse": _q(v["e"], (0.25, 0.5, 0.75))}
                for k, v in sorted(fam.items())}
    arch = Counter(lineage_family(g.lineage) for g, _, _ in recs)
    tfsets = Counter("+".join(genome_tfs(g)) for g, _, _ in recs)
    roles = Counter(role_path(g) for g, _, _ in recs)
    archive = NicheArchive()
    for g, r, _ in passers:
        key = niche_key(g, r.trades_per_day)
        archive.visit(key)
        archive.insert(Elite(key, r.genome_hash, train_fitness(r.train, ev.min_trades), r.twin_hash))
    tpd = [r.trades_per_day for _, r, _ in passers]
    t0 = time.perf_counter()
    cof = cofire_stats(ev, ctx, passers, cfg["confluence"]["top_k"])
    timings["cofire_s"] = round(time.perf_counter() - t0, 2)
    t0 = time.perf_counter()
    base = drift_baselines(ctx, ev, cfg, cfg["seed"], statistics.median([t for t in tpd if t]) if any(tpd) else None)
    timings["baselines_s"] = round(time.perf_counter() - t0, 2)
    cum_trials = prior["trials"] + led.total_trials
    cum_unique = prior["unique_specs"] + led.unique
    exp = [r.train.adverse.screen.expectancy_r for _, r, _ in passers]
    summary = {
        "summary_version": SUMMARY_VERSION, "market": ctx.market_name,
        "scope": "TRAIN side of search fold 0 only; no Validation/fold-test number anywhere",
        "folds": ctx.fold_rep, "search_train_days": n_days_train, "min_train_trades": ev.min_trades,
        "data": ctx.meta, "sizing": dataclasses.asdict(ctx.sizing), "rules": dataclasses.asdict(ctx.rules),
        "counts": {
            "n_candidates_target": n_candidates, "evaluations_total": led.total_trials,
            "unique_specs": led.unique, "duplicate_rejects": led.duplicate_rejects,
            "invalid_rejects": led.invalid_rejects, "cache_hits": led.cache_hits,
            "structural_trials": led.structural_trials, "param_trials": led.param_trials,
            "unique_behaviors": led.unique_behaviors, "behavioral_twins": led.behavioral_twins,
            "unique_twin_streams": led.unique_twin_streams, "valid_evaluated": len(recs),
            "sims_run": ev.sim_count, "reject_reasons": dict(reasons),
            "passed_min_trades": len(passers),
        },
        "cumulative": {"prior_label": prior["label"], "prior_trials": prior["trials"],
                       "prior_unique_specs": prior["unique_specs"], "cumulative_trials": cum_trials,
                       "cumulative_unique_specs": cum_unique},
        "zero_trade": {"share_zero_train_decisions": round(zero / len(recs), 4) if recs else None,
                       "n_zero": zero, "share_zero_candidates_reject": round(
                           reasons.get("zero_candidates", 0) / len(recs), 4) if recs else None},
        "train_decisions": _q(dec),
        "train_trades_passers": _q([r.train.adverse.screen.n_trades for _, r, _ in passers]),
        "train_trades_per_day_passers": _q(tpd),
        "train_expectancy_r_adverse_passers_informational": _q(exp),
        "families": families, "family_counts": dict(sorted(arch.items())),
        "tf_sets": dict(tfsets.most_common(15)), "n_tf_sets": len(tfsets),
        "event_signatures_top": dict(roles.most_common(15)), "n_event_signatures": len(roles),
        "niches": {**archive.stats(), "n_passer_niches": archive.n_niches},
        "sim_skips": dict(zip(SKIP_LABELS, (int(x) for x in ev.skip_totals), strict=True)),
        "sim_skips_note": "summed over the simulations actually run this campaign (cache hits excluded)",
        "confluence_cofire": cof, "drift_baselines_train": base, "search": search_info,
        "runtime_s": timings, "peak_rss_mb": peak_rss_mb(),
    }
    pool_rows = sorted(passers, key=lambda t: (-train_fitness(t[1].train, ev.min_trades), t[1].genome_hash))[:POOL_KEEP]
    t0 = time.perf_counter()
    feas, feas_rows = account_feasibility(ev, ctx, cfg, pool_rows)  # after ranking: cannot influence it
    timings["account_feasibility_s"] = round(time.perf_counter() - t0, 2)
    summary["account_feasibility"] = feas
    pool = [{"canonical_hash": r.genome_hash, "train_fitness": round(train_fitness(r.train, ev.min_trades), 8),
             "stage": stage, "lineage": g.lineage, "genome": g.to_dict(),
             "account_feasibility": feas_rows.get(r.genome_hash)} for g, r, stage in pool_rows]
    return summary, pool


# --------------------------------------------------------------------------- per-candidate compact stats
def exit_shock_cost(base: CostScenario, extra_spread_price: float) -> CostScenario:
    """EXIT_SHOCK = BASE plus one extra (median) spread on EVERY market fill (entry and market exits), the
    simulator-level analogue of the rawscan ``EXIT_SHOCK`` (BASE + one full extra spread at entry and exit)."""
    return dataclasses.replace(base, name=SHOCK, slippage_pts=base.slippage_pts + float(extra_spread_price))


def median_train_spread(ctx: ProbeContext) -> float:
    sp = ctx.market.spread[_train_mask(ctx)]
    sp = sp[np.isfinite(sp) & (sp > 0)]
    return float(np.median(sp)) if len(sp) else 0.0


STAT_COLUMNS = (
    "hash", "stage", "family", "reject", "passer", "complexity", "n_train_candidates", "n_trades", "tpd",
    "e_adv", "e_base", "e_shock", "t_day", "payoff", "profit_factor", "win_rate", "avg_win_r", "avg_loss_r",
    "daily_hash",
)


def collect_stats(ev: ProbeEvaluator, ctx: ProbeContext) -> tuple[list[dict], dict[str, np.ndarray], np.ndarray]:
    """Compact Train stats of ALL valid evaluated specs (passers and rejects with any Train candidate).

    Re-simulates each spec's Train candidates (same kernel, costs, sizing, window as the search) under
    BASE, COMBINED_ADVERSE and EXIT_SHOCK.  Train only: the candidate stream is cut to Train decisions
    before the simulation, so no Validation/fold-test bar is ever simulated here.  Returns (rows,
    hash -> daily-R vector (ADVERSE; train-day axis), train day ids)."""
    tm = _train_mask(ctx)
    days = np.unique(ctx.market.day[tm])
    n_days = len(days)
    shock = exit_shock_cost(ev._costs[BASE], median_train_spread(ctx))
    costs = {"adv": ev._costs[ADVERSE], "base": ev._costs[BASE], "shock": shock}
    rows: list[dict] = []
    daily: dict[str, np.ndarray] = {}
    for h, (g, r, stage) in sorted(ev.records.items()):
        row: dict[str, Any] = {c: None for c in STAT_COLUMNS}
        row.update(hash=h, stage=stage, family=lineage_family(r.lineage or g.lineage), reject=r.reject or "",
                   passer=not r.rejected, complexity=r.complexity, n_train_candidates=r.n_train_candidates,
                   n_trades=0)
        cands = ev.train_cands.get(h)
        if cands is None and r.n_train_candidates:
            spec = temporal_compile.compile_temporal(g, ev.resolver, canonical=True)
            res = evaluate_temporal_many([spec], ev.frame, use_cache=True, cache=ev._prefix_cache)[0].candidates
            res = attach_min_space(res, spec)
            cands = res.subset(tm[res.decision_idx]) if len(res.decision_idx) else res
        if cands is not None and len(cands.decision_idx):
            per = {}
            for k, cost in costs.items():
                tr = simulate_fast(ctx.market, cands, cost, ctx.sizing, ctx.rules, ctx.window)
                keep = tm[tr.entry_idx] if len(tr) else np.zeros(0, bool)
                per[k] = (tr.r_multiple[keep], tr.entry_day[keep])
            st = trade_stats(*per["adv"])
            row.update(n_trades=st["n_trades"], tpd=st["n_trades"] / n_days if n_days else None,
                       e_adv=st["mean_r"], t_day=st["t_day"], payoff=st["payoff"],
                       profit_factor=st["profit_factor"], win_rate=st["win_rate"], avg_win_r=st["avg_win_r"],
                       avg_loss_r=st["avg_loss_r"],
                       e_base=trade_stats(*per["base"])["mean_r"], e_shock=trade_stats(*per["shock"])["mean_r"])
            if st["n_trades"]:
                vec = daily_vector(per["adv"][0], per["adv"][1], days)
                daily[h] = vec.astype(np.float32)
                row["daily_hash"] = daily_hash(vec)
        rows.append(row)
    return rows, daily, days


def write_stats(out_dir: Path, ev: ProbeEvaluator, rows: list[dict], daily: dict[str, np.ndarray],
                days: np.ndarray) -> None:
    """``stats.csv.gz`` (ALL evaluated specs), ``genomes.json.gz`` (hash -> genome) and ``daily_r.npz``."""
    df = pd.DataFrame(rows, columns=list(STAT_COLUMNS))
    df.to_csv(out_dir / "stats.csv.gz", index=False, compression="gzip", float_format="%.8g")
    genomes = {h: g.to_dict() for h, (g, _, _) in sorted(ev.records.items())}
    with gzip.open(out_dir / "genomes.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(genomes, fh, sort_keys=True, allow_nan=False)
    hs = sorted(daily)
    np.savez_compressed(out_dir / "daily_r.npz", hashes=np.array(hs), day_ids=np.asarray(days, dtype=np.int64),
                        mat=np.stack([daily[h] for h in hs]) if hs else np.zeros((0, len(days)), np.float32))


def load_stats(out_dir: Path) -> tuple[pd.DataFrame, dict[str, np.ndarray]]:
    df = pd.read_csv(out_dir / "stats.csv.gz")
    z = np.load(out_dir / "daily_r.npz")
    return df, {str(h): z["mat"][i].astype(np.float64) for i, h in enumerate(z["hashes"])}


def render_markdown(s: dict) -> str:
    c, z = s["counts"], s["zero_trade"]
    lines = [
        f"# V2 probe: {s['market']}",
        "", f"Scope: {s['scope']}.", "",
        f"- unique specs {c['unique_specs']} (evaluations {c['evaluations_total']}, duplicates "
        f"{c['duplicate_rejects']}, invalid {c['invalid_rejects']}, behavioural twins {c['behavioral_twins']})",
        f"- cumulative trials incl. {s['cumulative']['prior_label']}: {s['cumulative']['cumulative_trials']} "
        f"(unique specs {s['cumulative']['cumulative_unique_specs']})",
        f"- passed min {s['min_train_trades']} Train trades: {c['passed_min_trades']}; reject reasons {c['reject_reasons']}",
        f"- zero-Train-decision share {z['share_zero_train_decisions']}",
        f"- Train trades/day (passers): {s['train_trades_per_day_passers']}",
        f"- families {s['family_counts']}",
        f"- TF sets {s['n_tf_sets']}, event signatures {s['n_event_signatures']}, niches {s['niches']['niches']}",
        f"- sim skips {s['sim_skips']}",
        f"- co-fire: {s['confluence_cofire']}",
        f"- drift baselines (Train): {s['drift_baselines_train']}",
        f"- runtime {s['runtime_s']}, peak RSS {s['peak_rss_mb']} MB",
    ]
    if s["data"].get("sanity", {}).get("calendar_provisional"):
        lines.append("- WARNING: provisional calendar for this market")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- driver
def make_evaluator(ctx: ProbeContext, cfg: dict, ledger: TemporalTrialLedger | None = None,
                   cache_dir: Path | None = None) -> ProbeEvaluator:
    return ProbeEvaluator(ctx.frame_provider, ctx.market, ctx.dates, ctx.plan, sizing=ctx.sizing, rules=ctx.rules,
                          cost_scenarios=ctx.costs, min_train_trades=cfg["min_train_trades"], ledger=ledger,
                          cache_dir=cache_dir, data_fingerprint=ctx.data_fingerprint, window=ctx.window,
                          events_cache_key=ctx.events_cache_key, features_cache_key=ctx.features_cache_key)


def run_probe(ctx: ProbeContext, cfg: dict, n_candidates: int, seed: int, out_dir: Path,
              cache_dir: Path | None = None, resume: bool = False) -> dict:
    t_all = time.perf_counter()
    timings: dict[str, float] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    prior = dict(cfg["prior"])
    ledger_path = out_dir / "ledger.json"
    ledger = TemporalTrialLedger()
    carried = {"trials": 0, "unique_specs": 0}
    if resume and ledger_path.exists():
        old = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger = TemporalTrialLedger.from_json(json.dumps(old["ledger"]))
        carried = {"trials": ledger.total_trials, "unique_specs": ledger.unique}
    ev = make_evaluator(ctx, cfg, ledger, cache_dir)
    log(f"[{ctx.market_name}] search: n_candidates={n_candidates} seed={seed} train_days="
        f"{len(np.unique(ctx.market.day[_train_mask(ctx)]))}")
    t0 = time.perf_counter()
    search_info = run_search(ev, ctx, cfg, n_candidates, seed, timings)
    timings["search_total_s"] = round(time.perf_counter() - t0, 2)
    t0 = time.perf_counter()
    stat_rows, daily, train_days = collect_stats(ev, ctx)
    timings["stats_s"] = round(time.perf_counter() - t0, 2)
    summary, pool = summarize(ev, ctx, cfg, n_candidates, timings, search_info, prior)
    summary["ledger_carried_in"] = carried
    summary["stats_file"] = {"version": STATS_VERSION, "rows": len(stat_rows), "with_trades": len(daily),
                             "cost_exit_shock": "BASE + one extra median Train spread on every market fill"}
    timings["total_s"] = round(time.perf_counter() - t_all, 2)
    summary["peak_rss_mb"] = peak_rss_mb()
    led_json = json.loads(ledger.to_json())
    header = {"v1_cumulative": prior, "this_campaign": {"seed": seed, "n_candidates": n_candidates,
                                                       "carried_in": carried},
              "config_hash": stable_hash(cfg), "fold_digest": ctx.plan.fold_digest}
    ledger_path.write_text(json.dumps({"header": header, "ledger": led_json}, indent=1, sort_keys=True),
                           encoding="utf-8")
    (out_dir / "candidate_pool.json").write_text(json.dumps(
        {"meta": {"scope": "Train-only ranking (search fold 0)", "fold_digest": ctx.plan.fold_digest,
                  "prior": prior}, "candidates": pool}, indent=1, sort_keys=True, allow_nan=False),
        encoding="utf-8")
    write_stats(out_dir, ev, stat_rows, daily, train_days)
    (out_dir / "probe_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True, allow_nan=False),
                                                encoding="utf-8")
    (out_dir / "probe_summary.md").write_text(render_markdown(summary), encoding="utf-8")
    log(f"[{ctx.market_name}] done: unique={summary['counts']['unique_specs']} "
        f"passed={summary['counts']['passed_min_trades']} total {timings['total_s']}s -> {out_dir}")
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--markets", nargs="*", default=None, help="subset/order of the config markets")
    p.add_argument("--n-candidates", type=int, default=None, help="default: config n_candidates (1500)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--out-root", default=None)
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--resume", action="store_true", help="carry the existing per-market ledger.json counts")
    p.add_argument("--keep-cache", action="store_true", help="do not delete the feature/event caches at the end")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    n = args.n_candidates if args.n_candidates is not None else cfg["n_candidates"]
    seed = args.seed if args.seed is not None else cfg["seed"]
    cache_dir = Path(args.cache_dir or REPO_ROOT / cfg["cache_dir"])
    out_root = Path(args.out_root or REPO_ROOT / cfg["out_root"])
    markets = args.markets or cfg["markets"]
    assert_free_space(cache_dir)
    try:
        for m in markets:
            ctx = build_real_context(m, cfg, cache_dir)
            log(f"[{m}] median ATR14 (train) = {ctx.meta['median_atr14_price_train']:.6g}, "
                f"window = {ctx.window}")
            run_probe(ctx, cfg, n, seed, out_root / m, cache_dir / "evals", args.resume)
    finally:
        if not args.keep_cache:
            shutil.rmtree(cache_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
