# ruff: noqa: E501
"""V2 SURVIVAL STAGE for the FROZEN, fold-test-blind finalists.  survival stage - not for selection.

Research only.  Two sub-commands:

    python research/runners/v2_survival.py select            # Train-only Pareto selection -> frozen finalist file
    python research/runners/v2_survival.py run               # DRY RUN (default): integrity check + plan, no data read
    python research/runners/v2_survival.py run --execute     # the sealed survival stage (ONCE per finalist hash)

``select`` needs only the probe's Train stats (``stats.csv.gz`` + ``daily_r.npz``) and freezes the finalist
list + hash to disk (``alpha.discovery.v2_select``).  ``run --execute`` is the FIRST and ONLY place fold TEST
sides are read.  It can be called ONCE PER frozen finalist hash: a persisted call ledger records the call
before any data is touched (a crash still consumes it: fail closed).  For each finalist (thresholds frozen
from the search fold-0 train block, exactly as in the probe):

* every fold TEST side (``test_fold_indices``; default folds 1..3) with the SAME costs / sizing / window as
  the probe: expectancy, day-clustered t per fold and pooled, cross-fold sign persistence;
* cost stress on the pooled test side: COMBINED_ADVERSE (main), BASE, EXIT_SHOCK (BASE + one extra median
  spread on every market fill); entry delay +1 bar (fill at the open two bars after the decision);
* parameter-neighbourhood robustness: +-1 grid step of every numeric gene of the FIXED structure (within,
  tol, stop buffer / mult / risk, target, expiry, feature quantiles ...; not the event variant): share of
  neighbours with pooled-test E > 0;
* drift baselines on the same test days (always long, always short, random long/short, same-session
  random) with a fixed ATR stop / R target under COMBINED_ADVERSE;
* cross-market: the SAME structure on the other markets (their own frozen Train thresholds), compact.

Every finalist evaluation, neighbour and cross-market instance counts as a trial in the cumulative ledger
(prior 30,310 + the probes' own trial counts + everything evaluated here).
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import hashlib
import json
import math
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402

from alpha.common.protocol import stable_hash  # noqa: E402
from alpha.common.sim import CostScenario, SimRules, SizingSpec  # noqa: E402
from alpha.discovery import temporal_compile  # noqa: E402
from alpha.discovery.folds import Fold, folds_digest, make_folds  # noqa: E402
from alpha.discovery.temporal_evaluate import attach_min_space  # noqa: E402
from alpha.discovery.temporal_genome import (  # noqa: E402
    EventPool,
    GenomeError,
    TemporalGenome,
    canonical_hash,
    canonicalize,
)
from alpha.discovery.temporal_search import current_values, param_space, with_params  # noqa: E402
from alpha.discovery.v2_select import (  # noqa: E402
    SelectConfig,
    build_frozen_payload,
    freeze_selection,
    load_frozen,
    select_finalists,
    trade_stats,
)
from alpha.fast.sim import (  # noqa: E402
    EXIT_FIXED_R,
    CandidateArrays,
    MarketArrays,
    SimWindow,
    TradeArrays,
    simulate_fast,
)
from alpha.temporal.batch import PrefixCache  # noqa: E402
from alpha.temporal.evaluate import evaluate_temporal_many  # noqa: E402
from research.runners import v2_probe  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "research/configs/v2_survival.json"
SURVIVAL_VERSION = "v2-survival-v1"
LABEL = "survival stage - not for selection"
ADVERSE, BASE = v2_probe.ADVERSE, v2_probe.BASE


class SurvivalAlreadyRun(RuntimeError):
    """The survival stage was already called for this frozen finalist hash."""


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- call-once ledger
class SurvivalLedger:
    """Persisted call ledger: ONE survival call per frozen finalist hash (fail closed)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": SURVIVAL_VERSION, "calls": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True, allow_nan=False), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def calls(self) -> dict[str, Any]:
        return self._load()["calls"]

    def acquire(self, finalist_hash: str, meta: dict[str, Any] | None = None) -> None:
        data = self._load()
        if finalist_hash in data["calls"]:
            raise SurvivalAlreadyRun(
                f"survival stage already called for finalist hash {finalist_hash[:12]} "
                f"(status={data['calls'][finalist_hash].get('status')}); one call per frozen list")
        data["calls"][finalist_hash] = {"status": "started", "started_utc": _now(), **(meta or {})}
        self._save(data)

    def complete(self, finalist_hash: str, meta: dict[str, Any]) -> None:
        data = self._load()
        data["calls"][finalist_hash].update(status="completed", completed_utc=_now(), **meta)
        self._save(data)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- context
