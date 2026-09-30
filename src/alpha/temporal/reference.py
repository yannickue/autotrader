"""Pure-Python streaming reference evaluator (semantic oracle) for StateMachineStrategySpec.

Design: docs/V2_TEMPORAL_ENGINE.md sections 3 (evaluation) and 8 (bound-clause semantics).
At step ``u`` the evaluator only holds a ``_View`` limited to bars/array values ``<= u``; every read
goes through ``_View.col`` which slices ``arr[:u+1]`` and rejects any index outside ``[0, u]``.
Not optimised: it is the oracle the numba kernel is checked against, never a production path.

Interpretations the frozen docs leave open (see the W2 report):
* window arithmetic: transition ``within=w`` allows firing at ``u`` iff ``u - enter_idx <= w``;
  ``expires_after=e`` allows a (final) transition at ``u`` iff ``u - anchor_idx <= e``.
* state clause is active iff value > 0; pulse clause iff value > 0.
* generic op wrappers apply to any base predicate (event/state/bound/feature): IS, NOT, HOLD(k)
  = base true on all of (u-k, u] inside the run, BEFORE(k) = true once in (u-k, u), SINCE_ENTER =
  true once in (enter_idx, u] (pulses only; states are rejected by validate()).
* bound tolerance comes from ``Clause.tol_atr``; RECLAIM/HOLD ``k`` from the ``k3/k6`` variant;
  RETEST_HOLD has no k in the registry: ``RETEST_LOOKBACK`` is used.
* TOUCH is self-mirrored in the registry, so its price mirror is chosen by ``spec.direction``.
* a non-finite register capture kills the instance (fail closed).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import numpy as np

from alpha.events import schema as ev
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.temporal.spec import MAX_REG, REGS, Capture, Clause, StateMachineStrategySpec

RETEST_LOOKBACK = 6  # k for RETEST_HOLD (not a registry axis); max of K_GRID

_CORE = ("o", "h", "l", "c", "atr", "run_start", "berlin_minute")


@dataclass(frozen=True)
class MarketFrame:
    """Synthetic-friendly market input. ``arrays`` holds event/state/level/feature arrays by
    registry array name (e.g. ``ev_m5_bos_up``) and next_structure levels by TARGET_LEVELS name."""

    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    atr: np.ndarray
    run_start: np.ndarray
    berlin_minute: np.ndarray
    arrays: Mapping[str, np.ndarray] = field(default_factory=dict)
    thresholds: Mapping[tuple[str, float], float] = field(default_factory=dict)
    ts_close_ns: np.ndarray | None = None

    def __post_init__(self) -> None:
        n = len(self.c)
        for name in _CORE:
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} length != {n}")
        for k, v in self.arrays.items():
            if len(v) != n:
                raise ValueError(f"array {k} length != {n}")
        if self.ts_close_ns is not None and len(self.ts_close_ns) != n:
            raise ValueError("ts_close_ns length")

    def __len__(self) -> int:
        return len(self.c)

    def prefix(self, n: int) -> MarketFrame:
        return MarketFrame(
            o=self.o[:n], h=self.h[:n], l=self.l[:n], c=self.c[:n], atr=self.atr[:n],
            run_start=self.run_start[:n], berlin_minute=self.berlin_minute[:n],
            arrays={k: v[:n] for k, v in self.arrays.items()}, thresholds=dict(self.thresholds),
            ts_close_ns=None if self.ts_close_ns is None else self.ts_close_ns[:n],
        )


@dataclass(frozen=True)
class InstanceTrail:
    """Evidence trail of one emitted candidate."""

    anchor_idx: int
    step_idx: tuple[int, ...]  # enter bar per state: anchor, T1..Tn (last == decision_idx)
    registers: tuple[float, ...]  # length MAX_REG, NaN where never captured
    event_ids: tuple[int, ...]  # zid of lv-captured levels (empty if no zid arrays)
    entry_zone_lo: float
    entry_zone_hi: float


@dataclass(frozen=True)
class ReferenceResult:
    candidates: CandidateArrays
    trails: tuple[InstanceTrail, ...]


class _View:
    """Causal window over the market at step u: nothing beyond index u is reachable."""

    __slots__ = ("_arrays", "_core", "u")

    def __init__(self, m: MarketFrame, u: int) -> None:
        self.u = u
        self._core = m
        self._arrays = m.arrays

    def col(self, name: str, t: int) -> float:
        if t < 0 or t > self.u:
            raise IndexError(f"causality violation: read {name}[{t}] at step {self.u}")
        src = getattr(self._core, name) if name in _CORE else self._arrays[name]
        return float(src[: self.u + 1][t])

    def has(self, name: str) -> bool:
        return name in self._arrays


# ------------------------------------------------------------------------------ predicates
Base = Callable[[_View, int, tuple], bool]


@dataclass(frozen=True)
class _Pred:
    base: Base
    op: str
    arg: int

    def eval(self, v: _View, u: int, enter: int | None, regs: tuple) -> bool:
        op = self.op
        if op == "IS":
            return self.base(v, u, regs)
        if op == "NOT":
            return not self.base(v, u, regs)
        rs = int(v.col("run_start", u))
        if op == "HOLD":
            lo = u - self.arg + 1
            return lo >= rs and all(self.base(v, t, regs) for t in range(lo, u + 1))
        if op == "BEFORE":
            return any(self.base(v, t, regs) for t in range(max(u - self.arg + 1, rs), u))
        if op == "SINCE_ENTER":
            if enter is None:
                raise ValueError("SINCE_ENTER without an enter bar")
            return any(self.base(v, t, regs) for t in range(max(enter + 1, rs), u + 1))
        raise ValueError(op)


def _bound_base(c: Clause, short: bool) -> Base:
    d = ev.get(c.name)
    parts = d.parse_variant(c.variant)
    k = int(parts.get("k", RETEST_LOOKBACK))
    tol = c.tol_atr
    ri = REGS.index(c.reg)
    name = c.name

    def reg(regs: tuple) -> float:
        r = regs[ri]
        return r if math.isfinite(r) else math.nan

    def rs_of(v: _View, t: int) -> int:
        return int(v.col("run_start", t))

    def base(v: _View, t: int, regs: tuple) -> bool:
        r = reg(regs)
        if math.isnan(r):
            return False
        a = v.col("atr", t)
        cl = v.col("c", t)
        if name == "TOUCH":
            if short:
                return v.col("h", t) >= r - tol * a and cl < r
            return v.col("l", t) <= r + tol * a and cl > r
        if name == "BREAK_UP":
            return t - 1 >= rs_of(v, t) and cl > r + tol * a and v.col("c", t - 1) <= r + tol * a
        if name == "BREAK_DN":
            return t - 1 >= rs_of(v, t) and cl < r - tol * a and v.col("c", t - 1) >= r - tol * a
        if name in ("RECLAIM_UP", "RECLAIM_DN"):
            up = name == "RECLAIM_UP"
            rng = range(max(t - k + 1, rs_of(v, t)), t)
            if up:
                return cl > r and any(v.col("c", x) < r for x in rng)
            return cl < r and any(v.col("c", x) > r for x in rng)
        if name in ("RETEST_HOLD_UP", "RETEST_HOLD_DN"):
            rng = range(max(t - k + 1, rs_of(v, t)), t)
            if name == "RETEST_HOLD_UP":
                return (
                    any(v.col("c", x) > r + tol * v.col("atr", x) for x in rng)
                    and v.col("l", t) <= r + tol * a and cl > r
                )
            return (
                any(v.col("c", x) < r - tol * v.col("atr", x) for x in rng)
                and v.col("h", t) >= r - tol * a and cl < r
            )
        if name in ("HOLD_ABOVE", "HOLD_BELOW"):
            lo = t - k + 1
            if lo < rs_of(v, t):
                return False
            if name == "HOLD_ABOVE":
                return all(v.col("c", x) > r for x in range(lo, t + 1))
            return all(v.col("c", x) < r for x in range(lo, t + 1))
        raise ValueError(f"unsupported bound clause {name}")

    return base


def _compile_clause(c: Clause, spec: StateMachineStrategySpec, m: MarketFrame) -> _Pred:
    if c.kind == "feature":
        thr = m.thresholds[(c.name, c.q)]
        gt = c.cmp == "gt"

        def fbase(v: _View, t: int, regs: tuple) -> bool:
            x = v.col(c.name, t)
            return x > thr if gt else x < thr

        return _Pred(fbase, c.op, c.arg)
    if c.kind == "bound":
        return _Pred(_bound_base(c, spec.direction == "SHORT"), c.op, c.arg)
    arr = ev.array_names(c.name, c.tf, c.variant)[0]

    def base(v: _View, t: int, regs: tuple) -> bool:
        return v.col(arr, t) > 0

    return _Pred(base, c.op, c.arg)


# ------------------------------------------------------------------------------ captures
@dataclass(frozen=True)
class _Cap:
    reg: int
    source: str
    array: str | None  # for evl/evx/lv
    zid: str | None  # zid array of the lv level (event_ids)


def level_arrays(of: str, clause: Clause) -> tuple[str, str | None]:
    """(lv array name, zid array name or None) for level ``of`` exposed by ``clause``."""
    d = ev.get(of)
    variant = clause.variant if clause.variant in d.variants() else ""
    names = ev.array_names(of, clause.tf, variant)
    return names[0], next((n for n in names if n.startswith("zid_")), None)


def _compile_capture(cap: Capture, clauses: tuple[Clause, ...]) -> _Cap:
    ri = REGS.index(cap.reg)
    if cap.source in ("evl", "evx"):
        cl = next(c for c in clauses if c.kind == "event" and c.name == cap.of and c.op == "IS")
        names = ev.array_names(cl.name, cl.tf, cl.variant)
        return _Cap(ri, cap.source, next(n for n in names if n.startswith(cap.source + "_")), None)
    if cap.source == "lv":
        cl = next(
            c for c in clauses
            if c.kind == "event" and c.op == "IS" and cap.of in ev.get(c.name).exposes
        )
        arr, zid = level_arrays(cap.of, cl)
        return _Cap(ri, "lv", arr, zid)
    return _Cap(ri, cap.source, None, None)


@dataclass
class _Inst:
    anchor: int
    enters: tuple[int, ...]
    regs: tuple[float, ...]
    runmin: float
    runmax: float
    zids: tuple[int, ...]
    zone: tuple[float, float]


def _read_caps(
    caps: list[_Cap], v: _View, u: int, regs: tuple, runmin: float, runmax: float
) -> tuple[tuple, tuple[int, ...]] | None:
    out = list(regs)
    zids: list[int] = []
    for cp in caps:
        s = cp.source
        if s in ("evl", "evx", "lv"):
            x = v.col(cp.array, u)  # type: ignore[arg-type]
            if s == "lv" and cp.zid is not None and v.has(cp.zid):
                zids.append(int(v.col(cp.zid, u)))
        elif s == "bar_low":
            x = v.col("l", u)
        elif s == "bar_high":
            x = v.col("h", u)
        elif s == "close":
            x = v.col("c", u)
        elif s == "min_low_since_enter":
            x = min(runmin, v.col("l", u))
        elif s == "max_high_since_enter":
            x = max(runmax, v.col("h", u))
        else:
            raise ValueError(s)
        if not math.isfinite(x):
            return None
        out[cp.reg] = x
    return tuple(out), tuple(zids)


# ------------------------------------------------------------------------------ target / stop
def resolve_target(
    rule, direction: str, close: float, risk: float, levels: list[float]
) -> tuple[float, float] | None:
    """(target, target_r) or None if the candidate must be rejected. ``levels`` are the level
    values known at the decision bar; only levels strictly beyond ``close`` are considered."""
    if rule.kind == "fixed_r":
        return math.nan, rule.r
    long = direction == "LONG"
    beyond = [x for x in levels if math.isfinite(x) and (x > close if long else x < close)]
    if not beyond:
        return math.nan, rule.fallback_r
    tgt = min(beyond) if long else max(beyond)
    space = (tgt - close) / risk if long else (close - tgt) / risk
    if space < rule.min_space_r - 1e-12:
        return None
    if not (tgt > close if long else tgt < close):  # defensive: wrong side of the decision close
        return None
    return tgt, space


def _stop(spec: StateMachineStrategySpec, v: _View, u: int, regs: tuple) -> float:
    s = spec.stop
    a = v.col("atr", u)
    long = spec.direction == "LONG"
    if s.kind in ("register", "zone_edge"):
        base = regs[REGS.index(s.reg)]
        off = s.buffer_atr * a
        return base - off if long else base + off
    if s.kind == "swing":
        lvl = v.col(ev.array_names(s.of, s.tf, "")[0], u)
        off = s.buffer_atr * a
        return lvl - off if long else lvl + off
    c = v.col("c", u)
    return c - s.atr_mult * a if long else c + s.atr_mult * a


# ------------------------------------------------------------------------------ evaluate
def _dedup_key(i: _Inst) -> tuple:
    return (i.anchor, tuple(-math.inf if math.isnan(x) else x for x in i.regs))


def evaluate_reference(spec: StateMachineStrategySpec, market: MarketFrame) -> ReferenceResult:
    n = len(market)
    long = spec.direction == "LONG"
    anchor_p = [_compile_clause(c, spec, market) for c in spec.anchor]
    context_p = [_compile_clause(c, spec, market) for c in spec.context]
    anchor_caps = [_compile_capture(cp, spec.anchor) for cp in spec.anchor_capture]
    zone_arrs: list[tuple[str, str]] = []
    for c in spec.anchor:
        if c.kind == "event" and c.op == "IS":
            ex = ev.get(c.name).exposes
            if "ZONE_LO" in ex and "ZONE_HI" in ex:
                zone_arrs.append((level_arrays("ZONE_LO", c)[0], level_arrays("ZONE_HI", c)[0]))
    trans = []
    for t in spec.states:
        same = (t.trigger, *t.guards)
        trans.append((
            _compile_clause(t.trigger, spec, market),
            [_compile_clause(g, spec, market) for g in t.guards],
            [_compile_clause(c, spec, market) for c in t.invalidate],
            [_compile_capture(cp, same) for cp in t.capture],
            t.within,
        ))
    n_tr = len(trans)
    levels = list(spec.target.levels) if spec.target.kind == "next_structure" else []
    empty_regs = (math.nan,) * MAX_REG

    insts: list[_Inst] = []
    out_idx: list[int] = []
    out_stop: list[float] = []
    out_tgt: list[float] = []
    out_tr: list[float] = []
    trails: list[InstanceTrail] = []

    for u in range(n):
        v = _View(market, u)
        rs_u = int(v.col("run_start", u))
        keep: list[_Inst] = []
        moved: dict[int, list[_Inst]] = {}
        for inst in insts:
            e = inst.enters[-1]
            k = len(inst.enters) - 1  # waiting for transition k
            trig, guards, invs, caps, within = trans[k]
            if rs_u != int(v.col("run_start", e)):  # (1) run boundary
                continue
            if u - inst.anchor > spec.expires_after or (within is not None and u - e > within):
                continue
            if any(p.eval(v, u, e, inst.regs) for p in invs):  # (2) invalidate
                continue
            if trig.eval(v, u, e, inst.regs) and all(g.eval(v, u, e, inst.regs) for g in guards):
                got = _read_caps(caps, v, u, inst.regs, inst.runmin, inst.runmax)  # (3)+(4)
                if got is None:
                    continue
                regs, zids = got
                moved.setdefault(k + 1, []).append(_Inst(
                    inst.anchor, (*inst.enters, u), regs, math.inf, -math.inf,
                    (*inst.zids, *zids), inst.zone,
                ))
                continue
            inst.runmin = min(inst.runmin, v.col("l", u))
            inst.runmax = max(inst.runmax, v.col("h", u))
            keep.append(inst)
        for stage, group in sorted(moved.items()):
            best = max(group, key=_dedup_key)  # all share enter_idx == u
            if stage == n_tr:
                _finalize(best, v, u, spec, context_p, levels, long,
                          out_idx, out_stop, out_tgt, out_tr, trails)
            else:
                keep.append(best)
        # anchors are created after the transitions of this bar (no same-bar chaining)
        if all(p.eval(v, u, None, empty_regs) for p in anchor_p):
            got = _read_caps(anchor_caps, v, u, empty_regs, math.inf, -math.inf)
            if got is not None:
                regs, zids = got
                lo = hi = math.nan
                for lo_a, hi_a in zone_arrs:
                    if v.has(lo_a):
                        lo = v.col(lo_a, u)
                    if v.has(hi_a):
                        hi = v.col(hi_a, u)
                    break
                keep.append(_Inst(u, (u,), regs, math.inf, -math.inf, zids, (lo, hi)))
        insts = keep

    cands = CandidateArrays(
        decision_idx=np.asarray(out_idx, dtype=np.int64),
        direction=np.full(len(out_idx), 1 if long else -1, dtype=np.int8),
        stop=np.asarray(out_stop, dtype=np.float64),
        target=np.asarray(out_tgt, dtype=np.float64),
        target_r=np.asarray(out_tr, dtype=np.float64),
        exit_kind=np.full(len(out_idx), EXIT_FIXED_R, dtype=np.int8),
    )
    return ReferenceResult(cands, tuple(trails))


def _finalize(
    inst: _Inst, v: _View, u: int, spec: StateMachineStrategySpec, context_p: list[_Pred],
    levels: list[str], long: bool, out_idx: list, out_stop: list, out_tgt: list, out_tr: list,
    trails: list,
) -> None:
    if not all(p.eval(v, u, u, inst.regs) for p in context_p):
        return
    sw = spec.session_window
    if sw is not None:
        bm = v.col("berlin_minute", u)
        if not sw[0] <= bm < sw[1]:
            return
    close = v.col("c", u)
    atr = v.col("atr", u)
    stop = _stop(spec, v, u, inst.regs)
    if not (math.isfinite(stop) and math.isfinite(atr) and atr > 0.0):
        return
    if not (stop < close if long else stop > close):
        return
    risk = abs(close - stop)
    if risk > spec.stop.max_risk_atr * atr + 1e-12:
        return
    lv_vals = [v.col(name, u) for name in levels]
    res = resolve_target(spec.target, spec.direction, close, risk, lv_vals)
    if res is None:
        return
    out_idx.append(u)
    out_stop.append(stop)
    out_tgt.append(res[0])
    out_tr.append(res[1])
    trails.append(InstanceTrail(
        anchor_idx=inst.anchor, step_idx=inst.enters, registers=inst.regs,
        event_ids=inst.zids, entry_zone_lo=inst.zone[0], entry_zone_hi=inst.zone[1],
    ))
