# ruff: noqa: E501
"""MATCHED CONTROL generator (OFFLINE; OBSERVATION_ONLY / NOT_ALPHA_VALIDATED).

A control is NOT a random bar. For every event (decision bar ``i``) it is a bar of the same market that matches on

* session bucket (PRE / OPEN_HOUR / MID / CLOSE_HOUR / POST from ``bars.session``; without a defensible cash open: four 6-hour blocks),
* market-local time of day, ``|minute_j - minute_i| <= minute_tol`` (default 30 min),
* volatility: percentile rank of ``atr / close``, ``|pct_j - pct_i| <= atr_pct_band`` (default 0.10),
* spread band: percentile rank of ``spread / atr``, ``<= spread_pct_band`` (default 0.15); an unknown spread matches only unknown,
* direction: the control INHERITS the direction of its event (a control is labelled in that direction; direction is not a bar property).

and is EXCLUDED when it lies within ``exclusion_bars`` (default = the label horizon, 48 bars) of ANY event bar - so no control sits on an
event, inside an event's own outcome window, or has an event inside its own outcome window - or has no following bar. Controls are drawn
without replacement across events (a bar is never reused), in a seeded random event order with seeded random choice among the candidates:
same seed => identical controls, independent of dict/set ordering. Matching never looks at outcomes.

PARTITION RULES (``observer-controls-2``; closes the split leak of ``observer-controls-1``):

* A control is drawn WITHIN the partition of its event (TRAIN event -> TRAIN control, VALIDATION -> VALIDATION, OOS -> OOS, FORWARD -> FORWARD).
  The per-bar partition (``bar_partitions``) is ``splits.assign_partitions`` applied to every bar with the label horizon (= ``exclusion_bars``)
  as the outcome window: bars whose horizon reaches into another partition are PURGED, the first horizon after a boundary is EMBARGO. PURGED /
  EMBARGO / UNASSIGNED bars are never controls; events that sit in such a bar are reported unmatched (``n_events_in_excluded_partition``).
* ``rank_mode`` decides how the volatility / spread percentile ranks are computed: ``"partition"`` (default) = rank among the bars of the bar's own
  partition only (a TRAIN event is never ranked against OOS / FORWARD bars); ``"causal"`` = expanding PAST-ONLY rank over every earlier bar of the
  market (the rank at bar j uses bars <= j only; ``min_history`` valid bars needed) - mandatory for the FORWARD partition, whose ranks must be
  computable live (``match_controls`` refuses FORWARD events with any other mode); ``"sample"`` = legacy whole-sample rank (descriptive only,
  NOT partition safe, kept for diagnostics). ``partition=None`` is the explicit legacy opt-out (one partition, ``partition_mode="UNPARTITIONED"``).
* ``exclude_idx``: bar indices of EVERY opportunity of the market (all families, not only the analysed subset). They and the events themselves are
  surrounded by ``exclusion_bars`` neighbourhoods in which no control may sit, so another family's real opportunity can never become a control.

Every call returns a ``MatchingReport``: match rate (overall and per partition), the UNMATCHED events (never silently dropped), and standardised
mean differences (SMD, event minus control over the events that got controls) on the matching variables.

PLACEBO hook: ``placebo_controls`` takes a generator callable supplied by the feature lanes ("real level vs matched artificial level",
"ratio vs non-ratio"): ``generator(bars, event_bar_index, rng) -> Sequence[PlaceboDraw]``. The rng is seeded from ``(seed, event_bar_index)``
(independent of event order). Draws inside an event neighbourhood, outside the bars, or (when ``partition`` is given) in another partition
than their event are rejected and counted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from coverage_analysis.observer_lab import splits as SP
from market_observer.schema import ObserverBars

CONTROL_METHOD_VERSION = "observer-controls-2"
DEFAULT_EXCLUSION_BARS = 48  # == labels.DEFAULT_MAX_BARS: the control's and the event's outcome windows can never overlap
RANK_MODES = ("partition", "causal", "sample")
_USABLE_PARTITIONS = (SP.TRAIN, SP.VALIDATION, SP.OOS, SP.FORWARD)
_ALL = "ALL"  # the single partition of the unpartitioned legacy mode


@dataclass(frozen=True)
class MatchSpec:
    minute_tol: int = 30
    atr_pct_band: float = 0.10
    spread_pct_band: float = 0.15
    n_controls: int = 1
    exclusion_bars: int = DEFAULT_EXCLUSION_BARS
    min_history: int = 50  # causal rank mode: valid past bars needed before a rank exists


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
    n_rejected_partition: int = 0  # placebo draws that fell into another partition than their event
    partition_mode: str = "UNPARTITIONED"  # "PARTITIONED" | "UNPARTITIONED"
    rank_mode: str = "sample"
    by_partition: Mapping[str, Mapping[str, float]] = field(default_factory=dict)  # partition -> {n_events, n_matched, match_rate}
    n_events_in_excluded_partition: int = 0  # events in a PURGED / EMBARGO / UNASSIGNED bar (no control by construction)
    n_exclusion_bars_blocked: int = 0  # distinct opportunity bars (events + exclude_idx) whose neighbourhood was blocked


@dataclass(frozen=True)
class ControlSet:
    event_pos: np.ndarray  # position of the event in the INPUT event list, one entry per control
    control_idx: np.ndarray  # bar index of the control's decision bar
    report: MatchingReport
    extra: tuple[Mapping[str, Any], ...] = ()  # per control (placebo meta)
    control_partition: tuple[str, ...] = ()  # partition label of every control (same order as control_idx)


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


def _causal_pct_rank(x: np.ndarray, min_history: int) -> np.ndarray:
    """Expanding past-only percentile rank: rank of x[j] among the valid x[0..j] (average rank / count, NaNs skipped; NaN until ``min_history``)."""
    return pd.Series(np.asarray(x, float)).expanding(min_periods=max(1, int(min_history))).rank(pct=True).to_numpy()


def _rank(x: np.ndarray, rank_mode: str, partition: np.ndarray | None, min_history: int) -> np.ndarray:
    if rank_mode == "sample":
        return _pct_rank(x)
    if rank_mode == "causal":
        return _causal_pct_rank(x, min_history)
    if rank_mode != "partition":
        raise ValueError(f"rank_mode must be one of {RANK_MODES}, got {rank_mode!r}")
    if partition is None:
        return _pct_rank(x)
    out = np.full(len(x), np.nan)
    for name in set(partition.tolist()):
        m = partition == name
        out[m] = _pct_rank(x[m])
    return out


def bar_covariates(
    bars: ObserverBars, *, rank_mode: str = "sample", partition: np.ndarray | None = None, min_history: int = 50,
) -> dict[str, np.ndarray]:
    """Matching covariates per bar. The defaults reproduce the legacy whole-sample ranks (descriptive only); ``match_controls`` passes its own mode."""
    atr, close, spread = np.asarray(bars.atr, float), np.asarray(bars.c, float), np.asarray(bars.spread, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        vol = np.where((atr > 0) & (close > 0), atr / close, np.nan)
        spr = np.where((atr > 0) & ~np.isnan(spread), spread / atr, np.nan)
    return {
        "session": session_bucket(bars), "minute": np.asarray(bars.local_minute, float),
        "atr_pct": _rank(vol, rank_mode, partition, min_history), "spread_pct": _rank(spr, rank_mode, partition, min_history),
    }


def bar_partitions(bars: ObserverBars, *, horizon_bars: int = DEFAULT_EXCLUSION_BARS, plan: SP.ObserverSplitPlan | None = None) -> np.ndarray:
    """Partition label of every bar (TRAIN/VALIDATION/OOS/FORWARD/PURGED/EMBARGO/UNASSIGNED) by ``splits.assign_partitions``: decision = bar close,
    label horizon = ``horizon_bars`` further bars. Without ``plan`` it is derived from the bars' own Berlin trading days and the repo freeze constants."""
    n = len(bars)
    if n == 0:
        return np.empty(0, dtype=object)
    step = int(bars.bar_seconds) * 1_000_000_000
    dec = np.asarray(bars.ts_ns, dtype=np.int64) + step
    hor = dec + horizon_bars * step
    if plan is None:
        try:
            plan = SP.build_plan(sorted(set(SP.berlin_dates(dec).tolist())))
        except ValueError as exc:
            raise ValueError(f"cannot derive the train/validation/OOS partitions from these bars ({exc}); pass partition=<per-bar labels> or partition=None") from exc
    return SP.assign_partitions(dec, hor, plan, embargo_s=float(horizon_bars * bars.bar_seconds))


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


