# ruff: noqa: E501
"""MarketMap builder: a THIN CAUSAL ADAPTER over the existing market-observer facts (RESEARCH / OFFLINE ONLY).

No trade authority; nothing production-reachable may import this module. Nothing is re-implemented that the observer already
computes; every field below names the call it comes from (also stored per field in ``MarketMap.provenance``).

Timing: ``decision_ts = bars.decision_ts_ns(i)`` (bar CLOSE). A map at ``i`` reads bars ``<= i`` only. The key causality property
(tested): ``build_market_map(bars, i) == build_market_map(bars.prefix(i + 1), i)`` with bit-identical ``content_hash``.

FIELD -> CALL (all values are ``None`` below the documented warm-up; never an exception, never back-filled)

=====================  ====================================================================================================
session                adapter (``SessionSpec`` + ``bars.local_minute[i]``): CASH / PRE_OPEN / POST_CLOSE, None w/o a cash open
h1_context             ADAPTER (the only missing fact): EMA(8) vs EMA(21) of the last <=96 CLOSED, COMPLETE UTC-epoch H1 buckets
                       built from the M5 bars (mirrors ``demo.opportunity.snapshot`` H1 trend); UP/DOWN/NEUTRAL (|fast-slow| <=
                       0.5 * M5 ATR => NEUTRAL). FeatureStore ``h1_*`` arrays need a prebuilt store (not an ObserverBars fact).
m15_structure          ``market_observer.swings.swing_state(bars, i, "M15").sequence`` (None while ``i + 1 < MIN_HISTORY_BARS``
m5_structure           ``market_observer.swings.swing_state(bars, i, "M5").sequence``   or the ATR is unknown)
nearest/second         ``market_observer.levels.build_level_context(bars, i, cfg.levels)`` -> ``levels_below[0|1]`` (support) and
support/resistance     ``levels_above[0|1]`` (resistance), measured against the close of bar ``i`` (second = nearest level of a DIFFERENT zone cluster) (None while the level
                       registry is not warm: ``i + 1 < levels.min_history_bars``)
active_*_zone          same context, the zone price is AT or INSIDE (None = not at a zone), ``(zone_low, zone_high)``: support: the
                       nearest zone with ``close >= zone_low`` and ``bar_low <= zone_high + cluster_tol``; resistance: the nearest
                       zone with ``close <= zone_high`` and ``bar_high >= zone_low - cluster_tol`` (a wick touch counts; mirror-safe)
role_reversal_zones    same context: zones of levels whose ``current_role`` is FLIPPED_TO_SUPPORT / FLIPPED_TO_RESISTANCE (sorted)
balance_state          ``market_observer.balance.balance_features(bars, i, cfg.balance)`` longest window -> BALANCE / DIRECTIONAL /
                       MIXED (frozen thresholds in ``MarketMapConfig``)
acceptance_state       ``levels.reference_level(ctx, +/-1, close)`` + ``acceptance.acceptance_features(bars, i, ref, +/-1, cfg)``
                       -> ``{"LONG:<zone_id>": label, "SHORT:<zone_id>": label}``; label in BROKEN / ACCEPTED / RECLAIMED /
                       RETEST_HELD / None (None = not evaluable OR no break yet; see ``acceptance_label``). The reference zone of LONG is the nearest zone BEHIND price (support side), of SHORT the
                       nearest zone above. A side without a reference zone has NO key (never a fake id).
participation_state    ``market_observer.participation.participation_features`` ``tick_activity_percentile`` -> LOW / NORMAL / HIGH.
                       A tick-activity PROXY (MT5 tick count), NOT exchange volume.
volatility_context     adapter: ATR[i] / median(ATR[i-95..i]) -> LOW / NORMAL / HIGH (None if the 96-bar ATR window is not finite)
market_phase           derived (see below) from the facts above only
=====================  ====================================================================================================

NO geometry lives in the MarketMap. Geometry exists only for a PROPOSED entry: :func:`geometry_for_entry`.

MARKET PHASE (frozen first-match rules, mirror-symmetric; every threshold is in ``MarketMapConfig``; W = ``event_window_bars``)
IMPLEMENTED: FAILED_BREAK, REVERSAL_ATTEMPT, BREAKOUT, TREND, PULLBACK, EXPANSION, COMPRESSION, BALANCE, RANGE, TRANSITION, UNDEFINED.
(every ``MarketPhase`` member is produced by some rule; UNDEFINED = any needed fact is None or no rule applies.)

1. UNDEFINED   if m5/m15 structure, balance_state or volatility_context is None.
2. FAILED_BREAK exactly one side with: the M5 close broke the latest M5 swing high (low) within the last W bars
                (``bars_since_beyond_*`` <= W) and the close is back at/inside it (``close_beyond_last_swing_* == 0``).
                The direction of the failed break is NOT stored in the contract (see the report) - consumers get no direction.
3. REVERSAL_ATTEMPT  M15 sequence is a trend d and the close is beyond the latest M15 swing AGAINST d (a structural break of the
                M15 trend; swing labels will follow later, the break fact is already causal).
4. BREAKOUT     a fresh (<= W bars) M5 close beyond the latest M5 swing in direction d, the acceptance label of d is
                BROKEN/ACCEPTED/RETEST_HELD, and the M15 sequence is NOT already a d-trend (otherwise it is TREND continuation).
5. TREND        M15 and M5 sequences are the same trend sequence.   PULLBACK  M15 trend d with the M5 sequence = -d.
                M15 trend d with an M5 sequence that is neither d nor -d => TRANSITION (not clearly a pullback).
6. EXPANSION    volatility HIGH, balance DIRECTIONAL, M15 sequence not a trend.
7. COMPRESSION  balance BALANCE, volatility LOW, M15 sequence RANGE_OR_UNDEFINED / MIXED.
8. BALANCE      balance BALANCE (volatility not LOW), M15 sequence RANGE_OR_UNDEFINED / MIXED.
9. RANGE        M15 sequence RANGE_OR_UNDEFINED and balance MIXED.
10. TRANSITION  M15 sequence MIXED_TRANSITION (or the M15/M5 conflict of rule 5);   else UNDEFINED.

Versioning: ``MARKETMAP_VERSION`` + ``marketmap_definition_hash()`` (which includes the definition hashes of every observer group it
calls, the config and the rule text). Any change of a rule or threshold must bump ``MARKETMAP_VERSION``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from demo.structure import StructuralGeometry, structural_geometry
from market_observer import acceptance as A
from market_observer import balance as B
from market_observer import levels as L
from market_observer import participation as P
from market_observer import swings as S
from market_observer.observer import OBSERVER_CONFIG, ObserverConfig
from market_observer.schema import GROUP_VERSIONS, OBSERVER_VERSION, LevelRole, ObserverBars
from research_workbench.thesis.contracts import Direction, MarketMap, MarketPhase, Provenance

MARKETMAP_VERSION = "marketmap-1"
_NS = 1_000_000_000
_H1_NS = 3600 * _NS
_ADAPTER_SRC = "research_workbench.thesis.marketmap"

RULES_TEXT = (
    "phase:1 UNDEFINED if m5/m15/balance/vol None",
    "phase:2 FAILED_BREAK exactly one side: m5 close beyond latest m5 swing within W bars and back inside",
    "phase:3 REVERSAL_ATTEMPT m15 trend d and close beyond latest m15 swing against d",
    "phase:4 BREAKOUT fresh m5 close beyond swing d, acceptance_d in BROKEN/ACCEPTED/RETEST_HELD, m15 not d-trend",
    "phase:5 TREND m15==m5 trend; PULLBACK m15 trend d, m5 = -d; other m5 => TRANSITION",
    "phase:6 EXPANSION vol HIGH, balance DIRECTIONAL, m15 not trend",
    "phase:7 COMPRESSION balance BALANCE, vol LOW, m15 RANGE/MIXED",
    "phase:8 BALANCE balance BALANCE, m15 RANGE/MIXED",
    "phase:9 RANGE m15 RANGE_OR_UNDEFINED and balance MIXED",
    "phase:10 TRANSITION m15 MIXED_TRANSITION else UNDEFINED",
    "balance: BALANCE if eff<=eff_max & crossings>=min & overlap>=min; DIRECTIONAL if eff>=min & crossings<=max; else MIXED",
    "acceptance: None (no break) | RECLAIMED (held 0) | BROKEN (held<A) | ACCEPTED (held>=A) | RETEST_HELD (accepted, wick back to edge, no reclaim)",
    "h1: EMA fast/slow of closed complete UTC-epoch H1 buckets, neutral band in M5 ATR units",
    "volatility: atr[i]/median(atr[i-w+1..i]) vs low/high bounds",
    "participation: tick_activity_percentile vs low/high bounds (PROXY, not exchange volume)",
    "session: CASH if cash_open<=local_minute<cash_close else PRE_OPEN/POST_CLOSE",
    "support/resistance: nearest/second level below/above the close; zones likewise; role reversal = FLIPPED_* levels",
)


# ------------------------------------------------------------------------------------------------ config
@dataclass(frozen=True)
class MarketMapConfig:
    """Predeclared, NOT tuned. Changing any value changes ``marketmap_definition_hash``."""

    observer: ObserverConfig = field(default_factory=lambda: OBSERVER_CONFIG)
    event_window_bars: int = 12  # W: "fresh" M5 swing-break events
    balance_eff_max: float = 0.30
    balance_min_crossings: int = 3
    balance_min_overlap: float = 0.50
    directional_eff_min: float = 0.50
    directional_max_crossings: int = 1
    vol_window: int = 96
    vol_low_ratio: float = 0.75
    vol_high_ratio: float = 1.35
    participation_low_pct: float = 0.20
    participation_high_pct: float = 0.80
    h1_window_closes: int = 96
    h1_ema_fast: int = 8
    h1_ema_slow: int = 21
    h1_neutral_band_m5atr: float = 0.5
    geometry_lookback_bars: int = (
        480  # trailing M5 bars (current segment) handed to structural_geometry
    )

    def __post_init__(self) -> None:
        if (
            self.event_window_bars < 1
            or self.vol_window < 2
            or self.h1_ema_fast >= self.h1_ema_slow
        ):
            raise ValueError("invalid MarketMapConfig")
        if self.h1_window_closes < self.h1_ema_slow or self.geometry_lookback_bars < 5:
            raise ValueError("invalid MarketMapConfig")


MARKETMAP_CONFIG = MarketMapConfig()


def marketmap_definition_hash(config: MarketMapConfig | None = None) -> str:
    """Hash of (MARKETMAP_VERSION, rules, config, observer version, every observer group definition hash, group versions)."""
    cfg = config or MARKETMAP_CONFIG
    payload = {
        "marketmap_version": MARKETMAP_VERSION,
        "observer_version": OBSERVER_VERSION,
        "rules": list(RULES_TEXT),
        "config": asdict(cfg),
        "observer_hashes": cfg.observer.definition_hashes(),
        "group_versions": dict(GROUP_VERSIONS),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


# ------------------------------------------------------------------------------------------------ small adapters
def session_label(bars: ObserverBars, i: int) -> str | None:
    """CASH / PRE_OPEN / POST_CLOSE from the market's SessionSpec and the local minute of the bar OPEN; None without a cash open."""
    spec = bars.session
    if spec.cash_open_min is None:
        return None
    m = int(bars.local_minute[i])
    if m < spec.cash_open_min:
        return "PRE_OPEN"
    if spec.cash_close_min is not None and m >= spec.cash_close_min:
        return "POST_CLOSE"
    return "CASH"


