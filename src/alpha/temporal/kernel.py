# ruff: noqa: SIM110
"""Numba all-instances NFA kernel for the V2 temporal engine.

Semantics: docs/V2_TEMPORAL_ENGINE.md sections 3, 8 and 9, matched exactly against
``alpha.temporal.reference`` (the streaming oracle).  Stage-wise formulation: ``P_0`` are the
anchor bars, ``P_{k+1} = dedup(advance(P_k, T_{k+1}))``; each partial match scans forward from its
enter bar and advances at its FIRST qualifying bar or dies.  Order per bar u: run boundary /
window (implicit in the scan limits), invalidate, trigger, guards, captures.  Dedup per stage:
the instances advanced to a stage at bar u keep the largest (anchor_idx, register tuple), ties keep
the earliest source (the oracle's list order).  Everything read at bar u is an array value <= u:
the scan never indexes beyond u, and the precomputed run-length / last-seen rows are prefix
recurrences.  No fastmath: every float operation is the same IEEE operation as the oracle's.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numba import njit

from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays

SRC_LEVEL, SRC_LOW, SRC_HIGH, SRC_CLOSE, SRC_MINLOW, SRC_MAXHIGH = range(6)
N_REG = 4


# ------------------------------------------------------------------------------ prefix rows
@njit(cache=True)
def derive_not(flag):
    out = np.empty(len(flag), dtype=np.uint8)
    for t in range(len(flag)):
        out[t] = 1 if flag[t] == 0 else 0
    return out


@njit(cache=True)
def derive_hold(flag, rs, k):
    """HOLD(k): flag true on all of (u-k, u], all inside the run of u."""
    n = len(flag)
    out = np.zeros(n, dtype=np.uint8)
    run = 0
    for u in range(n):
        if flag[u] != 0:
            run += 1
        else:
            run = 0
        if u - k + 1 >= rs[u] and run >= k:
            out[u] = 1
    return out


@njit(cache=True)
def derive_before(flag, rs, k):
    """BEFORE(k): flag true at least once in (u-k, u), strictly before u, run-local."""
    n = len(flag)
    out = np.zeros(n, dtype=np.uint8)
    last = -1  # last index <= u-1 with flag true
    for u in range(n):
        t0 = max(u - k + 1, rs[u])
        if u - 1 >= t0 and last >= t0:
            out[u] = 1
        if flag[u] != 0:
            last = u
    return out


@njit(cache=True)
def build_lastseen(F8, rows):
    """LS[a, t] = last index <= t where flag row rows[a] is true (-1 if none)."""
    n = F8.shape[1]
    out = np.empty((len(rows), n), dtype=np.int32)
    for a in range(len(rows)):
        r = rows[a]
        last = -1
        for t in range(n):
            if F8[r, t] != 0:
                last = t
            out[a, t] = last
    return out


# ------------------------------------------------------------------------------ clauses
@njit(cache=True)
def _bound_base(code, tol, k, r, t, h, low, c, atr, rs):
    if not np.isfinite(r):
        return False
    a = atr[t]
    cl = c[t]
    if code == 0:  # TOUCH (long frame)
        return low[t] <= r + tol * a and cl > r
    if code == 1:  # TOUCH (short frame)
        return h[t] >= r - tol * a and cl < r
    if code == 2:  # BREAK_UP
        return t - 1 >= rs[t] and cl > r + tol * a and c[t - 1] <= r + tol * a
    if code == 3:  # BREAK_DN
        return t - 1 >= rs[t] and cl < r - tol * a and c[t - 1] >= r - tol * a
    if code == 4:  # RECLAIM_UP
        if not cl > r:
            return False
        for x in range(max(t - k + 1, rs[t]), t):
            if c[x] < r:
                return True
        return False
    if code == 5:  # RECLAIM_DN
        if not cl < r:
            return False
        for x in range(max(t - k + 1, rs[t]), t):
            if c[x] > r:
                return True
        return False
    if code == 6:  # RETEST_HOLD_UP
        if not (low[t] <= r + tol * a and cl > r):
            return False
        for x in range(max(t - k + 1, rs[t]), t):
            if c[x] > r + tol * atr[x]:
                return True
        return False
    if code == 7:  # RETEST_HOLD_DN
        if not (h[t] >= r - tol * a and cl < r):
            return False
        for x in range(max(t - k + 1, rs[t]), t):
            if c[x] < r - tol * atr[x]:
                return True
        return False
    lo = t - k + 1
    if lo < rs[t]:
        return False
    if code == 8:  # HOLD_ABOVE
        for x in range(lo, t + 1):
            if not c[x] > r:
                return False
        return True
    for x in range(lo, t + 1):  # HOLD_BELOW
        if not c[x] < r:
            return False
    return True


@njit(cache=True)
def _bound_true(ci, u, enter, regs, CL, CP, h, low, c, atr, rs):
    """Bound clause (with its op wrapper) at bar u against the instance registers."""
    op = CL[ci, 1]
    arg = CL[ci, 2]
    rsu = rs[u]
    code = CL[ci, 7]
    r = regs[CL[ci, 4]]
    k = CL[ci, 5]
    tol = CP[ci]
    if op == 0:
        return _bound_base(code, tol, k, r, u, h, low, c, atr, rs)
    if op == 1:
        return not _bound_base(code, tol, k, r, u, h, low, c, atr, rs)
    if op == 2:
        lo = u - arg + 1
        if lo < rsu:
            return False
        for t in range(lo, u + 1):
            if not _bound_base(code, tol, k, r, t, h, low, c, atr, rs):
                return False
        return True
    if op == 3:
        for t in range(max(u - arg + 1, rsu), u):
            if _bound_base(code, tol, k, r, t, h, low, c, atr, rs):
                return True
        return False
    for t in range(max(enter + 1, rsu), u + 1):
        if _bound_base(code, tol, k, r, t, h, low, c, atr, rs):
            return True
    return False


@njit(cache=True, inline="always")
def _clause_true(ci, u, enter, regs, CL, CP, F8, LS, h, low, c, atr, rs):
    if CL[ci, 0] == 0:
        if CL[ci, 1] == 0:  # IS / NOT / HOLD(k) / BEFORE(k) are pre-derived into the row itself
            return F8[CL[ci, 3], u] != 0
        return LS[CL[ci, 6], u] >= max(enter + 1, rs[u])  # SINCE_ENTER: (enter, u]
    return _bound_true(ci, u, enter, regs, CL, CP, h, low, c, atr, rs)


@njit(cache=True)
def _capture_value(src, row, L64, u, low, h, c, rmin, rmax):
    if src == 0:
        return L64[row, u]
    if src == 1:
        return low[u]
    if src == 2:
        return h[u]
    if src == 3:
        return c[u]
    if src == 4:
        return min(rmin, low[u])
    return max(rmax, h[u])


@njit(cache=True)
def _greater(anchor_a, regs_a, anchor_b, regs_b):
    """(anchor, regs) key comparison with NaN registers ranking lowest (oracle dedup key)."""
    if anchor_a != anchor_b:
        return anchor_a > anchor_b
    for i in range(4):
        x = regs_a[i]
        y = regs_b[i]
        if x != x:
            x = -np.inf
        if y != y:
            y = -np.inf
        if x > y:
            return True
        if x < y:
            return False
    return False


# ------------------------------------------------------------------------------ stages
@njit(cache=True)
def anchor_stage(arows, cap0, ncap0, F8, L64, h, low, c):
    """P_0: bars where every anchor flag row is true (anchor clauses are never bound/SINCE)."""
    n = len(c)
    idx = np.empty(n, dtype=np.int32)
    m = 0
    r0 = arows[0]
    for u in range(n):
        if F8[r0, u] == 0:
            continue
        ok = True
        for q in range(1, len(arows)):
            if F8[arows[q], u] == 0:
                ok = False
                break
        if ok:
            idx[m] = u
            m += 1
    ent = np.empty((m, 1), dtype=np.int32)
    regs_out = np.empty((m, 4), dtype=np.float64)
    k = 0
    for a in range(m):
        u = idx[a]
        for i in range(4):
            regs_out[k, i] = np.nan
        ok = True
        for q in range(ncap0):
            x = _capture_value(cap0[q, 1], cap0[q, 2], L64, u, low, h, c, np.inf, -np.inf)
            if not np.isfinite(x):
                ok = False
                break
            regs_out[k, cap0[q, 0]] = x
        if not ok:
            continue
        ent[k, 0] = u
        k += 1
    return ent[:k].copy(), regs_out[:k].copy()


@njit(cache=True)
def _flag_code_next(tirow, CL, F8, n):
    """nz[u] = first index >= u where an invalidate or trigger+guards flag row fires (n = none).
    Only valid for flag-only transitions (no bound clause, no SINCE_ENTER)."""
    ng = tirow[2]
    ni = tirow[5]
    trow = CL[tirow[1], 3]
    nz = np.empty(n + 1, dtype=np.int32)
    nz[n] = n
    nxt = n
    for u in range(n - 1, -1, -1):
        fire = False
        for q in range(ni):
            if F8[CL[tirow[6 + q], 3], u] != 0:
                fire = True
                break
        if not fire and F8[trow, u] != 0:
            allg = True
            for q in range(ng):
                if F8[CL[tirow[3 + q], 3], u] == 0:
                    allg = False
                    break
            fire = allg
        if fire:
            nxt = u
        nz[u] = nxt
    return nz


@njit(cache=True)
def advance_stage(
    ent_in, regs_in, tirow, capk, CL, CP, expires, F8, LS, L64, h, low, c, atr, rs, bj, bregs,
    mode,
):
    """P_{k+1} from P_k for one transition. ``bj``/``bregs`` are per-frame scratch (bj all -1).

    ``mode`` 0 = auto, 1 = per-bar scan, 2 = next-fire jump (flag-only transitions only): both
    evaluate the same predicate; the jump skips bars where nothing can fire.
    """
    n_in = ent_in.shape[0]
    w = ent_in.shape[1]
    n = len(c)
    within = tirow[0]
    trig = tirow[1]
    ng = tirow[2]
    ni = tirow[5]
    ncap = tirow[8]
    need_minmax = False
    for q in range(ncap):
        if capk[q, 1] >= 4:
            need_minmax = True
    flag_only = tirow[9] != 0
    use_jump = flag_only and (mode == 2 or (mode == 0 and n_in * 8 > n))
    nz = np.empty(1, dtype=np.int32)
    if use_jump:
        nz = _flag_code_next(tirow, CL, F8, n)
    touched = np.empty(n_in, dtype=np.int64)
    nt = 0
    regs = np.empty(4, dtype=np.float64)
    newr = np.empty(4, dtype=np.float64)
    for j in range(n_in):
        e = ent_in[j, w - 1]
        a0 = ent_in[j, 0]
        hi = min(min(e + within, a0 + expires), n - 1)
        rse = rs[e]
        rmin = np.inf
        rmax = -np.inf
        for i in range(4):
            regs[i] = regs_in[j, i]
        u_hit = -1
        if use_jump:
            us = nz[e + 1]
            if us <= hi and rs[us] == rse:
                fired_inv = False
                for q in range(ni):
                    if F8[CL[tirow[6 + q], 3], us] != 0:
                        fired_inv = True
                        break
                if not fired_inv:
                    u_hit = us
                    if need_minmax:
                        for x in range(e + 1, us):
                            rmin = min(rmin, low[x])
                            rmax = max(rmax, h[x])
        else:
            for u in range(e + 1, hi + 1):
                if rs[u] != rse:  # run boundary kills the instance
                    break
                dead = False
                for q in range(ni):
                    if _clause_true(tirow[6 + q], u, e, regs, CL, CP, F8, LS, h, low, c, atr, rs):
                        dead = True
                        break
                if dead:
                    break
                ok = _clause_true(trig, u, e, regs, CL, CP, F8, LS, h, low, c, atr, rs)
                if ok:
                    for q in range(ng):
                        if not _clause_true(
                            tirow[3 + q], u, e, regs, CL, CP, F8, LS, h, low, c, atr, rs
                        ):
                            ok = False
                            break
                if ok:
                    u_hit = u
                    break  # earliest advance
                rmin = min(rmin, low[u])
                rmax = max(rmax, h[u])
        if u_hit < 0:
            continue
        u = u_hit
        for i in range(4):
            newr[i] = regs[i]
        valid = True
        for q in range(ncap):
            x = _capture_value(capk[q, 1], capk[q, 2], L64, u, low, h, c, rmin, rmax)
            if not np.isfinite(x):
                valid = False
                break
            newr[capk[q, 0]] = x
        if valid:
            cur = bj[u]
            if cur < 0:
                bj[u] = j
                for i in range(4):
                    bregs[u, i] = newr[i]
                touched[nt] = u
                nt += 1
            elif _greater(a0, newr, ent_in[cur, 0], bregs[u]):
                bj[u] = j
                for i in range(4):
                    bregs[u, i] = newr[i]
    order = np.sort(touched[:nt])
    ent_out = np.empty((nt, w + 1), dtype=np.int32)
    regs_out = np.empty((nt, 4), dtype=np.float64)
    for m in range(nt):
        u = order[m]
        j = bj[u]
        for i in range(w):
            ent_out[m, i] = ent_in[j, i]
        ent_out[m, w] = u
        for i in range(4):
            regs_out[m, i] = bregs[u, i]
        bj[u] = -1
    return ent_out, regs_out


@njit(cache=True)
def finalize_stage(
    ent, regs, ctx_cl, CL, CP, F8, LS, L64, h, low, c, atr, rs, bm, long_side, sw_lo,
    sw_hi, stop_kind, stop_reg, stop_row, stop_buf, stop_mult, max_risk, tgt_next, tgt_r,
    tgt_fallback, tgt_min_space, tgt_rows,
):
    m = ent.shape[0]
    w = ent.shape[1]
    keep = np.empty(m, dtype=np.int64)
    stop_o = np.empty(m, dtype=np.float64)
    tgt_o = np.empty(m, dtype=np.float64)
    tr_o = np.empty(m, dtype=np.float64)
    nk = 0
    for j in range(m):
        u = ent[j, w - 1]
        ok = True
        for q in range(len(ctx_cl)):
            if not _clause_true(ctx_cl[q], u, u, regs[j], CL, CP, F8, LS, h, low, c, atr, rs):
                ok = False
                break
        if not ok:
            continue
        if sw_hi >= 0:
            b = bm[u]
            if not (sw_lo <= b and b < sw_hi):
                continue
        close = c[u]
        a = atr[u]
        if stop_kind == 0:
            base = regs[j, stop_reg]
            off = stop_buf * a
            stop = base - off if long_side else base + off
        elif stop_kind == 1:
            lvl = L64[stop_row, u]
            off = stop_buf * a
            stop = lvl - off if long_side else lvl + off
        else:
            stop = close - stop_mult * a if long_side else close + stop_mult * a
        if not (np.isfinite(stop) and np.isfinite(a) and a > 0.0):
            continue
        if long_side:
            if not stop < close:
                continue
        elif not stop > close:
            continue
        risk = abs(close - stop)
        if risk > max_risk * a + 1e-12:
            continue
        target = np.nan
        target_r = tgt_r
        if tgt_next:
            found = False
            best = np.nan
            for q in range(len(tgt_rows)):
                x = L64[tgt_rows[q], u]
                if not np.isfinite(x):
                    continue
                if long_side:
                    if not x > close:
                        continue
                    if (not found) or x < best:
                        best = x
                        found = True
                else:
                    if not x < close:
                        continue
                    if (not found) or x > best:
                        best = x
                        found = True
            if not found:
                target_r = tgt_fallback
            else:
                space = (best - close) / risk if long_side else (close - best) / risk
                if space < tgt_min_space - 1e-12:
                    continue
                target = best
                target_r = space
        keep[nk] = j
        stop_o[nk] = stop
        tgt_o[nk] = target
        tr_o[nk] = target_r
        nk += 1
    return keep[:nk].copy(), stop_o[:nk].copy(), tgt_o[:nk].copy(), tr_o[:nk].copy()


# ------------------------------------------------------------------------------ wrappers
@dataclass(frozen=True)
class Stage:
    """One stage result P_k: ent[n, k+1] enter bars (col 0 = anchor), regs[n, 4]."""

    ent: np.ndarray
    regs: np.ndarray

    @property
    def nbytes(self) -> int:
        return int(self.ent.nbytes + self.regs.nbytes)

    def __len__(self) -> int:
        return len(self.ent)


class Workspace:
    """Per-frame scratch reused by every advance call (bj is restored to all -1)."""

    def __init__(self, n: int) -> None:
        self.bj = np.full(n, -1, dtype=np.int32)
        self.bregs = np.empty((n, N_REG), dtype=np.float64)


def run_anchor(prog, pa) -> Stage:
    ent, regs = anchor_stage(
        prog.anchor_rows, prog.CAP[0], int(prog.NCAP[0]), pa.F8, pa.L64, pa.h, pa.l, pa.c,
    )
    return Stage(ent, regs)


def run_advance(prog, pa, k: int, stage: Stage, ws: Workspace, mode: int = 0) -> Stage:
    """Advance ``stage`` (= P_k) through transition ``k`` (0-based) -> P_{k+1}."""
    if len(stage) == 0:
        return Stage(np.empty((0, stage.ent.shape[1] + 1), dtype=np.int32),
                     np.empty((0, N_REG), dtype=np.float64))
    ent, regs = advance_stage(
        stage.ent, stage.regs, prog.TI[k], prog.CAP[k + 1], prog.CL, prog.CP, prog.expires,
        pa.F8, pa.LS, pa.L64, pa.h, pa.l, pa.c, pa.atr, pa.rs, ws.bj, ws.bregs, mode,
    )
    return Stage(ent, regs)


def run_finalize(prog, pa, stage: Stage):
    if len(stage) == 0:
        e = np.empty(0)
        return np.empty(0, dtype=np.int64), e, e.copy(), e.copy()
    return finalize_stage(
        stage.ent, stage.regs, prog.ctx_cl, prog.CL, prog.CP, pa.F8, pa.LS, pa.L64, pa.h,
        pa.l, pa.c, pa.atr, pa.rs, pa.bm, prog.long, prog.sw_lo, prog.sw_hi, prog.stop_kind,
        prog.stop_reg, prog.stop_row, prog.stop_buf, prog.stop_mult, prog.max_risk,
        prog.tgt_next, prog.tgt_r, prog.tgt_fallback, prog.tgt_min_space, prog.tgt_rows,
    )


# ------------------------------------------------------------------------------ results
@dataclass(frozen=True)
class TemporalTrails:
    """Evidence trails (one row per candidate) for the confluence contract."""

    anchor_idx: np.ndarray  # int64 [m]
    step_idx: np.ndarray  # int64 [m, n_states]: enter bar per state, last == decision_idx
    registers: np.ndarray  # float64 [m, MAX_REG], NaN where never captured
    event_ids: np.ndarray  # int64 [m, n_zid]: zid of lv-captured levels, in capture order
    entry_zone_lo: np.ndarray  # float64 [m]
    entry_zone_hi: np.ndarray  # float64 [m]

    def __len__(self) -> int:
        return len(self.anchor_idx)

    def to_instance_trails(self):
        """Oracle-comparable ``InstanceTrail`` tuples."""
        from alpha.temporal.reference import InstanceTrail

        return tuple(
            InstanceTrail(
                anchor_idx=int(self.anchor_idx[i]),
                step_idx=tuple(int(x) for x in self.step_idx[i]),
                registers=tuple(float(x) for x in self.registers[i]),
                event_ids=tuple(int(x) for x in self.event_ids[i]),
                entry_zone_lo=float(self.entry_zone_lo[i]),
                entry_zone_hi=float(self.entry_zone_hi[i]),
            )
            for i in range(len(self))
        )


@dataclass(frozen=True)
class TemporalResult:
    candidates: CandidateArrays
    trails: TemporalTrails

    def to_bytes(self) -> bytes:
        c, t = self.candidates, self.trails
        parts = (c.decision_idx, c.direction, c.stop, c.target, c.target_r, c.exit_kind,
                 t.anchor_idx, t.step_idx, t.registers, t.event_ids, t.entry_zone_lo,
                 t.entry_zone_hi)
        return b"".join(np.ascontiguousarray(p).tobytes() for p in parts)


def assemble(prog, frame, stage: Stage, keep, stop, target, target_r) -> TemporalResult:
    """Build the public result from the finalize survivors (pure numpy, exact copies)."""
    ent = stage.ent[keep].astype(np.int64)
    m = len(keep)
    cand = CandidateArrays(
        decision_idx=ent[:, -1] if m else np.empty(0, dtype=np.int64),
        direction=np.full(m, 1 if prog.long else -1, dtype=np.int8),
        stop=stop, target=target, target_r=target_r,
        exit_kind=np.full(m, EXIT_FIXED_R, dtype=np.int8),
    )
    zid_cols = [
        np.asarray(frame.arrays[name])[ent[:, st]].astype(np.int64)
        for group in prog.stage_zids for st, name in group
    ]
    event_ids = np.stack(zid_cols, axis=1) if zid_cols else np.empty((m, 0), dtype=np.int64)
    lo = np.full(m, np.nan)
    hi = np.full(m, np.nan)
    if prog.zone_arrays is not None:
        lo_a, hi_a = prog.zone_arrays
        if lo_a in frame.arrays:
            lo = np.asarray(frame.arrays[lo_a], dtype=np.float64)[ent[:, 0]]
        if hi_a in frame.arrays:
            hi = np.asarray(frame.arrays[hi_a], dtype=np.float64)[ent[:, 0]]
    trails = TemporalTrails(
        anchor_idx=ent[:, 0].copy(), step_idx=ent, registers=stage.regs[keep].copy(),
        event_ids=event_ids, entry_zone_lo=lo, entry_zone_hi=hi,
    )
    return TemporalResult(cand, trails)
