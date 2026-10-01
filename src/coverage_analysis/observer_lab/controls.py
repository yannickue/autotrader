# ruff: noqa: E501
"""MATCHED CONTROL generator (OFFLINE; OBSERVATION_ONLY / NOT_ALPHA_VALIDATED).

A control is NOT a random bar. For every event (decision bar ``i``) it is a bar of the same market that matches on

* session bucket (PRE / OPEN_HOUR / MID / CLOSE_HOUR / POST from ``bars.session``; without a defensible cash open: four 6-hour blocks),
* market-local time of day, ``|minute_j - minute_i| <= minute_tol`` (default 30 min),
* volatility: percentile rank of ``atr / close`` over the market's bars, ``|pct_j - pct_i| <= atr_pct_band`` (default 0.10),
* spread band: percentile rank of ``spread / atr``, ``<= spread_pct_band`` (default 0.15); an unknown spread matches only unknown,
* direction: the control INHERITS the direction of its event (a control is labelled in that direction; direction is not a bar property).

and is EXCLUDED when it lies within ``exclusion_bars`` (default = the label horizon, 48 bars) of ANY event bar - so no control sits on an
event, inside an event's own outcome window, or has an event inside its own outcome window - or has no following bar. Controls are drawn
without replacement across events (a bar is never reused), in a seeded random event order with seeded random choice among the candidates:
same seed => identical controls, independent of dict/set ordering. The percentile ranks are descriptive covariates over the whole supplied
market sample (not a causal feature); matching never looks at outcomes.

Every call returns a ``MatchingReport``: match rate, the UNMATCHED events (never silently dropped), and standardised mean differences
(SMD, event minus control over the events that got controls) on the matching variables.

PLACEBO hook: ``placebo_controls`` takes a generator callable supplied by the feature lanes ("real level vs matched artificial level",
"ratio vs non-ratio"): ``generator(bars, event_bar_index, rng) -> Sequence[PlaceboDraw]``. The rng is seeded from ``(seed, event_bar_index)``
(independent of event order). Draws inside an event neighbourhood or outside the bars are rejected and counted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from market_observer.schema import ObserverBars

CONTROL_METHOD_VERSION = "observer-controls-1"
DEFAULT_EXCLUSION_BARS = 48  # == labels.DEFAULT_MAX_BARS: the control's and the event's outcome windows can never overlap


@dataclass(frozen=True)
class MatchSpec:
    minute_tol: int = 30
    atr_pct_band: float = 0.10
    spread_pct_band: float = 0.15
    n_controls: int = 1
    exclusion_bars: int = DEFAULT_EXCLUSION_BARS


@dataclass(frozen=True)
class PlaceboDraw:
    idx: int  # bar index of the placebo decision bar
    meta: Mapping[str, Any] = field(default_factory=dict)  # e.g. {"artificial_level": 101.3}


@dataclass(frozen=True)
class MatchingReport:
    n_events: int
    n_matched: int
    match_rate: float
    unmatched_event_pos: tuple[int, ...]
    n_controls: int
    smd: Mapping[str, float]
    session_share_diff_max: float
    method: str = CONTROL_METHOD_VERSION
    n_rejected_neighbourhood: int = 0
    n_rejected_range: int = 0


@dataclass(frozen=True)
class ControlSet:
    event_pos: np.ndarray  # position of the event in the INPUT event list, one entry per control
    control_idx: np.ndarray  # bar index of the control's decision bar
    report: MatchingReport
    extra: tuple[Mapping[str, Any], ...] = ()  # per control (placebo meta)


# ---------------------------------------------------------------------------------------------- covariates
def session_bucket(bars: ObserverBars) -> np.ndarray:
    m = np.asarray(bars.local_minute)
    sp = bars.session
    out = np.empty(len(m), dtype=object)
    if sp.cash_open_min is None:
        for k, name in enumerate(("BLOCK_0", "BLOCK_1", "BLOCK_2", "BLOCK_3")):
            out[(m // 360) % 4 == k] = name
        return out
    o, c = sp.cash_open_min, sp.cash_close_min
    out[:] = "MID"
    out[m < o] = "PRE"
    out[(m >= o) & (m < o + 60)] = "OPEN_HOUR"
    if c is not None:
        out[(m >= c - 60) & (m < c) & (m >= o + 60)] = "CLOSE_HOUR"
        out[m >= c] = "POST"
    return out


def _pct_rank(x: np.ndarray) -> np.ndarray:
    out = np.full(len(x), np.nan)
    ok = ~np.isnan(x)
    if ok.any():
        out[ok] = pd.Series(x[ok]).rank(pct=True).to_numpy()
    return out


def bar_covariates(bars: ObserverBars) -> dict[str, np.ndarray]:
    atr, close, spread = np.asarray(bars.atr, float), np.asarray(bars.c, float), np.asarray(bars.spread, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        vol = np.where((atr > 0) & (close > 0), atr / close, np.nan)
        spr = np.where((atr > 0) & ~np.isnan(spread), spread / atr, np.nan)
    return {
        "session": session_bucket(bars), "minute": np.asarray(bars.local_minute, float),
        "atr_pct": _pct_rank(vol), "spread_pct": _pct_rank(spr),
    }


def standardised_mean_difference(a: np.ndarray, b: np.ndarray) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) < 1 or len(b) < 1:
        return float("nan")
    sd = float(np.sqrt((a.var(ddof=1) if len(a) > 1 else 0.0) / 2 + (b.var(ddof=1) if len(b) > 1 else 0.0) / 2))
    diff = float(a.mean() - b.mean())
    if sd == 0:
        return 0.0 if diff == 0 else float("inf")
    return diff / sd


def _exclusion_mask(n: int, event_idx: np.ndarray, exclusion_bars: int) -> np.ndarray:
    diff = np.zeros(n + 1, dtype=np.int64)
    for i in event_idx:
        diff[max(0, int(i) - exclusion_bars)] += 1
        diff[min(n, int(i) + exclusion_bars + 1)] -= 1
    return np.cumsum(diff[:n]) > 0


def _check_events(bars: ObserverBars, event_idx: Sequence[int]) -> np.ndarray:
    ev = np.asarray(event_idx, dtype=np.int64)
    if len(ev) and (ev.min() < 0 or ev.max() >= len(bars)):
        raise ValueError("event index outside the bars")
    return ev


def _report(
    cov: Mapping[str, np.ndarray], ev: np.ndarray, event_pos: np.ndarray, control_idx: np.ndarray, *, rej_nb: int = 0, rej_rng: int = 0,
) -> MatchingReport:
    matched = sorted(set(event_pos.tolist()))
    unmatched = tuple(p for p in range(len(ev)) if p not in set(matched))
    smd: dict[str, float] = {}
    share = 0.0
    if len(control_idx):
        ei = ev[event_pos]
        for name, key in (("local_minute", "minute"), ("atr_pct", "atr_pct"), ("spread_pct", "spread_pct")):
            smd[name] = standardised_mean_difference(cov[key][ei], cov[key][control_idx])
        for b in set(cov["session"][ei].tolist()) | set(cov["session"][control_idx].tolist()):
            share = max(share, abs(float(np.mean(cov["session"][ei] == b)) - float(np.mean(cov["session"][control_idx] == b))))
    else:
        smd = {"local_minute": float("nan"), "atr_pct": float("nan"), "spread_pct": float("nan")}
    n = len(ev)
    return MatchingReport(n, len(matched), len(matched) / n if n else float("nan"), unmatched, len(control_idx), smd, share, n_rejected_neighbourhood=rej_nb, n_rejected_range=rej_rng)


# ---------------------------------------------------------------------------------------------- matched controls
def match_controls(
    bars: ObserverBars, event_idx: Sequence[int], *, spec: MatchSpec | None = None, seed: int = 0, eligible: np.ndarray | None = None,
) -> ControlSet:
    """``eligible`` (optional bool per bar) lets the caller add its own warm-up / data-quality exclusions."""
    spec = spec or MatchSpec()
    ev = _check_events(bars, event_idx)
    n = len(bars)
    cov = bar_covariates(bars)
    ok = ~np.isnan(cov["atr_pct"]) & (np.arange(n) < n - 1)
    if eligible is not None:
        ok &= np.asarray(eligible, bool)
    ok &= ~_exclusion_mask(n, ev, spec.exclusion_bars)
    pool_by_session = {s: np.flatnonzero(ok & (cov["session"] == s)) for s in set(cov["session"][ev].tolist())} if len(ev) else {}
    rng = np.random.default_rng(seed)
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for pos in rng.permutation(len(ev)):
        i = int(ev[pos])
        pool = pool_by_session.get(cov["session"][i], np.array([], dtype=np.int64))
        if len(pool) == 0:
            continue
        cand = pool[np.abs(cov["minute"][pool] - cov["minute"][i]) <= spec.minute_tol]
        cand = cand[np.abs(cov["atr_pct"][cand] - cov["atr_pct"][i]) <= spec.atr_pct_band + 1e-12]
        sp_i = cov["spread_pct"][i]
        if np.isnan(sp_i):
            cand = cand[np.isnan(cov["spread_pct"][cand])]
        else:
            cand = cand[np.abs(cov["spread_pct"][cand] - sp_i) <= spec.spread_pct_band + 1e-12]
        cand = np.array([c for c in cand.tolist() if c not in used], dtype=np.int64)
        if len(cand) == 0:
            continue
        take = rng.choice(cand, size=min(spec.n_controls, len(cand)), replace=False)
        for c in take.tolist():
            used.add(int(c))
            pairs.append((int(pos), int(c)))
    pairs.sort()
    event_pos = np.array([p for p, _ in pairs], dtype=np.int64)
    control_idx = np.array([c for _, c in pairs], dtype=np.int64)
    return ControlSet(event_pos, control_idx, _report(cov, ev, event_pos, control_idx))


# ---------------------------------------------------------------------------------------------- generic placebo hook
PlaceboGenerator = Callable[[ObserverBars, int, np.random.Generator], Sequence[PlaceboDraw]]


def placebo_controls(
    bars: ObserverBars, event_idx: Sequence[int], generator: PlaceboGenerator, *, seed: int = 0, n_per_event: int = 1,
    exclusion_bars: int = DEFAULT_EXCLUSION_BARS,
) -> ControlSet:
    """Controls produced by a feature-lane callable (artificial level / non-ratio counterpart). Same ``ControlSet`` shape and report as the
    matched controls; draws are validated (in range, with a following bar, outside every event neighbourhood). A bar may be drawn for
    several events here (the generator owns its sampling); n_per_event caps the valid draws kept per event."""
    ev = _check_events(bars, event_idx)
    n = len(bars)
    excl = _exclusion_mask(n, ev, exclusion_bars)
    pairs: list[tuple[int, int, Mapping[str, Any]]] = []
    rej_nb = rej_rng = 0
    for pos, i in enumerate(ev.tolist()):
        rng = np.random.default_rng([int(seed), int(i)])
        kept = 0
        for d in generator(bars, i, rng):
            if kept >= n_per_event:
                break
            if not 0 <= d.idx < n - 1:
                rej_rng += 1
            elif excl[d.idx]:
                rej_nb += 1
            else:
                pairs.append((pos, int(d.idx), dict(d.meta)))
                kept += 1
    cov = bar_covariates(bars)
    event_pos = np.array([p for p, _, _ in pairs], dtype=np.int64)
    control_idx = np.array([c for _, c, _ in pairs], dtype=np.int64)
    return ControlSet(event_pos, control_idx, _report(cov, ev, event_pos, control_idx, rej_nb=rej_nb, rej_rng=rej_rng), tuple(m for _, _, m in pairs))
