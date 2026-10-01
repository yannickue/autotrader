# ruff: noqa: E501
"""Swing market-structure STATE (group ``swings``; OBSERVATION_ONLY / NOT_ALPHA_VALIDATED; never an entry filter).

Pure, causal, prefix-invariant functions of ``(ObserverBars, i)``. The ONLY swing detector is the repo's
``demo.structure.confirmed_swings`` (fractal, ``SWING_N`` bars on each side). Nothing here scores, weights or names ICT/SMC concepts.

Definitions (any change => bump ``GROUP_VERSIONS['swings']`` and re-pin ``test_constants_and_definition_hash_are_pinned``):

* Window: the state at bar ``i`` is defined on the trailing ``LOOKBACK_BARS`` (480) M5 bars of the CURRENT SEGMENT (``bars.segment_id`` of bar
  ``i``) ending at ``i``: the sequence restarts after a data break, no label compares swings across a break, and nothing older than the window
  influences the result. Consequence (tested): the result at ``i`` is EXACTLY independent of how much history is loaded before ``i`` as soon as
  at least ``MIN_HISTORY_BARS`` (= LOOKBACK_BARS) bars are loaded. With less loaded history the loaded start acts as the window start: a swing
  whose predecessor of the same kind lies before it gets label None (unknown, never a guess), ``sequence`` falls back to RANGE_OR_UNDEFINED
  and ``sequence_length`` is a lower bound; every non-None value that remains equals the full-history value. A swing is a candidate only if its
  fractal (n bars each side) lies completely inside the window.
* Swings: built from the closed M5 bars of that window. M15 swings use ``demo.structure.resample_m15`` on the same slice (complete,
  contiguous three-bar groups only; assumes the segment contract that bars inside a segment are contiguous). A forming M15 bar is never used.
* Usability time: an M5 swing at bar ``j`` is usable from the close of bar ``j + SWING_N`` (the decision close of that bar). An M15 swing at
  M15 bar ``k`` is usable from the close of M15 bar ``k + SWING_N`` = (open of M15 bar ``k``) + (SWING_N + 1) * 900 s, i.e. from the decision close
  of the last M5 bar of that M15 bar. ``confirmed_at_ts_ns`` of a swing is exactly that instant.
* Labels: swing ``s`` of kind HIGH is compared with the previous HIGH of the window (LOW likewise): |diff| <= tol => EQ; else HIGH: HH/LH,
  LOW: HL/LL. ``tol = max(EQ_TOL_ATR * ATR, tick_size)`` with the M5 ATR of the bar whose close confirmed ``s``. A label is therefore fixed at
  confirmation time and never changes afterwards. First swing of a kind in the window => label None.
* ``SwingSequence`` (from the latest HIGH label and latest LOW label): UP_SEQUENCE = HH+HL; DOWN_SEQUENCE = LH+LL; MIXED_TRANSITION = one up-type
  and one down-type label (HH+LL or LH+HL); RANGE_OR_UNDEFINED = a label missing (fewer than two swings of a kind) or any EQ.
* ``sequence_length``: number of consecutive labelled swing confirmations, newest first, whose label points in the direction of the current
  UP/DOWN sequence (HH/HL = up, LH/LL = down); stops at the first other label, EQ or unlabelled swing. Swings confirmed at the same close are
  evaluated together (order-free, keeps long/short symmetry): if any of them is not in the sequence direction the run stops BEFORE that group.
  0 for MIXED_TRANSITION / RANGE_OR_UNDEFINED.
* ``structure_age``: time from the newest confirmation (either kind) to the decision close, in bars of the timeframe (floor) and in minutes.
* ``high_delta_atr`` / ``low_delta_atr``: (latest - previous swing of that kind) / ATR at the decision bar. All ATR values are the M5 ATR of
  ``ObserverBars.atr`` (also for M15 swings).
* ``close_beyond_last_swing_atr_high``: (decision close - latest swing high) / ATR if the close is above it, else 0.0;
  ``close_beyond_last_swing_atr_low``: (decision close - latest swing low) / ATR if the close is below it (negative), else 0.0.
  ``bars_since_beyond_high/low``: M5 bars since the FIRST close beyond that swing after its confirmation (None if none yet).
* EMA trend diagnostic (kept SEPARATE from the swing sequence): identical to the repo's ``demo.opportunity.snapshot`` M5 ``trend``: EMA(8) vs
  EMA(21) of the last ``EMA_WINDOW`` (200) closes, recursion seeded with the first close of the window (alpha = 2 / (span + 1)), None when
  fewer than ``EMA_SLOW`` closes; "up" / "down" / "flat". Not segment-aware (parity with the repo diagnostic). The EMA is a finite-memory
  recursion by this definition: exactly independent of history once ``EMA_WINDOW`` closes are loaded; with 21..199 closes it differs from the
  200-close value by the seed influence (decays like (1-2/22)^k). Versus an infinite-memory ``alpha.common.frame.Frame.ema`` the difference is
  <= ~6e-8 x seed gap in price units (tested, tolerance 1e-6).
* ATR: ``ObserverBars.atr`` is supplied by the adapter. If it is a Wilder (infinite-memory) recursion, ATR-normalised deltas inherit its warm-up
  error, which decays like (1-1/14)^k; the supplier must therefore compute it on >= ``MIN_HISTORY_BARS`` of history (tested).
* Detector: ``demo.structure.confirmed_swings`` is the default; ``detector=`` accepts any callable ``(frame, n, timeframe) -> list[Swing]`` that
  honours the same causality contract (usable at ``confirmed_at``, n bars each side). No other detector exists here.
* ``ema_swing_agreement``: M5 swing sequence vs EMA trend, descriptive only.
* Warm-up: if the ATR at the decision bar is NaN or <= 0 the state is None (all feature values None).
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd

from demo.structure import M5_SECONDS, M15_SECONDS, Swing, confirmed_swings, resample_m15
from market_observer.schema import (
    GROUP_VERSIONS,
    FeatureResult,
    JsonScalar,
    ObserverBars,
    SwingLabel,
    SwingSequence,
)

GROUP = "swings"
SWING_N = 2
EQ_TOL_ATR = 0.05  # fraction of the (confirmation-bar) ATR; floored at one tick
EMA_FAST = 8
EMA_SLOW = 21
EMA_WINDOW = 200
LOOKBACK_BARS = 480  # trailing M5 window (current segment) the state is defined on
MIN_HISTORY_BARS = max(LOOKBACK_BARS, EMA_WINDOW)  # loaded history needed for exact history-independence
TIMEFRAMES = ("M5", "M15")
_NS = 1_000_000_000
_TF_SECONDS = {"M5": M5_SECONDS, "M15": M15_SECONDS}

SwingDetector = Callable[[pd.DataFrame, int, str], list[Swing]]  # (frame, n, timeframe) -> swings; default demo.structure.confirmed_swings

DEFINITION = {
    "group": GROUP, "version": GROUP_VERSIONS[GROUP], "detector": "demo.structure.confirmed_swings", "swing_n": SWING_N, "lookback_bars": LOOKBACK_BARS,
    "eq_tol": f"max({EQ_TOL_ATR}*atr_at_confirmation_bar, tick_size)", "m15": "demo.structure.resample_m15 on the window slice",
    "usable_from": "close of bar j+n (own timeframe)", "segment_reset": True, "labels": "HH/LH/HL/LL/EQ vs previous swing of same kind in window",
    "sequence": "UP=HH+HL;DOWN=LH+LL;MIXED=one up one down;RANGE=missing or EQ", "sequence_length": "consecutive same-direction labelled confirmations; same-close swings evaluated as a group",
    "age": "decision close - newest confirmation", "deltas": "(latest-previous)/atr_decision", "beyond": "signed atr distance of close beyond last swing, 0 if not; first close beyond",
    "ema": [EMA_FAST, EMA_SLOW, EMA_WINDOW, "seeded with window first close, none if < slow", "up/down/flat"],
    "agreement": "m5 sequence vs ema trend",
}


def definition_hash() -> str:
    return hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, default=str).encode()).hexdigest()[:16]


class EmaSwingAgreement(StrEnum):
    AGREE_UP = "AGREE_UP"
    AGREE_DOWN = "AGREE_DOWN"
    DISAGREE = "DISAGREE"
    UNDEFINED = "UNDEFINED"


@dataclass(frozen=True)
class SwingStructureState:
    timeframe: str
    decision_ts_ns: int
    last_high_price: float | None
    prev_high_price: float | None
    last_low_price: float | None
    prev_low_price: float | None
    last_high_confirmed_at_ts_ns: int | None
    last_low_confirmed_at_ts_ns: int | None
    high_label: SwingLabel | None
    low_label: SwingLabel | None
    sequence: SwingSequence
    sequence_length: int
    structure_age_bars: int | None
    structure_age_minutes: float | None
    high_delta_atr: float | None
    low_delta_atr: float | None
    confirmed_at_ts_ns: int | None  # newest confirmation of either kind
    close_beyond_last_swing_atr_high: float | None
    close_beyond_last_swing_atr_low: float | None
    bars_since_beyond_high: int | None
    bars_since_beyond_low: int | None


@dataclass(frozen=True)
class _Sw:
    kind: int  # +1 high, -1 low
    price: float
    confirmed_ns: int
    open_ns: int  # open of the swing bar (own timeframe)


# ---------------------------------------------------------------------------------------------- swings of one slice
def _check(bars: ObserverBars) -> None:
    if bars.bar_seconds != M5_SECONDS:
        raise ValueError("swings group requires M5 bars (bar_seconds == 300)")


def _segment_start(bars: ObserverBars, i: int) -> int:
    seg = bars.segment_id
    return int(np.searchsorted(seg[: i + 1], seg[i], side="left"))


def _window_start(bars: ObserverBars, i: int) -> int:
    return max(_segment_start(bars, i), i + 1 - LOOKBACK_BARS)


def _slice_swings(bars: ObserverBars, s: int, e: int, tf: str, detector: SwingDetector) -> list[_Sw]:
    frame = pd.DataFrame({
        "ts": pd.to_datetime(bars.ts_ns[s:e], unit="ns", utc=True), "open": bars.o[s:e], "high": bars.h[s:e], "low": bars.l[s:e], "close": bars.c[s:e],
    })
    if tf == "M15":
        frame = resample_m15(frame)
    return [
        _Sw(1 if x.kind == "HIGH" else -1, float(x.price), int(pd.Timestamp(x.confirmed_at).value), int(pd.Timestamp(x.bar_open).value))
        for x in detector(frame, SWING_N, tf)
    ]


def _lower_bound_open_ns(bars: ObserverBars, w0: int, i: int, tf: str) -> int:
    """Earliest swing-bar open that has n complete bars of its own timeframe to its left inside the window starting at M5 bar ``w0``."""
    if tf == "M5":
        return int(bars.ts_ns[w0 + SWING_N]) if w0 + SWING_N <= i else 2**62
    g = _TF_SECONDS["M15"] * _NS
    g0 = -(-int(bars.ts_ns[w0]) // g) * g  # first complete M15 group of the window starts at the next 15-minute grid point
    return g0 + SWING_N * g


def _bar_index_at_close(bars: ObserverBars, close_ns: int) -> int | None:
    """Index of the M5 bar whose CLOSE is ``close_ns`` (None if there is none)."""
    j = int(np.searchsorted(bars.ts_ns, close_ns - bars.bar_seconds * _NS, side="left"))
    if j < len(bars) and int(bars.ts_ns[j]) + bars.bar_seconds * _NS == close_ns:
        return j
    return None


def _label_swings(bars: ObserverBars, swings: list[_Sw]) -> tuple[list[SwingLabel | None], list[int | None]]:
    """Label of every swing vs its same-kind predecessor, and the predecessor's bar open (None for the first of a kind)."""
    labels: list[SwingLabel | None] = []
    prev_open: list[int | None] = []
    last: dict[int, _Sw] = {}
    for sw in swings:
        prev = last.get(sw.kind)
        last[sw.kind] = sw
        prev_open.append(None if prev is None else prev.open_ns)
        j = _bar_index_at_close(bars, sw.confirmed_ns)
        if prev is None or j is None:
            labels.append(None)
            continue
        atr = float(bars.atr[j])
        if not (np.isfinite(atr) and atr > 0):
            labels.append(None)
            continue
        tol = max(EQ_TOL_ATR * atr, float(bars.tick_size))
        d = sw.price - prev.price
        if abs(d) <= tol * (1.0 + 1e-9):
            labels.append(SwingLabel.EQ)
        elif sw.kind > 0:
            labels.append(SwingLabel.HH if d > 0 else SwingLabel.LH)
        else:
            labels.append(SwingLabel.HL if d > 0 else SwingLabel.LL)
    return labels, prev_open