@dataclass
class SurvivalContext:
    """Everything one market needs for the survival stage (real or synthetic)."""

    name: str
    frame: Any  # MarketFrame (thresholds = frozen fold-0 Train quantiles)
    market: MarketArrays
    folds: list[Fold]
    sizing: SizingSpec
    rules: SimRules
    costs: dict[str, CostScenario]  # BASE and COMBINED_ADVERSE
    window: SimWindow | None
    pool: EventPool
    atr: np.ndarray
    extra_spread: float  # median Train spread (price units) for EXIT_SHOCK
    prefix_cache: Any = None

    @property
    def train_mask(self) -> np.ndarray:
        return self.folds[0].train_mask

    def resolver(self, name: str, q: float) -> float:
        return float(self.frame.thresholds[(name, q)])


def build_survival_context(canonical: str, cfg: dict, cache_dir: Path) -> SurvivalContext:
    """Real-data context (same wiring as the probe: costs / sizing / window / local clock / thresholds)."""
    pcfg = json.loads((REPO_ROOT / cfg["probe_config"]).read_text(encoding="utf-8"))
    ctx = v2_probe.build_real_context(canonical, pcfg, cache_dir)
    f = pcfg["folds"]
    folds = make_folds(ctx.dates, f["n_folds"], f["embargo_days"], f["purge_bars"],
                       initial_train_frac=f["initial_train_frac"], min_test_days=f["min_test_days"])
    if folds_digest(folds) != ctx.plan.fold_digest:
        raise RuntimeError("fold digest mismatch between the probe plan and the survival folds")
    return SurvivalContext(canonical, ctx.frame_provider(), ctx.market, folds, ctx.sizing, ctx.rules, ctx.costs,
                           ctx.window, ctx.pool, ctx.atr, v2_probe.median_train_spread(ctx),
                           PrefixCache(256 * 1024 * 1024))


# --------------------------------------------------------------------------- evaluation primitives
def genome_candidates(sctx: SurvivalContext, genome: TemporalGenome) -> CandidateArrays | None:
    """Full-frame candidate stream (as in the search) or None if the structure cannot run on this market."""
    try:
        canon = canonicalize(genome)
        spec = temporal_compile.compile_temporal(canon, sctx.resolver, canonical=True)
        res = evaluate_temporal_many([spec], sctx.frame, use_cache=True, cache=sctx.prefix_cache)[0].candidates
    except (GenomeError, KeyError, ValueError):
        return None
    return attach_min_space(res, spec)


def delayed(c: CandidateArrays, bars: int, n: int) -> CandidateArrays:
    """Same decisions filled ``bars`` later (fill at open[decision + 1 + bars]); stops/targets unchanged."""
    if bars <= 0 or len(c.decision_idx) == 0:
        return c
    keep = (c.decision_idx + bars) < (n - 1)
    d = c.subset(keep)
    return dataclasses.replace(d, decision_idx=d.decision_idx + bars)


def _variants(sctx: SurvivalContext, cfg: dict) -> dict[str, tuple[CostScenario, int]]:
    return {
        "ADVERSE": (sctx.costs[ADVERSE], 0),
        "BASE": (sctx.costs[BASE], 0),
        "EXIT_SHOCK": (v2_probe.exit_shock_cost(sctx.costs[BASE], sctx.extra_spread), 0),
        "DELAY": (sctx.costs[ADVERSE], int(cfg["entry_delay_bars"])),
    }


