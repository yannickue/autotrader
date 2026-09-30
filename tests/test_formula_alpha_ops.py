# ruff: noqa: E501
"""Every FormulaAlpha operator is STRICTLY TRAILING and run-local.

Per operator (all of ``ops.OPS`` x a parameter sample):
* equals an independent slow reference implementation (also with NaNs inside the input);
* future perturbation: randomising every bar > i0 leaves the values at bars <= i0 unchanged;
* truncation invariance: evaluating on a prefix equals the prefix of the full evaluation;
* NaN warm-up: the first ``warmup`` bars of every run are NaN (windows never cross a run boundary);
* run reset: perturbing everything before a run start leaves the values from that start on unchanged.
"""

from __future__ import annotations

import numpy as np
import pytest

from alpha.formula import ops
from tests.test_formula_alpha_synth import random_ohlc

EPS = ops.EPS
N = 520


def _params_sample(spec: ops.OpSpec) -> list[tuple]:
    grid = spec.param_grid()
    if spec.params == "n":
        return [p for p in grid if p[0] in (3, 20, 96)]
    if spec.params == "nq":
        return [p for p in grid if p[0] in (5, 48) and p[1] in (0.2, 0.8)] + [(20, 0.5)]
    return grid


CASES = [(name, p) for name, spec in sorted(ops.OPS.items()) for p in _params_sample(spec)]


def _ids(case):
    return f"{case[0]}{case[1]}"


def _make_inputs(spec: ops.OpSpec, rng: np.random.Generator, n: int, nan_frac: float = 0.0):
    arrs = []
    for t in spec.arg_types:
        a = rng.normal(size=n)
        if t == "b":
            a = (a > 0).astype(float)
        elif nan_frac:
            a[rng.random(n) < nan_frac] = np.nan
        arrs.append(a)
    return arrs


def _frame(rng, n, rs):
    _, h, lo, c, _ = random_ohlc(n, int(rng.integers(1 << 30)), None)
    o = np.r_[c[0], c[:-1]]
    return ops.simple_frame(o, h, lo, c, rs)


def _pdiv(a, b):
    den = np.where(np.abs(b) < EPS, np.where(b < 0, -EPS, EPS), b)
    return a / den


def _win(x, rs, n, fn):
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        if i - rs[i] + 1 < n:
            continue
        w = x[i - n + 1: i + 1]
        if np.isnan(w).any():
            continue
        out[i] = fn(w)
    return out


def _ref_of(x, rs, n):
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        if i - n >= rs[i]:
            out[i] = x[i - n]
    return out


def _slope_parts(w):
    t = np.arange(len(w))
    b, a = np.polyfit(t, w, 1)
    return t, a, b


