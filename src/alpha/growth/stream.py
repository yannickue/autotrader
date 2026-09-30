"""Empirical R-multiple streams (per-trade R, day labels, optional stop distance / price)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

_R_KEYS = ("r_multiple", "r", "R")
_DAY_KEYS = ("entry_day", "day", "date")
_STOP_KEYS = ("risk_pts", "stop_pts", "stop")
_PRICE_KEYS = ("entry_price", "price")


@dataclass(frozen=True)
class RStream:
    """Chronological trades. ``r`` already includes all modelled costs (net R)."""

    r: np.ndarray
    day: np.ndarray  # integer day label per trade (same label = same trading day)
    stop_pts: np.ndarray | None = None  # stop distance in price units (for lot feasibility)
    entry_price: np.ndarray | None = None  # for implied leverage of continuous sizing
    total_days: int | None = None  # trading days in the sample INCLUDING days without trades
    label: str = "stream"

    def __post_init__(self) -> None:
        n = len(self.r)
        if n == 0:
            raise ValueError("empty stream")
        if len(self.day) != n:
            raise ValueError("day length mismatch")
        for name in ("stop_pts", "entry_price"):
            arr = getattr(self, name)
            if arr is not None and len(arr) != n:
                raise ValueError(f"{name} length mismatch")
        if not np.all(np.isfinite(self.r)):
            raise ValueError("non-finite R")

    @property
    def n_trades(self) -> int:
        return len(self.r)

    def day_structure(self, cap: int | None = None) -> tuple[np.ndarray, int]:
        """(D, K) int32 matrix of trade indices per day (-1 padded), zero-trade days appended."""
        order = np.argsort(self.day, kind="stable")
        d_sorted = self.day[order]
        uniq, starts, counts = np.unique(d_sorted, return_index=True, return_counts=True)
        k = int(counts.max())
        if cap is not None:
            k = min(k, int(cap))
        extra = 0
        if self.total_days is not None:
            extra = max(int(self.total_days) - len(uniq), 0)
        mat = np.full((len(uniq) + extra, k), -1, dtype=np.int32)
        for i, (s, c) in enumerate(zip(starts, counts, strict=True)):
            m = min(int(c), k)
            mat[i, :m] = order[s : s + m]
        return mat, k

    def mean_se(self) -> tuple[float, float]:
        """Mean R and a day-cluster-robust standard error (>= the iid SE)."""
        r = self.r
        mean = float(r.mean())
        se_iid = float(r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 1 else 0.0
        _, inv = np.unique(self.day, return_inverse=True)
        sums = np.bincount(inv, weights=r - mean)
        nd = len(sums)
        se_cl = float(np.sqrt((sums**2).sum() * nd / max(nd - 1, 1)) / len(r)) if nd > 1 else se_iid
        return mean, max(se_iid, se_cl)


def synthetic_stream(
    n_trades: int = 6000,
    *,
    mean_r: float = 0.15,
    win_rate: float = 0.40,
    trades_per_day: float = 1.2,
    loss_r: float = 1.0,
    seed: int = 7,
    label: str | None = None,
    cluster: float = 0.0,
    stop_pts: float | None = None,
) -> RStream:
    """Two-point stream: losses of -``loss_r``, wins sized so E[R] == ``mean_r`` exactly.

    ``cluster`` in [0, 1) plants regime persistence (alternating good/bad ~10-day regimes with
    shifted win probability), i.e. loss-streak clustering. ``stop_pts`` adds a per-trade stop
    distance (lognormal around it) so lot feasibility can be simulated.
    """
    rng = np.random.default_rng(seed)
    win_r = (mean_r + (1.0 - win_rate) * loss_r) / win_rate
    n_days = max(int(np.ceil(n_trades / trades_per_day)), 1)
    day = np.sort(rng.integers(0, n_days, size=n_trades)).astype(np.int64)
    p = np.full(n_trades, win_rate)
    if cluster > 0.0:
        reg = np.sign(np.sin(day / 10.0 * np.pi) + 1e-9)
        p = np.clip(win_rate + cluster * 0.4 * reg, 0.02, 0.98)
    wins = rng.random(n_trades) < p
    r = np.where(wins, win_r, -loss_r).astype(float)
    r += mean_r - r.mean()  # exact mean (tiny shift, keeps the two-point shape)
    stops = None
    if stop_pts is not None:
        stops = stop_pts * np.exp(rng.normal(0.0, 0.35, size=n_trades))
    return RStream(r=r, day=day, stop_pts=stops, total_days=n_days,
                   label=label or f"synthetic_mean{mean_r:+.2f}")


def _pick(cols: dict[str, np.ndarray], keys: tuple[str, ...]) -> np.ndarray | None:
    for k in keys:
        if k in cols:
            return np.asarray(cols[k])
    return None


def load_stream(path: str | Path, *, label: str | None = None,
                total_days: int | None = None) -> RStream:
    """Load a stream from .npz / .csv / .parquet (column aliases: r_multiple|r, entry_day|day)."""
    p = Path(path)
    if p.suffix == ".npz":
        with np.load(p, allow_pickle=False) as z:
            cols = {k: z[k] for k in z.files}
    elif p.suffix in (".csv", ".parquet"):
        import pandas as pd

        df = pd.read_csv(p) if p.suffix == ".csv" else pd.read_parquet(p)
        cols = {c: df[c].to_numpy() for c in df.columns}
    else:
        raise ValueError(f"unsupported stream format {p.suffix}")
    r = _pick(cols, _R_KEYS)
    day = _pick(cols, _DAY_KEYS)
    if r is None or day is None:
        raise ValueError(f"{p}: need an R column {_R_KEYS} and a day column {_DAY_KEYS}")
    if day.dtype.kind not in "iu":
        if day.dtype.kind in "OUS":
            day = np.unique(day.astype(str), return_inverse=True)[1]
        else:
            day = day.astype("datetime64[D]").astype(np.int64)
    stop = _pick(cols, _STOP_KEYS)
    price = _pick(cols, _PRICE_KEYS)
    return RStream(
        r=np.asarray(r, dtype=float), day=np.asarray(day, dtype=np.int64),
        stop_pts=None if stop is None else np.asarray(stop, dtype=float),
        entry_price=None if price is None else np.asarray(price, dtype=float),
        total_days=total_days, label=label or p.stem,
    )
