# ruff: noqa: E501
"""Causal offline re-evaluation of the frozen families: strict signals + RELAXED-condition near-misses.

Near-miss rule (no re-implementation of any family logic): one trigger condition at a time is relaxed by a
fraction ``ratio`` of its scale and the family's OWN ``generate`` is re-run on the same ``FamilyData``::

    relaxed = base + sign * ratio * scale          (sign +1: relax by increasing the number, -1: by decreasing it)
    actual   = base + sign * ratio * scale   at the first firing level (the value the bar effectively reached;
               resolution = one level step, 0.01875 of the scale, i.e. an upper bound of the gap)
    required = base                          (the frozen threshold)
    normalized_gap = ratio = |required - actual| / scale   (scale = |required|; ATR-distance parameters are
               floored at 0.25 ATR so a zero buffer has a meaningful tolerance); gap_abs = |required - actual| in `unit`

A bar that fires in the relaxed run but not in the strict run is a NEAR-MISS of exactly that condition;
``ratio`` (first level in ``levels`` at which it fires) is the miss ratio. Levels above ``tolerance`` are kept
as WIDE triggers (diagnosis of UNSEEN moves: "closest failing condition"). All generators are causal (they
use bars <= the decision bar), so a full-frame run equals the bar-by-bar engine; ``test_causality`` proves it.

Not relaxable (never guessed): ROUND ``hold``, ORB failed-breakout re-entry, GAP ``confirm`` direction,
OVERNIGHT position filter, LEADLAG catch-up test. A relaxed decision inside the family COOLDOWN after a real
signal is not reported (it is a signal in cooldown, not "almost a signal"); the greedy cooldown chain of the
relaxed run can shift neighbouring decisions -- a documented approximation of a per-bar isolated probe.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from alpha.families.common import Thr
from alpha.families.data import FamilyData
from alpha.families.registry import FAMILY_MODULES, generate_candidates

SCALE_FLOOR = 0.25  # ATR: scale floor of ATR-distance parameters that may legitimately be 0 / tiny
DEFAULT_TOLERANCE = 0.15
DEFAULT_LEVELS: tuple[float, ...] = (0.01875, 0.0375, 0.05625, 0.075, 0.09375, 0.1125, 0.13125, 0.15, 0.3, 0.5, 0.75)


@dataclass(frozen=True, slots=True)
class Probe:
    condition: str
    kind: str  # "spec" | "thr"
    name: str  # spec field or thr index as text
    base: float
    scale: float
    sign: int
    unit: str = "ATR"  # unit of base/actual: ATR (ATR-normalised quantity) | rel_ATR (ATR / close)

    def apply(self, spec: Any, thr_values: tuple[float, ...], ratio: float) -> tuple[Any, Thr]:
        value = self.base + self.sign * ratio * self.scale
        if self.kind == "spec":
            s2 = copy.copy(spec)
            object.__setattr__(s2, self.name, float(value))  # bypass validate(): a relaxed buffer/prox may go < 0
            return s2, Thr(tuple(thr_values))
        vals = list(thr_values)
        vals[int(self.name)] = float(value)
        return spec, Thr(tuple(vals))

    def measured(self, ratio: float) -> float:
        return self.base + self.sign * ratio * self.scale


def _fin(x: float) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _spec_probe(cond: str, spec: Any, field_: str, sign: int, floor: float = SCALE_FLOOR) -> Probe:
    base = float(getattr(spec, field_))
    return Probe(cond, "spec", field_, base, max(abs(base), floor), sign)


def _thr_probe(cond: str, thr: tuple[float, ...], idx: int, sign: int, unit: str = "ATR") -> Probe | None:
    if idx >= len(thr) or not _fin(thr[idx]) or thr[idx] == 0.0:
        return None
    return Probe(cond, "thr", str(idx), float(thr[idx]), abs(float(thr[idx])), sign, unit)


def probes_for(spec: Any, thr: tuple[float, ...]) -> list[Probe]:
    fam = spec.FAMILY
    out: list[Probe | None] = []
    if fam == "ROUND":
        if spec.mode == "reject":
            out += [_spec_probe("prox_atr", spec, "prox_atr", +1), _spec_probe("rej_atr", spec, "rej_atr", -1)]
        else:
            out += [_spec_probe("break_beyond_level_atr", spec, "prox_atr", -1)]
    elif fam == "VOLREV":
        if spec.mode == "fade":
            out += [_spec_probe("anchor_ext_k_atr", spec, "k", -1, 0.5), _thr_probe("rel_atr_quantile", thr, 0, -1, "rel_ATR")]
        else:
            out += [_thr_probe("compression_rel_atr_quantile", thr, 0, +1, "rel_ATR")]
    elif fam == "GAP":
        out += [_thr_probe("gap_bucket_lo", thr, 0, -1)]
        if len(thr) > 1 and _fin(thr[1]):
            out += [_thr_probe("gap_bucket_hi", thr, 1, +1)]
    elif fam == "OVERNIGHT":
        out += [_thr_probe("overnight_drift_quantile", thr, 0, -1)]
    elif fam == "EOD":
        out += [_thr_probe("eod_trend_quantile", thr, 0, -1)]
    elif fam == "LEADLAG":
        out += [_thr_probe("leader_move_quantile", thr, 0, -1)]
    elif fam == "ORB":
        out += [_spec_probe("breakout_buffer_atr", spec, "buffer_atr", -1)]
    return [p for p in out if p is not None]


@dataclass(frozen=True, slots=True)
class Trigger:
    idx: int  # decision bar (close of this bar = signal time)
    direction: int
    strategy_id: str
    family: str
    mode: str
    kind: str  # SIGNAL | NEAR | WIDE
    condition: str = ""
    ratio: float = 0.0  # normalized gap (see module doc)
    actual: float = float("nan")
    required: float = float("nan")
    unit: str = "ATR"

    @property
    def gap_abs(self) -> float:
        return abs(self.required - self.actual)


@dataclass(slots=True)
class ReplayResult:
    market: str
    signals: list[Trigger] = field(default_factory=list)
    near: list[Trigger] = field(default_factory=list)  # ratio <= tolerance
    wide: list[Trigger] = field(default_factory=list)  # tolerance < ratio <= max level


def _cooldown(family: str) -> int:
    return int(getattr(FAMILY_MODULES[family], "COOLDOWN", 0))


def replay_market(
    market: str, data: FamilyData, specs: list[Any], *, tolerance: float = DEFAULT_TOLERANCE,
    levels: tuple[float, ...] = DEFAULT_LEVELS,
) -> ReplayResult:
    """``specs``: FrozenSpec-like objects (``.spec``, ``.thr_values``, ``.thr``, ``.family``, ``.strategy_id``)."""
    res = ReplayResult(market)
    for fs in specs:
        spec = fs.spec
        mode = str(getattr(spec, "mode", ""))
        strict = generate_candidates(data, spec, fs.thr)
        strict_idx = strict.decision_idx
        strict_set = set(strict_idx.tolist())
        for j, d in zip(strict_idx.tolist(), strict.direction.tolist(), strict=True):
            res.signals.append(Trigger(int(j), int(d), fs.strategy_id, fs.family, mode, "SIGNAL"))
        cd = _cooldown(fs.family)
        best: dict[int, Trigger] = {}
        for pr in probes_for(spec, fs.thr_values):
            seen: set[int] = set()
            for lv in sorted(levels):
                s2, t2 = pr.apply(spec, fs.thr_values, lv)
                rc = generate_candidates(data, s2, t2)
                for j, d in zip(rc.decision_idx.tolist(), rc.direction.tolist(), strict=True):
                    if j in strict_set or j in seen:
                        continue
                    seen.add(j)
                    if cd > 1 and len(strict_idx) and bool(np.any((strict_idx > j - cd) & (strict_idx < j))):
                        continue  # cooldown after a real signal
                    tr = Trigger(int(j), int(d), fs.strategy_id, fs.family, mode, "NEAR" if lv <= tolerance + 1e-12 else "WIDE",
                                 pr.condition, float(lv), pr.measured(lv), pr.base, pr.unit)
                    cur = best.get(j)
                    if cur is None or tr.ratio < cur.ratio:
                        best[j] = tr
        for tr in best.values():
            (res.near if tr.kind == "NEAR" else res.wide).append(tr)
    for lst in (res.signals, res.near, res.wide):
        lst.sort(key=lambda t: (t.idx, t.strategy_id))
    return res