def _ema(x: np.ndarray, span: int) -> float:
    a = 2.0 / (span + 1)
    e = float(x[0])
    for v in x[1:]:
        e = a * float(v) + (1 - a) * e
    return e


def h1_context(bars: ObserverBars, i: int, cfg: MarketMapConfig | None = None) -> str | None:
    """Minimal causal H1 trend adapter (the ONE field without an ObserverBars fact).

    H1 buckets are UTC-epoch aligned (like ``demo.opportunity.snapshot``), built from M5 bars <= i only. A bucket counts iff it is
    COMPLETE (all bars present, contiguous, one data segment) and CLOSED (its end <= decision_ts). Trend = EMA(fast) vs EMA(slow) of
    the last ``h1_window_closes`` closes (seeded with the first close); |diff| <= band * M5-ATR => NEUTRAL. None if < ``h1_ema_slow``
    buckets or ATR unknown.
    """
    cfg = cfg or MARKETMAP_CONFIG
    bar_ns = bars.bar_seconds * _NS
    if _H1_NS % bar_ns != 0:
        return None
    per_hour = _H1_NS // bar_ns
    atr = float(bars.atr[i])
    if not (np.isfinite(atr) and atr > 0):
        return None
    lo = max(0, i + 1 - per_hour * (cfg.h1_window_closes + 2))
    ts = bars.ts_ns[lo : i + 1]
    seg = bars.segment_id[lo : i + 1]
    c = bars.c[lo : i + 1]
    dts = bars.decision_ts_ns(i)
    bucket = ts // _H1_NS
    closes: list[float] = []
    uniq, first_idx, counts = np.unique(bucket, return_index=True, return_counts=True)
    for b, a, n in zip(uniq, first_idx, counts, strict=True):
        z = a + n - 1
        complete = (
            n == per_hour
            and int(ts[z] - ts[a]) == (per_hour - 1) * bar_ns
            and int(seg[a]) == int(seg[z])
        )
        if complete and (int(b) + 1) * _H1_NS <= dts:
            closes.append(float(c[z]))
    closes = closes[-cfg.h1_window_closes :]
    if len(closes) < cfg.h1_ema_slow or not np.all(np.isfinite(closes)):
        return None
    arr = np.asarray(closes, dtype=float)
    diff = _ema(arr, cfg.h1_ema_fast) - _ema(arr, cfg.h1_ema_slow)
    if abs(diff) <= cfg.h1_neutral_band_m5atr * atr:
        return "NEUTRAL"
    return "UP" if diff > 0 else "DOWN"


