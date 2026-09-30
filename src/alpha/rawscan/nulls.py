"""Selection-aware nulls for the raw edge scan.

Every null returns, per replicate, the maximum over the examined cells of the net (BASE) t-stat and
of |t|, plus how many cells exceed t > 2 and t > 3, so the OBSERVED best cell can be compared with
what the same multiplicity produces by chance.

* ``day_shift_null`` - circularly shift the OUTCOME series (returns, costs, validity) by k >= 1
  trading days relative to the labels (time-of-day is untouched, so the intraday shape and the
  serial dependence inside the outcome series are preserved; day-of-week labels and the causal
  conditioning signals - gap, first-30-minute move, prior-day return - are decoupled from the
  outcome).  HONEST LIMIT: a cell that is unconditional over all days (plain LONG/SHORT at a time of
  day) has exactly the same t under any whole-day shift, so this null only speaks for the LABELLED
  cells (day-of-week slices and conditioned families); it is applied to those and reported as such.
* ``day_bootstrap_null`` - resample whole days with replacement (all cells of a replicate share
  the same draw, keeping the cross-cell dependence).  Every cell's GROSS daily series is centred at
  its own sample mean while the per-day COSTS are kept: H0 = "no gross edge, costs still paid".
  Applies to ALL cells.  Caveat: with ~25-30 days per day-of-week cell and fat-tailed returns the
  resampled t of those cells is over-dispersed (a repeated outlier day); the all-days subset
  (``max_t_alldays``) is reported separately for that reason.
* ``day_signflip_null`` - the same centred gross series with each day's sign flipped at random
  (one draw per day shared by all cells, costs unflipped): an exact symmetric null that does not
  suffer from the repeated-outlier effect of the bootstrap.
"""

from __future__ import annotations

import numpy as np

from alpha.rawscan.engine import CellSpace, moments, net_matrix
from alpha.rawscan.stats import percentiles, t_from_moments


def draw_shifts(n_days: int, n_rep: int, rng: np.random.Generator) -> np.ndarray:
    """k in [1, n_days-1] (never 0); distinct when possible, else all values cycled."""
    if n_days < 3:
        raise ValueError("need at least 3 days for a shift null")
    pool = np.arange(1, n_days)
    if len(pool) >= n_rep:
        return np.sort(rng.choice(pool, size=n_rep, replace=False))
    return np.resize(pool, n_rep)


def _minn(cs: CellSpace, cols: np.ndarray, min_n: int, min_n_dow: int) -> np.ndarray:
    return np.where(cs.meta["dow"].to_numpy()[cols] >= 0, min_n_dow, min_n)


def _stats_row(t: np.ndarray, alldays: np.ndarray | None = None) -> tuple:
    ok = np.isfinite(t)
    if not ok.any():
        return float("nan"), float("nan"), 0, 0, float("nan")
    tv = t[ok]
    ad = float("nan")
    if alldays is not None and (ok & alldays).any():
        ad = float(t[ok & alldays].max())
    return float(tv.max()), float(np.abs(tv).max()), int((tv > 2).sum()), int((tv > 3).sum()), ad


def _pack(rows: list[tuple], extra: dict) -> dict:
    a = np.array(rows, dtype=float)
    return {
        "max_t": a[:, 0], "max_abs_t": a[:, 1], "n_gt2": a[:, 2], "n_gt3": a[:, 3],
        "max_t_alldays": a[:, 4], **extra,
    }


def day_shift_null(
    cs: CellSpace, *, n_rep: int = 200, seed: int = 0, min_n: int = 40, min_n_dow: int = 20,
) -> dict:
    D = cs.pairs.G.shape[0]
    lab = np.flatnonzero(cs.meta["labelled"].to_numpy())
    minn = _minn(cs, lab, min_n, min_n_dow)
    ks = draw_shifts(D, n_rep, np.random.default_rng(seed))
    rows = []
    for k in ks:
        n, s1, s2 = moments(net_matrix(cs, "BASE", cols=lab, shift=int(k)))
        _, t = t_from_moments(n, s1, s2, 2)
        rows.append(_stats_row(np.where(n >= minn, t, np.nan)))
    return _pack(rows, {"shifts": ks, "n_cells": len(lab)})


def _centred_gross(cs: CellSpace):
    """(Zg, C, fin, n_obs): gross series centred per cell (0 where invalid), costs, validity."""
    X = net_matrix(cs, "BASE")
    fin = np.isfinite(X)
    C = np.where(fin, cs.pairs.cost["BASE"][:, cs.pidx], 0.0)
    Xg = np.where(fin, X, 0.0) + C
    n_obs = fin.sum(0)
    mu = Xg.sum(0) / np.maximum(n_obs, 1)
    Zg = np.where(fin, Xg - mu[None, :], 0.0)
    return Zg, C, fin, n_obs


