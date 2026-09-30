"""Closed-form-ish growth analytics on an empirical R stream."""

from __future__ import annotations

import numpy as np


def growth_rate(r: np.ndarray, f: float | np.ndarray) -> np.ndarray | float:
    """g(f) = E[log(1 + f R)] per trade (-inf where any outcome would bust the account)."""
    r = np.asarray(r, dtype=float)
    fs = np.atleast_1d(np.asarray(f, dtype=float))
    out = np.empty(len(fs))
    for i, fi in enumerate(fs):
        x = 1.0 + fi * r
        out[i] = -np.inf if x.min() <= 0.0 else float(np.log(x).mean())
    return out if np.ndim(f) else float(out[0])


def f_bust(r: np.ndarray) -> float:
    """Largest f with 1 + f*min(R) > 0 (beyond it a single loss wipes out equity)."""
    m = float(np.min(r))
    return np.inf if m >= 0 else -1.0 / m


def kelly_fraction(r: np.ndarray, *, f_max: float = 1.0) -> float:
    """f* = argmax g(f) on (0, min(f_max, f_bust)); 0.0 when E[R] <= 0 (no positive-growth f).

    FULL KELLY IS TOO AGGRESSIVE under estimation error: overestimating the edge by 2x already
    drives growth to <= 0. Use a fraction of f* (and a shrunk edge), never f* itself.
    """
    r = np.asarray(r, dtype=float)
    if r.mean() <= 0.0:
        return 0.0
    hi = min(f_max, f_bust(r) * (1.0 - 1e-9))
    grid = np.linspace(0.0, hi, 2001)[1:]
    g = np.asarray(growth_rate(r, grid))
    i = int(np.argmax(g))
    a, b = grid[max(i - 1, 0)], grid[min(i + 1, len(grid) - 1)]
    inv = (np.sqrt(5.0) - 1.0) / 2.0  # golden-section refinement
    c, d = b - inv * (b - a), a + inv * (b - a)
    for _ in range(60):
        if growth_rate(r, c) > growth_rate(r, d):
            b = d
        else:
            a = c
        c, d = b - inv * (b - a), a + inv * (b - a)
    return float(0.5 * (a + b))


def required_fraction(
    r: np.ndarray, *, trades_per_day: float, days: int, start: float, target: float
) -> dict[str, float | None]:
    """Smallest risk fraction whose MEDIAN growth reaches ``target`` in ``days`` (log-growth g*n).

    ``f_needed`` is None when even f* cannot: the target is unreachable in the median for this
    edge. Pure arithmetic on the stream; it says nothing about survival: run the MC at f_needed
    to see P(ruin)/P(drawdown). No edge => no f works.
    """
    r = np.asarray(r, dtype=float)
    n = max(trades_per_day * days, 1e-12)
    need = float(np.log(target / start) / n)  # per-trade log growth required
    fs = kelly_fraction(r)
    gmax = float(growth_rate(r, fs)) if fs > 0 else 0.0
    out: dict[str, float | None] = {
        "g_needed_per_trade": need, "f_star": fs, "g_at_f_star": gmax, "f_needed": None,
    }
    if fs <= 0.0 or gmax < need:
        return out
    lo, hi = 0.0, fs
    for _ in range(80):  # g is increasing on (0, f*]
        mid = 0.5 * (lo + hi)
        if growth_rate(r, mid) >= need:
            hi = mid
        else:
            lo = mid
    out["f_needed"] = hi
    return out