def _select(trades: TradeArrays, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if not len(trades):
        return np.zeros(0), np.zeros(0, dtype=np.int64)
    keep = mask[trades.entry_idx]
    return trades.r_multiple[keep], trades.entry_day[keep]


def _f(x: Any) -> float | None:
    return None if x is None or not math.isfinite(float(x)) else round(float(x), 6)


def bundle(trades: TradeArrays, sctx: SurvivalContext, fold_idx: list[int]) -> dict[str, Any]:
    """Per-fold + pooled test-side numbers and cross-fold persistence of one trade stream."""
    per_fold, rs, ds, es = [], [], [], []
    for k in fold_idx:
        r, d = _select(trades, sctx.folds[k].test_mask)
        st = trade_stats(r, d)
        per_fold.append({"fold": k, "n_trades": st["n_trades"], "e": _f(st["mean_r"]), "t_day": _f(st["t_day"])})
        rs.append(r)
        ds.append(d)
        es.append(st["mean_r"])
    r_all, d_all = (np.concatenate(rs), np.concatenate(ds)) if rs else (np.zeros(0), np.zeros(0, np.int64))
    pooled = trade_stats(r_all, d_all)
    n_days = len(np.unique(sctx.market.day[np.any([sctx.folds[k].test_mask for k in fold_idx], axis=0)])) if fold_idx else 0
    pos = [e is not None and e > 0 for e in es]
    return {
        "per_fold": per_fold,
        "pooled": {"n_trades": pooled["n_trades"], "e": _f(pooled["mean_r"]), "t_day": _f(pooled["t_day"]),
                   "tpd": _f(pooled["n_trades"] / n_days) if n_days else None, "payoff": _f(pooled["payoff"]),
                   "profit_factor": _f(pooled["profit_factor"]), "win_rate": _f(pooled["win_rate"])},
        "persistence": {"folds_positive": int(sum(pos)), "folds": len(pos),
                        "share_positive": round(sum(pos) / len(pos), 4) if pos else None,
                        "min_fold_e": _f(min((e for e in es if e is not None), default=None))},
    }


def train_view(trades: TradeArrays, sctx: SurvivalContext) -> dict[str, Any]:
    r, d = _select(trades, sctx.train_mask)
    st = trade_stats(r, d)
    return {"n_trades": st["n_trades"], "e": _f(st["mean_r"]), "t_day": _f(st["t_day"])}


def neighbour_genomes(genome: TemporalGenome, pool: EventPool | None, max_n: int) -> list[TemporalGenome]:
    """+-1 grid step of every numeric gene of the FIXED structure (event variants excluded)."""
    g = canonicalize(genome)
    space = param_space(g, pool)
    cur = current_values(g, pool)
    parent = canonical_hash(g)
    seen = {parent}
    out: list[TemporalGenome] = []
    for name, lo, hi, kind, step in space:
        if name.startswith("evvar"):
            continue
        for sgn in (-1.0, 1.0):
            v = min(max(cur[name] + sgn * step, lo), hi)
            v = round(v) if kind == "int" else round(float(v), 10)
            if v == cur[name]:
                continue
            try:
                child = with_params(g, {name: v}, pool)
            except GenomeError:
                continue
            h = canonical_hash(child)
            if h not in seen:
                seen.add(h)
                out.append(child)
    if len(out) > max_n:  # deterministic, evenly spread subset
        pick = np.unique(np.linspace(0, len(out) - 1, max_n).round().astype(int))
        out = [out[i] for i in pick]
    return out


def test_baselines(sctx: SurvivalContext, fold_idx: list[int], cfg: dict, seed: int, decisions_per_day: int
                   ) -> dict[str, Any]:
    """Drift baselines on the pooled test days (market-level, fixed ATR stop and R target, ADVERSE cost)."""
    b = cfg["baselines"]
    m, atr = sctx.market, sctx.atr
    tmask = np.any([sctx.folds[k].test_mask for k in fold_idx], axis=0)
    win = sctx.window if sctx.window is not None else SimWindow()
    ok = tmask & np.isfinite(atr) & (atr > 0) & (m.minute >= win.entry_start_min) & (m.minute < win.entry_end_min)
    idx = np.flatnonzero(ok)
    cost = sctx.costs[ADVERSE]

    def sim(decision: np.ndarray, direction: np.ndarray) -> float | None:
        if len(decision) == 0:
            return None
        o = np.argsort(decision, kind="stable")
        decision, direction = decision[o], direction[o]
        stop = m.c[decision] - direction * b["stop_atr_mult"] * atr[decision]
        c = CandidateArrays(decision, direction.astype(np.int8), stop, np.full(len(decision), np.nan),
                            np.full(len(decision), float(b["target_r"])), np.full(len(decision), EXIT_FIXED_R, np.int8))
        tr = simulate_fast(m, c, cost, sctx.sizing, sctx.rules, sctx.window)
        r, _ = _select(tr, tmask)
        return float(r.mean()) if len(r) else None

    rng = np.random.default_rng(seed + 11)
    out: dict[str, Any] = {"cost": ADVERSE, "test_days": len(np.unique(m.day[tmask])), "eligible_bars": len(idx),
                           "always_long": _f(sim(idx, np.ones(len(idx), np.int8))),
                           "always_short": _f(sim(idx, -np.ones(len(idx), np.int8)))}
    rl = [sim(idx, rng.choice(np.array([-1, 1], np.int8), len(idx))) for _ in range(b["draws"])]
    rl = [x for x in rl if x is not None]
    out["random_long_short"] = {"draws": len(rl), "mean": _f(np.mean(rl)) if rl else None,
                                "sd": _f(np.std(rl, ddof=1)) if len(rl) > 1 else None}
    days, start = np.unique(m.day[idx], return_index=True)
    bounds = np.r_[start, len(idx)]
    k = max(1, int(decisions_per_day))
    ss = []
    for _ in range(b["draws"]):
        pick = np.concatenate([bounds[i] + rng.choice(bounds[i + 1] - bounds[i], min(k, bounds[i + 1] - bounds[i]),
                                                       replace=False) for i in range(len(days))]) if len(days) else np.zeros(0, int)
        v = sim(idx[pick], rng.choice(np.array([-1, 1], np.int8), len(pick)))
        if v is not None:
            ss.append(v)
    out["same_session_random"] = {"draws": len(ss), "mean": _f(np.mean(ss)) if ss else None,
                                  "sd": _f(np.std(ss, ddof=1)) if len(ss) > 1 else None, "decisions_per_day": k}
    return out


# --------------------------------------------------------------------------- finalist evaluation
def evaluate_home(sctx: SurvivalContext, genome: TemporalGenome, cfg: dict, seed: int) -> tuple[dict[str, Any], int]:
    """Full survival evaluation of one finalist on its HOME market.  Returns (record, n_evaluations)."""
    fold_idx = list(cfg["test_fold_indices"])
    evals = 1
    cands = genome_candidates(sctx, genome)
    if cands is None or not len(cands.decision_idx):
        return {"evaluable": False}, evals
    n = len(sctx.market.o)
    res: dict[str, Any] = {"evaluable": True, "variants": {}}
    trades_adv = None
    for name, (cost, delay) in _variants(sctx, cfg).items():
        tr = simulate_fast(sctx.market, delayed(cands, delay, n), cost, sctx.sizing, sctx.rules, sctx.window)
        res["variants"][name] = bundle(tr, sctx, fold_idx)
        if name == "ADVERSE":
            trades_adv = tr
            res["train"] = train_view(tr, sctx)
    # neighbourhood
    neigh = neighbour_genomes(genome, sctx.pool, int(cfg["neighbours"]["max_per_finalist"]))
    n_e, n_train_e = [], []
    for ng in neigh:
        evals += 1
        nc = genome_candidates(sctx, ng)
        if nc is None or not len(nc.decision_idx):
            continue
        tr = simulate_fast(sctx.market, nc, sctx.costs[ADVERSE], sctx.sizing, sctx.rules, sctx.window)
        e = bundle(tr, sctx, fold_idx)["pooled"]["e"]
        te = train_view(tr, sctx)["e"]
        if e is not None:
            n_e.append(e)
            n_train_e.append(te)
    res["neighbours"] = {
        "generated": len(neigh), "with_trades": len(n_e),
        "share_pos_test": round(float(np.mean(np.asarray(n_e) > 0)), 4) if n_e else None,
        "median_test_e": _f(np.median(n_e)) if n_e else None,
        "share_pos_train": round(float(np.mean(np.asarray([x for x in n_train_e if x is not None]) > 0)), 4)
        if any(x is not None for x in n_train_e) else None,
    }
    # drift baselines on the same test days
    tpd = res["variants"]["ADVERSE"]["pooled"]["tpd"]
    res["baseline"] = test_baselines(sctx, fold_idx, cfg, seed, max(1, round(tpd or 1)))
    rl = res["baseline"]["random_long_short"]
    e_main = res["variants"]["ADVERSE"]["pooled"]["e"]
    z = None
    if e_main is not None and rl["mean"] is not None and rl["sd"]:
        z = round((e_main - rl["mean"]) / rl["sd"], 3)
    res["baseline"]["finalist_minus_random_mean"] = None if z is None else _f(e_main - rl["mean"])
    res["baseline"]["z_vs_random_long_short"] = z
    res["checks"] = checks(res, cfg)
    del trades_adv
    return res, evals


def checks(res: dict[str, Any], cfg: dict) -> dict[str, bool]:
    """Informational checks of one home evaluation (the orchestrator decides; nothing here selects)."""
    c = cfg["checks"]
    v = res["variants"]
    nb = res["neighbours"]["share_pos_test"]
    z = res["baseline"].get("z_vs_random_long_short")
    return {
        "pooled_e_adverse_gt0": (v["ADVERSE"]["pooled"]["e"] or 0.0) > 0.0,
        "pooled_t_ge_min": (v["ADVERSE"]["pooled"]["t_day"] or -1e9) >= c["min_pooled_t"],
        "fold_sign_consistent": (v["ADVERSE"]["persistence"]["share_positive"] or 0.0) >= c["min_fold_positive_share"],
        "exit_shock_e_gt0": (v["EXIT_SHOCK"]["pooled"]["e"] or 0.0) > 0.0,
        "entry_delay_e_gt0": (v["DELAY"]["pooled"]["e"] or 0.0) > 0.0,
        "neighbours_share_pos_ge_min": nb is not None and nb >= c["min_neighbour_positive_share"],
        "beats_random_baseline": z is not None and z >= c["min_baseline_z"],
    }


def evaluate_away(sctx: SurvivalContext, genome: TemporalGenome, cfg: dict) -> dict[str, Any]:
    """Compact evaluation of a finalist's structure on ANOTHER market (its own Train thresholds)."""
    fold_idx = list(cfg["test_fold_indices"])
    cands = genome_candidates(sctx, genome)
    if cands is None or not len(cands.decision_idx):
        return {"evaluable": False}
    tr = simulate_fast(sctx.market, cands, sctx.costs[ADVERSE], sctx.sizing, sctx.rules, sctx.window)
    b = bundle(tr, sctx, fold_idx)
    return {"evaluable": True, "train": train_view(tr, sctx), "test_pooled": b["pooled"], "persistence": b["persistence"]}


def cross_market_summary(away: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ev = {m: a for m, a in away.items() if a.get("evaluable")}
    pos = [m for m, a in ev.items() if (a["test_pooled"]["e"] or 0.0) > 0]
    return {"markets_evaluated": len(ev), "markets_positive_pooled_test": len(pos), "positive_markets": sorted(pos),
            "consistent_positive": bool(ev) and len(pos) == len(ev)}


# --------------------------------------------------------------------------- driver
def _prior_totals(cfg: dict, probe_reports: Path) -> dict[str, Any]:
    trials, unique = int(cfg["prior"]["trials"]), int(cfg["prior"]["unique_specs"])
    parts = {}
    for m in cfg["markets"]:
        p = probe_reports / m / "ledger.json"
        if p.exists():
            led = json.loads(p.read_text(encoding="utf-8"))["ledger"]
            parts[m] = {"trials": led["total_trials"], "unique": len(led["seen"])}
            trials += led["total_trials"]
            unique += len(led["seen"])
    return {"prior_label": cfg["prior"]["label"], "prior_trials": int(cfg["prior"]["trials"]),
            "probe_ledgers": parts, "trials_before_survival": trials, "unique_specs_before_survival": unique}


def load_genomes(probe_reports: Path, market: str, hashes: list[str]) -> dict[str, TemporalGenome]:
    with gzip.open(probe_reports / market / "genomes.json.gz", "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    out = {}
    for h in hashes:
        g = TemporalGenome.from_dict(raw[h])
        if canonical_hash(canonicalize(g)) != h:
            raise RuntimeError(f"{market}: genome {h[:12]} does not match its canonical hash")
        out[h] = g
    return out


def plan_summary(frozen: dict[str, Any], cfg: dict) -> dict[str, Any]:
    sizes = {m: len(v["finalists"]) for m, v in frozen["markets"].items()}
    n = sum(sizes.values())
    per = 1 + int(cfg["neighbours"]["max_per_finalist"]) + (len(frozen["markets"]) - 1 if cfg["cross_market"] else 0)
    return {"finalist_hash": frozen["finalist_hash"], "finalists_per_market": sizes, "finalists_total": n,
            "test_fold_indices": cfg["test_fold_indices"], "max_evaluations": n * per,
            "note": "dry run: no data read, no ledger entry written"}


def run_survival(cfg: dict, frozen: dict[str, Any], genomes: dict[str, dict[str, TemporalGenome]],
                 loader: Callable[[str], SurvivalContext], ledger: SurvivalLedger, out_dir: Path,
                 execute: bool, probe_reports: Path | None = None, seed: int = 20260930,
                 release: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run (or dry-run) the survival stage for a frozen finalist list; ONE call per finalist hash."""
    from alpha.discovery.v2_select import verify_frozen

    verify_frozen(frozen)
    plan = plan_summary(frozen, cfg)
    if not execute:
        return {"dry_run": True, "plan": plan, "label": LABEL}
    fh = frozen["finalist_hash"]
    ledger.acquire(fh, {"plan": plan, "finalist_count": plan["finalists_total"]})
    t0 = time.perf_counter()
    prior = _prior_totals(cfg, probe_reports) if probe_reports else {
        "prior_label": cfg["prior"]["label"], "prior_trials": int(cfg["prior"]["trials"]),
        "trials_before_survival": int(cfg["prior"]["trials"]), "unique_specs_before_survival": int(cfg["prior"]["unique_specs"])}
    home: dict[tuple[str, str], dict[str, Any]] = {}
    away: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    evals = 0
    new_hashes: set[str] = set()
    markets = list(cfg["markets"])
    for x in markets:
        sctx = loader(x)
        for m, block in frozen["markets"].items():
            for f in block["finalists"]:
                g = genomes[m][f["hash"]]
                if m == x:
                    rec, n_ev = evaluate_home(sctx, g, cfg, seed)
                    rec.update(rank=f["rank"], cluster_id=f["cluster_id"], selection_objectives=f["objectives"])
                    home[(m, f["hash"])] = rec
                    evals += n_ev
                    new_hashes.add(f["hash"])
                    for ng in neighbour_genomes(g, sctx.pool, int(cfg["neighbours"]["max_per_finalist"])):
                        new_hashes.add(canonical_hash(ng))
                elif cfg["cross_market"]:
                    away.setdefault((m, f["hash"]), {})[x] = evaluate_away(sctx, g, cfg)
                    evals += 1
        del sctx
        if release is not None:
            release(x)
    results: dict[str, dict[str, Any]] = {}
    for (m, h), rec in sorted(home.items()):
        cm = cross_market_summary(away.get((m, h), {}))
        rec["cross_market"] = {"per_market": away.get((m, h), {}), **cm}
        if "checks" in rec:
            rec["checks"]["cross_market_any_positive"] = cm["markets_positive_pooled_test"] >= 1
            rec["n_core_checks_passed"] = int(sum(v for k, v in rec["checks"].items() if k != "cross_market_any_positive"))
        results.setdefault(m, {})[h] = rec
    out = {
        "label": LABEL, "version": SURVIVAL_VERSION, "finalist_hash": fh, "plan": plan,
        "config_hash": stable_hash(cfg), "executed_utc": _now(),
        "cumulative_ledger": {**prior, "survival_evaluations": evals,
                              "cumulative_trials": prior["trials_before_survival"] + evals,
                              "survival_unique_specs_new_upper_bound": len(new_hashes),
                              "cumulative_unique_specs_upper_bound":
                                  prior["unique_specs_before_survival"] + len(new_hashes)},
        "results": results, "runtime_s": round(time.perf_counter() - t0, 1), "peak_rss_mb": v2_probe.peak_rss_mb(),
        "caveat": "SURVIVAL STAGE: numbers here must not be used to re-select or re-tune finalists.",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "survival_results.json").write_text(json.dumps(out, indent=1, sort_keys=True, allow_nan=False),
                                                   encoding="utf-8")
    (out_dir / "survival_summary.md").write_text(render_md(out), encoding="utf-8")
    ledger.complete(fh, {"evaluations": evals, "results": str(out_dir / "survival_results.json")})
    return out


def render_md(out: dict[str, Any]) -> str:
    lines = [f"# V2 survival stage ({LABEL})", "", f"finalist hash {out['finalist_hash']}",
             f"cumulative trials {out['cumulative_ledger']['cumulative_trials']}", ""]
    for m, block in out["results"].items():
        lines.append(f"## {m}")
        for h, r in block.items():
            if not r.get("evaluable", True):
                lines.append(f"- {h[:10]} not evaluable")
                continue
            v = r["variants"]["ADVERSE"]
            lines.append(f"- {h[:10]} rank {r['rank']}: test E {v['pooled']['e']} t {v['pooled']['t_day']} "
                         f"n {v['pooled']['n_trades']} folds+ {v['persistence']['folds_positive']}/{v['persistence']['folds']} "
                         f"| shock {r['variants']['EXIT_SHOCK']['pooled']['e']} delay {r['variants']['DELAY']['pooled']['e']} "
                         f"| neigh+ {r['neighbours']['share_pos_test']} | checks {r.get('n_core_checks_passed')}/{len(r['checks']) - 1}")
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI
def _cfg(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_select(args: argparse.Namespace) -> int:
    cfg = _cfg(args.config)
    probe_reports = REPO_ROOT / cfg["probe_reports"]
    sc = SelectConfig(**{k: v for k, v in cfg["selection"].items() if k != "note"})
    sels, digests = {}, {}
    for m in cfg["markets"]:
        df, daily = v2_probe.load_stats(probe_reports / m)
        rows = []
        for r in df.to_dict("records"):
            r = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in r.items()}
            r["passer"] = bool(r["passer"])
            r["n_trades"] = int(r["n_trades"] or 0)
            rows.append(r)
        sels[m] = select_finalists(rows, daily, sc)
        digests[m] = hashlib.sha256((probe_reports / m / "stats.csv.gz").read_bytes()).hexdigest()
        log(f"[{m}] eligible {sels[m]['n_eligible']} fronts {sels[m]['n_fronts']} clusters {sels[m]['n_clusters']} "
            f"-> {len(sels[m]['finalists'])} finalists")
    payload = build_frozen_payload(sels, digests)
    frozen = freeze_selection(REPO_ROOT / cfg["frozen_finalists"], payload)
    log(f"frozen {cfg['frozen_finalists']} hash {frozen['finalist_hash']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args.config)
    frozen = load_frozen(REPO_ROOT / cfg["frozen_finalists"])
    probe_reports = REPO_ROOT / cfg["probe_reports"]
    genomes = {m: load_genomes(probe_reports, m, [f["hash"] for f in b["finalists"]])
               for m, b in frozen["markets"].items()}
    cache_root = REPO_ROOT / cfg["cache_dir"]

    def loader(m: str) -> SurvivalContext:
        shutil.rmtree(cache_root / m, ignore_errors=True)
        return build_survival_context(m, cfg, cache_root / m)

    def release(m: str) -> None:  # run-scoped cache: gone as soon as the market is done (mmaps released first)
        import gc

        gc.collect()
        shutil.rmtree(cache_root / m, ignore_errors=True)

    out = run_survival(cfg, frozen, genomes, loader, SurvivalLedger(REPO_ROOT / cfg["ledger"]),
                       REPO_ROOT / cfg["out_dir"], args.execute, probe_reports, release=release)
    log(json.dumps(out.get("plan") or out["cumulative_ledger"], indent=1, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.set_defaults(fn=cmd_select)
    r = sub.add_parser("run")
    r.add_argument("--execute", action="store_true", help="really run (default is the dry run)")
    r.set_defaults(fn=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
