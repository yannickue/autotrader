# ruff: noqa: E501
"""V2 fold-test-blind FINALIST selection (pre-registered, fixed, research only).

Input = the Train-only compact stats of the probe (``stats.csv.gz`` + ``daily_r.npz`` per market).  No
fold-test number can enter: the inputs are Train numbers of search fold 0 by construction.

Procedure (fixed; changing any constant below is a NEW pre-registration):

1. ELIGIBLE = passers of the probe (>= min Train trades) with finite Train expectancy.  There is NO
   binary sign / drawdown / volatility gate: positive-expectancy volatile strategies are not filtered.
2. OBJECTIVES (all maximised, six):
   ``e_adv``   Train expectancy R under COMBINED_ADVERSE cost,
   ``t_day``   day-clustered t-statistic of the Train R (CR1 cluster-robust, cluster = entry day),
   ``payoff``  payoff asymmetry = avg win R / |avg loss R| (nan -> 0, capped at ``PAYOFF_CAP``),
   ``tpd``     Train trades per day, capped at ``tpd_cap`` (target: more trades up to the cap),
   ``e_shock`` Train expectancy R under EXIT_SHOCK (BASE + one extra median spread on every market fill),
   ``novelty`` 1 - clip(max correlation of the daily-R vector with any OTHER eligible candidate, 0, 1).
3. Non-dominated (Pareto) sorting into fronts; inside a front NSGA-II crowding distance (larger first,
   boundary points first), ties broken by canonical hash.  => a total order.
4. De-duplication by daily-return correlation: walking that order, a candidate whose daily-R correlation
   with an already-kept leader exceeds ``corr_threshold`` (0.8) joins that leader's cluster; otherwise it
   becomes a new leader.  The finalists are the first K leaders (the Pareto-best of K distinct clusters).
5. The finalist list (per market) and its hash are FROZEN to disk before the survival stage and can never
   be silently changed (``freeze_selection`` refuses a different list).

Also here: shared small statistics helpers (day-clustered t, correlation clustering, effective independent
trials and a Bailey / Lopez de Prado style deflated-Sharpe adjustment, INFORMATIONAL only).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np

SELECT_VERSION = "v2-select-v1"
PAYOFF_CAP = 10.0
OBJECTIVES = ("e_adv", "t_day", "payoff", "tpd", "e_shock", "novelty")
EULER_GAMMA = 0.5772156649015329


class FrozenSelectionError(RuntimeError):
    """An existing frozen finalist list would be changed / is corrupt."""


@dataclass(frozen=True)
class SelectConfig:
    k: int = 30
    tpd_cap: float = 1.5
    corr_threshold: float = 0.8
    min_trades: int = 40

    def to_dict(self) -> dict[str, Any]:
        return {"version": SELECT_VERSION, **asdict(self), "objectives": list(OBJECTIVES),
                "payoff_cap": PAYOFF_CAP}


# --------------------------------------------------------------------------- statistics helpers
def day_clustered_t(r: np.ndarray, day: np.ndarray) -> float | None:
    """t-stat of mean(r) with cluster-robust (by ``day``) standard error, small-sample G/(G-1).

    None if fewer than 2 trades / 2 clusters or zero variance."""
    r = np.asarray(r, dtype=float)
    n = len(r)
    if n < 2:
        return None
    _, inv = np.unique(np.asarray(day), return_inverse=True)
    g = int(inv.max()) + 1
    if g < 2:
        return None
    mean = float(r.mean())
    s = np.bincount(inv, weights=r - mean, minlength=g)
    var = float(np.sum(s * s)) / (n * n) * g / (g - 1)
    if not var > 0.0:
        return None
    return mean / math.sqrt(var)


def trade_stats(r: np.ndarray, day: np.ndarray) -> dict[str, float | int | None]:
    """Compact per-candidate trade statistics (R multiples ``r`` of trades entering on ``day``)."""
    r = np.asarray(r, dtype=float)
    n = len(r)
    if n == 0:
        return {"n_trades": 0, "mean_r": None, "t_day": None, "payoff": None, "profit_factor": None,
                "win_rate": None, "avg_win_r": None, "avg_loss_r": None}
    wins, losses = r[r > 0], r[r < 0]
    gl = float(-losses.sum())
    return {
        "n_trades": n, "mean_r": float(r.mean()), "t_day": day_clustered_t(r, day),
        "payoff": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
        "profit_factor": float(wins.sum() / gl) if gl > 0 else None,
        "win_rate": float((r > 0).mean()),
        "avg_win_r": float(wins.mean()) if len(wins) else None,
        "avg_loss_r": float(losses.mean()) if len(losses) else None,
    }


def daily_vector(r: np.ndarray, entry_day: np.ndarray, days: np.ndarray) -> np.ndarray:
    """Sum of R per day over the sorted unique ``days`` (zero-trade days = 0)."""
    out = np.zeros(len(days))
    if len(r):
        pos = np.searchsorted(days, entry_day)
        ok = (pos < len(days)) & (days[np.minimum(pos, len(days) - 1)] == entry_day)
        np.add.at(out, pos[ok], np.asarray(r, dtype=float)[ok])
    return out


def daily_hash(vec: np.ndarray) -> str:
    return hashlib.sha1(np.round(np.asarray(vec, dtype=np.float64), 6).tobytes()).hexdigest()[:16]


def corr_matrix(mat: np.ndarray) -> np.ndarray:
    """Pearson correlation between ROWS; constant rows correlate 0 with everything (1 with themselves)."""
    x = np.asarray(mat, dtype=np.float64)
    if x.ndim != 2 or len(x) == 0:
        return np.zeros((len(x), len(x)))
    x = x - x.mean(axis=1, keepdims=True)
    norm = np.sqrt((x * x).sum(axis=1))
    ok = norm > 1e-12
    z = np.zeros_like(x)
    z[ok] = x[ok] / norm[ok, None]
    c = z @ z.T
    np.fill_diagonal(c, 1.0)
    return np.clip(c, -1.0, 1.0)


def leader_clusters(order: np.ndarray, corr: np.ndarray, threshold: float) -> tuple[np.ndarray, list[int]]:
    """Greedy leader clustering in ``order``: ``cluster[i]`` = index (into the leader list) of the FIRST
    leader with corr > threshold, else i becomes a new leader.  Returns (cluster id per item, leaders)."""
    cluster = np.full(len(order), -1, dtype=np.int64)
    leaders: list[int] = []
    for i in order:
        hit = -1
        for li, lead in enumerate(leaders):
            if corr[i, lead] > threshold:
                hit = li
                break
        if hit < 0:
            leaders.append(int(i))
            hit = len(leaders) - 1
        cluster[i] = hit
    return cluster, leaders


def effective_independent_trials(mat: np.ndarray, order: np.ndarray, threshold: float = 0.8) -> dict[str, Any]:
    """Distinct strategy behaviours: number of leader clusters at daily-R correlation > threshold."""
    n = len(mat)
    if n == 0:
        return {"n_strategies": 0, "n_clusters": 0, "cluster_sizes_top": []}
    corr = corr_matrix(mat)
    cluster, leaders = leader_clusters(np.asarray(order, dtype=np.int64), corr, threshold)
    sizes = np.bincount(cluster, minlength=len(leaders))
    return {"n_strategies": n, "n_clusters": len(leaders), "corr_threshold": threshold,
            "cluster_sizes_top": sorted((int(s) for s in sizes), reverse=True)[:10],
            "leaders": [int(x) for x in leaders]}


def expected_max_sharpe(var_sr: float, n_trials: float) -> float:
    """Bailey / Lopez de Prado expected maximum of ``n_trials`` independent Sharpe ratios with variance
    ``var_sr`` under the null of zero true Sharpe."""
    if n_trials < 2 or not var_sr > 0:
        return 0.0
    nd = NormalDist()
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * nd.inv_cdf(1 - 1.0 / n_trials)
                                + EULER_GAMMA * nd.inv_cdf(1 - 1.0 / (n_trials * math.e)))


def deflated_sharpe(vec: np.ndarray, sr_var_across_trials: float, n_eff: float) -> dict[str, float | None]:
    """INFORMATIONAL deflated-Sharpe-like adjustment for one daily-R vector.

    sr = per-day Sharpe (mean/sd of the daily R over all days); sr0 = expected max Sharpe of ``n_eff``
    independent trials; DSR = Phi((sr - sr0) sqrt(T - 1) / sqrt(1 - skew sr + (kurt - 1)/4 sr^2))."""
    x = np.asarray(vec, dtype=float)
    t = len(x)
    if t < 3 or not x.std() > 0:
        return {"sharpe_day": None, "sr0": None, "dsr": None}
    sr = float(x.mean() / x.std(ddof=1))
    z = (x - x.mean()) / x.std(ddof=0)
    skew, kurt = float((z**3).mean()), float((z**4).mean())
    sr0 = expected_max_sharpe(sr_var_across_trials, n_eff)
    denom2 = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if not denom2 > 0:
        return {"sharpe_day": sr, "sr0": sr0, "dsr": None}
    stat = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom2)
    return {"sharpe_day": round(sr, 6), "sr0": round(sr0, 6), "skew": round(skew, 4), "kurt": round(kurt, 4),
            "dsr": round(NormalDist().cdf(stat), 6)}


# --------------------------------------------------------------------------- Pareto
def non_dominated_fronts(obj: np.ndarray) -> np.ndarray:
    """Front index (0 = non-dominated) per row for MAXIMISED objectives ``obj`` [n, m]."""
    n = len(obj)
    front = np.full(n, -1, dtype=np.int64)
    if n == 0:
        return front
    ge = (obj[:, None, :] >= obj[None, :, :]).all(axis=2)
    gt = (obj[:, None, :] > obj[None, :, :]).any(axis=2)
    dominates = ge & gt  # dominates[i, j]: i dominates j
    n_dom = dominates.sum(axis=0)  # how many dominate j
    remaining = np.ones(n, dtype=bool)
    level = 0
    while remaining.any():
        current = remaining & (n_dom == 0)
        if not current.any():  # cannot happen for a strict partial order; fail closed
            raise RuntimeError("non-dominated sorting did not converge")
        front[current] = level
        n_dom = n_dom - dominates[current].sum(axis=0)
        remaining &= ~current
        n_dom = np.where(remaining, n_dom, 1)
        level += 1
    return front


def crowding_distance(obj: np.ndarray, front: np.ndarray) -> np.ndarray:
    """NSGA-II crowding distance within each front (boundary points = inf)."""
    dist = np.zeros(len(obj))
    for f in np.unique(front):
        idx = np.flatnonzero(front == f)
        if len(idx) <= 2:
            dist[idx] = np.inf
            continue
        for j in range(obj.shape[1]):
            col = obj[idx, j]
            o = np.argsort(col, kind="stable")
            span = col[o[-1]] - col[o[0]]
            dist[idx[o[0]]] = np.inf
            dist[idx[o[-1]]] = np.inf
            if span > 0:
                dist[idx[o[1:-1]]] += (col[o[2:]] - col[o[:-2]]) / span
    return dist


def pareto_order(obj: np.ndarray, hashes: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(order, front, crowding): total order by (front asc, crowding desc, hash asc)."""
    front = non_dominated_fronts(obj)
    crowd = crowding_distance(obj, front)
    order = np.array(sorted(range(len(obj)), key=lambda i: (front[i], -crowd[i], hashes[i])), dtype=np.int64)
    return order, front, crowd