def _blockers(bars: ObserverBars, ev: np.ndarray, exclude_idx: Sequence[int] | None) -> np.ndarray:
    """Events plus every extra opportunity bar (all families), unique and range-checked."""
    extra = np.asarray([] if exclude_idx is None else exclude_idx, dtype=np.int64)
    if len(extra) and (extra.min() < 0 or extra.max() >= len(bars)):
        raise ValueError("exclude_idx outside the bars")
    return np.unique(np.concatenate([ev, extra]))


def _resolve_partition(bars: ObserverBars, partition: np.ndarray | Sequence[str] | str | None, horizon_bars: int) -> np.ndarray | None:
    if partition is None:
        return None
    if isinstance(partition, str):
        if partition != "auto":
            raise ValueError("partition must be 'auto', None or a per-bar label array")
        return bar_partitions(bars, horizon_bars=horizon_bars)
    arr = np.asarray(partition, dtype=object)
    if len(arr) != len(bars):
        raise ValueError(f"partition has {len(arr)} labels for {len(bars)} bars")
    return arr


def _report(
    cov: Mapping[str, np.ndarray], ev: np.ndarray, event_pos: np.ndarray, control_idx: np.ndarray, *, rej_nb: int = 0, rej_rng: int = 0, rej_part: int = 0,
    part: np.ndarray | None = None, rank_mode: str = "sample", n_blocked: int = 0,
) -> MatchingReport:
    matched_set = set(event_pos.tolist())
    unmatched = tuple(p for p in range(len(ev)) if p not in matched_set)
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
    by_part: dict[str, dict[str, float]] = {}
    n_excl = 0
    if part is not None and n:
        ep = part[ev]
        for name in sorted(set(ep.tolist())):
            sel = np.flatnonzero(ep == name)
            if name not in _USABLE_PARTITIONS:
                n_excl += len(sel)
            nm = sum(int(p) in matched_set for p in sel)
            by_part[str(name)] = {"n_events": float(len(sel)), "n_matched": float(nm), "match_rate": nm / len(sel)}
    return MatchingReport(
        n, len(matched_set), len(matched_set) / n if n else float("nan"), unmatched, len(control_idx), smd, share,
        n_rejected_neighbourhood=rej_nb, n_rejected_range=rej_rng, n_rejected_partition=rej_part,
        partition_mode="PARTITIONED" if part is not None else "UNPARTITIONED", rank_mode=rank_mode, by_partition=by_part,
        n_events_in_excluded_partition=n_excl, n_exclusion_bars_blocked=n_blocked,
    )


