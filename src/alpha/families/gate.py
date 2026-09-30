# ruff: noqa: E501
"""SEALED survival gate for FROZEN family specs.  NEVER import this from search-time code or from the probe.

The family search (``spec data common orb gap overnight volrev roundnum leadlag eod registry evaluate`` and
``research/runners/v2_family_probe.py``) never touches a later-fold bar; ``tests/test_v2_families_seal.py`` asserts
that by source scan.  This module is the single accessor of fold TEST data.  Given already-selected specs it

  1. re-fits every Train-fitted number on the TRAIN side of fold 0 only (``full_data.prefix``: the Train slice
     physically contains no later bar),
  2. generates candidates for the FULL development frame with those frozen numbers,
  3. simulates (BASE and COMBINED_ADVERSE, same market wiring as the probe) and
  4. reduces the trades to per-fold TEST metrics (embargo / purge come from ``make_folds``' fold masks) plus a pooled
     number.

Selection is the orchestrator's job: this function returns numbers, it never picks or thresholds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from alpha.discovery.evaluate import cluster_se
from alpha.discovery.folds import Fold
from alpha.families.data import FamilyData
from alpha.families.evaluate import ADVERSE, BASE, SimContext, one_sided_p, simulate
from alpha.families.registry import fit_thresholds, generate_candidates
from alpha.families.spec import FamilySpec
from alpha.fast.screen import screen_partition_trades


@dataclass(frozen=True)
class FoldNumbers:
    fold: int
    n_trades: int
    expectancy_r: float | None
    profit_factor: float | None
    t_day_clustered: float | None
    trades_per_day: float | None


@dataclass(frozen=True)
class GateRow:
    spec_hash: str
    spec: dict[str, Any]
    cost: str
    folds: tuple[FoldNumbers, ...]
    pooled_n_trades: int
    pooled_expectancy_r: float | None
    pooled_t: float | None
    pooled_p_one_sided: float | None
    n_positive_folds: int
    min_fold_expectancy_r: float | None
    base_pooled_expectancy_r: float | None


def _fold_numbers(trades, day_of_bar: np.ndarray, test_mask: np.ndarray, contract: float, k: int) -> tuple[FoldNumbers, np.ndarray]:
    sel = test_mask[trades.entry_idx] if len(trades) else np.zeros(0, dtype=bool)
    n_days = len(np.unique(day_of_bar[test_mask]))
    s = screen_partition_trades(trades, sel, n_days, contract_size=contract)
    r = trades.r_multiple[sel]
    se = cluster_se(r, trades.entry_day[sel]) if len(r) else None
    t = s.expectancy_r / se if (se and se > 0 and s.expectancy_r is not None) else None
    return FoldNumbers(k, s.n_trades, s.expectancy_r, s.profit_factor, t, s.trades_per_day), sel


def survival_gate(top_specs: Sequence[FamilySpec], *, full_data: FamilyData, ctx: SimContext, folds: Sequence[Fold],
                  cost: str = ADVERSE) -> list[GateRow]:
    """Per-fold TEST numbers of ``top_specs`` (thresholds fitted on the fold-0 TRAIN side only)."""
    train_mask = folds[0].train_mask
    stop = int(np.flatnonzero(train_mask).max()) + 1
    if not train_mask[:stop].sum() == train_mask.sum():
        raise ValueError("fold-0 train side must be a bar prefix")
    train_view = full_data.prefix(stop)
    rows: list[GateRow] = []
    for spec in top_specs:
        thr = fit_thresholds(train_view, spec)  # Train bars only
        cands = generate_candidates(full_data, spec, thr)
        if len(cands.decision_idx) == 0:
            rows.append(GateRow(spec.canonical_hash(), spec.to_dict(), cost, (), 0, None, None, None, 0, None, None))
            continue
        trades = simulate(ctx, spec, cands, cost)
        base = simulate(ctx, spec, cands, BASE)
        per, pooled = [], np.zeros(len(trades), dtype=bool)
        for k, f in enumerate(folds):
            fn, sel = _fold_numbers(trades, full_data.day, f.test_mask, ctx.sizing.contract_size, k)
            per.append(fn)
            pooled |= sel
        r = trades.r_multiple[pooled]
        se = cluster_se(r, trades.entry_day[pooled]) if len(r) else None
        exp = float(r.mean()) if len(r) else None
        t = exp / se if (se and se > 0 and exp is not None) else None
        bsel = np.zeros(len(base), dtype=bool)
        for f in folds:
            bsel |= f.test_mask[base.entry_idx] if len(base) else bsel
        e_f = [x.expectancy_r for x in per if x.expectancy_r is not None]
        rows.append(GateRow(
            spec.canonical_hash(), spec.to_dict(), cost, tuple(per), int(pooled.sum()), exp, t, one_sided_p(t),
            sum(1 for e in e_f if e > 0), min(e_f) if e_f else None,
            float(base.r_multiple[bsel].mean()) if bsel.any() else None,
        ))
    return rows


__all__ = ("FoldNumbers", "GateRow", "survival_gate")
