# ruff: noqa: E501
"""FormulaAlpha operator registry: STRICTLY TRAILING (causal) operators on 1-D float arrays of M5 bars.

Research only.  Ideas follow the public Alpha158 / AlphaGen style of formulaic alpha operators; no code
is copied from any of those projects (AlphaGen has no licence).

Causality contract (every operator, proven by ``tests/test_formula_alpha_ops.py``)
* the value at bar ``i`` is a function of the inputs at bars ``<= i`` ONLY (future perturbation and
  truncation invariance are tested for every operator);
* windowed operators are RUN-LOCAL: a window never crosses a run boundary (``run_start[i]`` is the first
  bar of the day-contiguous run containing bar ``i``, as ``alpha.fast.store._run_start``); the value is
  NaN until a full window of ``n`` bars lies inside the run (``Ref`` needs ``n`` bars BEFORE ``i``, i.e.
  ``n + 1`` bars in the run);
* any NaN inside the window makes the result NaN (no silent imputation); ``EMA`` restarts after a NaN;
* pointwise operators map NaN to NaN and never return +-inf (inf is turned into NaN);
* protected division: denominator magnitude is floored at ``EPS`` (sign kept), so results are finite.

Boolean-typed operators (``Gt Lt And Or Not``) are represented as float arrays 0.0/1.0 (NaN propagates).

Cross-timeframe access: the frame arrays listed in ``FRAME_FEATURE_TERMINALS`` are FeatureStore columns
that are already causal as of the CLOSE of bar ``i`` (higher-timeframe columns are the last COMPLETED
higher bar, forward filled by the store).  The formula layer only reads them, it never re-derives them;
their causality is the store's responsibility and is spot-checked on real data in
``tests/test_formula_alpha_real.py``.

Window grid for windowed operators: ``WINDOWS = (3, 5, 10, 20, 48, 96)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
from numba import njit

EPS = 1e-6
WINDOWS: tuple[int, ...] = (3, 5, 10, 20, 48, 96)
QUANTILES: tuple[float, ...] = (0.2, 0.5, 0.8)
CLIP_LEVELS: tuple[float, ...] = (1.0, 2.0, 3.0)
CONSTS: tuple[float, ...] = (-2.0, -1.0, -0.5, 0.5, 1.0, 2.0)

# FeatureStore columns usable as leaf terminals (only those present in ``frame.arrays`` are used).
# All are causal at the close of bar i (M5 columns are trailing; m15_*/h1_* are the last COMPLETED
# higher-timeframe bar; dist_*_cash_* / minutes_since_cash_open use the running session only).
FRAME_FEATURE_TERMINALS: tuple[str, ...] = (
    "m5_ema_slope", "m5_rsi14", "m5_adx14", "m5_efficiency_ratio", "m5_bollinger_width",
    "m5_volatility_percentile", "mom_3_atr", "mom_6_atr", "mom_12_atr", "range_ratio_12_48",
    "bar_close_loc", "bar_body_ratio", "bar_range_atr", "compression_expansion_ratio",
    "dist_sess_open_cash_atr", "dist_sess_high_cash_atr", "dist_sess_low_cash_atr",
    "dist_pdh_cash_atr", "dist_pdl_cash_atr", "dist_pdc_cash_atr", "gap_cash_atr",
    "from_high_24_atr", "from_low_24_atr", "minutes_since_cash_open",
    "m15_ema_slope", "m15_rsi14", "m15_range_position", "m15_adx14",
    "h1_ema_slope", "h1_rsi14", "h1_range_position", "h1_adx14",
)
# Terminals computed from the frame's own OHLC (run-local, causal); name -> doc
BAR_TERMINALS: dict[str, str] = {
    "o": "open", "h": "high", "l": "low", "c": "close",
    "b_ret1": "close/prev close - 1 (NaN at run start)",
    "b_tr": "true range / ATR (run start: (h-l)/ATR)",
    "b_range_atr": "(h - l) / ATR",
    "b_body": "(c - o) / (h - l), signed body ratio in [-1, 1]",
    "b_uwick": "(h - max(o,c)) / (h - l)",
    "b_lwick": "(min(o,c) - l) / (h - l)",
    "b_cloc": "(c - l) / (h - l), close location",
}


# ------------------------------------------------------------------------------ numba kernels
@njit(cache=True)
def _k_ref(x, rs, n):
    N = len(x)
    out = np.full(N, np.nan)
    for i in range(N):
        if i - n >= rs[i]:
            out[i] = x[i - n]
    return out


@njit(cache=True)
def _k_sum_mean(x, rs, n, mean):
    N = len(x)
    out = np.full(N, np.nan)
    for i in range(N):
        if i - rs[i] + 1 < n:
            continue
        s = 0.0
        ok = True
        for j in range(i - n + 1, i + 1):
            v = x[j]
            if v != v:
                ok = False
                break
            s += v
        if ok:
            out[i] = s / n if mean else s
    return out


@njit(cache=True)
def _k_ema(x, rs, n):
    N = len(x)
    out = np.full(N, np.nan)
    a = 2.0 / (n + 1.0)
    st = 0.0
    cnt = 0
    for i in range(N):
        if i == rs[i]:
            cnt = 0
        v = x[i]
        if v != v:
            cnt = 0
            continue
        st = v if cnt == 0 else a * v + (1.0 - a) * st
        cnt += 1
        if cnt >= n:
            out[i] = st
    return out


@njit(cache=True)
def _k_moment(x, rs, n, kind):
    # kind: 0 var, 1 std, 2 skew, 3 kurt (excess), 4 mean-abs-dev  (population moments)
    N = len(x)
    out = np.full(N, np.nan)
    for i in range(N):
        if i - rs[i] + 1 < n:
            continue
        s = 0.0
        ok = True
        for j in range(i - n + 1, i + 1):
            v = x[j]
            if v != v:
                ok = False
                break
            s += v
        if not ok:
            continue
        m = s / n
        m2 = 0.0
        m3 = 0.0
        m4 = 0.0
        ad = 0.0
        for j in range(i - n + 1, i + 1):
            d = x[j] - m
            m2 += d * d
            m3 += d * d * d
            m4 += d * d * d * d
            ad += abs(d)
        m2 /= n
        m3 /= n
        m4 /= n
        ad /= n
        if kind == 0:
            out[i] = m2
        elif kind == 1:
            out[i] = np.sqrt(m2)
        elif kind == 2:
            if m2 > 1e-12:
                out[i] = m3 / (m2 * np.sqrt(m2))
        elif kind == 3:
            if m2 > 1e-12:
                out[i] = m4 / (m2 * m2) - 3.0
        else:
            out[i] = ad
    return out


@njit(cache=True)
def _k_minmax(x, rs, n, kind):
    # kind: 0 min, 1 max, 2 argmax age, 3 argmin age (age = bars since the MOST RECENT extreme)
    N = len(x)
    out = np.full(N, np.nan)
    for i in range(N):
        if i - rs[i] + 1 < n:
            continue
        ok = True
        best = x[i]
        age = 0
        if best != best:
            continue
        for j in range(i - 1, i - n, -1):
            v = x[j]
            if v != v:
                ok = False
                break
            if kind == 0 or kind == 3:
                if v < best:
                    best = v
                    age = i - j
            else:
                if v > best:
                    best = v
                    age = i - j
        if not ok:
            continue
        out[i] = age if kind >= 2 else best
    return out


@njit(cache=True)
def _k_rank(x, rs, n):
    # time-series rank of the last value inside the window in (0, 1): (#less + 0.5 * #equal_incl_self) / n
    N = len(x)
    out = np.full(N, np.nan)
    for i in range(N):
        if i - rs[i] + 1 < n:
            continue
        last = x[i]
        if last != last:
            continue
        less = 0.0
        eq = 0.0
        ok = True
        for j in range(i - n + 1, i + 1):
            v = x[j]
            if v != v:
                ok = False
                break
            if v < last:
                less += 1.0
            elif v == last:
                eq += 1.0
        if ok:
            out[i] = (less + 0.5 * eq) / n
    return out


@njit(cache=True)
def _k_zscore(x, rs, n):
    N = len(x)
    out = np.full(N, np.nan)
    for i in range(N):
        if i - rs[i] + 1 < n:
            continue
        s = 0.0
        ok = True
        for j in range(i - n + 1, i + 1):
            v = x[j]
            if v != v:
                ok = False
                break
            s += v
        if not ok:
            continue
        m = s / n
        m2 = 0.0
        for j in range(i - n + 1, i + 1):
            d = x[j] - m
            m2 += d * d
        sd = np.sqrt(m2 / n)
        if sd > 1e-9:
            out[i] = (x[i] - m) / sd
    return out


@njit(cache=True)
def _k_ols(x, rs, n, kind):
    # OLS of the last n values on t = 0..n-1.  kind: 0 slope, 1 r-square, 2 residual of the last point
    N = len(x)
    out = np.full(N, np.nan)
    tm = (n - 1) / 2.0
    stt = n * (n * n - 1.0) / 12.0
    for i in range(N):
        if i - rs[i] + 1 < n:
            continue
        s = 0.0
        ok = True
        for j in range(i - n + 1, i + 1):
            v = x[j]
            if v != v:
                ok = False
                break
            s += v
        if not ok:
            continue
        ym = s / n
        sty = 0.0
        syy = 0.0
        for k in range(n):
            y = x[i - n + 1 + k] - ym
            sty += (k - tm) * y
            syy += y * y
        slope = sty / stt
        if kind == 0:
            out[i] = slope
        elif kind == 1:
            if syy > 1e-12:
                out[i] = 1.0 - (syy - slope * sty) / syy
        else:
            out[i] = (x[i] - ym) - slope * (n - 1 - tm)
    return out


@njit(cache=True)
def _k_quantile(x, rs, n, q):
    N = len(x)
    out = np.full(N, np.nan)
    buf = np.empty(n)
    pos = q * (n - 1)
    lo = int(pos)
    frac = pos - lo
    hi = min(lo + 1, n - 1)
    for i in range(N):
        if i - rs[i] + 1 < n:
            continue
        ok = True
        for k in range(n):
            v = x[i - n + 1 + k]
            if v != v:
                ok = False
                break
            buf[k] = v
        if not ok:
            continue
        buf.sort()
        out[i] = buf[lo] + (buf[hi] - buf[lo]) * frac
    return out


@njit(cache=True)
def _k_pair(x, y, rs, n, corr):
    N = len(x)
    out = np.full(N, np.nan)
    for i in range(N):
        if i - rs[i] + 1 < n:
            continue
        sx = 0.0
        sy = 0.0
        ok = True
        for j in range(i - n + 1, i + 1):
            a = x[j]
            b = y[j]
            if a != a or b != b:
                ok = False
                break
            sx += a
            sy += b
        if not ok:
            continue
        mx = sx / n
        my = sy / n
        cxy = 0.0
        vx = 0.0
        vy = 0.0
        for j in range(i - n + 1, i + 1):
            dx = x[j] - mx
            dy = y[j] - my
            cxy += dx * dy
            vx += dx * dx
            vy += dy * dy
        if corr:
            if vx > 1e-12 and vy > 1e-12:
                out[i] = cxy / np.sqrt(vx * vy)
        else:
            out[i] = cxy / n
    return out


@njit(cache=True)
def _k_true_range(h, lo, c, rs):
    N = len(h)
    out = np.empty(N)
    for i in range(N):
        r = h[i] - lo[i]
        if i - 1 >= rs[i]:
            a = abs(h[i] - c[i - 1])
            b = abs(lo[i] - c[i - 1])
            if a > r:
                r = a
            if b > r:
                r = b
        out[i] = r
    return out


# ------------------------------------------------------------------------------ helpers
def _f64(a) -> np.ndarray:
    return np.ascontiguousarray(a, dtype=np.float64)


def _clean(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float64)
    return np.where(np.isfinite(a), a, np.nan)


def pdiv(a, b) -> np.ndarray:
    """Protected division: |denominator| floored at EPS, sign kept (0 -> +EPS); NaN propagates."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    den = np.where(np.abs(b) < EPS, np.where(b < 0, -EPS, EPS), b)
    with np.errstate(all="ignore"):
        return _clean(np.where(np.isnan(b), np.nan, a / den))