def volatility_label(bars: ObserverBars, i: int, cfg: MarketMapConfig | None = None) -> str | None:
    cfg = cfg or MARKETMAP_CONFIG
    if i + 1 < cfg.vol_window:
        return None
    win = bars.atr[i + 1 - cfg.vol_window : i + 1]
    if not np.all(np.isfinite(win)) or float(np.median(win)) <= 0:
        return None
    ratio = float(win[-1]) / float(np.median(win))
    if ratio <= cfg.vol_low_ratio:
        return "LOW"
    return "HIGH" if ratio >= cfg.vol_high_ratio else "NORMAL"


def balance_label(bars: ObserverBars, i: int, cfg: MarketMapConfig | None = None) -> str | None:
    cfg = cfg or MARKETMAP_CONFIG
    w = max(cfg.observer.balance.windows)
    v = B.balance_features(bars, i, cfg.observer.balance).values
    eff, cross, overlap = (
        v[f"directional_efficiency_w{w}"],
        v[f"midpoint_cross_count_w{w}"],
        v[f"bar_overlap_ratio_w{w}"],
    )
    if eff is None or cross is None or overlap is None:
        return None
    if (
        eff <= cfg.balance_eff_max
        and cross >= cfg.balance_min_crossings
        and overlap >= cfg.balance_min_overlap
    ):
        return "BALANCE"
    if eff >= cfg.directional_eff_min and cross <= cfg.directional_max_crossings:
        return "DIRECTIONAL"
    return "MIXED"


