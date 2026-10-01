# ruff: noqa: E501
"""Observer Lane A - LEVEL REGISTRY / LEVEL MEMORY (OBSERVATION ONLY / NOT ALPHA VALIDATED).

Describes, at a decision bar, which objective market-structure levels exist, what happened at them (touch / rejection / penetration / break /
reclaim) and which role state they are in. Nothing here is an entry filter, a score or an input to any live decision. There is NO confluence
weight and NO "more sources = stronger" assumption: counts are reported as measured, the analysis decides what (if anything) they mean.

Public API (pure, prefix-invariant): ``LevelConfig``, ``LevelRegistry.update(bars, i)`` (incremental, strictly sequential from bar 0),
``build_level_context(bars, i, config)`` (replay from bar 0 = the reference), ``LevelContext`` / ``LevelState`` / ``Zone``, ``cluster_levels``,
``level_features(context, direction, event_price)`` (group ``levels``), ``reference_level(context, direction, event_price)`` (input of the
acceptance group), ``LEVELS_DEFINITION_HASH``, ``MIN_HISTORY_BARS`` / ``min_history_bars(config)``.

CAUSALITY.  ``update(bars, i)`` reads bars ``<= i`` only. A level exists from its ``confirmed_at`` (a timestamp <= the decision time of the bar
that first shows it); only bars whose OPEN is >= ``confirmed_at`` can touch/break it. Events (touch, break, reclaim) are stamped with the CLOSE
of the bar that produced them. Everything unknown is ``None`` (never back-filled, never 0).

LEVEL SOURCES (``created_at`` = open of the bar that formed the price, ``confirmed_at`` = when it became knowable)
  SWING_M5 / SWING_M15  confirmed fractal swings, REUSING ``demo.structure.confirmed_swings`` (n=2, called on the 2n+1 bar window; M15 bars are
                        built from three complete, contiguous M5 bars of ONE segment, mirroring ``demo.structure.resample_m15``). A swing is
                        confirmed at the CLOSE of the bar (M5) / of the M15 bar (M15) n bars after the swing bar. A forming M15 bar is never
                        closed: the swing appears at the third M5 bar of the confirming M15 bar. Segment-scoped.
  PREV_DAY_HIGH/LOW/CLOSE  high/low/last close of the previous TRADING day (market-local day = ``bars.local_day``); known when the first bar of
                        the next trading day opens, so weekends are skipped by construction. Survive segment breaks inside a day (a daily
                        aggregate over the bars present; broker pauses do not end the day).
  SESSION_HIGH/LOW      high/low of the previous COMPLETED cash session (cash_open <= local_minute < cash_close; to the end of the day when no
                        close is supplied). Known at the first post-close bar (or the day rollover). None without ``session.cash_open_min``.
  OVERNIGHT_HIGH/LOW    high/low from the previous cash close (post-close bars of the previous trading day) to the session open (pre-open bars of
                        today); without a cash close only the pre-open bars of today. Known at the first bar at/after the open. None without a
                        session open. May span a segment break (daily-anchored).
  ORB_HIGH/LOW          high/low of the bars opening in [cash_open, cash_open + ORB_MINUTES). MIRRORS ``alpha.families.orb``: the range must be
                        complete (all minutes/bar_minutes bars present) and have width > 0; the level exists from the close of the last range bar
                        (alpha decides from the first bar AFTER the window, i.e. the same instant). Difference: alpha's kernel needs a later bar
                        of the same day to exist; here the level is created at the window close. Daily-anchored.
  STRUCT_RANGE_HIGH/LOW highest high / lowest low of the last N=24 bars (mirrors ``demo.structure.prior_range_edges``, which the STRUCT stops use)
                        inside ONE contiguous segment holding >= N bars. A change of the extreme creates a new level and retires the old one.
                        Compression/balance of the range belongs to the balance group, not here. Segment-scoped.
  ROUND_MAJOR/MINOR     the two nearest grid points on each side of the close of the grids supplied in ``LevelConfig`` (never guessed; None = no
                        round levels). Minor points that lie on the major grid are major only. Scope = one (segment x local day).
  VWAP_PROXY            NOT in V1 (tick volume is not exchange volume; no defensible anchored VWAP yet).

NUMERIC DEFINITIONS (all predeclared in ``LevelConfig``, none tuned)
  zone half-width     max(2*tick, 1*spread, 0.10*ATR), fixed at creation (finite terms only).
  cluster tolerance   max(2*tick, 1*spread, 0.15*ATR) at the decision bar: two zones belong to one cluster if the GAP between them is <= tol
                      (overlap = negative gap); clustering is single-linkage (chains), order-independent.
  touch               the bar range intersects the zone AND the previous close was outside the zone (the range enters from outside); counted at
                      most once per 3 bars (min gap measured from the last COUNTED touch). A level created while price sits inside its zone has no
                      touches until price has left and re-entered.
  clean rejection     a counted touch whose episode ends (first close outside the zone) on the ORIGIN side, >= 0.25 ATR beyond the zone edge.
  penetration         a counted touch episode in which the bar extreme went beyond the FAR edge of the zone (counted once per episode). A break
                      implies a penetration; the converse is false (wicks).
  break               close beyond the zone by >= 0.25 ATR (ATR unknown -> buffer 0).
  reclaim             after a break, a close back THROUGH the whole zone (below the zone for BROKEN_UP, above it for BROKEN_DOWN).
  acceptance          >= 3 consecutive closes beyond the zone (the break close counts as the first).
  event stamps        ``last_touch_ts_ns`` etc. = close time of the bar that produced the event.
  distance_atr        (level price - decision close) / ATR at the decision bar: positive = level above price. ``zone_width_atr`` likewise.

ROLE STATE MACHINE (observed state only; ``next_role`` raises ``IllegalRoleTransition`` for any pair not listed)
  initial role at creation: SUPPORT if the close is above the zone, RESISTANCE if below, UNCLASSIFIED if inside (``previous_role`` stays None)
  UNCLASSIFIED          --OUT_ABOVE--> SUPPORT              --OUT_BELOW--> RESISTANCE        (first close outside the zone)
  SUPPORT / FLIPPED_TO_SUPPORT / RECLAIMED_FROM_BELOW     --BREAK_DOWN--> BROKEN_DOWN
  RESISTANCE / FLIPPED_TO_RESISTANCE / RECLAIMED_FROM_ABOVE --BREAK_UP--> BROKEN_UP
  BROKEN_UP             --ACCEPT_UP--> ACCEPTED_ABOVE       --THROUGH_DOWN--> RECLAIMED_FROM_ABOVE
  BROKEN_DOWN           --ACCEPT_DOWN--> ACCEPTED_BELOW     --THROUGH_UP--> RECLAIMED_FROM_BELOW
  ACCEPTED_ABOVE        --THROUGH_DOWN--> RECLAIMED_FROM_ABOVE        --HELD_ABOVE--> FLIPPED_TO_SUPPORT   (pullback with a clean rejection)
  ACCEPTED_BELOW        --THROUGH_UP--> RECLAIMED_FROM_BELOW          --HELD_BELOW--> FLIPPED_TO_RESISTANCE
  (RECLAIMED_FROM_ABOVE = price had broken up and closed back down through the zone; RECLAIMED_FROM_BELOW is the mirror.)

SCOPES AND SEGMENT BREAKS.  Swings, STRUCT and ROUND levels are reset at every segment break (no structure across a data gap). Daily-anchored
levels (PREV_DAY, SESSION, OVERNIGHT, ORB) are defined by the market-local calendar day and survive broker pauses inside the day (documented
exception). Timezone handling is the adapter's job: only ``local_minute`` / ``local_day`` are read (a DST-like jump in ``local_minute`` is taken
as supplied; e.g. an opening range with a missing bar is simply incomplete).

WARM-UP / RECURSIVE INVARIANCE (measured, tested in test_levels_invariance.py)
  A level is only shown if it is at most ``max_level_age_bars`` (576 = two 24h M5 days) bars old: the level MEMORY HORIZON. Every creation rule is
  an observed EVENT inside the loaded bars, never a state read at the first loaded bar:
    * the first bar the registry sees is a warm-up bar: the first loaded local day is a warm-up day (nothing is derived from its aggregates:
      PREV_DAY/SESSION/OVERNIGHT of that day are not created, because its true start is unknown), the first loaded segment may not create a STRUCT
      level from its first definable range, and the first loaded (segment x day) scope does not create ROUND levels for the band price already
      sits in (it creates them as price enters new bands);
    * swings need 2n (M5) / 3*(2n+1)+2 (M15, in M5 bars) bars of history before the confirming bar;
    * an expired level is never re-created from its still-true condition: only a new event (a changed extreme, a new scope) creates a new one.
  CONSEQUENCE: for a window of at least ``MIN_HISTORY_BARS`` = max_level_age_bars + max(lineage margins) + 1 bars before T, the context at T is
  EXACTLY equal to the one from the full history (given the same causal ATR/spread inputs; a recomputed Wilder ATR is equal within float noise
  after its own warm-up: ~100 bars are enough for 1e-6). With a shorter window the context may MISS levels that began before the window (they
  are excluded, never shown with truncated age/counts): every level shown is a real event inside the window. LIVE ADAPTER: load at least
  MIN_HISTORY_BARS closed bars before the first decision; the first loaded local day is not used for daily levels; do not trust level state
  before ``update`` has seen MIN_HISTORY_BARS bars (``LevelRegistry.warm``). Research replays from bar 0 of a file have the same warm-up day.
  Level age / touch counts of levels older than the horizon are therefore by definition NOT reported (the level is dropped), not truncated.

External reference (independent pivot definition, not used here): QuantConnect LEAN ``PivotPointsHighLow``. ``demo.structure.confirmed_swings``
remains the single swing definition of this repository.

Status: OBSERVATION_ONLY / NOT_ALPHA_VALIDATED. ``market_observer`` must not import ``alpha``; ``demo.structure`` is the only repo import.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from dataclasses import fields as dc_fields
from enum import StrEnum

import numpy as np
import pandas as pd

from demo.structure import confirmed_swings
from market_observer.schema import (
    GROUP_VERSIONS,
    FeatureResult,
    LevelRef,
    LevelRole,
    LevelSource,
    ObserverBars,
)

NS = 1_000_000_000
M5_NS = 300 * NS
M15_START_OFFSET = 600  # the third M5 bar of a 15-minute group opens 600 s after the group start


# ---------------------------------------------------------------------------------------------- config
@dataclass(frozen=True)
class LevelConfig:
    """Every constant of the level group, predeclared (NOT tuned). Changing one changes ``LEVELS_DEFINITION_HASH``."""

    k_tick_zone: float = 2.0  # zone half-width = max(k_tick*tick, k_spread*spread, k_atr*ATR) at the creation bar
    k_spread_zone: float = 1.0
    k_atr_zone: float = 0.10
    k_tick_cluster: float = 2.0  # cluster tolerance (gap between zones), same style, at the decision bar
    k_spread_cluster: float = 1.0
    k_atr_cluster: float = 0.15
    reject_atr: float = 0.25  # m: clean rejection = close >= m*ATR beyond the zone edge on the origin side
    break_atr: float = 0.25  # b: break = close >= b*ATR beyond the zone
    accept_bars: int = 3  # A: consecutive closes beyond the zone
    min_touch_gap_bars: int = 3  # min bars between COUNTED touches
    orb_minutes: int = 15
    struct_range_n: int | None = 24  # mirrors the STRUCT stop lookback; None disables STRUCT_RANGE levels
    round_major_step: float | None = None  # supplied by the adapter, never guessed
    round_minor_step: float | None = None
    swing_n: int = 2
    swing_timeframes: tuple[str, ...] = ("M5", "M15")
    max_level_age_bars: int = 576  # level memory horizon (2 x 288 M5 bars)
    nearest_k: int = 3  # K nearest levels/zones above/below in the context

    def __post_init__(self) -> None:
        if min(self.k_tick_zone, self.k_spread_zone, self.k_atr_zone, self.k_tick_cluster, self.k_spread_cluster, self.k_atr_cluster) < 0:
            raise ValueError("zone/cluster coefficients must be >= 0")
        if self.reject_atr < 0 or self.break_atr < 0:
            raise ValueError("reject_atr / break_atr must be >= 0")
        if self.accept_bars < 1 or self.min_touch_gap_bars < 1 or self.swing_n < 1 or self.nearest_k < 1 or self.max_level_age_bars < 1:
            raise ValueError("accept_bars, min_touch_gap_bars, swing_n, nearest_k and max_level_age_bars must be >= 1")
        if self.orb_minutes < 5:
            raise ValueError("orb_minutes must be >= 5")
        if self.struct_range_n is not None and self.struct_range_n < 2:
            raise ValueError("struct_range_n must be >= 2 or None")
        for step in (self.round_major_step, self.round_minor_step):
            if step is not None and not step > 0:
                raise ValueError("round steps must be > 0 or None")
        if any(tf not in ("M5", "M15") for tf in self.swing_timeframes):
            raise ValueError("swing_timeframes must be a subset of ('M5', 'M15')")


def min_history_bars(config: LevelConfig) -> int:
    """Closed bars of history required before T for an exact window-independent context at T (see the module docstring)."""
    margins = [2]
    if config.swing_timeframes:
        margins.append(2 * config.swing_n + 1)
    if "M15" in config.swing_timeframes:
        margins.append(3 * (2 * config.swing_n + 1) + 2)
    if config.struct_range_n is not None:
        margins.append(config.struct_range_n + 2)
    return config.max_level_age_bars + max(margins) + 1


# ---------------------------------------------------------------------------------------------- roles
class RoleEvent(StrEnum):
    OUT_ABOVE = "OUT_ABOVE"
    OUT_BELOW = "OUT_BELOW"
    BREAK_UP = "BREAK_UP"
    BREAK_DOWN = "BREAK_DOWN"
    ACCEPT_UP = "ACCEPT_UP"
    ACCEPT_DOWN = "ACCEPT_DOWN"
    THROUGH_DOWN = "THROUGH_DOWN"  # close back through the zone to below it
    THROUGH_UP = "THROUGH_UP"
    HELD_ABOVE = "HELD_ABOVE"  # a pullback from above was cleanly rejected
    HELD_BELOW = "HELD_BELOW"


R, E = LevelRole, RoleEvent
ROLE_TRANSITIONS: dict[tuple[LevelRole, RoleEvent], LevelRole] = {
    (R.UNCLASSIFIED, E.OUT_ABOVE): R.SUPPORT,
    (R.UNCLASSIFIED, E.OUT_BELOW): R.RESISTANCE,
    (R.SUPPORT, E.BREAK_DOWN): R.BROKEN_DOWN,
    (R.FLIPPED_TO_SUPPORT, E.BREAK_DOWN): R.BROKEN_DOWN,
    (R.RECLAIMED_FROM_BELOW, E.BREAK_DOWN): R.BROKEN_DOWN,
    (R.RESISTANCE, E.BREAK_UP): R.BROKEN_UP,
    (R.FLIPPED_TO_RESISTANCE, E.BREAK_UP): R.BROKEN_UP,
    (R.RECLAIMED_FROM_ABOVE, E.BREAK_UP): R.BROKEN_UP,
    (R.BROKEN_UP, E.ACCEPT_UP): R.ACCEPTED_ABOVE,
    (R.BROKEN_UP, E.THROUGH_DOWN): R.RECLAIMED_FROM_ABOVE,
    (R.BROKEN_DOWN, E.ACCEPT_DOWN): R.ACCEPTED_BELOW,
    (R.BROKEN_DOWN, E.THROUGH_UP): R.RECLAIMED_FROM_BELOW,
    (R.ACCEPTED_ABOVE, E.THROUGH_DOWN): R.RECLAIMED_FROM_ABOVE,
    (R.ACCEPTED_ABOVE, E.HELD_ABOVE): R.FLIPPED_TO_SUPPORT,
    (R.ACCEPTED_BELOW, E.THROUGH_UP): R.RECLAIMED_FROM_BELOW,
    (R.ACCEPTED_BELOW, E.HELD_BELOW): R.FLIPPED_TO_RESISTANCE,
}
_BROKEN = (R.BROKEN_UP, R.BROKEN_DOWN)
_RECLAIMED = (R.RECLAIMED_FROM_ABOVE, R.RECLAIMED_FROM_BELOW)


class IllegalRoleTransition(ValueError):
    pass


def next_role(role: LevelRole, event: RoleEvent) -> LevelRole:
    try:
        return ROLE_TRANSITIONS[(role, event)]
    except KeyError:
        raise IllegalRoleTransition(f"illegal level role transition: {role} on {event}") from None


# ---------------------------------------------------------------------------------------------- source families
SOURCE_FAMILY: dict[LevelSource, str] = {
    LevelSource.SWING_M5: "SWING", LevelSource.SWING_M15: "SWING",
    LevelSource.PREV_DAY_HIGH: "PREV_DAY", LevelSource.PREV_DAY_LOW: "PREV_DAY", LevelSource.PREV_DAY_CLOSE: "PREV_DAY",
    LevelSource.SESSION_HIGH: "SESSION", LevelSource.SESSION_LOW: "SESSION", LevelSource.OVERNIGHT_HIGH: "SESSION", LevelSource.OVERNIGHT_LOW: "SESSION",
    LevelSource.ORB_HIGH: "ORB", LevelSource.ORB_LOW: "ORB",
    LevelSource.STRUCT_RANGE_HIGH: "RANGE", LevelSource.STRUCT_RANGE_LOW: "RANGE",
    LevelSource.ROUND_MAJOR: "ROUND", LevelSource.ROUND_MINOR: "ROUND",
}  # independence = distinct families (derived from different information); VWAP_PROXY is not in V1

_SEGMENT_SCOPED = frozenset({
    LevelSource.SWING_M5, LevelSource.SWING_M15, LevelSource.STRUCT_RANGE_HIGH, LevelSource.STRUCT_RANGE_LOW, LevelSource.ROUND_MAJOR, LevelSource.ROUND_MINOR,
})
_PREV_DAY = (LevelSource.PREV_DAY_HIGH, LevelSource.PREV_DAY_LOW, LevelSource.PREV_DAY_CLOSE)
_SESSION = (LevelSource.SESSION_HIGH, LevelSource.SESSION_LOW)
_OVERNIGHT = (LevelSource.OVERNIGHT_HIGH, LevelSource.OVERNIGHT_LOW)
_ORB = (LevelSource.ORB_HIGH, LevelSource.ORB_LOW)
_ROUND = (LevelSource.ROUND_MAJOR, LevelSource.ROUND_MINOR)

DEFINITION_TEXT = (
    "levels-def-1",
    "zone=max(k_tick*tick,k_spread*spread,k_atr*atr) fixed at creation; cluster=gap<=tol single-linkage; independence=distinct SOURCE_FAMILY",
    "touch=range enters zone from outside (prev close outside), min gap between counted touches; rejection=first outside close on origin side >= m*atr",
    "penetration=counted episode extreme beyond far edge; break=close beyond zone >= b*atr; reclaim=close back through whole zone; accept=A consecutive closes",
    "sources=swing(demo.structure)+prevday+session+overnight+orb+struct_range+round(adapter grid); scopes: segment(swing,struct,round) day(prevday,session,overnight,orb)",
    "warmup: first loaded day/segment/scope untrusted; creation only by observed events; level memory horizon max_level_age_bars",
    "roles=ROLE_TRANSITIONS; first_touch=touch_count==0 or the only counted touch is on the decision bar; reference_level=nearest zone behind the event price; nearest-level ties: behind first, then source family",
)


def definition_hash(config: LevelConfig) -> str:
    h = hashlib.sha256()
    for f in dc_fields(config):
        h.update(f"{f.name}={getattr(config, f.name)!r};".encode())
    for line in DEFINITION_TEXT:
        h.update(line.encode())
    for (role, event), new in sorted(ROLE_TRANSITIONS.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value)):
        h.update(f"{role.value}|{event.value}|{new.value};".encode())
    h.update(GROUP_VERSIONS["levels"].encode())
    return h.hexdigest()[:16]


LEVELS_DEFINITION_HASH = definition_hash(LevelConfig())
MIN_HISTORY_BARS = min_history_bars(LevelConfig())


# ---------------------------------------------------------------------------------------------- output types
@dataclass(frozen=True)
class LevelState:
    """Causal per-level state at a decision bar. Unknown => None (never back-filled)."""

    level_id: str
    market: str
    source: LevelSource
    price: float
    zone_low: float
    zone_high: float
    created_at_ts_ns: int
    confirmed_at_ts_ns: int
    first_tradeable_at_ts_ns: int
    age_bars: int
    age_minutes: float
    touch_count: int
    clean_rejection_count: int
    penetration_count: int
    break_count: int
    reclaim_count: int
    last_touch_ts_ns: int | None
    last_break_ts_ns: int | None
    last_reclaim_ts_ns: int | None
    current_role: LevelRole
    previous_role: LevelRole | None
    distance_atr: float | None
    zone_width_atr: float | None
    sources_in_cluster: int | None


@dataclass(frozen=True)
class Zone:
    """Levels whose zones lie within the cluster tolerance. ``source_count`` = number of member levels; ``independent_source_count`` = distinct
    SOURCE_FAMILY values among them. Shadow feature only: a larger count is NOT assumed to mean a stronger zone."""

    zone_id: str
    zone_center: float
    zone_low: float
    zone_high: float
    sources: tuple[str, ...]
    source_count: int
    independent_source_count: int
    level_ids: tuple[str, ...]
    created_at_ts_ns: int  # earliest member
    confirmed_at_ts_ns: int  # latest member: the whole zone is known only from here


@dataclass(frozen=True)
class LevelContext:
    market: str
    index: int
    decision_ts_ns: int
    close: float
    atr: float | None
    cluster_tol: float
    levels: tuple[LevelState, ...]  # all active levels, ordered by (price, source, level_id)
    zones: tuple[Zone, ...]  # ordered by (center, zone_id)
    levels_above: tuple[LevelState, ...]  # the nearest K with price > close, nearest first
    levels_below: tuple[LevelState, ...]  # the nearest K with price < close, nearest first (a level exactly at the close is in neither list)
    zones_above: tuple[Zone, ...]
    zones_below: tuple[Zone, ...]


# ---------------------------------------------------------------------------------------------- internals
def _finite(x: float) -> bool:
    return x == x and math.isfinite(x)


def _atr_ok(x: float) -> bool:
    return _finite(x) and x > 0.0


class _Ext:
    """Running high/low with the OPEN timestamp of their (first) bars."""

    def __init__(self) -> None:
        self.hi = -math.inf
        self.lo = math.inf
        self.hi_ts = 0
        self.lo_ts = 0
        self.n = 0

    def update(self, h: float, lo: float, ts: int) -> None:
        if h > self.hi:
            self.hi, self.hi_ts = h, ts
        if lo < self.lo:
            self.lo, self.lo_ts = lo, ts
        self.n += 1

    @property
    def empty(self) -> bool:
        return self.n == 0


class _Lv:
    """Mutable internal level; ``LevelState`` is its immutable view."""

    def __init__(self, level_id: str, source: LevelSource, price: float, zl: float, zh: float, created: int, confirmed: int, idx_created: int, side: int) -> None:
        self.level_id, self.source, self.price, self.zl, self.zh = level_id, source, price, zl, zh
        self.created, self.confirmed, self.idx_created = created, confirmed, idx_created
        self.side_prev = side  # side of the previous close: +1 above the zone, -1 below, 0 inside
        self.role = LevelRole.SUPPORT if side > 0 else LevelRole.RESISTANCE if side < 0 else LevelRole.UNCLASSIFIED
        self.prev_role: LevelRole | None = None
        self.touch = self.rej = self.pen = self.brk = self.rec = 0
        self.last_touch_idx = -(10**9)
        self.last_touch_ts: int | None = None
        self.last_break_ts: int | None = None
        self.last_reclaim_ts: int | None = None
        self.consec = 0
        self.ep_active = False
        self.ep_origin = 0
        self.ep_pen = False


def _half_width(cfg: LevelConfig, tick: float, spread: float, atr: float) -> float:
    parts = [cfg.k_tick_zone * tick]
    if _finite(spread):
        parts.append(cfg.k_spread_zone * spread)
    if _finite(atr):
        parts.append(cfg.k_atr_zone * atr)
    return max(parts)


def _make_id(market: str, source: LevelSource, price: float, tick: float, confirmed: int) -> str:
    key = f"{market}|{source.value}|{round(price / tick)}|{confirmed}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def cluster_levels(states: Iterable[LevelState], tol: float) -> tuple[Zone, ...]:
    """Single-linkage clusters of zones: sorted by (zone_low, zone_high, source, level_id); a level joins the running cluster if its zone_low is
    within ``tol`` of the cluster's highest zone_high. Deterministic and independent of the insertion order."""
    ordered = sorted(states, key=lambda s: (s.zone_low, s.zone_high, s.source.value, s.level_id))
    groups: list[list[LevelState]] = []
    top = -math.inf
    for s in ordered:
        if groups and s.zone_low - top <= tol:
            groups[-1].append(s)
            top = max(top, s.zone_high)
        else:
            groups.append([s])
            top = s.zone_high
    zones = []
    for g in groups:
        ids = tuple(sorted(s.level_id for s in g))
        prices = sorted(s.price for s in g)
        zones.append(Zone(
            hashlib.sha1("|".join(ids).encode()).hexdigest()[:16],
            math.fsum(prices) / len(prices), min(s.zone_low for s in g), max(s.zone_high for s in g),
            tuple(sorted(s.source.value for s in g)), len(g), len({SOURCE_FAMILY.get(s.source, s.source.value) for s in g}), ids,
            min(s.created_at_ts_ns for s in g), max(s.confirmed_at_ts_ns for s in g),
        ))
    return tuple(sorted(zones, key=lambda z: (z.zone_center, z.zone_id)))


