# ruff: noqa: E501
"""Lane E2: pure, deterministic, CAUSAL chart-structure functions over CLOSED M5 (and derived M15) bars.

Chart first, R second: the initial invalidation stop and the take-profit targets come from the chart
(confirmed swings, prior range edges); R is only ever a RESULT computed from those prices afterwards.
No level is invented from an arbitrary R multiple; ATR is only a buffer / a scale for minimum gaps.

Causality (no lookahead): every function sees only the bars it is handed and treats all of them as
closed. A fractal swing at bar ``i`` needs ``n`` bars on each side, so it is usable only from the close
of bar ``i + n`` (its ``confirmed_at``): truncating future bars can only REMOVE not-yet-confirmed
swings, never change a confirmed one. Every level carries ``source`` / ``structure_id`` /
``time_created`` (open of the swing bar) and ``confirmed_at`` (close of the confirming bar).

Frame schema: the V1 bar frame (``ts`` = tz-aware UTC bar OPEN, ``open, high, low, close``; BID prices).
Shorts stop on the ASK, so a short stop/trail adds the current ``spread`` on top of the BID-based level.
No IO, no MT5, no randomness, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import pandas as pd

from exits.models import SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED

M5_SECONDS = 300
M15_SECONDS = 900

NO_STRUCTURAL_STOP = "NO_STRUCTURAL_STOP"
STOP_TOO_FAR = "STRUCTURAL_STOP_TOO_FAR"
NO_STRUCTURAL_TP1 = "NO_STRUCTURAL_TP1"
ATR_UNAVAILABLE = "ATR_UNAVAILABLE"
INSUFFICIENT_BARS = "INSUFFICIENT_BARS"

D = Decimal


def _dec(x: float | int | Decimal | str) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclass(frozen=True, slots=True)
class Swing:
    kind: str  # "HIGH" | "LOW"
    timeframe: str  # "M5" | "M15"
    price: float
    bar_open: datetime  # time_created: open of the swing bar
    confirmed_at: datetime  # close of the bar that completed the fractal (usable from here)
    structure_id: str


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    source: str  # "SWING:M5" | "SWING:M15" | "RANGE:M5"
    structure_id: str
    time_created: datetime
    confirmed_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": str(self.price), "source": self.source, "structure_id": self.structure_id,
            "time_created": self.time_created.isoformat(), "confirmed_at": self.confirmed_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class StructuralGeometry:
    direction: int
    entry: Decimal
    stop: Decimal | None
    stop_level: Level | None
    stop_buffer: Decimal
    tp1: Level | None
    tp2: Level | None
    markers: tuple[str, ...]
    as_of: datetime | None  # close of the newest bar that was used (the confirmation clock)

    @property
    def valid(self) -> bool:
        return self.stop is not None and self.tp1 is not None

    def risk_reward(self) -> dict[str, str | None]:
        """R of each target as a RESULT of the chart prices (never an input)."""
        if self.stop is None or self.stop == self.entry:
            return {"tp1_r": None, "tp2_r": None}
        risk = abs(self.entry - self.stop)
        return {
            "tp1_r": None if self.tp1 is None else str(abs(self.tp1.price - self.entry) / risk),
            "tp2_r": None if self.tp2 is None else str(abs(self.tp2.price - self.entry) / risk),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction, "entry": str(self.entry),
            "stop": None if self.stop is None else str(self.stop),
            "stop_level": None if self.stop_level is None else self.stop_level.as_dict(),
            "stop_buffer": str(self.stop_buffer),
            "tp1": None if self.tp1 is None else self.tp1.as_dict(),
            "tp2": None if self.tp2 is None else self.tp2.as_dict(),
            "markers": list(self.markers),
            "as_of": None if self.as_of is None else self.as_of.isoformat(),
            **self.risk_reward(),
        }


@dataclass(frozen=True, slots=True)
class ManagementSignals:
    """Cheap per-cycle management inputs derived from closed bars after the entry."""

    trail_candidate: Decimal | None  # tighter stop candidate behind the latest confirmed post-entry swing
    trail_structure_id: str | None
    structure_failure: bool  # a closed bar broke the latest confirmed post-entry swing against the trade
    failure_structure_id: str | None
    momentum_score: Decimal | None  # signed, in trade direction, in ATR units over ``momentum_bars``
    as_of: datetime | None


# -------------------------------------------------------------------------------------------- frames


def _ts(frame: pd.DataFrame) -> list[datetime]:
    return [t.to_pydatetime() for t in pd.DatetimeIndex(frame["ts"])]


def resample_m15(frame: pd.DataFrame) -> pd.DataFrame:
    """CLOSED M15 bars from M5 bars: only complete, contiguous three-bar groups on the 15-minute grid
    (a group still forming or with a gap is dropped, never extrapolated)."""
    if len(frame) == 0:
        return frame.iloc[0:0][["ts", "open", "high", "low", "close"]].copy()
    work = frame[["ts", "open", "high", "low", "close"]].copy()
    work["ts"] = pd.DatetimeIndex(work["ts"])
    work["grp"] = work["ts"].dt.floor("15min")
    rows = []
    for grp, part in work.groupby("grp", sort=True):
        if len(part) != 3:
            continue
        opens = list(part["ts"])
        if (opens[1] - opens[0]).total_seconds() != M5_SECONDS or (opens[2] - opens[1]).total_seconds() != M5_SECONDS:
            continue
        rows.append((grp, part["open"].iloc[0], part["high"].max(), part["low"].min(), part["close"].iloc[-1]))
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])


def confirmed_swings(frame: pd.DataFrame, n: int = 2, timeframe: str = "M5") -> list[Swing]:
    """Fractal swings: bar ``i`` is a swing HIGH if its high is strictly above the ``n`` bars to its left
    and at least the ``n`` bars to its right (a flat double top keeps the FIRST); a swing LOW mirrors it.
    Usable only from ``confirmed_at`` = close of bar ``i + n``. Chronological order."""
    if n < 1:
        raise ValueError("n must be >= 1")
    size = M5_SECONDS if timeframe == "M5" else M15_SECONDS
    count = len(frame)
    if count < 2 * n + 1:
        return []
    ts = _ts(frame)
    hi = [float(x) for x in frame["high"]]
    lo = [float(x) for x in frame["low"]]
    out: list[Swing] = []
    for i in range(n, count - n):
        left_h, right_h = max(hi[i - n:i]), max(hi[i + 1:i + 1 + n])
        left_l, right_l = min(lo[i - n:i]), min(lo[i + 1:i + 1 + n])
        confirmed = ts[i + n] + timedelta(seconds=size)
        if hi[i] > left_h and hi[i] >= right_h:
            out.append(Swing("HIGH", timeframe, hi[i], ts[i], confirmed, f"{timeframe}:HIGH:{ts[i].isoformat()}"))
        if lo[i] < left_l and lo[i] <= right_l:
            out.append(Swing("LOW", timeframe, lo[i], ts[i], confirmed, f"{timeframe}:LOW:{ts[i].isoformat()}"))
    return out


def all_swings(frame: pd.DataFrame, n: int = 2) -> list[Swing]:
    """M5 + M15 swings together, chronological by confirmation."""
    swings = confirmed_swings(frame, n, "M5") + confirmed_swings(resample_m15(frame), n, "M15")
    return sorted(swings, key=lambda s: (s.confirmed_at, s.timeframe, s.kind, s.price))


def prior_range_edges(frame: pd.DataFrame, lookback: int) -> tuple[Level, Level] | None:
    """(range high, range low) of the last ``lookback`` closed M5 bars, as levels (None if too few bars)."""
    if lookback < 2 or len(frame) < lookback:
        return None
    part = frame.iloc[-lookback:]
    ts = _ts(part)
    hi = [float(x) for x in part["high"]]
    lo = [float(x) for x in part["low"]]
    i_h, i_l = max(range(len(hi)), key=lambda k: (hi[k], -k)), min(range(len(lo)), key=lambda k: (lo[k], k))
    end = ts[-1] + timedelta(seconds=M5_SECONDS)
    rng = f"{ts[0].isoformat()}..{ts[-1].isoformat()}"
    return (
        Level(_dec(hi[i_h]), "RANGE:M5", f"M5:RANGE_HIGH:{rng}", ts[i_h], end),
        Level(_dec(lo[i_l]), "RANGE:M5", f"M5:RANGE_LOW:{rng}", ts[i_l], end),
    )


def levels_beyond(direction: int, entry: Decimal, levels: list[Level]) -> list[Level]:
    """Levels strictly beyond ``entry`` in the trade direction, nearest first."""
    beyond = [lv for lv in levels if (lv.price > entry if direction == 1 else lv.price < entry)]
    return sorted(beyond, key=lambda lv: abs(lv.price - entry))


def _merge(levels: list[Level], tol: Decimal) -> list[Level]:
    """Drop near-duplicates (the same swing seen on M5 and M15): keep the first in price order, preferring M15."""
    out: list[Level] = []
    for lv in sorted(levels, key=lambda x: (x.price, x.source != "SWING:M15")):
        if out and abs(lv.price - out[-1].price) <= tol:
            if lv.source == "SWING:M15" and out[-1].source != "SWING:M15":
                out[-1] = lv
            continue
        out.append(lv)
    return out


def _swing_level(s: Swing) -> Level:
    return Level(_dec(s.price), f"SWING:{s.timeframe}", s.structure_id, s.bar_open, s.confirmed_at)


def _round_out(price: Decimal, tick: Decimal | None, *, up: bool) -> Decimal:
    if tick is None or tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick


# ----------------------------------------------------------------------------------------- geometry


def structural_geometry(
    direction: int,
    entry: float | Decimal,
    bars: pd.DataFrame,
    spread: float | Decimal,
    atr: float | Decimal | None,
    *,
    cost: float | Decimal | None = None,
    swing_n: int = 2,
    range_lookback: int = 24,
    atr_buffer_mult: float = 0.25,
    min_stop_atr: float = 0.5,
    max_stop_atr: float = 3.0,
    min_movement_to_cost: float = 3.0,
    min_tp_gap_atr: float = 0.5,
    tick_size: float | Decimal | None = None,
) -> StructuralGeometry:
    """Invalidation stop + ordered TP1/TP2 from the chart, for a trade in ``direction`` at ``entry``.

    * stop = the NEAREST confirmed swing (low for a long, high for a short) on the losing side that is at
      least ``min_stop_atr`` ATR away, else the prior range edge; plus an ATR buffer (and the spread for a
      short, which stops on the ASK). Farther than ``max_stop_atr`` ATR -> no stop (``STRUCTURAL_STOP_TOO_FAR``).
    * TP1 = the nearest confirmed swing / range edge beyond entry whose distance is at least
      ``min_movement_to_cost`` x the expected exit+entry cost (``cost``, default the spread).
    * TP2 = the next distinct level beyond TP1 (>= ``min_tp_gap_atr`` ATR further); none -> the marker
      ``SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED`` (TP1 + runner; a TP2 is never invented).

    Direction integrity is asserted: LONG stop < entry < TP1 < TP2, SHORT mirrored."""
    if direction not in (1, -1):
        raise ValueError("direction must be +1 or -1")
    entry_d, spread_d = _dec(entry), _dec(spread)
    cost_d = spread_d if cost is None else _dec(cost)
    tick = None if tick_size is None else _dec(tick_size)
    markers: list[str] = []
    n_bars = len(bars)
    as_of = None if n_bars == 0 else _ts(bars)[-1] + timedelta(seconds=M5_SECONDS)
    atr_d = None if atr is None or _dec(atr) <= 0 else _dec(atr)
    if atr_d is None:
        markers.append(ATR_UNAVAILABLE)
    if n_bars < 2 * swing_n + 1:
        markers.append(INSUFFICIENT_BARS)
        return StructuralGeometry(direction, entry_d, None, None, D(0), None, None, tuple(markers), as_of)

    swings = all_swings(bars, swing_n)
    edges = prior_range_edges(bars, range_lookback)
    highs = [_swing_level(s) for s in swings if s.kind == "HIGH"]
    lows = [_swing_level(s) for s in swings if s.kind == "LOW"]
    if edges is not None:
        highs.append(edges[0])
        lows.append(edges[1])
    tol = (atr_d * D("0.05")) if atr_d is not None else (tick or D(0))

    buffer = (atr_d * _dec(atr_buffer_mult)) if atr_d is not None else D(0)
    # ---- invalidation stop (losing side) -----------------------------------------------------
    losing = lows if direction == 1 else highs
    cands = sorted(
        (lv for lv in _merge(losing, tol) if (lv.price < entry_d if direction == 1 else lv.price > entry_d)),
        key=lambda lv: abs(lv.price - entry_d),
    )
    stop_level: Level | None = None
    for lv in cands:  # nearest first; skip levels closer than min_stop_atr (noise-distance)
        if atr_d is not None and abs(entry_d - lv.price) < atr_d * _dec(min_stop_atr):
            continue
        stop_level = lv
        break
    stop: Decimal | None = None
    if stop_level is None:
        markers.append(NO_STRUCTURAL_STOP)
    else:
        raw = stop_level.price - buffer if direction == 1 else stop_level.price + buffer + spread_d
        stop = _round_out(raw, tick, up=direction == -1)  # outward: long floors, short ceils
        if atr_d is not None and abs(entry_d - stop) > atr_d * _dec(max_stop_atr):
            markers.append(STOP_TOO_FAR)
            stop = None
    # ---- targets (winning side), nearest first ------------------------------------------------
    winning = highs if direction == 1 else lows
    ahead = levels_beyond(direction, entry_d, _merge(winning, tol))
    min_dist = cost_d * _dec(min_movement_to_cost)
    tp1: Level | None = None
    for lv in ahead:
        if abs(lv.price - entry_d) >= min_dist and abs(lv.price - entry_d) > 0:
            tp1 = lv
            break
    tp2: Level | None = None
    if tp1 is None:
        markers.append(NO_STRUCTURAL_TP1)
    else:
        gap = (atr_d * _dec(min_tp_gap_atr)) if atr_d is not None else D(0)
        for lv in ahead:
            if abs(lv.price - entry_d) > abs(tp1.price - entry_d) and abs(lv.price - tp1.price) >= max(gap, tol):
                tp2 = lv
                break
        if tp2 is None:
            markers.append(SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED)
    if tick is not None:  # targets round TOWARDS entry (a target must be reachable, never optimistic)
        if tp1 is not None:
            tp1 = Level(_round_out(tp1.price, tick, up=direction == -1), tp1.source, tp1.structure_id, tp1.time_created, tp1.confirmed_at)
        if tp2 is not None:
            tp2 = Level(_round_out(tp2.price, tick, up=direction == -1), tp2.source, tp2.structure_id, tp2.time_created, tp2.confirmed_at)
    _assert_direction(direction, entry_d, stop, tp1, tp2)
    return StructuralGeometry(direction, entry_d, stop, stop_level, buffer, tp1, tp2, tuple(markers), as_of)


def _assert_direction(direction: int, entry: Decimal, stop: Decimal | None, tp1: Level | None, tp2: Level | None) -> None:
    """LONG: stop < entry < TP1 < TP2; SHORT mirrored. Raises ValueError on any violation."""
    sign = Decimal(direction)
    if stop is not None and not (stop - entry) * sign < 0:
        raise ValueError("direction integrity: stop must be on the losing side of entry")
    if tp1 is not None and not (tp1.price - entry) * sign > 0:
        raise ValueError("direction integrity: TP1 must be beyond entry")
    if tp2 is not None and (tp1 is None or not (tp2.price - tp1.price) * sign > 0):
        raise ValueError("direction integrity: TP2 must be beyond TP1")


# ------------------------------------------------------------------------------------ management


def management_signals(
    direction: int,
    bars: pd.DataFrame,
    *,
    entered_at: datetime,
    current_stop: float | Decimal,
    price: float | Decimal,
    atr: float | Decimal | None,
    spread: float | Decimal = 0,
    swing_n: int = 2,
    atr_buffer_mult: float = 0.25,
    momentum_bars: int = 3,
) -> ManagementSignals:
    """Trailing / structure-failure / momentum inputs for an OPEN position from closed bars.

    Only swings created AFTER the entry bar (and confirmed) count: they are the structure the trade built.
    * trail candidate: behind the newest such swing (long: swing low - ATR buffer; short: swing high + buffer +
      spread), offered only if it is tighter than ``current_stop`` and still on the safe side of ``price``.
      The engine ratchets it forward only (a stop never loosens).
    * structure failure: the newest closed bar closed beyond that swing against the trade.
    * momentum score: ``direction * (close[-1] - close[-1-k]) / ATR`` (signed, in ATR units)."""
    atr_d = None if atr is None or _dec(atr) <= 0 else _dec(atr)
    n_bars = len(bars)
    if n_bars == 0:
        return ManagementSignals(None, None, False, None, None, None)
    ts = _ts(bars)
    as_of = ts[-1] + timedelta(seconds=M5_SECONDS)
    closes = [float(x) for x in bars["close"]]
    momentum: Decimal | None = None
    if atr_d is not None and n_bars > momentum_bars:
        momentum = (_dec(closes[-1]) - _dec(closes[-1 - momentum_bars])) * Decimal(direction) / atr_d
    kind = "LOW" if direction == 1 else "HIGH"
    # M5 swings only: the M15 confirmation lag (two more 15-minute bars) is too slow for management
    mine = [s for s in confirmed_swings(bars, swing_n, "M5") if s.kind == kind and s.bar_open >= entered_at]
    if not mine:
        return ManagementSignals(None, None, False, None, momentum, as_of)
    latest = max(mine, key=lambda s: (s.bar_open, s.timeframe))  # newest structure the trade built
    failure = closes[-1] < latest.price if direction == 1 else closes[-1] > latest.price
    buffer = (atr_d * _dec(atr_buffer_mult)) if atr_d is not None else D(0)
    level = _dec(latest.price)
    raw = level - buffer if direction == 1 else level + buffer + _dec(spread)
    stop_d, price_d = _dec(current_stop), _dec(price)
    tighter = raw > stop_d if direction == 1 else raw < stop_d
    safe = raw < price_d if direction == 1 else raw > price_d
    cand = raw if tighter and safe else None
    return ManagementSignals(cand, latest.structure_id if cand is not None else None, failure, latest.structure_id if failure else None, momentum, as_of)


__all__ = [
    "ATR_UNAVAILABLE", "INSUFFICIENT_BARS", "NO_STRUCTURAL_STOP", "NO_STRUCTURAL_TP1",
    "SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED", "STOP_TOO_FAR", "Level", "ManagementSignals", "StructuralGeometry",
    "Swing", "all_swings", "confirmed_swings", "levels_beyond", "management_signals", "prior_range_edges",
    "resample_m15", "structural_geometry",
]