# ------------------------------------------------------------------------------ registry
Fn = Callable[[list, tuple, object], np.ndarray]


@dataclass(frozen=True)
class OpSpec:
    name: str
    arity: int  # number of expression children
    arg_types: tuple[str, ...]  # "f" float / "b" bool per child
    ret_type: str
    params: str  # "" | "n" (window) | "nq" (window, quantile) | "k" (clip level)
    fn: Fn
    family: str
    commutative: bool
    windowed: bool  # True: run-local trailing window (NaN warm-up)
    note: str

    def param_grid(self) -> list[tuple]:
        if self.params == "":
            return [()]
        if self.params == "n":
            return [(n,) for n in WINDOWS]
        if self.params == "nq":
            return [(n, q) for n in WINDOWS for q in QUANTILES]
        if self.params == "k":
            return [(k,) for k in CLIP_LEVELS]
        raise ValueError(self.params)

    def warmup(self, params: tuple) -> int:
        """Bars of the run that are NaN at the start (window operators only)."""
        if not self.windowed:
            return 0
        n = int(params[0]) if params else 0
        return n if self.name in ("Ref", "Delta", "Ret", "LogRet", "ROC") else n - 1


OPS: dict[str, OpSpec] = {}


def _reg(name, arity, arg_types, ret, params, fn, family, *, commutative=False, windowed=False, note=""):
    if name in OPS:
        raise ValueError(f"duplicate op {name}")
    OPS[name] = OpSpec(name, arity, tuple(arg_types), ret, params, fn, family, commutative, windowed, note)


