# ruff: noqa: E501
"""Typed formula expression trees: well-formedness checks, canonical form, hashing, cached batch evaluation.

Node kinds
* ``T`` terminal ``(name,)`` -- a named causal array of ``FormulaData``      (float)
* ``K`` float constant ``(value,)``                                          (float)
* ``B`` bool constant ``(0|1,)``                                             (bool)
* any name in ``ops.OPS`` with ``params`` from its grid and typed children.

Limits: ``MAX_DEPTH = 5`` levels (a lone terminal has depth 1), ``MAX_NODES = 15``; the root is float.

Canonical form (bottom-up, repeated to a fixed point): pointwise constant folding, commutative child
sort, and NaN-pattern-preserving identities (``x+0``, ``x-0``, ``x*1``, ``x/1``, ``--x``, ``||x||``,
``!!x``, ``sign(sign x)``, ``max(x,x)``, ``min(x,x)``, ``x and x``, ``x or x``, nested ``Clip``, constant
``IfThenElse`` condition).  Rewrites that would turn a NaN warm-up value into a number (``x-x -> 0``,
``x*0 -> 0``) are deliberately NOT applied.  Windowed operators are never folded (warm-up / run
boundaries make even ``MA(const)`` non-constant).
"""

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass
from functools import cached_property

import numpy as np

from alpha.formula import ops
from alpha.formula.data import FormulaData

MAX_DEPTH = 5
MAX_NODES = 15


class FormulaError(ValueError):
    """Invalid expression tree."""


@dataclass(frozen=True)
class Node:
    op: str
    params: tuple = ()
    kids: tuple[Node, ...] = ()

    @cached_property
    def key(self) -> str:
        if self.op == "T":
            return f"${self.params[0]}"
        if self.op in ("K", "B"):
            return f"{self.op}{self.params[0]:g}"
        p = "" if not self.params else "[" + ",".join(f"{v:g}" for v in self.params) + "]"
        return f"{self.op}{p}(" + ",".join(k.key for k in self.kids) + ")"

    def __str__(self) -> str:
        return self.key


def T(name: str) -> Node:
    return Node("T", (name,))


def K(value: float) -> Node:
    return Node("K", (float(value),))


def B(value: bool | int) -> Node:
    return Node("B", (1 if value else 0,))


def op(name: str, *kids: Node, params: tuple = ()) -> Node:
    return Node(name, tuple(params), tuple(kids))


def node_type(n: Node) -> str:
    if n.op in ("T", "K"):
        return "f"
    if n.op == "B":
        return "b"
    return ops.OPS[n.op].ret_type


def size(n: Node) -> int:
    return 1 + sum(size(k) for k in n.kids)


def depth(n: Node) -> int:
    return 1 + max((depth(k) for k in n.kids), default=0)


def complexity(n: Node) -> int:
    """Node count (the GP complexity penalty is linear in this)."""
    return size(n)


def validate(n: Node, terminals: set[str] | None = None, *, root: bool = True) -> None:
    """Raise ``FormulaError`` unless ``n`` is a well-typed, in-grid tree within the size limits."""
    if root:
        if node_type(n) != "f":
            raise FormulaError("root must be float typed")
        if depth(n) > MAX_DEPTH:
            raise FormulaError(f"depth {depth(n)} > {MAX_DEPTH}")
        if size(n) > MAX_NODES:
            raise FormulaError(f"size {size(n)} > {MAX_NODES}")
    if n.op == "T":
        if len(n.params) != 1 or n.kids or (terminals is not None and n.params[0] not in terminals):
            raise FormulaError(f"bad terminal {n.params}")
        return
    if n.op in ("K", "B"):
        if len(n.params) != 1 or n.kids or not math.isfinite(n.params[0]):
            raise FormulaError("bad constant")
        if n.op == "B" and n.params[0] not in (0, 1):
            raise FormulaError("bad bool constant")
        return
    spec = ops.OPS.get(n.op)
    if spec is None:
        raise FormulaError(f"unknown op {n.op}")
    if len(n.kids) != spec.arity:
        raise FormulaError(f"{n.op}: arity {len(n.kids)} != {spec.arity}")
    if tuple(n.params) not in spec.param_grid():
        raise FormulaError(f"{n.op}: params {n.params} not in grid")
    for kid, want in zip(n.kids, spec.arg_types, strict=True):
        if node_type(kid) != want:
            raise FormulaError(f"{n.op}: child type {node_type(kid)} != {want}")
        validate(kid, terminals, root=False)


# ------------------------------------------------------------------------------ canonical form
def _const_fold(n: Node) -> Node | None:
    spec = ops.OPS.get(n.op)
    if spec is None or spec.windowed or spec.arity == 0 or not n.kids:
        return None
    if not all(k.op in ("K", "B") for k in n.kids):
        return None
    vals = [np.array([float(k.params[0])]) for k in n.kids]
    with np.errstate(all="ignore"):
        out = float(spec.fn(vals, n.params, None)[0])
    if not math.isfinite(out):
        return None
    return B(out > 0.5) if spec.ret_type == "b" else K(out)


