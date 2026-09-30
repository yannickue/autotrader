"""Compile a StateMachineStrategySpec + MarketFrame into a compact numba-friendly program.

Design: docs/V2_TEMPORAL_ENGINE.md sections 3 and 9.  The program is int32 opcode rows plus
float64 parameters; every array the kernel reads lives in a frame-level ``ArrayPool`` stacked into
``F8[k, N]`` (uint8 flags), ``L64[m, N]`` (float64 levels) and ``LS`` last-seen rows.  Flag
clauses with NOT / HOLD(k) / BEFORE(k) are pre-derived per batch into extra F8 rows (run-length /
last-seen recurrences, run-local), so the kernel evaluates them with one byte load; only
SINCE_ENTER (which depends on the instance's enter bar) reads a last-seen row.

Semantics are those of ``alpha.temporal.reference`` (the oracle); this module only lowers them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from alpha.events import schema as ev
from alpha.temporal import kernel as kn
from alpha.temporal.reference import (
    RETEST_LOOKBACK,
    MarketFrame,
    _compile_capture,
    level_arrays,
)
from alpha.temporal.spec import MAX_REG, REGS, Capture, Clause, StateMachineStrategySpec

# clause-table columns
C_KIND, C_OP, C_ARG, C_ROW, C_REG, C_K, C_AUX, C_CODE = range(8)
# transition-table columns (T_FLAGONLY: no bound / SINCE_ENTER clause -> next-fire jump ok)
T_WITHIN, T_TRIG, T_NG, T_G0, T_G1, T_NI, T_I0, T_I1, T_NCAP, T_FLAGONLY = range(10)
NO_WITHIN = 1 << 30

OPS = {"IS": 0, "NOT": 1, "HOLD": 2, "BEFORE": 3, "SINCE_ENTER": 4}
BOUND_CODES = {
    "TOUCH_LONG": 0, "TOUCH_SHORT": 1, "BREAK_UP": 2, "BREAK_DN": 3, "RECLAIM_UP": 4,
    "RECLAIM_DN": 5, "RETEST_HOLD_UP": 6, "RETEST_HOLD_DN": 7, "HOLD_ABOVE": 8, "HOLD_BELOW": 9,
}
SRC_CODES = {
    "evl": kn.SRC_LEVEL, "evx": kn.SRC_LEVEL, "lv": kn.SRC_LEVEL, "bar_low": kn.SRC_LOW,
    "bar_high": kn.SRC_HIGH, "close": kn.SRC_CLOSE, "min_low_since_enter": kn.SRC_MINLOW,
    "max_high_since_enter": kn.SRC_MAXHIGH,
}
STOP_CODES = {"register": 0, "zone_edge": 0, "swing": 1, "atr": 2}


@dataclass(frozen=True)
class PoolArrays:
    """Frozen, C-contiguous stacked arrays shared by every program of one frame."""

    n: int
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    atr: np.ndarray
    rs: np.ndarray
    bm: np.ndarray
    F8: np.ndarray
    L64: np.ndarray
    LS: np.ndarray


class ArrayPool:
    """Registers the flag / level rows programs need, then freezes them into stacked arrays."""

    def __init__(self, frame: MarketFrame) -> None:
        self.frame = frame
        self._flag_ix: dict[tuple, int] = {}
        self._flags: list = []  # ndarray, or ("d", base_row, op, arg) derived at freeze
        self._level_ix: dict[str, int] = {}
        self._levels: list[np.ndarray] = []
        self._ls_ix: dict[int, int] = {}
        self._frozen: PoolArrays | None = None

    def _check_open(self) -> None:
        if self._frozen is not None:
            raise RuntimeError("pool already frozen")

    def flag(self, key: tuple, build) -> int:
        ix = self._flag_ix.get(key)
        if ix is None:
            self._check_open()
            ix = len(self._flags)
            self._flag_ix[key] = ix
            self._flags.append(np.ascontiguousarray(build(), dtype=np.uint8))
        return ix

    def level(self, name: str) -> int:
        ix = self._level_ix.get(name)
        if ix is None:
            self._check_open()
            arr = self.frame.arrays[name]  # KeyError for a missing array, like the oracle
            ix = len(self._levels)
            self._level_ix[name] = ix
            self._levels.append(np.asarray(arr, dtype=np.float64))
        return ix

    def derived(self, base_row: int, op: str, arg: int) -> int:
        """Row of ``op(arg)`` applied to flag row ``base_row`` (NOT / HOLD / BEFORE)."""
        key = ("d", base_row, op, arg)
        ix = self._flag_ix.get(key)
        if ix is None:
            self._check_open()
            ix = len(self._flags)
            self._flag_ix[key] = ix
            self._flags.append(key)
        return ix

    def ls(self, flag_row: int) -> int:
        ix = self._ls_ix.get(flag_row)
        if ix is None:
            self._check_open()
            ix = self._ls_ix[flag_row] = len(self._ls_ix)
        return ix

    def freeze(self) -> PoolArrays:
        if self._frozen is not None:
            return self._frozen
        m = self.frame
        n = len(m)
        rs = np.ascontiguousarray(m.run_start, dtype=np.int64)
        mat: list[np.ndarray] = []
        for e in self._flags:
            if isinstance(e, tuple):
                _, base, op, arg = e
                if op == "NOT":
                    e = kn.derive_not(mat[base])
                elif op == "HOLD":
                    e = kn.derive_hold(mat[base], rs, arg)
                else:
                    e = kn.derive_before(mat[base], rs, arg)
            mat.append(e)
        F8 = np.stack(mat) if mat else np.zeros((0, n), dtype=np.uint8)
        L64 = np.stack(self._levels) if self._levels else np.zeros((0, n), dtype=np.float64)
        LS = kn.build_lastseen(F8, np.asarray(list(self._ls_ix), dtype=np.int64))
        f64 = lambda a: np.ascontiguousarray(a, dtype=np.float64)  # noqa: E731
        self._frozen = PoolArrays(
            n=n, o=f64(m.o), h=f64(m.h), l=f64(m.l), c=f64(m.c), atr=f64(m.atr),
            rs=rs, bm=f64(m.berlin_minute),
            F8=np.ascontiguousarray(F8), L64=np.ascontiguousarray(L64), LS=LS,
        )
        return self._frozen


@dataclass(frozen=True)
class Program:
    n_tr: int
    long: bool
    expires: int
    CL: np.ndarray  # int32 [nc, 8]
    CP: np.ndarray  # float64 [nc] tolerance
    anchor_rows: np.ndarray  # int32 F8 rows of the anchor clauses (all must be true)
    ctx_cl: np.ndarray  # int32
    TI: np.ndarray  # int32 [n_tr, 10]
    CAP: np.ndarray  # int32 [n_tr + 1, MAX_REG, 3]  (state 0 = anchor captures)
    NCAP: np.ndarray  # int32 [n_tr + 1]
    sw_lo: int
    sw_hi: int  # -1 = no session window
    stop_kind: int
    stop_reg: int
    stop_row: int
    stop_buf: float
    stop_mult: float
    max_risk: float
    tgt_next: bool
    tgt_r: float
    tgt_fallback: float
    tgt_min_space: float
    tgt_rows: np.ndarray  # int32
    prefix_keys: tuple[bytes, ...]  # cumulative hashes: [anchor, T1, ..., Tn]
    stage_zids: tuple[tuple[tuple[int, str], ...], ...]  # per stage: ((stage, zid array), ...)
    zone_arrays: tuple[str, str] | None

    @property
    def n_states(self) -> int:
        return self.n_tr + 1


def _h(prev: bytes, sig: object) -> bytes:
    return hashlib.blake2b(prev + repr(sig).encode(), digest_size=16).digest()


class _Compiler:
    def __init__(self, spec: StateMachineStrategySpec, frame: MarketFrame, pool: ArrayPool):
        self.spec = spec
        self.frame = frame
        self.pool = pool
        self.short = spec.direction == "SHORT"
        self.rows: list[list[int]] = []
        self.tols: list[float] = []

    def clause(self, c: Clause) -> tuple[int, object]:
        """Append a clause row; returns (row index, semantic signature)."""
        row = [0] * 8
        row[C_OP] = OPS[c.op]
        row[C_ARG] = c.arg
        tol = 0.0
        if c.kind == "bound":
            d = ev.get(c.name)
            k = int(d.parse_variant(c.variant).get("k", RETEST_LOOKBACK))
            name = c.name
            if name == "TOUCH":
                name = "TOUCH_SHORT" if self.short else "TOUCH_LONG"
            row[C_KIND] = 1
            row[C_CODE] = BOUND_CODES[name]
            row[C_REG] = REGS.index(c.reg)
            row[C_K] = k
            tol = c.tol_atr
            sig: object = ("bound", name, row[C_REG], tol, k, c.op, c.arg)
        else:
            if c.kind == "feature":
                thr = self.frame.thresholds[(c.name, c.q)]
                arr = self.frame.arrays[c.name]
                gt = c.cmp == "gt"
                key: tuple = ("feat", c.name, float(thr), gt)
                frow = self.pool.flag(
                    key,
                    lambda arr=arr, thr=thr, gt=gt: (
                        np.asarray(arr, dtype=np.float64) > thr if gt
                        else np.asarray(arr, dtype=np.float64) < thr
                    ),
                )
            else:
                aname = ev.array_names(c.name, c.tf, c.variant)[0]
                key = ("flag", aname)
                frow = self.pool.flag(
                    key, lambda aname=aname: np.asarray(self.frame.arrays[aname]) > 0
                )
            if c.op == "IS":
                row[C_ROW] = frow
            elif c.op == "SINCE_ENTER":
                row[C_ROW] = frow
                row[C_AUX] = self.pool.ls(frow)
            else:  # NOT / HOLD(k) / BEFORE(k): derived row, evaluated as a plain flag
                row[C_ROW] = self.pool.derived(frow, c.op, c.arg)
                row[C_OP] = OPS["IS"]
            sig = (key, c.op, c.arg)
        self.rows.append(row)
        self.tols.append(tol)
        return len(self.rows) - 1, sig

    def captures(self, caps: tuple[Capture, ...], clauses: tuple[Clause, ...], stage: int):
        arr = np.zeros((MAX_REG, 3), dtype=np.int32)
        sigs: list[tuple] = []
        zids: list[tuple[int, str]] = []
        for j, cp in enumerate(caps):
            cc = _compile_capture(cp, clauses)
            row = -1
            if cc.array is not None:
                row = self.pool.level(cc.array)
            arr[j] = (cc.reg, SRC_CODES[cc.source], row)
            sigs.append((cc.reg, cc.source, cc.array))
            if cc.source == "lv" and cc.zid is not None and cc.zid in self.frame.arrays:
                zids.append((stage, cc.zid))
        return arr, len(caps), tuple(sorted(sigs, key=repr)), tuple(zids)


def compile_program(
    spec: StateMachineStrategySpec, frame: MarketFrame, pool: ArrayPool
) -> Program:
    cp = _Compiler(spec, frame, pool)
    long = spec.direction == "LONG"
    n_tr = len(spec.states)

    anchor_ix, anchor_sigs = [], []
    for c in spec.anchor:
        i, s = cp.clause(c)
        anchor_ix.append(i)
        anchor_sigs.append(repr(s))
    CAP = np.zeros((n_tr + 1, MAX_REG, 3), dtype=np.int32)
    NCAP = np.zeros(n_tr + 1, dtype=np.int32)
    a_arr, a_n, a_sig, a_zids = cp.captures(spec.anchor_capture, spec.anchor, 0)
    CAP[0], NCAP[0] = a_arr, a_n
    stage_zids = [a_zids]
    keys = [_h(b"anchor", (spec.direction, spec.expires_after, tuple(sorted(anchor_sigs)), a_sig))]

    TI = np.zeros((n_tr, 10), dtype=np.int32)
    for k, t in enumerate(spec.states):
        trig, trig_sig = cp.clause(t.trigger)
        guards = [cp.clause(g) for g in t.guards]
        invs = [cp.clause(g) for g in t.invalidate]
        same = (t.trigger, *t.guards)
        c_arr, c_n, c_sig, z = cp.captures(t.capture, same, k + 1)
        CAP[k + 1], NCAP[k + 1] = c_arr, c_n
        stage_zids.append(z)
        TI[k, T_WITHIN] = NO_WITHIN if t.within is None else t.within
        TI[k, T_TRIG] = trig
        TI[k, T_NG] = len(guards)
        for j, (gi, _) in enumerate(guards):
            TI[k, T_G0 + j] = gi
        TI[k, T_NI] = len(invs)
        for j, (ii, _) in enumerate(invs):
            TI[k, T_I0 + j] = ii
        TI[k, T_NCAP] = c_n
        TI[k, T_FLAGONLY] = int(all(
            cp.rows[i][C_KIND] == 0 and cp.rows[i][C_OP] == 0
            for i in (trig, *(g[0] for g in guards), *(v[0] for v in invs))
        ))
        sig = (t.within, repr(trig_sig), tuple(sorted(repr(g[1]) for g in guards)),
               tuple(sorted(repr(i[1]) for i in invs)), c_sig)
        keys.append(_h(keys[-1], sig))

    ctx_ix = [cp.clause(c)[0] for c in spec.context]

    s = spec.stop
    stop_reg = REGS.index(s.reg) if s.reg else 0
    stop_row = pool.level(ev.array_names(s.of, s.tf, "")[0]) if s.kind == "swing" else 0
    t = spec.target
    tgt_rows = np.asarray(
        [pool.level(name) for name in t.levels] if t.kind == "next_structure" else [],
        dtype=np.int32,
    )
    sw = spec.session_window
    zone = None
    for c in spec.anchor:
        if c.kind == "event" and c.op == "IS":
            ex = ev.get(c.name).exposes
            if "ZONE_LO" in ex and "ZONE_HI" in ex:
                zone = (level_arrays("ZONE_LO", c)[0], level_arrays("ZONE_HI", c)[0])
                break
    return Program(
        n_tr=n_tr, long=long, expires=spec.expires_after,
        CL=np.asarray(cp.rows, dtype=np.int32).reshape(-1, 8),
        CP=np.asarray(cp.tols, dtype=np.float64),
        anchor_rows=np.asarray([cp.rows[i][C_ROW] for i in anchor_ix], dtype=np.int32),
        ctx_cl=np.asarray(ctx_ix, dtype=np.int32),
        TI=TI, CAP=CAP, NCAP=NCAP,
        sw_lo=-1 if sw is None else sw[0], sw_hi=-1 if sw is None else sw[1],
        stop_kind=STOP_CODES[s.kind], stop_reg=stop_reg, stop_row=stop_row,
        stop_buf=float(s.buffer_atr), stop_mult=float(s.atr_mult), max_risk=float(s.max_risk_atr),
        tgt_next=t.kind == "next_structure", tgt_r=float(t.r),
        tgt_fallback=float(t.fallback_r), tgt_min_space=float(t.min_space_r), tgt_rows=tgt_rows,
        prefix_keys=tuple(keys), stage_zids=tuple(stage_zids), zone_arrays=zone,
    )