def _slow(name, params, inp, d):
    rs = d.run_start
    x = inp[0] if inp else None
    n = int(params[0]) if params and name != "Clip" else 0
    if name == "Ref":
        return _ref_of(x, rs, n)
    if name == "Delta":
        return x - _ref_of(x, rs, n)
    if name == "Ret":
        r = _ref_of(x, rs, n)
        return _pdiv(x - r, np.abs(r))
    if name == "LogRet":
        return np.log(np.abs(x) + EPS) - np.log(np.abs(_ref_of(x, rs, n)) + EPS)
    if name == "ROC":
        return _pdiv(_ref_of(x, rs, n), x)
    if name == "MA":
        return _win(x, rs, n, np.mean)
    if name == "Sum":
        return _win(x, rs, n, np.sum)
    if name == "EMA":
        out = np.full(len(x), np.nan)
        a = 2.0 / (n + 1.0)
        st, cnt = 0.0, 0
        for i in range(len(x)):
            if i == rs[i]:
                cnt = 0
            if np.isnan(x[i]):
                cnt = 0
                continue
            st = x[i] if cnt == 0 else a * x[i] + (1 - a) * st
            cnt += 1
            if cnt >= n:
                out[i] = st
        return out
    if name == "Var":
        return _win(x, rs, n, np.var)
    if name == "Std":
        return _win(x, rs, n, np.std)
    if name == "Skew":
        def f(w):
            m2 = np.mean((w - w.mean()) ** 2)
            return np.mean((w - w.mean()) ** 3) / m2**1.5 if m2 > 1e-12 else np.nan
        return _win(x, rs, n, f)
    if name == "Kurt":
        def f(w):
            m2 = np.mean((w - w.mean()) ** 2)
            return np.mean((w - w.mean()) ** 4) / m2**2 - 3 if m2 > 1e-12 else np.nan
        return _win(x, rs, n, f)
    if name == "MAD":
        return _win(x, rs, n, lambda w: np.mean(np.abs(w - w.mean())))
    if name == "Min":
        return _win(x, rs, n, np.min)
    if name == "Max":
        return _win(x, rs, n, np.max)
    if name == "ArgmaxAge":
        return _win(x, rs, n, lambda w: float(np.argmax(w[::-1])))
    if name == "ArgminAge":
        return _win(x, rs, n, lambda w: float(np.argmin(w[::-1])))
    if name == "Rank":
        return _win(x, rs, n, lambda w: (np.sum(w < w[-1]) + 0.5 * np.sum(w == w[-1])) / len(w))
    if name == "Zscore":
        return _win(x, rs, n, lambda w: (w[-1] - w.mean()) / w.std() if w.std() > 1e-9 else np.nan)
    if name == "Slope":
        return _win(x, rs, n, lambda w: _slope_parts(w)[2])
    if name == "Rsquare":
        def f(w):
            t, a, b = _slope_parts(w)
            sst = np.sum((w - w.mean()) ** 2)
            return 1 - np.sum((w - (a + b * t)) ** 2) / sst if sst > 1e-12 else np.nan
        return _win(x, rs, n, f)
    if name == "Resi":
        def f(w):
            t, a, b = _slope_parts(w)
            return w[-1] - (a + b * t[-1])
        return _win(x, rs, n, f)
    if name == "Quantile":
        q = params[1]
        return _win(x, rs, n, lambda w: np.quantile(w, q))
    if name in ("Corr", "Cov"):
        y = inp[1]
        out = np.full(len(x), np.nan)
        for i in range(len(x)):
            if i - rs[i] + 1 < n:
                continue
            a, b = x[i - n + 1: i + 1], y[i - n + 1: i + 1]
            if np.isnan(a).any() or np.isnan(b).any():
                continue
            cov = np.mean((a - a.mean()) * (b - b.mean()))
            if name == "Cov":
                out[i] = cov
            elif a.var() > 1e-12 and b.var() > 1e-12:
                out[i] = cov / (a.std() * b.std())
        return out
    if name == "Sign":
        return np.sign(x)
    if name == "Abs":
        return np.abs(x)
    if name == "Neg":
        return -x
    if name == "Log":
        return np.log(np.abs(x) + EPS)
    if name == "SSqrt":
        return np.sign(x) * np.sqrt(np.abs(x))
    if name == "Pow2":
        return x**2
    if name == "Clip":
        return np.clip(x, -params[0], params[0])
    y = inp[1] if len(inp) > 1 else None
    if name == "Add":
        return x + y
    if name == "Sub":
        return x - y
    if name == "Mul":
        return x * y
    if name == "Div":
        return _pdiv(x, y)
    if name == "Greater":
        return np.maximum(x, y)
    if name == "Less":
        return np.minimum(x, y)
    if name == "Gt":
        return np.where(np.isnan(x + y), np.nan, (x > y).astype(float))
    if name == "Lt":
        return np.where(np.isnan(x + y), np.nan, (x < y).astype(float))
    if name == "And":
        return np.where(np.isnan(x + y), np.nan, ((x > 0.5) & (y > 0.5)).astype(float))
    if name == "Or":
        return np.where(np.isnan(x + y), np.nan, ((x > 0.5) | (y > 0.5)).astype(float))
    if name == "Not":
        return np.where(np.isnan(x), np.nan, (x <= 0.5).astype(float))
    if name == "IfThenElse":
        return np.where(np.isnan(x), np.nan, np.where(x > 0.5, inp[1], inp[2]))
    if name == "TR":
        tr = d.h - d.l
        for i in range(len(tr)):
            if i - 1 >= rs[i]:
                tr[i] = max(d.h[i] - d.l[i], abs(d.h[i] - d.c[i - 1]), abs(d.l[i] - d.c[i - 1]))
        return tr
    if name == "ATR":
        tr = _slow("TR", (), [], d)
        return _win(tr, rs, n, np.mean)
    raise AssertionError(name)


def _eq(a, b, tol=1e-7):
    assert a.shape == b.shape
    assert np.array_equal(np.isnan(a), np.isnan(b)), "NaN pattern differs"
    m = ~np.isnan(a)
    np.testing.assert_allclose(a[m], b[m], rtol=tol, atol=tol)