def _is_k(n: Node, v: float) -> bool:
    return n.op == "K" and n.params[0] == v


def _simplify(n: Node) -> Node:
    """One bottom-up pass."""
    if not n.kids:
        return n
    kids = tuple(_simplify(k) for k in n.kids)
    n = Node(n.op, n.params, kids)
    folded = _const_fold(n)
    if folded is not None:
        return folded
    spec = ops.OPS[n.op]
    if spec.commutative:
        n = Node(n.op, n.params, tuple(sorted(kids, key=lambda k: k.key)))
        kids = n.kids
    name = n.op
    if name in ("Add", "Sub") and _is_k(kids[1], 0.0):
        return kids[0]
    if name == "Add" and _is_k(kids[0], 0.0):
        return kids[1]
    if name in ("Mul", "Div") and _is_k(kids[1], 1.0):
        return kids[0]
    if name == "Mul" and _is_k(kids[0], 1.0):
        return kids[1]
    if name in ("Neg", "Not") and kids[0].op == name:
        return kids[0].kids[0]
    if name == "Abs" and kids[0].op in ("Abs", "Neg"):
        return Node("Abs", (), kids[0].kids) if kids[0].op == "Neg" else kids[0]
    if name == "Sign" and kids[0].op == "Sign":
        return kids[0]
    if name in ("Greater", "Less", "And", "Or") and kids[0].key == kids[1].key:
        return kids[0]
    if name == "Clip" and kids[0].op == "Clip":
        return Node("Clip", (min(n.params[0], kids[0].params[0]),), kids[0].kids)
    if name == "IfThenElse" and kids[0].op == "B":
        return kids[1] if kids[0].params[0] == 1 else kids[2]
    return n


def canonicalize(n: Node) -> Node:
    """Fixed-point canonical form (idempotent)."""
    for _ in range(32):
        m = _simplify(n)
        if m.key == n.key:
            return m
        n = m
    raise FormulaError("canonicalisation did not converge")


def canonical_hash(n: Node) -> str:
    return hashlib.sha256(("formula-v1:" + canonicalize(n).key).encode()).hexdigest()[:32]


# ------------------------------------------------------------------------------ evaluation
class EvalContext:
    """Evaluates trees against one ``FormulaData`` with a byte-bounded sub-expression cache.

    The cache is keyed by the sub-tree's structural key, so shared prefixes across a whole batch of
    formulas are computed once.  ``FormulaData`` has no label field, so labels are unreachable.
    """

    def __init__(self, data: FormulaData, max_cache_mb: float = 256.0) -> None:
        self.data = data
        self.max_bytes = int(max_cache_mb * 1024 * 1024)
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    def _put(self, key: str, arr: np.ndarray) -> None:
        arr.setflags(write=False)
        self._cache[key] = arr
        self._bytes += arr.nbytes
        while self._bytes > self.max_bytes and len(self._cache) > 1:
            _, old = self._cache.popitem(last=False)
            self._bytes -= old.nbytes

    def evaluate(self, n: Node) -> np.ndarray:
        if n.op == "T":
            return self.data.column(n.params[0])
        if n.op in ("K", "B"):
            return np.full(len(self.data), float(n.params[0]))
        key = n.key
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            self.hits += 1
            return hit
        self.misses += 1
        spec = ops.OPS[n.op]
        inputs = [self.evaluate(k) for k in n.kids]
        out = np.asarray(spec.fn(inputs, n.params, self.data), dtype=np.float64)
        out = np.where(np.isfinite(out), out, np.nan)
        self._put(key, out)
        return out

    def stats(self) -> dict[str, float]:
        return {"hits": self.hits, "misses": self.misses, "cache_mb": round(self._bytes / 2**20, 1)}


def evaluate(n: Node, data: FormulaData, ctx: EvalContext | None = None) -> np.ndarray:
    """Factor array of ``n`` (float64, NaN where undefined), length ``len(data)``."""
    validate(n)
    return (ctx or EvalContext(data)).evaluate(n)


def dominant_family(n: Node) -> str:
    """Most frequent operator family in the tree (ties: alphabetical); 'terminal' if no operator."""
    counts: dict[str, int] = {}

    def walk(x: Node) -> None:
        if x.op not in ("T", "K", "B"):
            fam = ops.OPS[x.op].family
            if fam != "logic":  # arithmetic glue is not a signal family
                counts[fam] = counts.get(fam, 0) + 1
        for k in x.kids:
            walk(k)

    walk(n)
    if not counts:
        return "logic" if n.op not in ("T", "K", "B") else "terminal"
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def terminals_of(n: Node) -> set[str]:
    out: set[str] = set()
    if n.op == "T":
        out.add(n.params[0])
    for k in n.kids:
        out |= terminals_of(k)
    return out


__all__ = (
    "MAX_DEPTH",
    "MAX_NODES",
    "B",
    "EvalContext",
    "FormulaError",
    "K",
    "Node",
    "T",
    "canonical_hash",
    "canonicalize",
    "complexity",
    "depth",
    "dominant_family",
    "evaluate",
    "node_type",
    "op",
    "size",
    "terminals_of",
    "validate",
)
