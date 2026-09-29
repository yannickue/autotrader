"""Degeneracy audit of the discovery catalog.  Research only.

For every catalog entry x quantile grid point x allowed op x direction, compute the fraction of
TRAIN bars on which the compiled clause is true (through the SAME ``mirror_rule_parts`` the
compiler uses, so floors and mirroring are included).  A clause that is true on almost every
bar carries no selectivity (it is a disguised no-op that only inflates complexity/trial
counts); one that is true on almost no bar cannot produce a sample.  Bounds are the fixed
pre-registered [0.5 %, 95 %] band, not tuned to any result.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

import numpy as np

from alpha.discovery.catalog import Q_GRID, FeaturePool
from alpha.discovery.compile import ThresholdResolver, _snap_clause, mirror_rule_parts
from alpha.discovery.genome import Clause

RARE_BELOW = 0.005
ALWAYS_ABOVE = 0.95


def _cmp(left: np.ndarray, op: str, right: Any) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == "==":
            return left == right
    raise ValueError(op)


def q_points(entry: Any) -> list[float]:
    """Canonical quantile grid points of an entry's declared domain."""
    pts, q = set(), entry.q_lo
    while q < entry.q_hi + 1e-9:
        pts.add(_snap_clause(Clause(entry.name, ">", q, ())).q)
        q += Q_GRID
    pts.add(_snap_clause(Clause(entry.name, ">", entry.q_hi, ())).q)
    return sorted(p for p in pts if entry.q_lo - 1e-9 <= p <= entry.q_hi + 1e-9)


def degeneracy_rows(features: Any, train_mask: np.ndarray, pool: FeaturePool | None = None
                    ) -> list[dict[str, Any]]:
    """One row per (feature, direction, op, q | labels): fraction of TRAIN bars where true."""
    mask = np.asarray(train_mask, dtype=bool)
    n_train = int(mask.sum())
    resolver = ThresholdResolver(features, mask)
    pool = pool or FeaturePool.from_features(features)
    rows: list[dict[str, Any]] = []

    def add(name: str, direction: str, op: str, param: Any, true_mask: np.ndarray) -> None:
        rows.append({"feature": name, "direction": direction, "op": op, "param": param,
                     "frac": float(np.count_nonzero(true_mask[mask])) / max(1, n_train)})

    maps = features.metadata.get("maps", {}).get("regime", {})
    for name, entry in pool.entries.items():
        for direction in ("LONG", "SHORT"):
            short = direction == "SHORT"
            if entry.kind == "label":
                codes = maps.get(entry.dimension, {})
                arr = np.asarray(features[f"regime_{entry.dimension.lower()}"])
                mapping = dict(entry.mirror.labels) if short else {}
                for k in range(1, len(entry.labels)):  # proper subsets: the full set is a no-op
                    for subset in combinations(entry.labels, k):
                        allowed = {mapping.get(x, x) for x in subset}
                        cs = [int(c) for c, lab in codes.items() if lab in allowed]
                        add(name, direction, "in", "|".join(subset), np.isin(arr, cs))
                continue
            if entry.kind in ("flag", "level", "fixed"):
                ops = entry.ops if entry.kind != "flag" else ("==",)
                for op in ops:
                    clause = Clause(name, op, None, ())
                    feat, o, thr, other = mirror_rule_parts(clause, resolver, short)
                    left = np.asarray(features[feat])
                    if entry.kind == "flag":
                        true = left.astype(bool)
                    else:
                        right = np.asarray(features[other]) if other else thr
                        true = _cmp(left.astype(float), o, right)
                    add(name, direction, o, None, true)
                continue
            for q in q_points(entry):
                for op in entry.ops:
                    clause = Clause(name, op, q, ())
                    feat, o, thr, _ = mirror_rule_parts(clause, resolver, short)
                    add(name, direction, o, q,
                        _cmp(np.asarray(features[feat]).astype(float), o, thr))
    return rows


def flagged(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per feature: how many audited clauses are too rare / always true, and the extremes."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        rare, always = r["frac"] < RARE_BELOW, r["frac"] > ALWAYS_ABOVE
        if not (rare or always):
            continue
        d = out.setdefault(r["feature"], {"n_rare": 0, "n_always": 0, "n_total": 0,
                                          "min_frac": 1.0, "max_frac": 0.0, "examples": []})
        d["n_rare"] += rare
        d["n_always"] += always
        d["min_frac"] = min(d["min_frac"], r["frac"])
        d["max_frac"] = max(d["max_frac"], r["frac"])
        if len(d["examples"]) < 3:
            d["examples"].append(f"{r['direction']} {r['op']} {r['param']} -> {r['frac']:.4f}")
    for feat, d in out.items():
        d["n_total"] = sum(1 for r in rows if r["feature"] == feat)
    return out


def render_table(flags: dict[str, dict[str, Any]]) -> str:
    lines = ["| feature | rare (<0.5%) | always (>95%) | of | min frac | max frac | e.g. |",
             "|---|---|---|---|---|---|---|"]
    for f, d in sorted(flags.items()):
        lines.append(f"| {f} | {d['n_rare']} | {d['n_always']} | {d['n_total']} | "
                     f"{d['min_frac']:.4f} | {d['max_frac']:.4f} | {'; '.join(d['examples'])} |")
    return "\n".join(lines)


__all__ = ("ALWAYS_ABOVE", "RARE_BELOW", "degeneracy_rows", "flagged", "q_points",
           "render_table")
