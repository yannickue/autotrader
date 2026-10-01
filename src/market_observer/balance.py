# ruff: noqa: E501
"""BALANCE feature group (OBSERVATION ONLY / NOT ALPHA VALIDATED): is the recent window a true balance or a slow trend inside a range?

Pure, prefix-invariant: ``balance_features(bars, i)`` reads bars ``[i-N+1 .. i]`` only, all inside ONE contiguous segment (``segment_id`` equal
at both ends; ids are monotone), else every value is ``None``. No parameter sweep: two predeclared windows (STRUCT's ``n_range`` default 24 and one
longer window 48). Metrics, for a window of N bars with high H = max(h), low L = min(l), R = H - L:

* ``range_width_atr``        R / atr[i] (STRUCT's width-in-ATR; atr is the causal ATR at the decision bar). None if atr is not finite / <= 0.
* ``directional_efficiency`` |c_end - c_start| / sum |c_k - c_(k-1)| over the N-1 close-to-close moves (Kaufman efficiency ratio; 1 = pure trend,
                             ~0 = back-and-forth). None if the path length is 0 (completely flat closes: undefined, not imputed).
* ``bar_overlap_ratio``      mean over the N-1 consecutive bar pairs of |intersection of the two [l,h] ranges| / |union|; two identical zero-width
                             ranges count as 1.0.
* ``close_occupancy_ratio``  share of the N closes inside the CENTRAL zone [L + z*R, H - z*R], z = (1 - central_zone_fraction) / 2 (default
                             central_zone_fraction 0.5 = middle half of the window range); bounds inclusive; R = 0 -> 1.0.
* ``midpoint_cross_count``   number of sign changes of (close - (H+L)/2) along the window; a close exactly ON the midpoint carries the previous side.

``close_distribution_entropy`` is deliberately NOT included: with N=24 closes any histogram needs a bin-count parameter, the value is dominated by
that choice, and occupancy + crossings already separate the intended cases. All constants live in :class:`BalanceConfig`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

import numpy as np

from market_observer.schema import GROUP_VERSIONS, FeatureResult, JsonScalar, ObserverBars

GROUP = "balance"
METRICS: tuple[str, ...] = (
    "range_width_atr", "directional_efficiency", "bar_overlap_ratio", "close_occupancy_ratio", "midpoint_cross_count",
)


@dataclass(frozen=True)
class BalanceConfig:
    windows: tuple[int, ...] = (24, 48)  # 24 = STRUCT default n_range; 48 = one longer predeclared window (4 h of M5)
    central_zone_fraction: float = 0.5  # the middle half of the window range counts as "central"

    def __post_init__(self) -> None:
        if not self.windows or any(w < 2 for w in self.windows):
            raise ValueError("windows must be >= 2")
        if not 0.0 < self.central_zone_fraction <= 1.0:
            raise ValueError("central_zone_fraction must be in (0, 1]")


def definition_hash(config: BalanceConfig | None = None) -> str:
    """SHA-256 of (group version, metric names, every constant): any silent change of a definition changes this value."""
    cfg = config or BalanceConfig()
    payload = {"group": GROUP, "version": GROUP_VERSIONS[GROUP], "metrics": list(METRICS), "config": asdict(cfg)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


DEFINITION_HASH = definition_hash()


def _window_values(bars: ObserverBars, i: int, n: int, cfg: BalanceConfig) -> dict[str, JsonScalar]:
    out: dict[str, JsonScalar] = {m: None for m in METRICS}
    s = i - n + 1
    if s < 0 or int(bars.segment_id[s]) != int(bars.segment_id[i]):
        return out
    h, lo, c = bars.h[s: i + 1], bars.l[s: i + 1], bars.c[s: i + 1]
    if not (np.all(np.isfinite(h)) and np.all(np.isfinite(lo)) and np.all(np.isfinite(c))):
        return out
    top, bot = float(h.max()), float(lo.min())
    rng = top - bot
    atr = float(bars.atr[i])
    if np.isfinite(atr) and atr > 0:
        out["range_width_atr"] = rng / atr
    path = float(np.abs(np.diff(c)).sum())
    if path > 0:
        out["directional_efficiency"] = abs(float(c[-1] - c[0])) / path
    inter = np.minimum(h[1:], h[:-1]) - np.maximum(lo[1:], lo[:-1])
    union = np.maximum(h[1:], h[:-1]) - np.minimum(lo[1:], lo[:-1])
    ratio = np.where(union > 0, np.clip(inter, 0.0, None) / np.where(union > 0, union, 1.0), 1.0)
    out["bar_overlap_ratio"] = float(ratio.mean())
    if rng > 0:
        z = (1.0 - cfg.central_zone_fraction) / 2.0
        inside = (c >= bot + z * rng) & (c <= top - z * rng)
        out["close_occupancy_ratio"] = float(inside.mean())
    else:
        out["close_occupancy_ratio"] = 1.0
    mid = (top + bot) / 2.0
    side = np.sign(c - mid)
    side = side[side != 0]
    out["midpoint_cross_count"] = int(np.count_nonzero(side[1:] != side[:-1])) if len(side) > 1 else 0
    return out


def balance_features(bars: ObserverBars, i: int, config: BalanceConfig | None = None) -> FeatureResult:
    cfg = config or BalanceConfig()
    values: dict[str, JsonScalar] = {}
    for n in cfg.windows:
        for name, v in _window_values(bars, i, n, cfg).items():
            values[f"{name}_w{n}"] = v
    return FeatureResult(GROUP, GROUP_VERSIONS[GROUP], values)