def _ts(kernel, *extra):
    def fn(inp, params, d):
        return kernel(_f64(inp[0]), d.run_start, int(params[0]), *extra)
    return fn


def _ref(x, d, n):
    return _k_ref(_f64(x), d.run_start, int(n))


# time-series, unary (x, n)
_reg("Ref", 1, "f", "f", "n", lambda i, p, d: _ref(i[0], d, p[0]), "momentum", windowed=True,
     note="x[i-n]; NaN for the first n bars of a run")
_reg("Delta", 1, "f", "f", "n", lambda i, p, d: _clean(_f64(i[0]) - _ref(i[0], d, p[0])), "momentum",
     windowed=True, note="x[i] - x[i-n]")
_reg("Ret", 1, "f", "f", "n", lambda i, p, d: pdiv(_f64(i[0]) - _ref(i[0], d, p[0]), np.abs(_ref(i[0], d, p[0]))),
     "momentum", windowed=True, note="(x[i]-x[i-n]) / |x[i-n]| (protected)")
_reg("LogRet", 1, "f", "f", "n",
     lambda i, p, d: _clean(np.log(np.abs(_f64(i[0])) + EPS) - np.log(np.abs(_ref(i[0], d, p[0])) + EPS)),
     "momentum", windowed=True, note="log(|x[i]|+eps) - log(|x[i-n]|+eps)")
