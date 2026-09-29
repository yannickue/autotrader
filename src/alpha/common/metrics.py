"""Standardized research metrics, breakdown tables and seeded bootstrap inference."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from alpha.common.sim import DEFAULT_SIZING, SizingSpec

QUANTILES = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)


def _f(x: float) -> float | None:
    x = float(x)
    return None if (math.isnan(x) or math.isinf(x)) else round(x, 6)


def _max_consecutive_losses(pnl: np.ndarray) -> int:
    best = cur = 0
    for v in pnl:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def bootstrap_mean_ci(
    x: np.ndarray, *, seed: int, n_boot: int = 2000, alpha: float = 0.05
) -> tuple[float | None, float | None]:
    """Seeded percentile bootstrap CI of the mean (i.i.d. resampling of trades)."""
    if len(x) < 5:
        return None, None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(n_boot, len(x)))
    means = x[idx].mean(axis=1)
    return _f(np.quantile(means, alpha / 2)), _f(np.quantile(means, 1 - alpha / 2))


def bootstrap_day_clustered_mean_ci(
    trades: pd.DataFrame, *, seed: int, n_boot: int = 2000, alpha: float = 0.05
) -> tuple[float | None, float | None]:
    """Seeded CI resampling Berlin entry dates, then pooling their trades."""
    dates = trades["date"].drop_duplicates().to_numpy()
    if len(dates) < 2 or len(trades) < 5:
        return None, None
    clusters = {
        date: trades.loc[trades["date"] == date, "r_multiple"].to_numpy(float) for date in dates
    }
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for index in range(n_boot):
        drawn = rng.choice(dates, size=len(dates), replace=True)
        means[index] = np.concatenate([clusters[date] for date in drawn]).mean()
    return _f(np.quantile(means, alpha / 2)), _f(np.quantile(means, 1 - alpha / 2))


def compute_metrics(
    trades: pd.DataFrame,
    *,
    trading_days: np.ndarray,
    window_bars: int,
    sizing: SizingSpec = DEFAULT_SIZING,
    seed: int = 0,
    bar_minutes: int = 5,
) -> dict:
    """All required metrics for one strategy/version/partition/cost scenario.

    `trading_days`: array of Berlin dates on which the entry window existed (zero-trade days
    are counted against it). `window_bars`: number of bars inside the entry..flat window.
    """
    n_days = len(trading_days)
    m: dict = {"trades": len(trades), "trading_days": int(n_days)}
    if len(trades) == 0:
        m.update(net_pnl_eur=0.0, zero_trade_days=int(n_days), trades_per_day=0.0)
        return m
    pnl = trades["pnl_eur"].to_numpy(float)
    r = trades["r_multiple"].to_numpy(float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    order = trades.sort_values("entry_idx")
    cum = np.cumsum(order["pnl_eur"].to_numpy(float))
    equity = sizing.equity_eur + np.concatenate([[0.0], cum])
    peak = np.maximum.accumulate(equity)
    dd = peak - equity
    dd_pct = dd / peak
    daily = trades.groupby("date")["pnl_eur"].sum()
    daily_full = daily.reindex(pd.Index(trading_days), fill_value=0.0)
    per_day_counts = trades.groupby("date").size().reindex(pd.Index(trading_days), fill_value=0)
    trade_ci = bootstrap_mean_ci(r, seed=seed)
    day_ci = bootstrap_day_clustered_mean_ci(trades, seed=seed)
    se = r.std(ddof=1) / math.sqrt(len(r)) if len(r) > 1 else float("nan")
    m.update(
        net_pnl_eur=_f(pnl.sum()),
        gross_pnl_eur=_f(trades["gross_pnl_eur"].sum()),
        expectancy_eur=_f(pnl.mean()),
        expectancy_r=_f(r.mean()),
        expectancy_r_ci95_trade_iid=list(trade_ci),
        expectancy_r_ci95_day_clustered=list(day_ci),
        expectancy_r_tstat=_f(r.mean() / se) if se and not math.isnan(se) and se > 0 else None,
        profit_factor=_f(gross_win / gross_loss) if gross_loss > 0 else None,
        win_rate=_f((pnl > 0).mean()),
        avg_winner_eur=_f(wins.mean()) if len(wins) else None,
        avg_loser_eur=_f(losses.mean()) if len(losses) else None,
        payoff_ratio=_f(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
        avg_winner_r=_f(r[r > 0].mean()) if (r > 0).any() else None,
        avg_loser_r=_f(r[r < 0].mean()) if (r < 0).any() else None,
        max_drawdown_eur=_f(dd.max()),
        max_drawdown_pct=_f(dd_pct.max() * 100.0),
        max_consecutive_losses=_max_consecutive_losses(order["pnl_eur"].to_numpy(float)),
        worst_day_eur=_f(daily_full.min()),
        best_day_eur=_f(daily_full.max()),
        trades_per_day=_f(len(trades) / n_days) if n_days else None,
        median_trades_per_day=_f(per_day_counts.median()) if n_days else None,
        max_trades_in_a_day=int(per_day_counts.max()) if n_days else 0,
        zero_trade_days=int((per_day_counts == 0).sum()),
        exposure_time_frac=(
            _f(trades["holding_minutes"].sum() / (window_bars * bar_minutes))
            if window_bars
            else None
        ),
        avg_holding_minutes=_f(trades["holding_minutes"].mean()),
        avg_leverage=_f(trades["leverage"].mean()),
        max_leverage=_f(trades["leverage"].max()),
        leverage_capped_trades=int(trades["leverage_capped"].sum()),
        avg_entry_spread_pts=_f(trades["entry_spread_pts"].mean()),
        avg_spread_cost_pts=_f(trades["spread_cost_pts"].mean()),
        avg_slippage_cost_pts=_f(trades["slippage_cost_pts"].mean()),
        est_execution_cost_eur=_f(trades["cost_eur"].sum()),
        cost_per_trade_eur=_f(trades["cost_eur"].mean()),
        cost_share_of_gross=(
            _f(trades["cost_eur"].sum() / abs(trades["gross_pnl_eur"].sum()))
            if trades["gross_pnl_eur"].sum() != 0
            else None
        ),
        crossed_rollover_trades=int(trades["crossed_rollover"].sum()),
        # distribution level
        r_quantiles={f"q{int(q * 100):02d}": _f(np.quantile(r, q)) for q in QUANTILES},
        r_std=_f(r.std(ddof=1)) if len(r) > 1 else None,
        r_skew=_f(pd.Series(r).skew()) if len(r) > 2 else None,
        daily_pnl_quantiles={
            f"q{int(q * 100):02d}": _f(np.quantile(daily_full.to_numpy(), q)) for q in QUANTILES
        },
        exit_reasons={k: int(v) for k, v in trades["exit_reason"].value_counts().items()},
        mfe_r_median=_f(trades["mfe_r"].median()),
        mae_r_median=_f(trades["mae_r"].median()),
        mfe_r_quantiles={
            f"q{int(q * 100):02d}": _f(np.quantile(trades["mfe_r"], q))
            for q in (0.25, 0.5, 0.75, 0.9)
        },
        mae_r_quantiles={
            f"q{int(q * 100):02d}": _f(np.quantile(trades["mae_r"], q))
            for q in (0.25, 0.5, 0.75, 0.9)
        },
        long_share=_f((trades["side"] > 0).mean()),
    )
    return m


def breakdown(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    """Per-group trade count, expectancy (R), win rate, profit factor, net PnL, standard error."""
    rows = []
    for key, g in trades.groupby(by, dropna=False):
        r = g["r_multiple"].to_numpy(float)
        p = g["pnl_eur"].to_numpy(float)
        gl = -p[p < 0].sum()
        rows.append(
            {
                by: key,
                "n": len(g),
                "expectancy_r": _f(r.mean()),
                "se_r": _f(r.std(ddof=1) / math.sqrt(len(r))) if len(r) > 1 else None,
                "win_rate": _f((p > 0).mean()),
                "profit_factor": _f(p[p > 0].sum() / gl) if gl > 0 else None,
                "net_pnl_eur": _f(p.sum()),
            }
        )
    return pd.DataFrame(rows)


def material_difference(table: pd.DataFrame, *, min_n: int = 30) -> dict:
    """Flag whether the best and worst group expectancy differ by more than 2 combined SE."""
    t = table[table["n"] >= min_n].dropna(subset=["expectancy_r", "se_r"])
    if len(t) < 2:
        return {"material": None, "reason": "fewer than two groups with enough trades"}
    hi = t.loc[t["expectancy_r"].idxmax()]
    lo = t.loc[t["expectancy_r"].idxmin()]
    se = math.sqrt(hi["se_r"] ** 2 + lo["se_r"] ** 2)
    diff = hi["expectancy_r"] - lo["expectancy_r"]
    return {
        "material": bool(diff > 2 * se),
        "best": [str(hi.iloc[0]), float(hi["expectancy_r"]), int(hi["n"])],
        "worst": [str(lo.iloc[0]), float(lo["expectancy_r"]), int(lo["n"])],
        "diff_r": round(float(diff), 4),
        "z": round(float(diff / se), 2) if se > 0 else None,
        "note": "descriptive only: many groups are compared, no multiplicity correction",
    }