def participation_label(
    bars: ObserverBars, i: int, cfg: MarketMapConfig | None = None
) -> str | None:
    cfg = cfg or MARKETMAP_CONFIG
    pct = P.participation_features(bars, i, cfg.observer.participation).values[
        "tick_activity_percentile"
    ]
    if pct is None:
        return None
    if pct >= cfg.participation_high_pct:
        return "HIGH"
    return "LOW" if pct <= cfg.participation_low_pct else "NORMAL"


def acceptance_label(values: dict, accept_bars: int) -> str | None:
    """Frozen label of one ``acceptance_features`` row (vocabulary of the setup specs).

    None = not evaluable (warm-up / no level) OR no break found. ``held`` = trailing closes beyond the level edge (direction-relative).
    held == 0 after a break -> RECLAIMED; 0 < held < A -> BROKEN; held >= A (A = ``levels.accept_bars``) -> ACCEPTED, or RETEST_HELD if
    additionally a wick went back to the edge after the break bar (``max_reentry_depth_atr > 0``) without any close back through it.
    """
    if not values["break_found"]:
        return None
    held = values["time_held_beyond_level_bars"]
    if held is None:
        return None
    if held == 0:
        return "RECLAIMED"
    if held < accept_bars:
        return "BROKEN"
    depth = values["max_reentry_depth_atr"]
    if depth is not None and depth > 0 and not values["reclaim_occurred"]:
        return "RETEST_HELD"
    return "ACCEPTED"