def _dir(label: SwingLabel | None) -> int:
    if label in (SwingLabel.HH, SwingLabel.HL):
        return 1
    if label in (SwingLabel.LH, SwingLabel.LL):
        return -1
    return 0


def _classify(hl: SwingLabel | None, ll: SwingLabel | None) -> SwingSequence:
    if hl is None or ll is None or hl == SwingLabel.EQ or ll == SwingLabel.EQ:
        return SwingSequence.RANGE_OR_UNDEFINED
    up, dn = _dir(hl), _dir(ll)
    if up > 0 and dn > 0:
        return SwingSequence.UP_SEQUENCE
    if up < 0 and dn < 0:
        return SwingSequence.DOWN_SEQUENCE
    return SwingSequence.MIXED_TRANSITION


def _beyond(bars: ObserverBars, i: int, atr: float, level: float, conf_ns: int, high: bool) -> tuple[float, int | None]:
    c = float(bars.c[i])
    dist = (c - level) / atr if (c > level if high else c < level) else 0.0
    m = _bar_index_at_close(bars, conf_ns)
    if m is None or m >= i:
        return dist, None
    closes = bars.c[m + 1 : i + 1]
    hit = closes > level if high else closes < level
    if not bool(hit.any()):
        return dist, None
    return dist, i - (m + 1 + int(np.argmax(hit)))


