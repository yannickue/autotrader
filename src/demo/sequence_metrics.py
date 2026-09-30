# ruff: noqa: E501
"""Same-zone / direction-flip / whipsaw INSTRUMENTATION (Lane U2, goal C). MEASURE ONLY.

There is deliberately NO cooldown, NO direction lock and NO filter here: nothing in this module can change a
decision. For every emitted signal (live, catch-up, out-of-window shadow, shadow universe) the engine asks
``SequenceTracker.observe`` for a small dict that is persisted INSIDE the snapshot's ``signal`` JSON
(``signal.sequence``; no new table, no new subsystem), so the funnel/report can slice MFE/MAE/R of the
counterfactual / outcome rows by these facts later.

Causality: a signal is described only by signals that were observed BEFORE it (chronological, per market). The
tracker is idempotent per ``opportunity_id`` (an engine retry of a half-built bar must not double count) and its
memory is bounded (``MAX_HISTORY`` signals per market, ``LOOKBACK_S`` seconds).

Definitions (all per market; "zone" = same price level neighbourhood = the structural STOP within ``ZONE_ATR`` x ATR
of a prior signal's stop, the same idea as ``funnel.STRUCTURE_ATR_STEP`` but distance based, so a zone boundary does
not split neighbours):
  * ``same_zone_reengagement``     a prior signal (any family/direction) in the same zone within ``LOOKBACK_S``
  * ``same_zone_count``            how many
  * ``repeated_level_attempt``     a prior signal with the SAME direction in the same zone
  * ``direction_flip``             the previous signal of this market had the OPPOSITE direction
  * ``time_between_signals_s``     seconds since the previous signal of this market (None = first)
  * ``price_distance_between_signals`` / ``_atr``   |close - previous close| (price / in ATR)
  * ``whipsaw_sequence``           e.g. ``"SHORT,SHORT,LONG,SHORT"``: the last <= 4 directions in this zone incl. this one
  * ``whipsaw_flips``              direction changes inside that sequence; ``whipsaw`` = flips >= 2
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any

ZONE_ATR = 0.5
LOOKBACK_S = 24 * 3600.0
MAX_HISTORY = 64
SEQ_LEN = 4
WHIPSAW_MIN_FLIPS = 2
SCHEMA = "signal-sequence-1"


@dataclass(frozen=True, slots=True)
class _Sig:
    oid: str
    ts: float
    direction: int
    family: str
    stop: float
    close: float


def _name(d: int) -> str:
    return "LONG" if d > 0 else "SHORT"


def regime_of(context: dict[str, Any] | None) -> str | None:
    """Coarse trend regime label from the snapshot context (H1 / D1 EMA trend), ``None`` if unavailable."""
    if not context:
        return None
    h1 = (context.get("H1") or {}).get("trend")
    d1 = (context.get("D1") or {}).get("trend")
    if h1 is None and d1 is None:
        return None
    return f"H1:{h1 or '?'}/D1:{d1 or '?'}"


class SequenceTracker:
    def __init__(self, *, zone_atr: float = ZONE_ATR, lookback_s: float = LOOKBACK_S) -> None:
        self._zone_atr = float(zone_atr)
        self._lookback = float(lookback_s)
        self._hist: dict[str, deque[_Sig]] = {}
        self._memo: dict[str, dict[str, Any]] = {}

    def observe(
        self, *, market: str, oid: str, signal_ts: datetime, direction: int, family: str, stop: float,
        close: float, atr: float, regime: str | None = None,
    ) -> dict[str, Any]:
        memo = self._memo.get(oid)
        if memo is not None:
            return dict(memo)
        ts = signal_ts.timestamp()
        hist = self._hist.setdefault(market, deque(maxlen=MAX_HISTORY))
        recent = [s for s in hist if 0.0 <= ts - s.ts <= self._lookback]
        atr_ok = math.isfinite(atr) and atr > 0.0
        tol = self._zone_atr * atr if atr_ok else 0.0
        zone = [
            s for s in recent
            if math.isfinite(stop) and math.isfinite(s.stop) and atr_ok and abs(s.stop - stop) <= tol
        ]
        prev = hist[-1] if hist and ts >= hist[-1].ts else None
        seq_dirs = [*[s.direction for s in zone][-(SEQ_LEN - 1):], direction]
        flips = sum(1 for a, b in pairwise(seq_dirs) if a != b)
        out: dict[str, Any] = {
            "schema": SCHEMA,
            "family": family,
            "regime": regime,
            "same_zone_reengagement": bool(zone),
            "same_zone_count": len(zone),
            "repeated_level_attempt": any(s.direction == direction for s in zone),
            "direction_flip": bool(prev is not None and prev.direction != direction),
            "prev_direction": None if prev is None else _name(prev.direction),
            "time_between_signals_s": None if prev is None else ts - prev.ts,
            "price_distance_between_signals": None if prev is None else abs(close - prev.close),
            "price_distance_between_signals_atr": (abs(close - prev.close) / atr) if prev is not None and atr_ok else None,
            "whipsaw_sequence": ",".join(_name(d) for d in seq_dirs),
            "whipsaw_flips": flips,
            "whipsaw": flips >= WHIPSAW_MIN_FLIPS,
        }
        hist.append(_Sig(oid, ts, direction, family, stop, close))
        self._memo[oid] = out
        if len(self._memo) > 4 * MAX_HISTORY * max(1, len(self._hist)):  # bounded idempotence memo
            for k in list(self._memo)[: len(self._memo) // 2]:
                del self._memo[k]
        return dict(out)


# ------------------------------------------------------------------------------------ read model
def summarize(rows: list[dict[str, Any]], cf: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Funnel / report section from persisted rows: each row needs ``sequence`` (the ``signal.sequence`` dict or None) and
    ``opportunity_id``; ``cf`` maps opportunity_id -> counterfactual label row (``r``, ``mfe_r``, ``mae_r``) for the
    later MFE/MAE slices. Pure, no I/O."""
    cf = cf or {}
    seqs = [(r, r["sequence"]) for r in rows if isinstance(r.get("sequence"), dict)]
    n = len(seqs)

    def share(pred: Any) -> dict[str, Any]:
        sub = [(r, s) for r, s in seqs if pred(s)]
        labs = [cf[r["opportunity_id"]] for r, _s in sub if r["opportunity_id"] in cf]
        return {
            "n": len(sub), "share": (len(sub) / n) if n else None,
            "n_labelled": len(labs),
            "mean_r": (sum(x["r"] for x in labs) / len(labs)) if labs else None,
            "mean_mfe_r": (sum(x["mfe_r"] for x in labs) / len(labs)) if labs else None,
            "mean_mae_r": (sum(x["mae_r"] for x in labs) / len(labs)) if labs else None,
        }

    gaps = sorted(s["time_between_signals_s"] for _r, s in seqs if s.get("time_between_signals_s") is not None)
    seq_counts: dict[str, int] = {}
    for _r, s in seqs:
        if s.get("whipsaw"):
            seq_counts[s["whipsaw_sequence"]] = seq_counts.get(s["whipsaw_sequence"], 0) + 1
    return {
        "schema": SCHEMA,
        "measure_only": True,
        "note": "instrumentation only: no cooldown, no direction lock, no filter",
        "signals_with_sequence": n,
        "same_zone_reengagement": share(lambda s: s.get("same_zone_reengagement")),
        "repeated_level_attempt": share(lambda s: s.get("repeated_level_attempt")),
        "direction_flip": share(lambda s: s.get("direction_flip")),
        "whipsaw": share(lambda s: s.get("whipsaw")),
        "median_time_between_signals_s": gaps[len(gaps) // 2] if gaps else None,
        "top_whipsaw_sequences": dict(sorted(seq_counts.items(), key=lambda kv: -kv[1])[:10]),
    }