_reg("ROC", 1, "f", "f", "n", lambda i, p, d: pdiv(_ref(i[0], d, p[0]), _f64(i[0])), "momentum",
     windowed=True, note="x[i-n] / x[i] (Alpha158 style, protected)")
_reg("MA", 1, "f", "f", "n", _ts(_k_sum_mean, True), "trend", windowed=True, note="trailing mean of the last n bars")
_reg("Sum", 1, "f", "f", "n", _ts(_k_sum_mean, False), "trend", windowed=True, note="trailing sum of the last n bars")
_reg("EMA", 1, "f", "f", "n", _ts(_k_ema), "trend", windowed=True,
     note="alpha=2/(n+1), seeded at the run start (or after a NaN); NaN until n valid bars")
_reg("Var", 1, "f", "f", "n", _ts(_k_moment, 0), "volatility", windowed=True, note="population variance")
_reg("Std", 1, "f", "f", "n", _ts(_k_moment, 1), "volatility", windowed=True, note="population std")
_reg("Skew", 1, "f", "f", "n", _ts(_k_moment, 2), "volatility", windowed=True, note="population skewness; NaN if var~0")
_reg("Kurt", 1, "f", "f", "n", _ts(_k_moment, 3), "volatility", windowed=True, note="population excess kurtosis; NaN if var~0")
_reg("MAD", 1, "f", "f", "n", _ts(_k_moment, 4), "volatility", windowed=True, note="mean absolute deviation around the window mean")
_reg("Min", 1, "f", "f", "n", _ts(_k_minmax, 0), "range", windowed=True, note="trailing minimum (includes bar i)")
_reg("Max", 1, "f", "f", "n", _ts(_k_minmax, 1), "range", windowed=True, note="trailing maximum (includes bar i)")
_reg("ArgmaxAge", 1, "f", "f", "n", _ts(_k_minmax, 2), "range", windowed=True, note="bars since the most recent window maximum (0 = now)")
_reg("ArgminAge", 1, "f", "f", "n", _ts(_k_minmax, 3), "range", windowed=True, note="bars since the most recent window minimum (0 = now)")
_reg("Rank", 1, "f", "f", "n", _ts(_k_rank), "meanrev", windowed=True, note="ts-rank of x[i] in its window: (#less+0.5*#equal)/n")
_reg("Zscore", 1, "f", "f", "n", _ts(_k_zscore), "meanrev", windowed=True, note="(x[i]-mean)/std over the window; NaN if std~0")
_reg("Slope", 1, "f", "f", "n", _ts(_k_ols, 0), "trend", windowed=True, note="OLS slope of the last n values on t=0..n-1")
_reg("Rsquare", 1, "f", "f", "n", _ts(_k_ols, 1), "trend", windowed=True, note="R^2 of that OLS fit")
_reg("Resi", 1, "f", "f", "n", _ts(_k_ols, 2), "meanrev", windowed=True, note="residual of the LAST point vs the OLS fit")
_reg("Quantile", 1, "f", "f", "nq",
     lambda i, p, d: _k_quantile(_f64(i[0]), d.run_start, int(p[0]), float(p[1])), "range", windowed=True,
     note="linear-interpolated window quantile q in {0.2,0.5,0.8}")
