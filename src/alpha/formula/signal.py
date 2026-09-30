# ruff: noqa: E501
"""Factor -> trade candidates (``CandidateArrays``) for the V1 ``simulate_fast`` path.

Direction is the sign of the factor's Train-fitted relation with the forward return
(``FactorScore.sign``); the working series is ``g = sign * factor`` so LONG always means "g high".

Entry: LONG at the bar where ``g`` crosses UP through the Train quantile ``hi`` (``g[i] >= hi`` and
``g[i-1] < hi`` inside the run), SHORT at the mirrored down-cross through the ``1-q`` quantile ``lo``.
Thresholds are fitted from the Train bars handed to ``fit_thresholds`` (the caller passes the Train view
only), globally or per session bucket (local hour of the bar open; buckets with fewer than
``MIN_BUCKET_N`` Train values fall back to the global quantile).  One candidate per crossing bar with a
cooldown of ``cooldown`` bars between candidates.  Decision at the bar close, fill at the next open (the
simulator's rule); stop = ``stop_k * ATR`` from the decision close; target = fixed R (``target`` is NaN and
``exit_kind`` is EXIT_FIXED_R) so the V1 fixed-R simulator path is used unchanged.

Causality: ``g[i]``/``g[i-1]``, the thresholds (a frozen Train table) and ``atr[i]`` are all known at the
close of bar ``i``; candidates only additionally require that the NEXT bar exists in the same run and lies
inside the entry window (a calendar fact, not price information).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from alpha.common.frame import ENTRY_END_MIN, ENTRY_START_MIN
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.formula.data import FormulaData

Q_GRID: tuple[float, ...] = (0.8, 0.9, 0.95)
STOP_K_GRID: tuple[float, ...] = (1.0, 1.5, 2.0)
MIN_BUCKET_N = 50
N_BUCKETS = 24


@dataclass(frozen=True)
class SignalSpec:
    sign: int
    q: float = 0.9
    stop_k: float = 1.5
    target_r: float = 1.5
    cooldown: int = 12
    session: bool = False

    def __post_init__(self) -> None:
        if self.sign not in (-1, 1):
            raise ValueError("sign must be +1 or -1")
        if not 0.5 < self.q < 1.0:
            raise ValueError("q must be in (0.5, 1)")
        if self.stop_k <= 0 or self.target_r <= 0 or self.cooldown < 1:
            raise ValueError("stop_k, target_r > 0 and cooldown >= 1 required")


@dataclass(frozen=True)
class Thresholds:
    """Frozen Train-fitted upper/lower thresholds of ``g`` (global + optional per hour bucket)."""

    hi: float
    lo: float
    hi_bucket: tuple[float, ...] | None = None  # N_BUCKETS values, NaN -> use the global one
    lo_bucket: tuple[float, ...] | None = None

    def per_bar(self, minute: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        n = len(minute)
        if self.hi_bucket is None or self.lo_bucket is None:
            return np.full(n, self.hi), np.full(n, self.lo)
        b = np.clip(np.asarray(minute, dtype=np.int64) // 60, 0, N_BUCKETS - 1)
        hb, lb = np.asarray(self.hi_bucket), np.asarray(self.lo_bucket)
        return np.where(np.isfinite(hb[b]), hb[b], self.hi), np.where(np.isfinite(lb[b]), lb[b], self.lo)


def fit_thresholds(g_train: np.ndarray, minute_train: np.ndarray, q: float, session: bool) -> Thresholds:
    """Quantiles of ``g`` over the given (Train) bars only."""
    fin = np.isfinite(g_train)
    if fin.sum() < MIN_BUCKET_N:
        raise ValueError("too few finite Train values to fit thresholds")
    hi = float(np.quantile(g_train[fin], q))
    lo = float(np.quantile(g_train[fin], 1.0 - q))
    if not session:
        return Thresholds(hi, lo)
    bucket = np.clip(np.asarray(minute_train, dtype=np.int64) // 60, 0, N_BUCKETS - 1)
    hb = np.full(N_BUCKETS, np.nan)
    lb = np.full(N_BUCKETS, np.nan)
    for b in range(N_BUCKETS):
        v = g_train[fin & (bucket == b)]
        if len(v) >= MIN_BUCKET_N:
            hb[b], lb[b] = float(np.quantile(v, q)), float(np.quantile(v, 1.0 - q))
    return Thresholds(hi, lo, tuple(hb), tuple(lb))


def _cooldown(idx: np.ndarray, cooldown: int) -> np.ndarray:
    keep = []
    last = -10**12
    for i in idx:
        if i - last >= cooldown:
            keep.append(i)
            last = i
    return np.asarray(keep, dtype=np.int64)


def build_candidates(factor: np.ndarray, data: FormulaData, spec: SignalSpec, thr: Thresholds) -> CandidateArrays:
    """Candidates for ALL bars of ``data`` (the caller restricts the bar range; see ``prepare``)."""
    n = len(data)
    g = spec.sign * np.asarray(factor, dtype=np.float64)
    hi, lo = thr.per_bar(data.minute)
    prev = np.r_[np.nan, g[:-1]]
    same_run = np.arange(n) - 1 >= data.run_start
    with np.errstate(invalid="ignore"):
        up = same_run & (g >= hi) & (prev < hi)
        dn = same_run & (g <= lo) & (prev > lo)
    # entry-bar calendar facts: next bar exists, same run, inside the entry window
    nxt = np.minimum(np.arange(n) + 1, n - 1)
    ok = (np.arange(n) + 1 < n) & (data.run_start[nxt] == data.run_start)
    m_next = np.asarray(data.minute)[nxt]
    ok &= (m_next >= ENTRY_START_MIN) & (m_next < ENTRY_END_MIN)
    ok &= np.isfinite(data.atr) & (data.atr > 0)
    up &= ok
    dn &= ok
    idx = _cooldown(np.flatnonzero(up | dn), spec.cooldown)
    if len(idx) == 0:
        z = np.zeros(0)
        return CandidateArrays(z.astype(np.int64), z.astype(np.int8), z, z, z, z.astype(np.int8))
    direction = np.where(up[idx], 1, -1).astype(np.int8)
    stop = data.c[idx] - direction * spec.stop_k * data.atr[idx]
    return CandidateArrays(
        idx, direction, stop, np.full(len(idx), np.nan), np.full(len(idx), spec.target_r),
        np.full(len(idx), EXIT_FIXED_R, dtype=np.int8),
    )


__all__ = ("MIN_BUCKET_N", "Q_GRID", "STOP_K_GRID", "SignalSpec", "Thresholds", "build_candidates", "fit_thresholds")