# ---------------------------------------------------------------------------------------------- matched controls
def match_controls(
    bars: ObserverBars, event_idx: Sequence[int], *, spec: MatchSpec | None = None, seed: int = 0, eligible: np.ndarray | None = None,
    partition: np.ndarray | Sequence[str] | str | None = "auto", rank_mode: str = "partition", exclude_idx: Sequence[int] | None = None,
) -> ControlSet:
    """Matched controls WITHIN each event's partition (see module docstring).

    ``eligible`` (optional bool per bar): the caller's own warm-up / data-quality exclusions. ``partition``: ``"auto"`` (default; per-bar labels from
    ``bar_partitions``), a per-bar label array (length == len(bars); only TRAIN/VALIDATION/OOS/FORWARD bars can be controls), or ``None`` (legacy:
    one partition, no split guarantees). ``rank_mode``: ``"partition"`` | ``"causal"`` | ``"sample"``. ``exclude_idx``: ALL opportunity bars of this
    market (every family) whose +-``exclusion_bars`` neighbourhood is closed to controls, in addition to the events themselves."""
    spec = spec or MatchSpec()
    if rank_mode not in RANK_MODES:
        raise ValueError(f"rank_mode must be one of {RANK_MODES}, got {rank_mode!r}")
    ev = _check_events(bars, event_idx)
    n = len(bars)
    part = _resolve_partition(bars, partition, spec.exclusion_bars)
    if part is not None and len(ev) and rank_mode != "causal" and bool(np.any(part[ev] == SP.FORWARD)):
        raise ValueError("FORWARD events need rank_mode='causal' (past-only ranks): partition/sample ranks would use later forward bars")
    cov = bar_covariates(bars, rank_mode=rank_mode, partition=part, min_history=spec.min_history)
    blockers = _blockers(bars, ev, exclude_idx)
    ok = ~np.isnan(cov["atr_pct"]) & (np.arange(n) < n - 1)
    if eligible is not None:
        ok &= np.asarray(eligible, bool)
    if part is not None:
        ok &= np.isin(part, _USABLE_PARTITIONS)
    ok &= ~_exclusion_mask(n, blockers, spec.exclusion_bars)
    key = part if part is not None else np.full(n, _ALL, dtype=object)
    pool_by = {(p, s): np.flatnonzero(ok & (key == p) & (cov["session"] == s)) for p, s in set(zip(key[ev].tolist(), cov["session"][ev].tolist(), strict=True))} if len(ev) else {}
    rng = np.random.default_rng(seed)
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for pos in rng.permutation(len(ev)):
        i = int(ev[pos])
        if part is not None and part[i] not in _USABLE_PARTITIONS:
            continue  # the event sits in a purged / embargoed / unassigned bar: no valid control universe, reported as unmatched
        pool = pool_by.get((key[i], cov["session"][i]), np.array([], dtype=np.int64))
        if len(pool) == 0 or np.isnan(cov["atr_pct"][i]):
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
    rep = _report(cov, ev, event_pos, control_idx, part=part, rank_mode=rank_mode, n_blocked=len(blockers))
    return ControlSet(event_pos, control_idx, rep, control_partition=tuple(str(key[j]) for j in control_idx.tolist()))