# time-series, binary
_reg("Corr", 2, "ff", "f", "n",
     lambda i, p, d: _k_pair(_f64(i[0]), _f64(i[1]), d.run_start, int(p[0]), True), "corr",
     commutative=True, windowed=True, note="rolling Pearson correlation; NaN if either variance ~0")
_reg("Cov", 2, "ff", "f", "n",
     lambda i, p, d: _k_pair(_f64(i[0]), _f64(i[1]), d.run_start, int(p[0]), False), "corr",
     commutative=True, windowed=True, note="rolling population covariance")
# pointwise unary
_reg("Sign", 1, "f", "f", "", lambda i, p, d: np.sign(_f64(i[0])), "logic", note="sign(x), NaN stays NaN")
_reg("Abs", 1, "f", "f", "", lambda i, p, d: np.abs(_f64(i[0])), "logic", note="|x|")
_reg("Neg", 1, "f", "f", "", lambda i, p, d: -_f64(i[0]), "logic", note="-x")
_reg("Log", 1, "f", "f", "", lambda i, p, d: _clean(np.log(np.abs(_f64(i[0])) + EPS)), "logic", note="log(|x|+eps)")
_reg("SSqrt", 1, "f", "f", "", lambda i, p, d: np.sign(_f64(i[0])) * np.sqrt(np.abs(_f64(i[0]))), "logic",
     note="signed sqrt: sign(x)*sqrt(|x|)")
