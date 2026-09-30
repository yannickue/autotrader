# ruff: noqa: E501
"""Monte-Carlo capital-growth engine: resample an R stream, compound with a risk schedule.

It SCALES a given R stream; it never creates edge. Costs are already inside R. Vectorised over
paths (numpy only); deterministic per seed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from alpha.growth.analytics import growth_rate, kelly_fraction
from alpha.growth.schedules import Schedule
from alpha.growth.sizing import LotSizing
from alpha.growth.stream import RStream

SCHEMES = ("iid_trade", "day_block", "stationary_block")
POLICIES = ("ideal", "skip", "forced_min_lot")
PCTS = (5, 25, 50, 75, 95)


@dataclass(frozen=True)
class SimConfig:
    scheme: str = "day_block"
    policy: str = "ideal"  # ideal = continuous sizing; skip / forced_min_lot need ``lots``
    horizons: tuple[int, ...] = (60, 120, 250)
    n_paths: int = 4000
    seed: int = 1
    start_equity: float = 500.0
    targets: tuple[float, ...] = (1000.0, 2500.0, 5000.0)
    edge_uncertainty: bool = False  # draw the TRUE mean R from N(mean, SE) per path
    lots: LotSizing | None = None
    leverage_cap: float = 10.0  # only used by ``ideal`` (needs stop_pts + entry_price)
    ruin_frac: float = 0.20  # ruin = equity below this fraction of start (path then frozen)
    block_len: float = 10.0  # mean block length (days) of the stationary bootstrap
    max_trades_per_day: int | None = None  # daily cap on the stream's trades/day


@dataclass
class SimResult:
    cfg: SimConfig
    schedule: str
    stream: str
    equity: np.ndarray  # (n_h, P) equity at each horizon
    maxdd: np.ndarray  # (n_h, P)
    ruined: np.ndarray  # (n_h, P) bool
    streak: np.ndarray  # (n_h, P) longest loss streak so far
    hit_day: np.ndarray  # (n_targets, P) first day equity >= target (0 = never)
    n_exec: np.ndarray  # (P,)
    n_skip_min: np.ndarray  # (P,) skipped: minimum lot would risk more than the target (skip)
    n_skip_lev: np.ndarray  # (P,) skipped: leverage cap
    realised: dict[str, float] = field(default_factory=dict)  # realised-risk statistics
    edge_shift: np.ndarray | None = None


def _draw_index(stream: RStream, cfg: SimConfig, rng: np.random.Generator,
                horizon: int) -> np.ndarray:
    """(P, H, K) int32 trade indices, -1 = no trade."""
    p = cfg.n_paths
    mat, k = stream.day_structure(cfg.max_trades_per_day)
    d = len(mat)
    if cfg.scheme == "day_block":
        pos = rng.integers(0, d, size=(p, horizon))
        return mat[pos]
    if cfg.scheme == "stationary_block":
        pos = np.empty((p, horizon), dtype=np.int64)
        pos[:, 0] = rng.integers(0, d, size=p)
        restart = rng.random((p, horizon)) < 1.0 / max(cfg.block_len, 1.0)
        fresh = rng.integers(0, d, size=(p, horizon))
        for h in range(1, horizon):
            pos[:, h] = np.where(restart[:, h], fresh[:, h], (pos[:, h - 1] + 1) % d)
        return mat[pos]
    if cfg.scheme == "iid_trade":
        counts = (mat >= 0).sum(axis=1)  # trades/day incl. zero-trade days, daily cap applied
        n = counts[rng.integers(0, d, size=(p, horizon))]
        idx = rng.integers(0, stream.n_trades, size=(p, horizon, k)).astype(np.int32)
        return np.where(np.arange(k)[None, None, :] < n[:, :, None], idx, -1).astype(np.int32)
    raise ValueError(f"unknown scheme {cfg.scheme!r}")


def simulate(stream: RStream, schedule: Schedule, cfg: SimConfig | None = None) -> SimResult:
    cfg = cfg or SimConfig()
    if cfg.policy not in POLICIES:
        raise ValueError(f"unknown policy {cfg.policy!r}")
    if cfg.policy != "ideal" and cfg.lots is None:
        raise ValueError("policy skip/forced_min_lot needs cfg.lots")
    if cfg.policy != "ideal" and stream.stop_pts is None:
        raise ValueError("lot policies need stream.stop_pts (stop distance in price units)")
    if not 0.0 < cfg.leverage_cap <= 30.0:
        raise ValueError("leverage_cap must be in (0, 30]")
    rng = np.random.default_rng(cfg.seed)
    hz = tuple(sorted(cfg.horizons))
    horizon = hz[-1]
    p = cfg.n_paths
    ti = _draw_index(stream, cfg, rng, horizon)
    r_all = stream.r
    mean, se = stream.mean_se()
    shift = None
    if cfg.edge_uncertainty:
        shift = rng.normal(mean, se, size=p) - mean  # posterior draw of the TRUE mean R

    st = schedule.init(p, r_all)
    eq = np.full(p, float(cfg.start_equity))
    peak = eq.copy()
    maxdd = np.zeros(p)
    cur_streak = np.zeros(p)
    max_streak = np.zeros(p)
    frozen = np.zeros(p, dtype=bool)
    hit = np.zeros((len(cfg.targets), p), dtype=np.int32)
    n_exec = np.zeros(p)
    n_skip_min = np.zeros(p)
    n_skip_lev = np.zeros(p)
    rf_sum = ratio_sum = rf_max = 0.0
    n_rf = n_infl = 0
    ruin_level = cfg.ruin_frac * cfg.start_equity
    snaps_eq, snaps_dd, snaps_ru, snaps_st = [], [], [], []
    lots = cfg.lots
    can_lev = stream.stop_pts is not None and stream.entry_price is not None

    for d in range(horizon):
        for k in range(ti.shape[2]):
            idx = ti[:, d, k]
            has = (idx >= 0) & ~frozen & (eq > 0)
            if not has.any():
                continue
            ii = np.where(idx >= 0, idx, 0)
            r = r_all[ii] + (0.0 if shift is None else shift)
            f = schedule.fraction(eq, peak, st)
            risk_t = f * eq
            if cfg.policy == "ideal":
                if can_lev:  # implied leverage = risk / (stop/price) / equity <= cap
                    f_cap = cfg.leverage_cap * stream.stop_pts[ii] / stream.entry_price[ii]
                    risk_t = np.minimum(risk_t, f_cap * eq)
                risk = risk_t
                ok = has
            else:
                assert lots is not None
                stop = stream.stop_pts[ii]
                per_lot = np.maximum(stop * lots.contract_eur_per_pt, 1e-12)
                q = np.floor(risk_t / per_lot / lots.lot_step + 1e-9) * lots.lot_step
                below = q < lots.min_lot - 1e-12
                if cfg.policy == "forced_min_lot":
                    q = np.maximum(q, lots.min_lot)
                    skip_min = np.zeros(p, dtype=bool)
                else:
                    skip_min = has & below
                q_lev = np.floor(cfg.lots.leverage_cap * eq / lots.notional_eur_per_lot
                                 / lots.lot_step + 1e-9) * lots.lot_step
                q = np.minimum(q, q_lev)
                skip_lev = has & ~skip_min & (q < lots.min_lot - 1e-12)
                ok = has & ~skip_min & ~skip_lev
                risk = q * per_lot
                n_skip_min += skip_min
                n_skip_lev += skip_lev
            pnl = np.where(ok, r * risk, 0.0)
            if ok.any():
                rf = risk[ok] / eq[ok]
                rf_sum += float(rf.sum())
                ratio_sum += float((rf / np.maximum(f[ok], 1e-12)).sum())
                rf_max = max(rf_max, float(rf.max()))
                n_infl += int((rf > 2.0 * f[ok]).sum())
                n_rf += int(ok.sum())
            eq = np.maximum(eq + pnl, 0.0)
            n_exec += ok
            peak = np.maximum(peak, eq)
            maxdd = np.maximum(maxdd, 1.0 - eq / peak)
            cur_streak = np.where(ok, np.where(r < 0, cur_streak + 1.0, 0.0), cur_streak)
            max_streak = np.maximum(max_streak, cur_streak)
            frozen |= eq < ruin_level
            schedule.update(st, r, ok, eq, peak)
        for ti_, tg in enumerate(cfg.targets):
            newly = (eq >= tg) & (hit[ti_] == 0)
            hit[ti_] = np.where(newly, d + 1, hit[ti_])
        if (d + 1) in hz:
            snaps_eq.append(eq.copy())
            snaps_dd.append(maxdd.copy())
            snaps_ru.append(eq < ruin_level)
            snaps_st.append(max_streak.copy())

    realised = {
        "mean_realised_risk_frac": rf_sum / n_rf if n_rf else float("nan"),
        "mean_realised_over_target": ratio_sum / n_rf if n_rf else float("nan"),
        "max_realised_risk_frac": rf_max,
        "frac_trades_risk_gt_2x_target": n_infl / n_rf if n_rf else float("nan"),
    }
    return SimResult(
        cfg=cfg, schedule=schedule.name, stream=stream.label,
        equity=np.array(snaps_eq), maxdd=np.array(snaps_dd), ruined=np.array(snaps_ru),
        streak=np.array(snaps_st), hit_day=hit, n_exec=n_exec, n_skip_min=n_skip_min,
        n_skip_lev=n_skip_lev, realised=realised, edge_shift=shift,
    )


def summarize(res: SimResult) -> dict:
    """Compact JSON-able summary per horizon."""
    cfg = res.cfg
    hz = tuple(sorted(cfg.horizons))
    out: dict = {"stream": res.stream, "schedule": res.schedule, "scheme": cfg.scheme,
                 "policy": cfg.policy, "edge_uncertainty": cfg.edge_uncertainty,
                 "start_equity": cfg.start_equity, "n_paths": cfg.n_paths, "horizons": {}}
    for i, h in enumerate(hz):
        e = res.equity[i]
        pct = np.percentile(e, PCTS)
        hd = {
            "ending_capital": {f"p{q}": round(float(v), 2) for q, v in zip(PCTS, pct, strict=True)},
            "p_ruin": float((res.ruined[i]).mean()),
            "p_dd_ge_25": float((res.maxdd[i] >= 0.25).mean()),
            "p_dd_ge_50": float((res.maxdd[i] >= 0.50).mean()),
            "maxdd_p5_p50_p95": [round(float(v), 4) for v in np.percentile(res.maxdd[i], (5, 50, 95))],
            "loss_streak_p50_p95_max": [float(np.percentile(res.streak[i], 50)),
                                        float(np.percentile(res.streak[i], 95)),
                                        float(res.streak[i].max())],
            "p_reach": {}, "median_days_to_target": {},
        }
        for ti_, tg in enumerate(cfg.targets):
            hit = res.hit_day[ti_]
            reached = (hit > 0) & (hit <= h)
            hd["p_reach"][f"{tg:g}"] = float(reached.mean())
            hd["median_days_to_target"][f"{tg:g}"] = (
                float(np.median(hit[reached])) if reached.any() else None
            )
        out["horizons"][str(h)] = hd
    tot = cfg.n_paths
    out["mean_trades_executed"] = float(res.n_exec.mean())
    out["skip_fraction_min_lot"] = float(res.n_skip_min.sum() / max(res.n_skip_min.sum() + res.n_skip_lev.sum() + res.n_exec.sum(), 1) )
    out["skip_fraction_leverage"] = float(res.n_skip_lev.sum() / max(res.n_skip_min.sum() + res.n_skip_lev.sum() + res.n_exec.sum(), 1))
    out["realised_risk"] = {k: (None if v != v else round(v, 5)) for k, v in res.realised.items()}
    out["paths"] = tot
    return out


def analytic_block(stream: RStream, fractions: tuple[float, ...]) -> dict:
    """Per-trade log-growth g(f), Kelly f*, and the too-aggressive warning."""
    fs = kelly_fraction(stream.r)
    mean, se = stream.mean_se()
    return {
        "mean_r": mean, "se_mean_r": se, "mean_over_se": (mean / se) if se > 0 else None,
        "f_star": fs,
        "g_at_f_star": float(growth_rate(stream.r, fs)) if fs > 0 else 0.0,
        "g_per_trade": {f"{f:g}": float(growth_rate(stream.r, f)) for f in fractions},
        "warning": "Full Kelly is too aggressive under estimation error; f* is an upper bound "
                   "on a sane fraction, computed on an ESTIMATED edge. Use a fraction of it.",
    }