# ------------------------------------------------------------------------------------------------ phase
_TREND_SEQ = {"UP_SEQUENCE": 1, "DOWN_SEQUENCE": -1}


def derive_phase(
    sw5: S.SwingStructureState | None,
    sw15: S.SwingStructureState | None,
    balance: str | None,
    vol: str | None,
    acc: dict[int, str | None],
    cfg: MarketMapConfig,
) -> MarketPhase:
    """The frozen first-match rules of the module docstring. ``acc`` maps +1 (LONG) / -1 (SHORT) to its acceptance label."""
    if sw5 is None or sw15 is None or balance is None or vol is None:
        return MarketPhase.UNDEFINED
    w = cfg.event_window_bars
    seq5, seq15 = sw5.sequence.value, sw15.sequence.value
    d15 = _TREND_SEQ.get(seq15, 0)
    d5 = _TREND_SEQ.get(seq5, 0)

    def fresh_beyond(dist: float | None, since: int | None, sign: int) -> bool:
        return dist is not None and since is not None and since <= w and sign * dist > 0

    def failed(dist: float | None, since: int | None) -> bool:
        return dist is not None and since is not None and since <= w and dist == 0.0

    failed_up = failed(sw5.close_beyond_last_swing_atr_high, sw5.bars_since_beyond_high)
    failed_dn = failed(sw5.close_beyond_last_swing_atr_low, sw5.bars_since_beyond_low)
    if failed_up != failed_dn:
        return MarketPhase.FAILED_BREAK
    if d15 != 0:
        against = (
            sw15.close_beyond_last_swing_atr_low
            if d15 > 0
            else sw15.close_beyond_last_swing_atr_high
        )
        if against is not None and against != 0.0:
            return MarketPhase.REVERSAL_ATTEMPT
    up_ev = fresh_beyond(sw5.close_beyond_last_swing_atr_high, sw5.bars_since_beyond_high, 1)
    dn_ev = fresh_beyond(sw5.close_beyond_last_swing_atr_low, sw5.bars_since_beyond_low, -1)
    if up_ev != dn_ev:
        d = 1 if up_ev else -1
        if acc.get(d) in ("BROKEN", "ACCEPTED", "RETEST_HELD") and d15 != d:
            return MarketPhase.BREAKOUT
    if d15 != 0:
        if d5 == d15:
            return MarketPhase.TREND
        if d5 == -d15:
            return MarketPhase.PULLBACK
        return MarketPhase.TRANSITION
    if vol == "HIGH" and balance == "DIRECTIONAL":
        return MarketPhase.EXPANSION
    flat15 = seq15 in ("RANGE_OR_UNDEFINED", "MIXED_TRANSITION")
    if balance == "BALANCE" and flat15:
        return MarketPhase.COMPRESSION if vol == "LOW" else MarketPhase.BALANCE
    if seq15 == "RANGE_OR_UNDEFINED" and balance == "MIXED":
        return MarketPhase.RANGE
    if seq15 == "MIXED_TRANSITION":
        return MarketPhase.TRANSITION
    return MarketPhase.UNDEFINED