# ---------------------------------------------------------------------------------------------- generic placebo hook
PlaceboGenerator = Callable[[ObserverBars, int, np.random.Generator], Sequence[PlaceboDraw]]


def placebo_controls(
    bars: ObserverBars, event_idx: Sequence[int], generator: PlaceboGenerator, *, seed: int = 0, n_per_event: int = 1,
    exclusion_bars: int = DEFAULT_EXCLUSION_BARS, exclude_idx: Sequence[int] | None = None, partition: np.ndarray | Sequence[str] | str | None = None,
) -> ControlSet:
    """Controls produced by a feature-lane callable (artificial level / non-ratio counterpart). Same ``ControlSet`` shape and report as the
    matched controls; draws are validated (in range, with a following bar, outside every event / ``exclude_idx`` neighbourhood and, when
    ``partition`` is given ('auto' or per-bar labels), inside the partition of their event). A bar may be drawn for several events here (the
    generator owns its sampling); n_per_event caps the valid draws kept per event."""
    ev = _check_events(bars, event_idx)
    n = len(bars)
    part = _resolve_partition(bars, partition, exclusion_bars)
    blockers = _blockers(bars, ev, exclude_idx)
    excl = _exclusion_mask(n, blockers, exclusion_bars)
    pairs: list[tuple[int, int, Mapping[str, Any]]] = []
    rej_nb = rej_rng = rej_part = 0
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
            elif part is not None and (part[d.idx] != part[i] or part[d.idx] not in _USABLE_PARTITIONS):
                rej_part += 1
            else:
                pairs.append((pos, int(d.idx), dict(d.meta)))
                kept += 1
    cov = bar_covariates(bars, rank_mode="partition", partition=part)
    event_pos = np.array([p for p, _, _ in pairs], dtype=np.int64)
    control_idx = np.array([c for _, c, _ in pairs], dtype=np.int64)
    rep = _report(cov, ev, event_pos, control_idx, rej_nb=rej_nb, rej_rng=rej_rng, rej_part=rej_part, part=part, rank_mode="partition" if part is not None else "sample", n_blocked=len(blockers))
    labels = tuple(str(part[j]) for j in control_idx.tolist()) if part is not None else ()
    return ControlSet(event_pos, control_idx, rep, tuple(m for _, _, m in pairs), labels)