def _tstats(N, S1, S2, ok):
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = S1 / N
        var = (S2 - N * mean * mean) / (N - 1.0)
        t = mean / np.sqrt(np.where(var > 1e-18, var, np.nan) / N)
    return np.where(ok, t, np.nan)


def day_bootstrap_null(
    cs: CellSpace, *, n_rep: int = 200, seed: int = 0, min_n: int = 40, min_n_dow: int = 20,
    block: int = 50,
) -> dict:
    D = cs.pairs.G.shape[0]
    minn = _minn(cs, np.arange(cs.n_cells), min_n, min_n_dow)
    Zg, C, fin, n_obs = _centred_gross(cs)
    Z = Zg - C  # centred gross, costs paid (0 where invalid: Zg and C are 0 there)
    Z2, V = Z * Z, fin.astype(float)
    cell_ok = n_obs >= minn
    alldays = ~(cs.meta["dow"].to_numpy() >= 0)
    rng = np.random.default_rng(seed)
    rows: list[tuple] = []
    done = 0
    while done < n_rep:
        m = min(block, n_rep - done)
        W = np.zeros((m, D))
        np.add.at(W, (np.repeat(np.arange(m), D), rng.integers(0, D, size=(m, D)).ravel()), 1.0)
        N, S1, S2 = W @ V, W @ Z, W @ Z2
        t = _tstats(N, S1, S2, cell_ok[None, :] & (minn[None, :] <= N))
        rows.extend(_stats_row(t[i], alldays) for i in range(m))
        done += m
    return _pack(rows, {"n_cells": int(cell_ok.sum())})


def day_signflip_null(
    cs: CellSpace, *, n_rep: int = 200, seed: int = 0, min_n: int = 40, min_n_dow: int = 20,
    block: int = 50,
) -> dict:
    D = cs.pairs.G.shape[0]
    minn = _minn(cs, np.arange(cs.n_cells), min_n, min_n_dow)
    Zg, C, _fin, n_obs = _centred_gross(cs)
    cell_ok = n_obs >= minn
    alldays = ~(cs.meta["dow"].to_numpy() >= 0)
    sumC, sumC2, sumZ2 = C.sum(0), (C * C).sum(0), (Zg * Zg).sum(0)
    ZC = Zg * C
    N = n_obs.astype(float)[None, :]
    rng = np.random.default_rng(seed)
    rows: list[tuple] = []
    done = 0
    while done < n_rep:
        m = min(block, n_rep - done)
        sg = rng.choice([-1.0, 1.0], size=(m, D))
        S1 = sg @ Zg - sumC[None, :]
        S2 = sumZ2[None, :] + sumC2[None, :] - 2.0 * (sg @ ZC)
        t = _tstats(N, S1, S2, cell_ok[None, :])
        rows.extend(_stats_row(t[i], alldays) for i in range(m))
        done += m
    return _pack(rows, {"n_cells": int(cell_ok.sum())})


def null_summary(null: dict, observed_max_t: float, observed_max_abs_t: float,
                 observed_n_gt2: int, observed_n_gt3: int,
                 observed_max_t_alldays: float | None = None) -> dict:
    """Percentiles of the null and where the observation falls (share of replicates >= observed)."""
    def tail(a, x):
        a = np.asarray(a, dtype=float)
        a = a[np.isfinite(a)]
        return float((a >= x).mean()) if len(a) and np.isfinite(x) else float("nan")

    out = {
        "n_rep": len(null["max_t"]), "n_cells": null.get("n_cells"),
        "max_t": {**percentiles(null["max_t"]), "observed": observed_max_t,
                  "share_null_ge_observed": tail(null["max_t"], observed_max_t)},
        "max_abs_t": {**percentiles(null["max_abs_t"]), "observed": observed_max_abs_t,
                      "share_null_ge_observed": tail(null["max_abs_t"], observed_max_abs_t)},
        "n_cells_t_gt2": {"null_mean": float(np.mean(null["n_gt2"])),
                          **percentiles(null["n_gt2"], (95, 99)), "observed": observed_n_gt2},
        "n_cells_t_gt3": {"null_mean": float(np.mean(null["n_gt3"])),
                          **percentiles(null["n_gt3"], (95, 99)), "observed": observed_n_gt3},
    }
    if observed_max_t_alldays is not None:
        out["max_t_alldays"] = {
            **percentiles(null["max_t_alldays"]), "observed": observed_max_t_alldays,
            "share_null_ge_observed": tail(null["max_t_alldays"], observed_max_t_alldays),
        }
    return out