# ---------------------------------------------------------------------------------------------- the registry
class LevelRegistry:
    """Incremental, strictly sequential level memory of ONE market. ``update(bars, i)`` must be called for i = 0, 1, 2, ... (``bars`` may be a
    longer object than at the previous call: only indices <= i are read). State is plain data (deepcopy / pickle safe)."""

    def __init__(self, config: LevelConfig | None = None) -> None:
        self.cfg = config or LevelConfig()
        self._n = 0
        self._levels: list[_Lv] = []
        self._seg: int | None = None
        self._seg_ord = 0
        self._seg_start = 0
        self._m15: list[tuple[int, float, float]] = []
        self._struct_prev: tuple[float, float] | None = None
        self._round_seen: set[tuple[str, int]] = set()
        self._cur_day: int | None = None
        self._day_trusted = False
        self._prev_day_trusted = False
        self._day, self._sess, self._ov, self._post, self._orb = _Ext(), _Ext(), _Ext(), _Ext(), _Ext()
        self._ov_done = False
        self._sess_final = False
        self._orb_nb = 0

    @property
    def warm(self) -> bool:
        """True once at least MIN_HISTORY_BARS (for this config) bars were seen: the context is then window-independent."""
        return self._n >= min_history_bars(self.cfg)

    # ------------------------------------------------------------------------------- creation / removal
    def _new(self, bars: ObserverBars, source: LevelSource, price: float, created: int, confirmed: int, idx_created: int, ref: int) -> None:
        tick = bars.tick_size
        hw = _half_width(self.cfg, tick, float(bars.spread[ref]), float(bars.atr[ref]))
        zl, zh = price - hw, price + hw
        close = float(bars.c[ref])
        side = 1 if close > zh else -1 if close < zl else 0
        self._levels.append(_Lv(_make_id(bars.market, source, price, tick, confirmed), source, price, zl, zh, created, confirmed, idx_created, side))

    def _drop(self, sources: Iterable[LevelSource]) -> None:
        gone = set(sources)
        self._levels = [lv for lv in self._levels if lv.source not in gone]

    # ------------------------------------------------------------------------------- per-bar machinery
    def update(self, bars: ObserverBars, i: int, build_context: bool = True) -> LevelContext | None:
        if i != self._n:
            raise ValueError(f"LevelRegistry.update must be called sequentially: expected i={self._n}, got {i}")
        cfg = self.cfg
        if i == 0 and cfg.swing_timeframes and bars.bar_seconds != 300:
            raise ValueError("swing levels reuse demo.structure which is defined on M5 bars: bar_seconds must be 300")
        ts = int(bars.ts_ns[i])
        h, lo, c = float(bars.h[i]), float(bars.l[i]), float(bars.c[i])
        atr = float(bars.atr[i])
        dts = ts + bars.bar_seconds * NS
        seg, day, minute = int(bars.segment_id[i]), int(bars.local_day[i]), int(bars.local_minute[i])
        first_bar = i == 0
        new_seg = (not first_bar) and seg != self._seg
        new_day = (not first_bar) and day != self._cur_day
        if first_bar:
            self._seg, self._seg_ord, self._seg_start = seg, 0, 0
        elif new_seg:
            self._drop(_SEGMENT_SCOPED)
            self._m15 = []
            self._struct_prev = None
            self._seg, self._seg_ord, self._seg_start = seg, self._seg_ord + 1, i
        elif new_day:
            self._drop(_ROUND)  # ROUND scope = segment x local day
        scope_start = new_seg or new_day

        self._day_start(bars, i, day, minute, ts, first_bar, new_day)
        for lv in self._levels:
            self._advance(lv, h, lo, c, atr, dts, i)
        self._accumulate(bars, i, minute, ts, h, lo)
        self._create_at_close(bars, i, ts, dts, minute, h, lo, c, first_bar, scope_start)
        horizon = cfg.max_level_age_bars
        self._levels = [lv for lv in self._levels if i - lv.idx_created <= horizon]
        self._n += 1
        return self.context(bars) if build_context else None

    def _day_start(self, bars: ObserverBars, i: int, day: int, minute: int, ts: int, first_bar: bool, new_day: bool) -> None:
        sess = bars.session
        open_m, close_m = sess.cash_open_min, sess.cash_close_min
        if first_bar:
            self._cur_day = day
            self._day_trusted = False
            self._prev_day_trusted = False
            return
        ref = i - 1
        if new_day:
            self._drop(_PREV_DAY)
            self._drop(_OVERNIGHT)
            self._drop(_ORB)
            if self._day_trusted and not self._day.empty:
                self._new(bars, LevelSource.PREV_DAY_HIGH, self._day.hi, self._day.hi_ts, ts, ref, ref)
                self._new(bars, LevelSource.PREV_DAY_LOW, self._day.lo, self._day.lo_ts, ts, ref, ref)
                self._new(bars, LevelSource.PREV_DAY_CLOSE, float(bars.c[ref]), int(bars.ts_ns[ref]), ts, ref, ref)
            if open_m is not None and not self._sess_final and not self._sess.empty and self._day_trusted:
                self._session_levels(bars, ts, ref)
            self._prev_day_trusted = self._day_trusted
            self._day_trusted = True
            self._cur_day = day
            self._day, self._sess, self._ov, self._post, self._orb = _Ext(), _Ext(), self._post, _Ext(), _Ext()
            self._ov_done = False
            self._sess_final = False
            self._orb_nb = 0
        if open_m is None:
            return
        if minute >= open_m and not self._ov_done:
            self._ov_done = True
            if not self._ov.empty and self._day_trusted and (close_m is None or self._prev_day_trusted):
                self._drop(_OVERNIGHT)
                self._new(bars, LevelSource.OVERNIGHT_HIGH, self._ov.hi, self._ov.hi_ts, ts, ref, ref)
                self._new(bars, LevelSource.OVERNIGHT_LOW, self._ov.lo, self._ov.lo_ts, ts, ref, ref)
        if close_m is not None and minute >= close_m and not self._sess_final:
            self._sess_final = True
            if not self._sess.empty and self._day_trusted:
                self._session_levels(bars, ts, ref)

    def _session_levels(self, bars: ObserverBars, confirmed: int, ref: int) -> None:
        self._drop(_SESSION)
        self._new(bars, LevelSource.SESSION_HIGH, self._sess.hi, self._sess.hi_ts, confirmed, ref, ref)
        self._new(bars, LevelSource.SESSION_LOW, self._sess.lo, self._sess.lo_ts, confirmed, ref, ref)

    def _accumulate(self, bars: ObserverBars, i: int, minute: int, ts: int, h: float, lo: float) -> None:
        self._day.update(h, lo, ts)
        open_m, close_m = bars.session.cash_open_min, bars.session.cash_close_min
        if open_m is None:
            return
        if minute < open_m:
            if not self._ov_done:
                self._ov.update(h, lo, ts)
        elif close_m is None or minute < close_m:
            self._sess.update(h, lo, ts)
        else:
            self._post.update(h, lo, ts)

    def _create_at_close(self, bars: ObserverBars, i: int, ts: int, dts: int, minute: int, h: float, lo: float, c: float, first_bar: bool, scope_start: bool) -> None:
        cfg = self.cfg
        n = cfg.swing_n
        # ---- ORB (mirrors alpha.families.orb: complete range, width > 0)
        open_m = bars.session.cash_open_min
        if open_m is not None:
            bar_min = bars.bar_seconds // 60
            if open_m <= minute < open_m + cfg.orb_minutes:
                self._orb.update(h, lo, ts)
                self._orb_nb += 1
                if minute + bar_min >= open_m + cfg.orb_minutes and self._orb_nb == cfg.orb_minutes // bar_min and self._orb.hi > self._orb.lo:
                    self._drop(_ORB)
                    self._new(bars, LevelSource.ORB_HIGH, self._orb.hi, self._orb.hi_ts, dts, i, i)
                    self._new(bars, LevelSource.ORB_LOW, self._orb.lo, self._orb.lo_ts, dts, i, i)
        # ---- swings (REUSE demo.structure.confirmed_swings on the 2n+1 window; the centre bar is the only candidate)
        if "M5" in cfg.swing_timeframes and i - self._seg_start >= 2 * n:
            lo_i = i - 2 * n
            frame = pd.DataFrame({"ts": pd.to_datetime(bars.ts_ns[lo_i: i + 1], utc=True), "high": bars.h[lo_i: i + 1], "low": bars.l[lo_i: i + 1]})
            for s in confirmed_swings(frame, n, "M5"):
                self._new(bars, LevelSource.SWING_M5, float(s.price), int(bars.ts_ns[i - n]), dts, i, i)
        m15_complete = (
            "M15" in cfg.swing_timeframes and i - self._seg_start >= 2 and ts % (900 * NS) == M15_START_OFFSET * NS
            and int(bars.ts_ns[i - 1]) == ts - M5_NS and int(bars.ts_ns[i - 2]) == ts - 2 * M5_NS
        )
        if m15_complete:
            self._m15.append((ts - 2 * M5_NS, float(np.max(bars.h[i - 2: i + 1])), float(np.min(bars.l[i - 2: i + 1]))))
            if len(self._m15) >= 2 * n + 1:
                win = self._m15[-(2 * n + 1):]
                frame = pd.DataFrame({"ts": pd.to_datetime([w[0] for w in win], utc=True), "high": [w[1] for w in win], "low": [w[2] for w in win]})
                for s in confirmed_swings(frame, n, "M15"):
                    self._new(bars, LevelSource.SWING_M15, float(s.price), win[n][0], dts, i, i)
        # ---- STRUCT range (mirrors demo.structure.prior_range_edges over the last N bars of ONE segment)
        big_n = cfg.struct_range_n
        if big_n is not None and i - self._seg_start + 1 >= big_n:
            hs, ls = bars.h[i - big_n + 1: i + 1], bars.l[i - big_n + 1: i + 1]
            kh, kl = int(np.argmax(hs)), int(np.argmin(ls))
            hi, lw = float(hs[kh]), float(ls[kl])
            prev = self._struct_prev
            from_nothing = prev is None and self._seg_ord >= 1  # a real (observed) segment start; the first loaded segment may be truncated
            if (prev is not None and hi != prev[0]) or from_nothing:
                self._drop((LevelSource.STRUCT_RANGE_HIGH,))
                self._new(bars, LevelSource.STRUCT_RANGE_HIGH, hi, int(bars.ts_ns[i - big_n + 1 + kh]), dts, i, i)
            if (prev is not None and lw != prev[1]) or from_nothing:
                self._drop((LevelSource.STRUCT_RANGE_LOW,))
                self._new(bars, LevelSource.STRUCT_RANGE_LOW, lw, int(bars.ts_ns[i - big_n + 1 + kl]), dts, i, i)
            self._struct_prev = (hi, lw)
        # ---- ROUND grid (adapter supplied)
        cands: list[tuple[LevelSource, float]] = []
        major, minor = cfg.round_major_step, cfg.round_minor_step
        for step, src in ((major, LevelSource.ROUND_MAJOR), (minor, LevelSource.ROUND_MINOR)):
            if step is None:
                continue
            k = math.floor(c / step)
            for m in (k, k + 1):
                p = round(m * step, 10)
                if src is LevelSource.ROUND_MINOR and major is not None and abs(p / major - round(p / major)) < 1e-9:
                    continue
                cands.append((src, p))
        if first_bar:
            self._round_seen = {(s.value, round(p / bars.tick_size)) for s, p in cands}  # warm-up scope: the band price starts in is unknown history
        else:
            if scope_start:
                self._round_seen = set()
            for src, p in cands:
                key = (src.value, round(p / bars.tick_size))
                if key not in self._round_seen:
                    self._round_seen.add(key)
                    self._new(bars, src, p, dts, dts, i, i)

    def _advance(self, lv: _Lv, h: float, lo: float, c: float, atr: float, dts: int, idx: int) -> None:
        cfg = self.cfg
        zl, zh = lv.zl, lv.zh
        side = 1 if c > zh else -1 if c < zl else 0
        rejected, origin = False, 0
        if lv.side_prev != 0 and h >= zl and lo <= zh and idx - lv.last_touch_idx >= cfg.min_touch_gap_bars:
            lv.touch += 1
            lv.last_touch_idx, lv.last_touch_ts = idx, dts
            lv.ep_active, lv.ep_origin, lv.ep_pen = True, lv.side_prev, False
        if lv.ep_active:
            if not lv.ep_pen and ((lv.ep_origin > 0 and lo < zl) or (lv.ep_origin < 0 and h > zh)):
                lv.pen += 1
                lv.ep_pen = True
            if side != 0:
                lv.ep_active = False
                if side == lv.ep_origin and _atr_ok(atr) and ((c - zh) if side > 0 else (zl - c)) >= cfg.reject_atr * atr:
                    lv.rej += 1
                    rejected, origin = True, side
        buf = cfg.break_atr * atr if _atr_ok(atr) else 0.0
        above, below = c > zh, c < zl
        up_break, dn_break = above and (c - zh) >= buf, below and (zl - c) >= buf
        role = lv.role
        ev: RoleEvent | None = None
        if role is R.UNCLASSIFIED:
            ev = E.OUT_ABOVE if above else E.OUT_BELOW if below else None
        elif role in (R.SUPPORT, R.FLIPPED_TO_SUPPORT, R.RECLAIMED_FROM_BELOW):
            ev = E.BREAK_DOWN if dn_break else None
        elif role in (R.RESISTANCE, R.FLIPPED_TO_RESISTANCE, R.RECLAIMED_FROM_ABOVE):
            ev = E.BREAK_UP if up_break else None
        elif role is R.BROKEN_UP:
            if below:
                ev = E.THROUGH_DOWN
            else:
                lv.consec = lv.consec + 1 if above else 0
                ev = E.ACCEPT_UP if lv.consec >= cfg.accept_bars else None
        elif role is R.BROKEN_DOWN:
            if above:
                ev = E.THROUGH_UP
            else:
                lv.consec = lv.consec + 1 if below else 0
                ev = E.ACCEPT_DOWN if lv.consec >= cfg.accept_bars else None
        elif role is R.ACCEPTED_ABOVE:
            ev = E.THROUGH_DOWN if below else E.HELD_ABOVE if rejected and origin > 0 else None
        elif role is R.ACCEPTED_BELOW:
            ev = E.THROUGH_UP if above else E.HELD_BELOW if rejected and origin < 0 else None
        if ev is not None:
            new = next_role(role, ev)
            lv.prev_role, lv.role = role, new
            if new in _BROKEN:
                lv.brk += 1
                lv.last_break_ts = dts
                lv.consec = 1
            elif new in _RECLAIMED:
                lv.rec += 1
                lv.last_reclaim_ts = dts
                lv.consec = 0
        lv.side_prev = side

    # ------------------------------------------------------------------------------- context
    def context(self, bars: ObserverBars) -> LevelContext:
        if self._n == 0:
            raise ValueError("no bar processed yet")
        i = self._n - 1
        cfg = self.cfg
        close, atr_raw, spread = float(bars.c[i]), float(bars.atr[i]), float(bars.spread[i])
        atr = atr_raw if _atr_ok(atr_raw) else None
        dts = bars.decision_ts_ns(i)
        states = []
        for lv in self._levels:
            states.append(LevelState(
                lv.level_id, bars.market, lv.source, lv.price, lv.zl, lv.zh, lv.created, lv.confirmed, lv.confirmed, i - lv.idx_created,
                (dts - lv.confirmed) / (60 * NS), lv.touch, lv.rej, lv.pen, lv.brk, lv.rec, lv.last_touch_ts, lv.last_break_ts, lv.last_reclaim_ts,
                lv.role, lv.prev_role, None if atr is None else (lv.price - close) / atr, None if atr is None else (lv.zh - lv.zl) / atr, None,
            ))
        tol_parts = [cfg.k_tick_cluster * bars.tick_size]
        if _finite(spread):
            tol_parts.append(cfg.k_spread_cluster * spread)
        if atr is not None:
            tol_parts.append(cfg.k_atr_cluster * atr)
        tol = max(tol_parts)
        zones = cluster_levels(states, tol)
        in_zone = {lid: z.source_count for z in zones for lid in z.level_ids}
        states = sorted((replace(s, sources_in_cluster=in_zone[s.level_id]) for s in states), key=lambda s: (s.price, s.source.value, s.level_id))
        k = cfg.nearest_k
        above = tuple(sorted((s for s in states if s.price > close), key=lambda s: (s.price - close, s.level_id))[:k])
        below = tuple(sorted((s for s in states if s.price < close), key=lambda s: (close - s.price, s.level_id))[:k])
        z_above = tuple(sorted((z for z in zones if z.zone_center > close), key=lambda z: (z.zone_center - close, z.zone_id))[:k])
        z_below = tuple(sorted((z for z in zones if z.zone_center < close), key=lambda z: (close - z.zone_center, z.zone_id))[:k])
        return LevelContext(bars.market, i, dts, close, atr, tol, tuple(states), zones, above, below, z_above, z_below)