# ------------------------------------------------------------------------------------------------ assembly
def _provenance(cfg: MarketMapConfig, mm_hash: str) -> dict[str, Provenance]:
    h = cfg.observer.definition_hashes()
    sw = Provenance("market_observer.swings.swing_state", GROUP_VERSIONS["swings"], h["swings"])
    lv_ctx = Provenance(
        "market_observer.levels.build_level_context", GROUP_VERSIONS["levels"], h["levels"]
    )
    return {
        "session": Provenance(f"{_ADAPTER_SRC}.session_label", MARKETMAP_VERSION, mm_hash),
        "market_phase": Provenance(f"{_ADAPTER_SRC}.derive_phase", MARKETMAP_VERSION, mm_hash),
        "h1_context": Provenance(f"{_ADAPTER_SRC}.h1_context", MARKETMAP_VERSION, mm_hash),
        "m15_structure": sw,
        "m5_structure": sw,
        "nearest_support": lv_ctx,
        "nearest_resistance": lv_ctx,
        "second_support": lv_ctx,
        "second_resistance": lv_ctx,
        "active_support_zone": lv_ctx,
        "active_resistance_zone": lv_ctx,
        "role_reversal_zones": lv_ctx,
        "balance_state": Provenance(
            "market_observer.balance.balance_features", GROUP_VERSIONS["balance"], h["balance"]
        ),
        "acceptance_state": Provenance(
            "market_observer.acceptance.acceptance_features",
            GROUP_VERSIONS["acceptance"],
            h["acceptance"],
        ),
        "participation_state": Provenance(
            "market_observer.participation.participation_features",
            GROUP_VERSIONS["participation"],
            h["participation"],
        ),
        "volatility_context": Provenance(
            f"{_ADAPTER_SRC}.volatility_label", MARKETMAP_VERSION, mm_hash
        ),
    }


def _side_prices(ctx: L.LevelContext, close: float, *, below: bool) -> list[float | None]:
    """[nearest, second] level price on one side of the close; the second is the nearest level in a DIFFERENT zone cluster (never a same-price duplicate)."""
    zone_of = {lid: z.zone_id for z in ctx.zones for lid in z.level_ids}
    side = [s for s in ctx.levels if (s.price < close if below else s.price > close)]
    side.sort(key=lambda s: (abs(s.price - close), s.level_id))
    out: list[float | None] = []
    seen: set[str] = set()
    for s in side:
        zid = zone_of[s.level_id]
        if zid not in seen:
            seen.add(zid)
            out.append(s.price)
    return [*out[:2], None, None][:2]