def _pow2(i, _p, _d):
    with np.errstate(over="ignore"):
        return _clean(_f64(i[0]) ** 2)


_reg("Pow2", 1, "f", "f", "", _pow2, "logic", note="x^2 (inf -> NaN)")
_reg("Clip", 1, "f", "f", "k", lambda i, p, d: np.clip(_f64(i[0]), -float(p[0]), float(p[0])), "logic",
     note="clip to [-k, k], k in {1,2,3}")
# pointwise binary
_reg("Add", 2, "ff", "f", "", lambda i, p, d: _clean(_f64(i[0]) + _f64(i[1])), "logic", commutative=True, note="x+y")
_reg("Sub", 2, "ff", "f", "", lambda i, p, d: _clean(_f64(i[0]) - _f64(i[1])), "logic", note="x-y")
_reg("Mul", 2, "ff", "f", "", lambda i, p, d: _clean(_f64(i[0]) * _f64(i[1])), "logic", commutative=True, note="x*y")
_reg("Div", 2, "ff", "f", "", lambda i, p, d: pdiv(i[0], i[1]), "logic", note="protected x/y (|y| floored at eps)")
_reg("Greater", 2, "ff", "f", "", lambda i, p, d: np.fmax(_f64(i[0]), _f64(i[1])) + 0.0 * (_f64(i[0]) + _f64(i[1])),
     "logic", commutative=True, note="max(x,y); NaN if either NaN")
_reg("Less", 2, "ff", "f", "", lambda i, p, d: np.fmin(_f64(i[0]), _f64(i[1])) + 0.0 * (_f64(i[0]) + _f64(i[1])),
     "logic", commutative=True, note="min(x,y); NaN if either NaN")
# boolean-typed
_reg("Gt", 2, "ff", "b", "", lambda i, p, d: np.where(np.isnan(_f64(i[0]) + _f64(i[1])), np.nan, (_f64(i[0]) > _f64(i[1])).astype(np.float64)),
     "logic", note="x > y as 0/1")
_reg("Lt", 2, "ff", "b", "", lambda i, p, d: np.where(np.isnan(_f64(i[0]) + _f64(i[1])), np.nan, (_f64(i[0]) < _f64(i[1])).astype(np.float64)),
     "logic", note="x < y as 0/1")
_reg("And", 2, "bb", "b", "", lambda i, p, d: np.where(np.isnan(_f64(i[0]) + _f64(i[1])), np.nan, ((_f64(i[0]) > 0.5) & (_f64(i[1]) > 0.5)).astype(np.float64)),
     "logic", commutative=True, note="logical and")
_reg("Or", 2, "bb", "b", "", lambda i, p, d: np.where(np.isnan(_f64(i[0]) + _f64(i[1])), np.nan, ((_f64(i[0]) > 0.5) | (_f64(i[1]) > 0.5)).astype(np.float64)),
     "logic", commutative=True, note="logical or")
_reg("Not", 1, "b", "b", "", lambda i, p, d: np.where(np.isnan(_f64(i[0])), np.nan, (_f64(i[0]) <= 0.5).astype(np.float64)),
     "logic", note="logical not")
