# ruff: noqa: E501
"""Typed expression trees: limits, canonical form (fixed point, semantics preserved), caching."""

from __future__ import annotations

import random

import numpy as np
import pytest
from deap import gp

from alpha.formula import ops, tree
from alpha.formula.gp import build_pset, to_node
from alpha.formula.tree import B, EvalContext, FormulaError, K, T, canonical_hash, canonicalize, op
from tests.test_formula_alpha_synth import planted_data

FD, _, _ = planted_data(12, 3)


def _random_trees(n: int, seed: int, hmax: int = 4):
    random.seed(seed)
    pset = build_pset(list(FD.terminals))
    out = []
    while len(out) < n:
        node = to_node(gp.PrimitiveTree(gp.genHalfAndHalf(pset, 0, hmax)))
        if tree.size(node) <= tree.MAX_NODES:  # the GP enforces this with staticLimit
            out.append(node)
    return out


def test_limits_and_typing():
    x = T("c")
    deep = x
    for _ in range(5):  # depth 6
        deep = op("Abs", deep)
    with pytest.raises(FormulaError):
        tree.validate(deep)
    wide = op("Add", op("Add", op("Add", op("Add", x, x), op("Add", x, x)), op("Add", op("Add", x, x), op("Add", x, x))), x)
    assert tree.size(wide) > tree.MAX_NODES
    with pytest.raises(FormulaError):
        tree.validate(wide)
    with pytest.raises(FormulaError):
        tree.validate(op("Gt", x, K(1)))  # bool root
    with pytest.raises(FormulaError):
        tree.validate(op("IfThenElse", x, x, x))  # float where bool expected
    with pytest.raises(FormulaError):
        tree.validate(op("MA", x, params=(7,)))  # window not in grid
    with pytest.raises(FormulaError):
        tree.validate(op("Add", x))  # arity
    tree.validate(op("IfThenElse", op("Gt", x, K(1)), x, op("MA", x, params=(20,))))
    assert tree.complexity(op("Add", x, K(1))) == 3 and tree.depth(op("Abs", op("Abs", x))) == 3


def test_canonical_rules():
    a, b = T("c"), T("h")
    assert canonicalize(op("Add", a, b)).key == canonicalize(op("Add", b, a)).key
    assert canonical_hash(op("Corr", a, b, params=(10,))) == canonical_hash(op("Corr", b, a, params=(10,)))
    assert canonicalize(op("Sub", a, b)).key != canonicalize(op("Sub", b, a)).key  # not commutative
    assert canonicalize(op("Add", K(1), K(2))) == K(3.0)
    assert canonicalize(op("Add", a, K(0))) == a and canonicalize(op("Mul", K(1), a)) == a and canonicalize(op("Div", a, K(1))) == a
    assert canonicalize(op("Neg", op("Neg", a))) == a
    assert canonicalize(op("Abs", op("Neg", a))) == op("Abs", a)
    assert canonicalize(op("Greater", a, a)) == a
    assert canonicalize(op("Clip", op("Clip", a, params=(3.0,)), params=(1.0,))) == op("Clip", a, params=(1.0,))
    assert canonicalize(op("IfThenElse", B(True), a, b)) == a
    assert canonicalize(op("IfThenElse", op("Lt", K(1), K(2)), a, b)) == a  # folded bool condition
    # NaN-pattern-preserving: x - x is NOT rewritten to 0 (warm-up NaNs must stay NaN)
    assert canonicalize(op("Sub", op("MA", a, params=(20,)), op("MA", a, params=(20,)))).op == "Sub"
    # windowed ops are never folded, even on constants
    assert canonicalize(op("MA", K(1), params=(5,))).op == "MA"


def test_canonical_is_a_fixed_point_and_preserves_semantics():
    ctx = EvalContext(FD)
    for t in _random_trees(400, 1):
        c = canonicalize(t)
        assert canonicalize(c).key == c.key
        assert canonical_hash(t) == canonical_hash(c)
        tree.validate(c)
        assert tree.size(c) <= tree.size(t) and tree.depth(c) <= tree.depth(t)
        a, b = ctx.evaluate(t), ctx.evaluate(c)
        assert np.array_equal(np.isnan(a), np.isnan(b)), t.key
        m = ~np.isnan(a)
        np.testing.assert_allclose(a[m], b[m], rtol=1e-6, atol=1e-9)


def test_random_trees_respect_limits_and_types():
    for t in _random_trees(300, 2):
        assert tree.node_type(t) == "f"
        tree.validate(t)
    random.seed(9)
    pset = build_pset(list(FD.terminals))
    big = [to_node(gp.PrimitiveTree(gp.genFull(pset, 4, 4))) for _ in range(20)]
    for b in big:  # validate() rejects exactly the over-sized ones
        assert (tree.size(b) > tree.MAX_NODES) == (not _valid(b))


def test_batch_cache_shares_subexpressions_and_matches_uncached():
    shared = op("Zscore", T("c"), params=(10,))
    t1 = op("Add", shared, op("MA", T("b_ret1"), params=(5,)))
    t2 = op("Mul", shared, op("Std", T("b_ret1"), params=(20,)))
    ctx = EvalContext(FD)
    a1, a2 = ctx.evaluate(t1), ctx.evaluate(t2)
    assert ctx.hits >= 1  # the shared Zscore prefix was computed once
    fresh = EvalContext(FD)
    np.testing.assert_array_equal(np.nan_to_num(a1, nan=-999), np.nan_to_num(fresh.evaluate(t1), nan=-999))
    np.testing.assert_array_equal(np.nan_to_num(a2, nan=-999), np.nan_to_num(fresh.evaluate(t2), nan=-999))
    small = EvalContext(FD, max_cache_mb=0.05)  # tiny budget: eviction must not change results
    np.testing.assert_array_equal(np.nan_to_num(small.evaluate(t1), nan=-999), np.nan_to_num(a1, nan=-999))
    assert small.stats()["cache_mb"] <= 0.2


def test_evaluate_output_is_finite_or_nan_and_causal_on_prefix():
    for t in _random_trees(60, 5):
        full = tree.evaluate(t, FD)
        assert not np.isinf(full).any()
        m = 700
        part = tree.evaluate(t, FD.prefix(m))
        assert np.array_equal(np.nan_to_num(part, nan=-999), np.nan_to_num(full[:m], nan=-999)), t.key


def test_dominant_family_and_terminals():
    t = op("Add", op("Std", T("c"), params=(5,)), op("Std", T("h"), params=(5,)))
    assert tree.dominant_family(t) == "volatility"
    assert tree.dominant_family(T("c")) == "terminal"
    assert tree.terminals_of(t) == {"c", "h"}
    assert all(name in FD.arrays for name in ("c", "b_cloc", "noise0"))
    assert set(ops.BAR_TERMINALS) <= set(FD.arrays)


def _valid(n) -> bool:
    try:
        tree.validate(n)
    except FormulaError:
        return False
    return True
