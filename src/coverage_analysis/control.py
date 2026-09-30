# ruff: noqa: E501
"""False-positive COUNTERFACTUAL CONTROL: does "trigger -> comparable move" beat what random bars do?

``follows[d][j]`` = after the CLOSE of bar ``j`` a move in direction ``d`` of ``>= n_atr * ATR[j]`` (measured from
``close[j]``) is reached within ``m_bars`` bars before the price first goes ``max_adverse_atr * ATR[j]`` the
other way (same bar: adverse first, pessimistic). Bars whose look-ahead window is not fully inside the data are
excluded from every rate (no censoring bias).

For a set of triggers (signals / near-misses) the hit rate is compared with
* ``base_all``       exact mean of ``follows`` over ALL eligible bars (in-entry-window, finite ATR), weighted by
                     the triggers' direction mix;
* ``uniform_sample`` seeded random eligible bars, the triggers' directions resampled, ``K`` per trigger;
* ``matched_sample`` seeded random eligible bars of the SAME local hour and direction as each trigger;
* ``shifted``        the trigger bars moved by +-1 / +-2 trading days (same direction, only if that bar is eligible).
Caveat printed with every table: neighbouring bars are strongly autocorrelated, so the Wilson intervals are
optimistic (too narrow); n < 30 triggers is reported as "no conclusion".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from alpha.families.common import entry_mask
from alpha.families.data import FamilyData
from coverage_analysis.moves import MoveParams

MIN_N = 30
DAY_BARS = 288
NOISE, STRONG, ADVERSE, NA = 0, 1, 2, -1


def outcome_arrays(data: FamilyData, p: MoveParams) -> tuple[dict[int, np.ndarray], np.ndarray]:
    """(``{+1: int8[n], -1: int8[n]}``, ``avail``): outcome of a same-direction entry at the CLOSE of each bar:
    ``STRONG`` (1): the favourable excursion reaches ``n_atr * ATR`` first; ``ADVERSE`` (2): price first goes
    ``max_adverse_atr * ATR`` the wrong way (same bar: adverse first, pessimistic); ``NOISE`` (0): neither within
    ``m_bars``; ``-1``: look-ahead window not fully available / not contiguous (excluded from every rate)."""
    n = len(data)
    m = p.m_bars
    c, h, low, atr, rs = data.c, data.h, data.l, data.atr, data.run_start
    idx = np.arange(n)
    end = np.minimum(idx + m, n - 1)
    avail = (idx + m < n) & np.isfinite(atr) & (atr > 0) & (rs[end] <= idx)
    out: dict[int, np.ndarray] = {}
    for d in (1, -1):
        alive = avail.copy()
        strong = np.zeros(n, dtype=bool)
        adverse = np.zeros(n, dtype=bool)
        for k in range(1, m + 1):
            j = np.minimum(idx + k, n - 1)
            adv = (c - low[j]) if d == 1 else (h[j] - c)
            fav = (h[j] - c) if d == 1 else (c - low[j])
            a_hit = alive & (adv > p.max_adverse_atr * atr)
            adverse |= a_hit
            alive &= ~a_hit
            f_hit = alive & (fav >= p.n_atr * atr)
            strong |= f_hit
            alive &= ~f_hit
        o = np.full(n, NOISE, dtype=np.int8)
        o[adverse] = ADVERSE
        o[strong] = STRONG
        o[~avail] = NA
        out[d] = o
    return out, avail


def follows_arrays(data: FamilyData, p: MoveParams) -> tuple[dict[int, np.ndarray], np.ndarray]:
    """bool view of ``outcome_arrays`` (STRONG)."""
    oc, avail = outcome_arrays(data, p)
    return {d: a == STRONG for d, a in oc.items()}, avail


def eligible_mask(data: FamilyData, specs: Sequence[Any], avail: np.ndarray) -> np.ndarray:
    """Bars where at least one spec's entry window allows an entry (same facts as the generators' ``entry_mask``)."""
    ok = np.zeros(len(data), dtype=bool)
    for fs in specs:
        ok |= entry_mask(data, fs.spec.effective_window(data.cal), "all")
    return ok & avail


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (float("nan"), float("nan"))
    ph = k / n
    den = 1 + z * z / n
    centre = (ph + z * z / (2 * n)) / den
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def _mix(codes: np.ndarray) -> dict[str, Any]:
    n = len(codes)
    cnt = {"strong": int((codes == STRONG).sum()), "adverse": int((codes == ADVERSE).sum()), "noise": int((codes == NOISE).sum())}
    lo, hi = wilson(cnt["strong"], n)
    return {"n": n, **cnt, "strong_share": (cnt["strong"] / n) if n else None, "adverse_share": (cnt["adverse"] / n) if n else None,
            "noise_share": (cnt["noise"] / n) if n else None, "strong_ci95": [lo, hi]}


def control_table(
    data: FamilyData, triggers: Sequence[Any], outcomes: dict[int, np.ndarray], eligible: np.ndarray, *,
    seed: int = 20260930, k_per_trigger: int = 20, shifts: tuple[int, ...] = (DAY_BARS, -DAY_BARS, 2 * DAY_BARS, -2 * DAY_BARS),
) -> dict[str, Any]:
    """Outcome mix (STRONG / ADVERSE / NOISE) of the triggers vs the controls; ``rate`` = STRONG share."""
    trig = [t for t in triggers if 0 <= t.idx < len(data) and eligible[t.idx]]
    res: dict[str, Any] = {"n_triggers_all": len(triggers), "n_triggers_evaluable": len(trig),
                           "n_dropped_no_lookahead_or_window": len(triggers) - len(trig)}
    if not trig:
        res.update(trigger={**_mix(np.zeros(0, dtype=np.int8)), "n": 0, "hits": 0, "rate": None, "ci95": [float("nan")] * 2},
                   base_all=None, uniform_sample=None, matched_sample=None, shifted=None, verdict="no triggers: no conclusion")
        return res
    dirs = np.array([t.direction for t in trig])
    idxs = np.array([t.idx for t in trig])
    tcodes = np.array([outcomes[int(d)][int(j)] for j, d in zip(idxs, dirs, strict=True)], dtype=np.int8)
    res["trigger"] = _with_rate(_mix(tcodes))
    el = np.flatnonzero(eligible)
    frac = {d: float((dirs == d).mean()) for d in (1, -1)}
    base_mix = {k: sum(frac[d] * float((outcomes[d][el] == code).mean()) for d in (1, -1)) for k, code in (("strong", STRONG), ("adverse", ADVERSE), ("noise", NOISE))}
    base = base_mix["strong"]
    res["base_all"] = {"rate": base, "n_bars": len(el), "direction_mix": frac, "adverse_share": base_mix["adverse"], "noise_share": base_mix["noise"]}
    rng = np.random.default_rng(seed)
    kk = k_per_trigger * len(trig)
    sj = rng.choice(el, size=kk, replace=True)
    sd = rng.choice(dirs, size=kk, replace=True)
    res["uniform_sample"] = _with_rate(_mix(np.array([outcomes[int(d)][int(j)] for j, d in zip(sj, sd, strict=True)], dtype=np.int8)))
    hour = data.minute // 60
    by_hour: dict[int, np.ndarray] = {int(hh): el[hour[el] == hh] for hh in np.unique(hour[el])}
    mcodes: list[int] = []
    for j, d in zip(idxs, dirs, strict=True):
        pool = by_hour.get(int(hour[j]))
        if pool is None or len(pool) == 0:
            continue
        mcodes += outcomes[int(d)][rng.choice(pool, size=k_per_trigger, replace=True)].tolist()
    res["matched_sample"] = _with_rate(_mix(np.array(mcodes, dtype=np.int8)))
    scodes: list[int] = []
    for j, d in zip(idxs, dirs, strict=True):
        for s in shifts:
            jj = int(j) + s
            if 0 <= jj < len(data) and eligible[jj]:
                scodes.append(int(outcomes[int(d)][jj]))
    res["shifted"] = _with_rate(_mix(np.array(scodes, dtype=np.int8)))
    lo, hi = res["trigger"]["ci95"]
    if len(trig) < MIN_N:
        res["verdict"] = f"n={len(trig)} < {MIN_N}: too small for any conclusion"
    elif base < lo:
        res["verdict"] = "strong-move share ABOVE the base rate of random bars (95% CI excludes it; CI optimistic, clustered bars)"
    elif base > hi:
        res["verdict"] = "strong-move share BELOW the base rate of random bars"
    else:
        res["verdict"] = "indistinguishable from random bars (base rate inside the trigger 95% CI)"
    return res


def _with_rate(mix: dict[str, Any]) -> dict[str, Any]:
    return {**mix, "hits": mix["strong"], "rate": mix["strong_share"], "ci95": mix["strong_ci95"]}