def build_level_context(bars: ObserverBars, i: int, config: LevelConfig | None = None) -> LevelContext:
    """The reference: replay from bar 0 up to ``i`` (reads bars <= i only)."""
    reg = LevelRegistry(config)
    for j in range(i + 1):
        reg.update(bars, j, build_context=False)
    return reg.context(bars)


# ---------------------------------------------------------------------------------------------- features
def _nearest(ctx: LevelContext, direction: int, event_price: float) -> LevelState | None:
    if not ctx.levels:
        return None
    return min(
        ctx.levels,
        key=lambda s: (abs(s.price - event_price), direction * (s.price - event_price), SOURCE_FAMILY.get(s.source, ""), s.level_id),  # family: mirror-invariant tie-break
    )


def level_features(ctx: LevelContext, direction: int, event_price: float) -> FeatureResult:
    """Group ``levels`` at the decision bar of ``ctx``. The nearest level is the one closest to ``event_price`` (ties: the one BEHIND the event in
    the trade direction first, then the source FAMILY name, then the level id). ``nearest_level_distance_atr`` is signed in the TRADE frame: direction * (level - event_price) / ATR (positive =
    the level lies ahead of the event). ``first_touch`` = no counted touch yet, or the only counted touch is on the decision bar itself.
    Counts are as measured; no score, no weight."""
    if direction not in (1, -1):
        raise ValueError("direction must be +1 or -1")
    atr = ctx.atr
    near = _nearest(ctx, direction, event_price)
    zone = None
    if near is not None:
        zone = next(z for z in ctx.zones if near.level_id in z.level_ids)
    v: dict[str, float | int | str | bool | None] = {
        "nearest_level_distance_atr": None if near is None or atr is None else direction * (near.price - event_price) / atr,
        "nearest_level_source": None if near is None else near.source.value,
        "nearest_level_age_bars": None if near is None else near.age_bars,
        "touch_count": None if near is None else near.touch_count,
        "clean_rejection_count": None if near is None else near.clean_rejection_count,
        "penetration_count": None if near is None else near.penetration_count,
        "first_touch": None if near is None else (near.touch_count == 0 or (near.touch_count == 1 and near.last_touch_ts_ns == ctx.decision_ts_ns)),
        "break_count": None if near is None else near.break_count,
        "reclaim_count": None if near is None else near.reclaim_count,
        "role": None if near is None else near.current_role.value,
        "previous_role": None if near is None or near.previous_role is None else near.previous_role.value,
        "zone_source_count": None if zone is None else zone.source_count,
        "zone_independent_source_count": None if zone is None else zone.independent_source_count,
        "zone_width_atr": None if zone is None or atr is None else (zone.zone_high - zone.zone_low) / atr,
        "n_levels_within_1atr": None if atr is None else sum(1 for s in ctx.levels if abs(s.price - event_price) <= atr),
        "last_touch_ts_ns": None if near is None else near.last_touch_ts_ns,
        "last_break_ts_ns": None if near is None else near.last_break_ts_ns,
        "last_reclaim_ts_ns": None if near is None else near.last_reclaim_ts_ns,
    }
    return FeatureResult("levels", GROUP_VERSIONS["levels"], v)


