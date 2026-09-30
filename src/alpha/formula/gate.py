# ruff: noqa: E501
"""SEALED validation gate for a frozen factor.  NEVER import this from search-time code.

The formula search (``ops tree fitness gp signal prepare evaluate``) never touches the Validation
partition; that is asserted by ``tests/test_formula_alpha_seal.py`` (source scan: no other module may
contain the word 'validation' or import this one).  This module is the single accessor, mirroring
``temporal_validation_gate_view``: given an already FROZEN factor (Train-fitted sign, quantile
thresholds fitted on Train bars only), it evaluates the full development frame with ``light_screen``
(embargo-aware ``SplitPlan.mask``) and returns the Validation ``PartitionScreen``.
"""

from __future__ import annotations

import numpy as np

from alpha.common.protocol import SplitPlan
from alpha.common.sim import COST_SCENARIOS, DEFAULT_RULES, DEFAULT_SIZING, SimRules, SizingSpec
from alpha.fast.screen import PartitionScreen, light_screen
from alpha.fast.sim import MarketArrays
from alpha.formula.data import FormulaData
from alpha.formula.fitness import FactorScore
from alpha.formula.signal import SignalSpec, build_candidates, fit_thresholds
from alpha.formula.tree import EvalContext, Node


def validation_gate_screen(
    full_data: FormulaData, full_market: MarketArrays, dates: np.ndarray, plan: SplitPlan,
    node: Node, score: FactorScore, spec: SignalSpec, *, cost: str = "COMBINED_ADVERSE",
    sizing: SizingSpec = DEFAULT_SIZING, rules: SimRules = DEFAULT_RULES,
) -> PartitionScreen:
    dates = np.asarray(dates).astype("datetime64[D]")
    factor = EvalContext(full_data, 128.0).evaluate(node)
    train = plan.mask(dates, plan.train)
    g = spec.sign * factor
    thr = fit_thresholds(g[train], full_data.minute[train], spec.q, spec.session)  # Train bars only
    cands = build_candidates(factor, full_data, spec, thr)
    return light_screen(full_market, cands, COST_SCENARIOS[cost], plan, dates=dates, sizing=sizing, rules=rules).validation


__all__ = ("validation_gate_screen",)