def _build(
    bars: ObserverBars, i: int, tf: str, swings: list[_Sw], labels: list[SwingLabel | None], prev_open: list[int | None], lo: int, k: int, lb: int,
) -> SwingStructureState | None:
    """State from swings[lo:k] (window members); a label whose predecessor lies before ``lb`` (the window's first admissible swing bar) is None."""
    atr = float(bars.atr[i])
    if not (np.isfinite(atr) and atr > 0):
        return None
    dec = bars.decision_ts_ns(i)

    def lab(idx: int) -> SwingLabel | None:
        p = prev_open[idx]
        return labels[idx] if p is not None and p >= lb else None

    found: dict[tuple[int, int], int] = {}
    for idx in range(k - 1, lo - 1, -1):
        kind = swings[idx].kind
        slot = (kind, 0) if (kind, 0) not in found else (kind, 1)
        if slot not in found:
            found[slot] = idx
        if len(found) == 4:
            break
    lh, ph, ll, pl = found.get((1, 0)), found.get((1, 1)), found.get((-1, 0)), found.get((-1, 1))
    hlab = lab(lh) if lh is not None else None
    llab = lab(ll) if ll is not None else None
    seq = _classify(hlab, llab)
    length = 0
    if seq in (SwingSequence.UP_SEQUENCE, SwingSequence.DOWN_SEQUENCE):
        want = 1 if seq == SwingSequence.UP_SEQUENCE else -1
        idx = k - 1
        while idx >= lo:
            t = swings[idx].confirmed_ns
            grp = [idx]
            while idx - 1 >= lo and swings[idx - 1].confirmed_ns == t:
                idx -= 1
                grp.append(idx)
            if any(_dir(lab(g)) != want for g in grp):
                break
            length += len(grp)
            idx -= 1
    conf = swings[k - 1].confirmed_ns if k > lo else None
    age_bars = age_min = None
    if conf is not None:
        age_bars = int((dec - conf) // (_TF_SECONDS[tf] * _NS))
        age_min = (dec - conf) / (60 * _NS)
    hd = None if lh is None or ph is None else (swings[lh].price - swings[ph].price) / atr
    ld = None if ll is None or pl is None else (swings[ll].price - swings[pl].price) / atr
    bh = bl = None
    sbh = sbl = None
    if lh is not None:
        bh, sbh = _beyond(bars, i, atr, swings[lh].price, swings[lh].confirmed_ns, True)
    if ll is not None:
        bl, sbl = _beyond(bars, i, atr, swings[ll].price, swings[ll].confirmed_ns, False)
    return SwingStructureState(
        tf, dec,
        None if lh is None else swings[lh].price, None if ph is None else swings[ph].price,
        None if ll is None else swings[ll].price, None if pl is None else swings[pl].price,
        None if lh is None else swings[lh].confirmed_ns, None if ll is None else swings[ll].confirmed_ns,
        hlab, llab, seq, length, age_bars, age_min, hd, ld, conf, bh, bl, sbh, sbl,
    )


# ---------------------------------------------------------------------------------------------- public API
def history_sufficient(i: int) -> bool:
    """True if ``i + 1`` loaded bars reach ``MIN_HISTORY_BARS`` (state is then exactly independent of the loaded history length)."""
    return i + 1 >= MIN_HISTORY_BARS


def swing_state(bars: ObserverBars, i: int, timeframe: str, detector: SwingDetector = confirmed_swings) -> SwingStructureState | None:
    """Reference (functional replay): rebuilds the window slice on every call. None during ATR warm-up."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    _check(bars)
    w0 = _window_start(bars, i)
    swings = _slice_swings(bars, w0, i + 1, timeframe, detector)
    labels, prev_open = _label_swings(bars, swings)
    return _build(bars, i, timeframe, swings, labels, prev_open, 0, len(swings), -1)


class SwingReplay:
    """TEST-ONLY HELPER (not used by any ``src`` module; a static test forbids importing it from ``src``; not exported from ``market_observer``).

    Incremental variant for OFFLINE sweeps over a finished bar array: the swings of each SEGMENT are computed ONCE over the WHOLE segment, i.e. also
    over bars AFTER the evaluated bar ``i``, and cached by ``(timeframe, segment start)``; the result at ``i`` is then windowed / filtered by confirmation
    time. It equals ``swing_state`` on a full array (that equality is what the swing tests pin), but it is a LATENT FUTURE-LEAK HAZARD in any live or
    growing-array context (the cache would be computed from an incomplete segment and never refreshed, and it reads bars beyond ``i``). The live observer
    uses the pure ``swing_features(bars, i)`` instead. Do not use this class outside tests; numerical behaviour is deliberately unchanged."""

    def __init__(self, bars: ObserverBars, detector: SwingDetector = confirmed_swings) -> None:
        _check(bars)
        self.bars = bars
        self.detector = detector
        self._cache: dict[tuple[str, int], tuple[list[_Sw], list[int], list[int], list[SwingLabel | None], list[int | None]]] = {}

    def state(self, i: int, timeframe: str) -> SwingStructureState | None:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {timeframe!r}")
        b = self.bars
        seg = b.segment_id
        s = int(np.searchsorted(seg, seg[i], side="left"))
        key = (timeframe, s)
        if key not in self._cache:
            e = int(np.searchsorted(seg, seg[i], side="right"))
            sw = _slice_swings(b, s, e, timeframe, self.detector)
            labels, prev_open = _label_swings(b, sw)
            self._cache[key] = (sw, [x.confirmed_ns for x in sw], [x.open_ns for x in sw], labels, prev_open)
        sw, conf, opens, labels, prev_open = self._cache[key]
        w0 = max(s, i + 1 - LOOKBACK_BARS)
        lb = _lower_bound_open_ns(b, w0, i, timeframe)
        k = bisect_right(conf, b.decision_ts_ns(i))
        lo = min(bisect_left(opens, lb), k)
        return _build(b, i, timeframe, sw, labels, prev_open, lo, k, lb)

    def features(self, i: int) -> FeatureResult:
        return _features(self.bars, i, lambda tf: self.state(i, tf))


# ---------------------------------------------------------------------------------------------- EMA trend diagnostic
def _ema(x: np.ndarray, span: int) -> float | None:
    if len(x) < span:
        return None
    a = 2.0 / (span + 1)
    e = float(x[0])
    for v in x[1:]:
        e = a * float(v) + (1 - a) * e
    return e


def ema_values(bars: ObserverBars, i: int) -> tuple[float, float] | None:
    c = bars.c[max(0, i + 1 - EMA_WINDOW) : i + 1]
    fast, slow = _ema(c, EMA_FAST), _ema(c, EMA_SLOW)
    if fast is None or slow is None:
        return None
    return fast, slow


def ema_trend_state(bars: ObserverBars, i: int) -> str | None:
    v = ema_values(bars, i)
    if v is None:
        return None
    return "up" if v[0] > v[1] else "down" if v[0] < v[1] else "flat"


def ema_swing_agreement(ema_trend: str | None, sequence: SwingSequence | None) -> EmaSwingAgreement:
    if ema_trend == "up" and sequence == SwingSequence.UP_SEQUENCE:
        return EmaSwingAgreement.AGREE_UP
    if ema_trend == "down" and sequence == SwingSequence.DOWN_SEQUENCE:
        return EmaSwingAgreement.AGREE_DOWN
    if (ema_trend == "up" and sequence == SwingSequence.DOWN_SEQUENCE) or (ema_trend == "down" and sequence == SwingSequence.UP_SEQUENCE):
        return EmaSwingAgreement.DISAGREE
    return EmaSwingAgreement.UNDEFINED


# ---------------------------------------------------------------------------------------------- features
_STATE_FIELDS = (
    "sequence", "high_label", "low_label", "sequence_length", "structure_age_bars", "structure_age_minutes", "high_delta_atr", "low_delta_atr",
    "confirmed_at_ts_ns", "last_high_confirmed_at_ts_ns", "last_low_confirmed_at_ts_ns", "close_beyond_last_swing_atr_high",
    "close_beyond_last_swing_atr_low", "bars_since_beyond_high", "bars_since_beyond_low",
)


def _features(bars: ObserverBars, i: int, state_fn: Callable[[str], SwingStructureState | None]) -> FeatureResult:
    values: dict[str, JsonScalar] = {}
    states: dict[str, SwingStructureState | None] = {}
    for tf in TIMEFRAMES:
        st = state_fn(tf)
        states[tf] = st
        p = tf.lower()
        for name in _STATE_FIELDS:
            v = None if st is None else getattr(st, name)
            if isinstance(v, StrEnum):
                v = v.value
            values[f"{p}_{name}"] = v
    ema = ema_trend_state(bars, i)
    values["ema_trend"] = ema
    m5 = states["M5"]
    values["ema_swing_agreement"] = None if m5 is None else ema_swing_agreement(ema, m5.sequence).value
    return FeatureResult(GROUP, GROUP_VERSIONS[GROUP], values)


def swing_features(bars: ObserverBars, i: int, detector: SwingDetector = confirmed_swings) -> FeatureResult:
    """Group ``swings`` at decision bar ``i`` (reference implementation)."""
    _check(bars)
    return _features(bars, i, lambda tf: swing_state(bars, i, tf, detector))