def reference_level(ctx: LevelContext, direction: int, event_price: float) -> LevelRef | None:
    """The explicit level input of the acceptance group: the nearest ZONE BEHIND the event price in the trade direction, i.e. the zone the
    event is holding/accepting beyond. Long (+1): zones with zone_low <= event_price, distance max(0, event_price - zone_high) (0 when the
    price is inside), ties -> the higher centre. Short (-1): zones with zone_high >= event_price, distance max(0, zone_low - event_price), ties ->
    the lower centre. None if there is no such zone. The ref carries the cluster's earliest creation and its LATEST member confirmation."""
    best: tuple[tuple[float, float, str], Zone] | None = None
    for z in ctx.zones:
        if direction > 0:
            if z.zone_low > event_price:
                continue
            key = (max(0.0, event_price - z.zone_high), event_price - z.zone_center, z.zone_id)
        else:
            if z.zone_high < event_price:
                continue
            key = (max(0.0, z.zone_low - event_price), z.zone_center - event_price, z.zone_id)
        if best is None or key < best[0]:
            best = (key, z)
    if best is None:
        return None
    z = best[1]
    return LevelRef(z.zone_id, z.zone_center, z.zone_low, z.zone_high, z.sources, z.created_at_ts_ns, z.confirmed_at_ts_ns)


__all__: Sequence[str] = [
    "LEVELS_DEFINITION_HASH", "MIN_HISTORY_BARS", "ROLE_TRANSITIONS", "SOURCE_FAMILY", "IllegalRoleTransition", "LevelConfig", "LevelContext",
    "LevelRegistry", "LevelState", "RoleEvent", "Zone", "build_level_context", "cluster_levels", "definition_hash", "level_features",
    "min_history_bars", "next_role", "reference_level",
]