def _active_zones(
    bars: ObserverBars, i: int, ctx: L.LevelContext
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """(support zone, resistance zone) price is AT or INSIDE at the decision bar (wick touch within the cluster tolerance counts)."""
    close, lo, hi, tol = float(bars.c[i]), float(bars.l[i]), float(bars.h[i]), ctx.cluster_tol
    sup = [z for z in ctx.zones if z.zone_low <= close and lo <= z.zone_high + tol]
    res = [z for z in ctx.zones if close <= z.zone_high and hi >= z.zone_low - tol]

    def pick(zs: list[L.Zone]) -> tuple[float, float] | None:
        if not zs:
            return None
        z = min(zs, key=lambda q: (abs(q.zone_center - close), q.zone_id))
        return (z.zone_low, z.zone_high)

    return pick(sup), pick(res)


def _assemble(
    bars: ObserverBars,
    i: int,
    ctx: L.LevelContext | None,
    cfg: MarketMapConfig,
    code_sha: str,
) -> MarketMap:
    obs = cfg.observer
    dts = bars.decision_ts_ns(i)
    swings_ok = S.history_sufficient(i)
    sw5 = S.swing_state(bars, i, "M5") if swings_ok else None
    sw15 = S.swing_state(bars, i, "M15") if swings_ok else None
    bal = balance_label(bars, i, cfg)
    vol = volatility_label(bars, i, cfg)

    support = resistance = (None, None)
    zone_s = zone_r = None
    reversal: tuple[tuple[float, float], ...] = ()
    acc_state: dict[str, str | None] = {}
    acc_by_dir: dict[int, str | None] = {}
    if ctx is not None:
        if ctx.index != i:
            raise ValueError(f"level context is for bar {ctx.index}, not {i}")
        below = _side_prices(ctx, float(bars.c[i]), below=True)
        above = _side_prices(ctx, float(bars.c[i]), below=False)
        support, resistance = (below[0], below[1]), (above[0], above[1])
        zone_s, zone_r = _active_zones(bars, i, ctx)
        flipped = {LevelRole.FLIPPED_TO_SUPPORT, LevelRole.FLIPPED_TO_RESISTANCE}
        reversal = tuple(
            sorted({(lv.zone_low, lv.zone_high) for lv in ctx.levels if lv.current_role in flipped})
        )
        close = float(bars.c[i])
        for direction, name in ((1, "LONG"), (-1, "SHORT")):
            ref = L.reference_level(ctx, direction, close)
            if ref is None:
                continue
            vals = A.acceptance_features(bars, i, ref, direction, obs.acceptance).values
            label = acceptance_label(vals, obs.levels.accept_bars)
            acc_state[f"{name}:{ref.level_id}"] = label
            acc_by_dir[direction] = label

    phase = derive_phase(sw5, sw15, bal, vol, acc_by_dir, cfg)
    mm_hash = marketmap_definition_hash(cfg)
    return MarketMap(
        market=bars.market,
        decision_ts_ns=dts,
        bar_index=i,
        session=session_label(bars, i),
        market_phase=phase,
        h1_context=h1_context(bars, i, cfg),
        m15_structure=None if sw15 is None else sw15.sequence.value,
        m5_structure=None if sw5 is None else sw5.sequence.value,
        nearest_support=support[0],
        nearest_resistance=resistance[0],
        second_support=support[1],
        second_resistance=resistance[1],
        active_support_zone=zone_s,
        active_resistance_zone=zone_r,
        role_reversal_zones=reversal,
        balance_state=bal,
        acceptance_state=dict(sorted(acc_state.items())),
        participation_state=participation_label(bars, i, cfg),
        volatility_context=vol,
        provenance=_provenance(cfg, mm_hash),
        marketmap_version=MARKETMAP_VERSION,
        observer_version=OBSERVER_VERSION,
        definition_hashes={**obs.definition_hashes(), "marketmap": mm_hash},
        code_sha=code_sha,
    )


def _levels_warm(i: int, cfg: MarketMapConfig) -> bool:
    return i + 1 >= L.min_history_bars(cfg.observer.levels)


def build_market_map(
    bars: ObserverBars, i: int, config: MarketMapConfig | None = None, *, code_sha: str = "-"
) -> MarketMap:
    """REFERENCE builder: pure, deterministic, causal (reads bars <= i; the level registry is replayed from bar 0).

    Warm-up gaps are ``None`` fields (never exceptions). Cost is O(i) per call; use :class:`MarketMapReplay` for sequential replays.
    """
    cfg = config or MARKETMAP_CONFIG
    if not 0 <= i < len(bars):
        raise IndexError(i)
    ctx = L.build_level_context(bars, i, cfg.observer.levels) if _levels_warm(i, cfg) else None
    return _assemble(bars, i, ctx, cfg, code_sha)


class MarketMapReplay:
    """Incremental builder for sequential replays (``LevelRegistry`` advanced once per bar). Equals :func:`build_market_map` (tested)."""

    def __init__(self, config: MarketMapConfig | None = None, *, code_sha: str = "-") -> None:
        self.config = config or MARKETMAP_CONFIG
        self.code_sha = code_sha
        self._reg = L.LevelRegistry(self.config.observer.levels)

    @property
    def bars_seen(self) -> int:
        return self._reg._n

    def advance(self, bars: ObserverBars, i: int) -> None:
        """Advance the level registry through bar ``i`` (must be the next bar, starting at 0) WITHOUT assembling a map (cheap warm-up)."""
        self._reg.update(bars, i, build_context=False)

    def step(self, bars: ObserverBars, i: int) -> MarketMap:
        """Advance through bar ``i`` (must be the next bar, starting at 0) and return its map."""
        self.advance(bars, i)
        ctx = self._reg.context(bars) if _levels_warm(i, self.config) else None
        return _assemble(bars, i, ctx, self.config, self.code_sha)


# ------------------------------------------------------------------------------------------------ geometry (proposed entry only)
def prefix_frame(
    bars: ObserverBars, i: int, lookback: int = MARKETMAP_CONFIG.geometry_lookback_bars
) -> pd.DataFrame:
    """OHLC frame of the CURRENT SEGMENT through bar ``i`` only (at most ``lookback`` bars): ts (UTC bar open), open/high/low/close."""
    seg = bars.segment_id
    seg_start = int(np.searchsorted(seg[: i + 1], seg[i], side="left"))
    lo = max(seg_start, i + 1 - lookback)
    sl = slice(lo, i + 1)
    return pd.DataFrame(
        {
            "ts": pd.to_datetime(bars.ts_ns[sl], utc=True),
            "open": bars.o[sl].astype(float),
            "high": bars.h[sl].astype(float),
            "low": bars.l[sl].astype(float),
            "close": bars.c[sl].astype(float),
        }
    )


def geometry_for_entry(
    bars: ObserverBars,
    i: int,
    direction: Direction,
    entry_price: float,
    *,
    cost: float | None = None,
    config: MarketMapConfig | None = None,
) -> StructuralGeometry | None:
    """``demo.structure.structural_geometry`` for a PROPOSED entry at decision bar ``i``, on a PREFIX-ONLY frame (bars <= i).

    ``spread`` / ``atr`` are the values of bar ``i`` (ATR None if unknown), ``tick_size`` from the bars. Returns ``None`` if the
    index/entry is invalid or no bar is available. Insufficient structure still returns the geometry with its markers (``valid`` False).
    """
    cfg = config or MARKETMAP_CONFIG
    if not 0 <= i < len(bars) or not np.isfinite(entry_price):
        return None
    frame = prefix_frame(bars, i, cfg.geometry_lookback_bars)
    if len(frame) == 0:
        return None
    spread = float(bars.spread[i])
    atr = float(bars.atr[i])
    return structural_geometry(
        direction.sign,
        float(entry_price),
        frame,
        spread if np.isfinite(spread) else 0.0,
        atr if np.isfinite(atr) and atr > 0 else None,
        cost=cost,
        tick_size=bars.tick_size,
    )


__all__ = [
    "MARKETMAP_CONFIG",
    "MARKETMAP_VERSION",
    "MarketMapConfig",
    "MarketMapReplay",
    "acceptance_label",
    "balance_label",
    "build_market_map",
    "derive_phase",
    "geometry_for_entry",
    "h1_context",
    "marketmap_definition_hash",
    "participation_label",
    "prefix_frame",
    "session_label",
    "volatility_label",
]
