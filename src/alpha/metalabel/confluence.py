# ruff: noqa: E501
"""Confluence value: does agreement of INDEPENDENT strategies improve conditional expectancy? (research only)

For every (decision bar, direction) where at least one pool strategy fires, a STANDARD reference outcome
is computed that does not depend on any strategy's own exits: enter at the next open, stop 1.0 ATR,
target 1.5R, COMBINED_ADVERSE cost, the market's sim window (``reference_outcomes``).  Observations are
grouped by ``k`` = the number of independent strategy CLUSTERS firing the same direction at that bar
(clusters = connected components of {decision-bar Jaccard > 0.5 or daily-R correlation > 0.8}); the same
table is also cut by the number of distinct families.  The incremental edge is
``mean R(k >= 2 clusters) - mean R(k = 1)`` with a day-cluster bootstrap SE.  The null moves each cluster's
firings to another trading day (same clock minute, one random cyclic day offset per cluster), which
preserves how often and when each cluster fires and the intra-cluster alignment but breaks the cross-cluster
agreement at the bar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from alpha.common.sim import CostScenario, SimRules, SizingSpec
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, MarketArrays, SimWindow, simulate_fast
from alpha.metalabel import metrics

MINUTE_KEY = 4096


# --------------------------------------------------------------------------- reference outcomes
def reference_outcomes(market: MarketArrays, atr: np.ndarray, decisions: np.ndarray, direction: int, cost: CostScenario,
                       sizing: SizingSpec, rules: SimRules, window: SimWindow | None, stop_atr: float = 1.0,
                       target_r: float = 1.5) -> np.ndarray:
    """Net R (after ``cost``) of the standard reference trade per decision bar; NaN where it cannot be booked
    (window / spread filter / invalid risk).  Overlapping decisions are simulated in successive passes so that
    every bar gets its own independent outcome (no single-position occupancy effect)."""
    n = len(market.o)
    out = np.full(n, np.nan)
    rem = np.unique(np.asarray(decisions, dtype=np.int64))
    rem = rem[np.isfinite(atr[rem]) & (atr[rem] > 0)]
    free_rules = SimRules(max_trades_per_day=10 ** 6, max_entry_spread_pts=rules.max_entry_spread_pts)
    while len(rem):
        d = np.full(len(rem), direction, dtype=np.int8)
        stop = market.c[rem] - d * stop_atr * atr[rem]
        cand = CandidateArrays(rem, d, stop, np.full(len(rem), np.nan), np.full(len(rem), float(target_r)),
                               np.full(len(rem), EXIT_FIXED_R, dtype=np.int8))
        tr = simulate_fast(market, cand, cost, sizing, free_rules, window)
        if len(tr) == 0:
            break
        out[tr.decision_idx] = tr.r_multiple
        rem = rem[~np.isin(rem, tr.decision_idx)]
    return out


# --------------------------------------------------------------------------- independence clusters
def _components(n: int, edges: np.ndarray) -> np.ndarray:
    parent = np.arange(n)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in edges:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    roots = np.array([find(i) for i in range(n)])
    _, cid = np.unique(roots, return_inverse=True)
    return cid


def cluster_specs(codes: Sequence[np.ndarray], daily_r: np.ndarray | None, jac_thr: float = 0.5,
                  corr_thr: float = 0.8) -> tuple[np.ndarray, dict]:
    """Cluster strategies: link if Jaccard of their (bar, direction) sets > ``jac_thr`` or daily-R correlation
    > ``corr_thr``; clusters = connected components.  ``codes[s]`` = sorted unique ``bar*2+dir>0`` codes."""
    n = len(codes)
    if n == 0:
        return np.zeros(0, dtype=np.int64), {"n_specs": 0, "n_clusters": 0}
    universe = np.unique(np.concatenate(codes)) if any(len(c) for c in codes) else np.zeros(0, dtype=np.int64)
    inc = np.zeros((n, max(len(universe), 1)), dtype=np.float32)
    for s, c in enumerate(codes):
        if len(c):
            inc[s, np.searchsorted(universe, c)] = 1.0
    inter = inc @ inc.T
    size = inc.sum(axis=1)
    union = size[:, None] + size[None, :] - inter
    with np.errstate(invalid="ignore", divide="ignore"):
        jac = np.where(union > 0, inter / union, 0.0)
    link = jac > jac_thr
    corr_links = 0
    if daily_r is not None and daily_r.shape[0] == n and daily_r.shape[1] >= 3:
        sd = daily_r.std(axis=1)
        ok = sd > 1e-12
        z = np.zeros_like(daily_r, dtype=np.float64)
        z[ok] = (daily_r[ok] - daily_r[ok].mean(axis=1, keepdims=True)) / sd[ok, None]
        corr = z @ z.T / daily_r.shape[1]
        cl = (corr > corr_thr) & ok[:, None] & ok[None, :]
        corr_links = int(np.triu(cl & ~link, 1).sum())
        link = link | cl
    np.fill_diagonal(link, False)
    edges = np.argwhere(np.triu(link, 1))
    cid = _components(n, edges)
    iu = np.triu_indices(n, 1)
    return cid, {"n_specs": n, "n_clusters": int(cid.max() + 1), "jaccard_gt_thr_pairs": int(np.triu(jac > jac_thr, 1).sum()),
                 "corr_only_links": corr_links, "pairs": len(iu[0]),
                 "largest_cluster": int(np.bincount(cid).max())}


# --------------------------------------------------------------------------- group counts
def _distinct_per_code(code: np.ndarray, other: np.ndarray, uc: np.ndarray) -> np.ndarray:
    m = int(other.max()) + 1 if len(other) else 1
    pair = np.unique(code.astype(np.int64) * m + other.astype(np.int64))
    c2 = pair // m
    u2, cnt = np.unique(c2, return_counts=True)
    return cnt[np.searchsorted(u2, uc)]


def group_table(bar: np.ndarray, dirpos: np.ndarray, spec: np.ndarray, cluster: np.ndarray, family: np.ndarray
                ) -> dict[str, np.ndarray]:
    """One row per (bar, direction) group with the number of distinct specs / clusters / families firing."""
    code = bar.astype(np.int64) * 2 + dirpos.astype(np.int64)
    uc = np.unique(code)
    return {"code": uc, "bar": uc // 2, "dirpos": uc % 2,
            "k_raw": _distinct_per_code(code, spec, uc), "k_cl": _distinct_per_code(code, cluster, uc),
            "k_fam": _distinct_per_code(code, family, uc)}


def _lookup_r(bar: np.ndarray, dirpos: np.ndarray, ref_long: np.ndarray, ref_short: np.ndarray) -> np.ndarray:
    return np.where(dirpos > 0, ref_long[bar], ref_short[bar])


def _bucket(k: np.ndarray) -> np.ndarray:
    return np.minimum(k, 3)


def _bucket_rows(r: np.ndarray, day: np.ndarray, k: np.ndarray, seed: int) -> dict:
    out = {}
    b = _bucket(k)
    for v, name in ((1, "1"), (2, "2"), (3, "3+")):
        m = b == v
        st = metrics.day_clustered_mean(r[m], day[m]) if m.any() else {"n": 0}
        st["hit_rate"] = float((r[m] > 0).mean()) if m.any() else None
        out[name] = st
    out["ge2_vs_1"] = metrics.cluster_boot_contrast(r, day, b >= 2, b == 1, seed=seed)
    out["3plus_vs_1"] = metrics.cluster_boot_contrast(r, day, b >= 3, b == 1, seed=seed + 1)
    return out


def _shift_codes(bar: np.ndarray, dirpos: np.ndarray, cluster: np.ndarray, delta_by_cluster: np.ndarray,
                 day_ord: np.ndarray, minute: np.ndarray, keys: np.ndarray, key_bar: np.ndarray, n_days: int
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Move rows to bar (day + delta[cluster]) mod n_days at the same minute; drops rows without such a bar."""
    nd = (day_ord[bar] + delta_by_cluster[cluster]) % n_days
    new_key = nd * MINUTE_KEY + minute[bar]
    pos = np.clip(np.searchsorted(keys, new_key), 0, len(keys) - 1)
    ok = keys[pos] == new_key
    return key_bar[pos[ok]], dirpos[ok], np.flatnonzero(ok)