def _run(spec, params, inp, d):
    return np.asarray(spec.fn(inp, params, d), dtype=float)


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_matches_slow_reference(case):
    name, params = case
    spec = ops.OPS[name]
    rng = np.random.default_rng(11)
    o, h, lo, c, rs = random_ohlc(N, 3)
    d = ops.simple_frame(o, h, lo, c, rs)
    for nan_frac in (0.0, 0.03):
        inp = _make_inputs(spec, rng, N, nan_frac)
        got = _run(spec, params, [a.copy() for a in inp], d)
        _eq(got, np.asarray(_slow(name, params, inp, d), dtype=float))


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_future_perturbation_leaves_past_unchanged(case):
    name, params = case
    spec = ops.OPS[name]
    rng = np.random.default_rng(5)
    o, h, lo, c, rs = random_ohlc(N, 4)
    inp = _make_inputs(spec, rng, N)
    base = _run(spec, params, [a.copy() for a in inp], ops.simple_frame(o, h, lo, c, rs))
    for i0 in (137, 251, 399):
        inp2 = [a.copy() for a in inp]
        rng2 = np.random.default_rng(i0)
        for a, t in zip(inp2, spec.arg_types, strict=True):
            a[i0 + 1:] = rng2.normal(size=N - i0 - 1) if t == "f" else (rng2.normal(size=N - i0 - 1) > 0)
        o2, h2, l2, c2 = (a.copy() for a in (o, h, lo, c))
        for a in (o2, h2, l2, c2):
            a[i0 + 1:] = rng2.normal(100, 5, N - i0 - 1)
        pert = _run(spec, params, inp2, ops.simple_frame(o2, h2, l2, c2, rs))
        _eq(pert[: i0 + 1], base[: i0 + 1], 0)


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_truncation_invariance(case):
    name, params = case
    spec = ops.OPS[name]
    rng = np.random.default_rng(6)
    o, h, lo, c, rs = random_ohlc(N, 5)
    inp = _make_inputs(spec, rng, N, 0.02)
    full = _run(spec, params, [a.copy() for a in inp], ops.simple_frame(o, h, lo, c, rs))
    for m in (97, 200, 333, 517):
        part = _run(spec, params, [a[:m].copy() for a in inp], ops.simple_frame(o[:m], h[:m], lo[:m], c[:m], rs[:m]))
        _eq(part, full[:m], 0)


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_nan_warmup_and_run_reset(case):
    name, params = case
    spec = ops.OPS[name]
    rng = np.random.default_rng(8)
    o, h, lo, c, rs = random_ohlc(N, 7)
    inp = _make_inputs(spec, rng, N)
    base = _run(spec, params, [a.copy() for a in inp], ops.simple_frame(o, h, lo, c, rs))
    warm = spec.warmup(params)
    starts = np.unique(rs)
    for s in starts:
        L = int((rs == s).sum())
        seg = base[s: s + L]
        assert np.isnan(seg[: min(warm, L)]).all(), f"{name}{params}: warm-up must be NaN at run start {s}"
        if spec.windowed and warm + 2 < L and name not in ("Skew", "Kurt"):
            assert np.isfinite(seg[warm]), f"{name}{params}: first full window should be finite"
    # run reset: everything before a run start is irrelevant to the values from it on
    for s in starts[1:4]:
        inp2 = [a.copy() for a in inp]
        r2 = np.random.default_rng(int(s))
        for a, t in zip(inp2, spec.arg_types, strict=True):
            a[:s] = r2.normal(size=s) if t == "f" else (r2.normal(size=s) > 0)
        o2, h2, l2, c2 = (a.copy() for a in (o, h, lo, c))
        for a in (o2, h2, l2, c2):
            a[:s] = r2.normal(100, 9, s)
        pert = _run(spec, params, inp2, ops.simple_frame(o2, h2, l2, c2, rs))
        _eq(pert[s:], base[s:], 0)


def test_registry_size_and_metadata():
    assert len(ops.OPS) >= 35
    for name, spec in ops.OPS.items():
        assert spec.note, name
        assert spec.ret_type in ("f", "b") and len(spec.arg_types) == spec.arity
        if spec.windowed and spec.params in ("n", "nq"):
            assert {p[0] for p in spec.param_grid()} == set(ops.WINDOWS)
    assert set(ops.WINDOWS) == {3, 5, 10, 20, 48, 96}


def test_protected_division_and_no_inf():
    a = np.array([1.0, -1.0, 0.0, np.nan, 5.0])
    b = np.array([0.0, 0.0, 0.0, 1.0, np.nan])
    out = ops.pdiv(a, b)
    assert np.isfinite(out[:3]).all() and np.isnan(out[3:]).all()
    big = np.array([1e200, -1e200])
    assert np.isnan(ops.OPS["Pow2"].fn([big], (), None)).all()


def test_bar_helpers_are_pointwise_or_run_local():
    o, h, lo, c, rs = random_ohlc(300, 9)
    atr14 = ops.atr(h, lo, c, rs, 14)
    b = ops.bar_terminals(o, h, lo, c, atr14, rs)
    assert set(b) == set(ops.BAR_TERMINALS)
    for k in ("b_body", "b_uwick", "b_lwick", "b_cloc"):
        v = b[k][np.isfinite(b[k])]
        assert v.min() >= -1 - 1e-9 and v.max() <= 1 + 1e-9
    starts = np.flatnonzero(rs == np.arange(300))
    assert np.isnan(b["b_ret1"][starts]).all()  # no previous close inside the run
    # value at i unchanged when the future is randomised
    h2, l2, c2, o2 = (a.copy() for a in (h, lo, c, o))
    for a in (h2, l2, c2, o2):
        a[150:] = np.random.default_rng(1).normal(100, 3, 150)
    b2 = ops.bar_terminals(o2, h2, l2, c2, ops.atr(h2, l2, c2, rs, 14), rs)
    for k in b:
        np.testing.assert_array_equal(b[k][:150], b2[k][:150])


def test_perturbation_harness_would_catch_lookahead():
    """Meta-test: the future-perturbation check fails for a deliberately non-causal operator."""
    rng = np.random.default_rng(0)
    x = rng.normal(size=200)

    def leaky(a):
        return np.r_[a[1:], np.nan]  # value at i reads bar i+1

    base = leaky(x)
    x2 = x.copy()
    x2[101:] = rng.normal(size=99)
    assert not np.array_equal(leaky(x2)[:101], base[:101], equal_nan=True)