# --------------------------------------------------------------------------- selection
def _fin(x: Any, default: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def objective_matrix(rows: list[dict[str, Any]], novelty: np.ndarray, cfg: SelectConfig) -> np.ndarray:
    out = np.zeros((len(rows), len(OBJECTIVES)))
    for i, r in enumerate(rows):
        out[i] = (
            _fin(r["e_adv"], -1e9), _fin(r["t_day"], 0.0),
            PAYOFF_CAP if r["payoff"] == math.inf else min(max(_fin(r["payoff"], 0.0), 0.0), PAYOFF_CAP),
            min(_fin(r["tpd"], 0.0), cfg.tpd_cap), _fin(r["e_shock"], -1e9), float(novelty[i]),
        )
    return out


def select_finalists(rows: list[dict[str, Any]], daily: dict[str, np.ndarray], cfg: SelectConfig | None = None
                     ) -> dict[str, Any]:
    """Fixed finalist selection for ONE market.

    ``rows``: dicts with hash, passer, n_trades, e_adv, t_day, payoff, tpd, e_shock (Train numbers).
    ``daily``: hash -> daily-R vector (same day axis for all).  Returns the selection record."""
    cfg = cfg or SelectConfig()
    elig = [r for r in rows if r.get("passer") and r["n_trades"] >= cfg.min_trades
            and math.isfinite(_fin(r["e_adv"], float("nan"))) and r["hash"] in daily]
    elig.sort(key=lambda r: r["hash"])  # deterministic base order
    hashes = [r["hash"] for r in elig]
    if not elig:
        return {"config": cfg.to_dict(), "n_eligible": 0, "n_fronts": 0, "n_clusters": 0, "finalists": []}
    mat = np.stack([np.asarray(daily[h], dtype=float) for h in hashes])
    corr = corr_matrix(mat)
    off = corr.copy()
    np.fill_diagonal(off, -1.0)
    novelty = 1.0 - np.clip(off.max(axis=1), 0.0, 1.0) if len(elig) > 1 else np.ones(1)
    obj = objective_matrix(elig, novelty, cfg)
    order, front, crowd = pareto_order(obj, hashes)
    cluster, leaders = leader_clusters(order, corr, cfg.corr_threshold)
    sizes = np.bincount(cluster, minlength=len(leaders))
    finalists = []
    for rank, li in enumerate(leaders[: cfg.k]):
        r = elig[li]
        finalists.append({
            "rank": rank, "hash": r["hash"], "front": int(front[li]), "cluster_id": int(cluster[li]),
            "cluster_size": int(sizes[cluster[li]]),
            "objectives": {k: round(float(obj[li, j]), 6) for j, k in enumerate(OBJECTIVES)},
            "crowding": None if not math.isfinite(crowd[li]) else round(float(crowd[li]), 6),
        })
    return {"config": cfg.to_dict(), "n_eligible": len(elig), "n_fronts": int(front.max()) + 1,
            "n_front0": int((front == 0).sum()), "n_clusters": len(leaders), "finalists": finalists}


# --------------------------------------------------------------------------- freezing
def finalist_hash(markets: dict[str, list[str]], cfg: dict[str, Any]) -> str:
    """Hash of the ordered per-market finalist canonical hashes + the selection config."""
    payload = json.dumps({"markets": {m: markets[m] for m in sorted(markets)}, "config": cfg},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(b"v2-finalists-v1:" + payload.encode()).hexdigest()


def build_frozen_payload(selections: dict[str, dict[str, Any]], stats_digests: dict[str, str] | None = None
                         ) -> dict[str, Any]:
    cfgs = {json.dumps(s["config"], sort_keys=True) for s in selections.values()}
    if len(cfgs) != 1:
        raise ValueError("all markets must use one selection config")
    cfg = json.loads(next(iter(cfgs)))
    lists = {m: [f["hash"] for f in s["finalists"]] for m, s in selections.items()}
    return {
        "kind": "v2-frozen-finalists", "version": SELECT_VERSION, "config": cfg,
        "finalist_hash": finalist_hash(lists, cfg), "sizes": {m: len(v) for m, v in sorted(lists.items())},
        "stats_digests": stats_digests or {},
        "markets": {m: {"n_eligible": s["n_eligible"], "n_clusters": s["n_clusters"],
                        "finalists": s["finalists"]} for m, s in sorted(selections.items())},
        "frozen_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "fold-test-blind selection; frozen BEFORE the survival stage",
    }


def verify_frozen(payload: dict[str, Any]) -> None:
    lists = {m: [f["hash"] for f in v["finalists"]] for m, v in payload["markets"].items()}
    if finalist_hash(lists, payload["config"]) != payload.get("finalist_hash"):
        raise FrozenSelectionError("frozen finalist list does not match its hash (tampered?)")


def freeze_selection(path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Write the frozen finalist file; an existing file with a DIFFERENT finalist hash is never replaced."""
    p = Path(path)
    if p.exists():
        old = json.loads(p.read_text(encoding="utf-8"))
        verify_frozen(old)
        if old["finalist_hash"] != payload["finalist_hash"]:
            raise FrozenSelectionError(
                f"{p} is already frozen with hash {old['finalist_hash'][:12]}; refusing a different list "
                f"({payload['finalist_hash'][:12]}); a new selection needs a new pre-registration file")
        return old
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=1, sort_keys=True, allow_nan=False), encoding="utf-8")
    return payload


def load_frozen(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    verify_frozen(payload)
    return payload


__all__ = (
    "OBJECTIVES", "PAYOFF_CAP", "SELECT_VERSION", "FrozenSelectionError", "SelectConfig", "build_frozen_payload",
    "corr_matrix", "crowding_distance", "daily_hash", "daily_vector", "day_clustered_t", "deflated_sharpe",
    "effective_independent_trials", "expected_max_sharpe", "finalist_hash", "freeze_selection",
    "leader_clusters", "load_frozen", "non_dominated_fronts", "pareto_order", "select_finalists",
    "trade_stats", "verify_frozen",
)