@dataclass
class ConfluenceData:
    """Everything the analysis needs (all Train side, arrays per pool candidate row / per bar)."""

    row_bar: np.ndarray  # decision bar of every candidate row
    row_dirpos: np.ndarray  # 1 = long
    row_spec: np.ndarray  # spec index of the row
    spec_family: np.ndarray  # family index per spec
    spec_codes: list[np.ndarray]  # per spec: sorted unique codes
    daily_r: np.ndarray | None  # specs x train days daily R (for the correlation link), optional
    ref_long: np.ndarray  # per-bar reference R (NaN = none)
    ref_short: np.ndarray
    day_ord: np.ndarray  # trading-day ordinal per bar (train bars; -1 elsewhere)
    minute: np.ndarray
    train_mask: np.ndarray


def analyse(data: ConfluenceData, n_null: int = 200, seed: int = 0) -> dict:
    """Full confluence analysis (see module doc) -> JSON-ready dict with verdict."""
    cid, cinfo = cluster_specs(data.spec_codes, data.daily_r)
    cluster = cid[data.row_spec]
    fam = data.spec_family[data.row_spec]
    g = group_table(data.row_bar, data.row_dirpos, data.row_spec, cluster, fam)
    r = _lookup_r(g["bar"], g["dirpos"], data.ref_long, data.ref_short)
    ok = np.isfinite(r) & data.train_mask[g["bar"]]
    day = data.day_ord[g["bar"]]
    res: dict = {"clusters": cinfo, "n_groups": len(g["code"]), "n_groups_with_reference": int(ok.sum()),
                 "n_groups_without_reference": int((~ok).sum())}
    elig = np.flatnonzero(data.train_mask)
    allr = np.concatenate([data.ref_long[elig], data.ref_short[elig]])
    allr = allr[np.isfinite(allr)]
    res["reference_all_eligible_bars_mean_r"] = float(allr.mean()) if len(allr) else None
    res["by_clusters"] = _bucket_rows(r[ok], day[ok], g["k_cl"][ok], seed)
    res["by_families"] = _bucket_rows(r[ok], day[ok], g["k_fam"][ok], seed + 10)
    res["by_raw_specs"] = _bucket_rows(r[ok], day[ok], g["k_raw"][ok], seed + 20)
    k_share = {str(v): float(np.mean(_bucket(g["k_cl"][ok]) == v)) for v in (1, 2, 3)}
    res["share_groups_by_cluster_bucket"] = k_share
    obs = res["by_clusters"]["ge2_vs_1"]["diff"]
    # ---- day-shift null (per-cluster cyclic day offset; same minute)
    train_bars = np.flatnonzero(data.train_mask & (data.day_ord >= 0))
    keys = data.day_ord[train_bars].astype(np.int64) * MINUTE_KEY + data.minute[train_bars]
    order = np.argsort(keys)
    keys, key_bar = keys[order], train_bars[order]
    n_days = int(data.day_ord[train_bars].max()) + 1 if len(train_bars) else 1
    rng = np.random.default_rng(seed + 99)
    n_cl = int(cid.max() + 1) if len(cid) else 0
    diffs, means_ge2 = [], []
    for _ in range(n_null if obs is not None else 0):
        delta = rng.integers(max(n_days // 10, 1), max(n_days - n_days // 10, 2), size=n_cl)
        b2, d2, kept = _shift_codes(data.row_bar, data.row_dirpos, cluster, delta, data.day_ord, data.minute, keys, key_bar, n_days)
        gs = group_table(b2, d2, data.row_spec[kept], cluster[kept], fam[kept])
        rs = _lookup_r(gs["bar"], gs["dirpos"], data.ref_long, data.ref_short)
        oks = np.isfinite(rs)
        bk = _bucket(gs["k_cl"][oks])
        rr = rs[oks]
        if (bk == 1).any() and (bk >= 2).any():
            diffs.append(rr[bk >= 2].mean() - rr[bk == 1].mean())
            means_ge2.append(rr[bk >= 2].mean())
    diffs_a = np.asarray(diffs)
    if obs is not None and len(diffs_a):
        res["null_day_shift"] = {
            "n_draws": len(diffs_a), "diff_mean": float(diffs_a.mean()), "diff_sd": float(diffs_a.std(ddof=1)),
            "diff_q95": float(np.quantile(diffs_a, 0.95)), "obs_diff": float(obs),
            "p_upper": float((1 + np.sum(diffs_a >= obs)) / (1 + len(diffs_a))),
            "p_lower": float((1 + np.sum(diffs_a <= obs)) / (1 + len(diffs_a))),
        }
    res["verdict"] = confluence_verdict(res)
    return res


def confluence_verdict(res: dict, alpha: float = 0.05) -> dict:
    c = res["by_clusters"]["ge2_vs_1"]
    nul = res.get("null_day_shift")
    if c["diff"] is None or c["t"] is None or nul is None:
        return {"verdict": "INCONCLUSIVE", "reason": "too few agreeing groups or null unavailable"}
    if c["diff"] > 0 and c["t"] > 2.0 and nul["p_upper"] <= alpha:
        return {"verdict": "CONFLUENCE POSITIVE", "reason": "k>=2 beats k=1 (t>2) and the day-shift null"}
    if c["diff"] < 0 and c["t"] < -2.0 and nul["p_lower"] <= alpha:
        return {"verdict": "CONFLUENCE NEGATIVE", "reason": "k>=2 is worse than k=1 (t<-2) and the day-shift null"}
    return {"verdict": "INCONCLUSIVE", "reason": f"diff={c['diff']:.4f}, t={c['t']:.2f}, p_upper={nul['p_upper']:.3f}, p_lower={nul['p_lower']:.3f}"}


__all__ = ("ConfluenceData", "analyse", "cluster_specs", "confluence_verdict", "group_table", "reference_outcomes")