_reg("IfThenElse", 3, "bff", "f", "",
     lambda i, p, d: np.where(np.isnan(_f64(i[0])), np.nan, np.where(_f64(i[0]) > 0.5, _f64(i[1]), _f64(i[2]))),
     "logic", note="cond ? a : b; NaN if cond NaN")


# ------------------------------------------------------------------------------ bar helpers
def true_range(h, lo, c, run_start) -> np.ndarray:
    """max(h-l, |h-c[-1]|, |l-c[-1]|) with the previous close taken inside the run; run start: h-l."""
    return _k_true_range(_f64(h), _f64(lo), _f64(c), np.ascontiguousarray(run_start, dtype=np.int64))


def atr(h, lo, c, run_start, n: int) -> np.ndarray:
    """Trailing mean of the true range over ``n`` bars (run-local, NaN warm-up)."""
    tr = true_range(h, lo, c, run_start)
    return _k_sum_mean(tr, np.ascontiguousarray(run_start, dtype=np.int64), int(n), True)


def bar_shape(o, h, lo, c) -> dict[str, np.ndarray]:
    """Per-bar shape ratios (pointwise, no look-back)."""
    o, h, lo, c = _f64(o), _f64(h), _f64(lo), _f64(c)
    rng = h - lo
    ok = rng > EPS
    with np.errstate(all="ignore"):
        return {
            "b_body": np.where(ok, (c - o) / rng, np.nan),
            "b_uwick": np.where(ok, (h - np.maximum(o, c)) / rng, np.nan),
            "b_lwick": np.where(ok, (np.minimum(o, c) - lo) / rng, np.nan),
            "b_cloc": np.where(ok, (c - lo) / rng, np.nan),
        }


def _leaf_atr(_inp, params, d):
    return atr(d.h, d.l, d.c, d.run_start, int(params[0]))


def _leaf_tr(_inp, _params, d):
    return true_range(d.h, d.l, d.c, d.run_start)


_reg("ATR", 0, "", "f", "n", _leaf_atr, "volatility", windowed=True,
     note="leaf: trailing mean of the true range over n bars (uses h,l,c of the frame)")
_reg("TR", 0, "", "f", "", _leaf_tr, "volatility",
     note="leaf: true range (previous close inside the run; run start uses h-l)")


def bar_terminals(o, h, lo, c, atr14, run_start) -> dict[str, np.ndarray]:
    """The ``BAR_TERMINALS`` arrays for one frame (all causal, run-local)."""
    o, h, lo, c, a = _f64(o), _f64(h), _f64(lo), _f64(c), _f64(atr14)
    rs = np.ascontiguousarray(run_start, dtype=np.int64)
    out: dict[str, np.ndarray] = {"o": o, "h": h, "l": lo, "c": c}
    prev = _k_ref(c, rs, 1)
    out["b_ret1"] = pdiv(c - prev, np.abs(prev))
    tr = true_range(h, lo, c, rs)
    out["b_tr"] = pdiv(tr, a)
    out["b_range_atr"] = pdiv(h - lo, a)
    out.update(bar_shape(o, h, lo, c))
    return out


def simple_frame(o, h, lo, c, run_start) -> SimpleNamespace:
    """Minimal ``d`` argument for ``OpSpec.fn`` (tests, synthetic data)."""
    return SimpleNamespace(o=_f64(o), h=_f64(h), l=_f64(lo), c=_f64(c),
                           run_start=np.ascontiguousarray(run_start, dtype=np.int64))


def op_names() -> list[str]:
    return sorted(OPS)


__all__ = (
    "BAR_TERMINALS",
    "CLIP_LEVELS",
    "CONSTS",
    "EPS",
    "FRAME_FEATURE_TERMINALS",
    "OPS",
    "QUANTILES",
    "WINDOWS",
    "OpSpec",
    "atr",
    "bar_shape",
    "bar_terminals",
    "op_names",
    "pdiv",
    "simple_frame",
    "true_range",
)
