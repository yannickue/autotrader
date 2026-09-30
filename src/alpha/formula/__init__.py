# ruff: noqa: E501
"""FormulaAlpha: formulaic alpha generation (Alpha158 / AlphaGen-style ideas, NumPy/numba/DEAP only).

Modules: ``ops`` (trailing causal operators) -> ``tree`` (typed expression trees) -> ``fitness``
(Train IC vs forward return) -> ``gp`` (DEAP search, random baseline, null hooks) -> ``signal`` (factor to
CandidateArrays) -> ``evaluate`` (Train-only screen through simulate_fast) ; ``prepare`` builds the Train-only
view; ``gate`` is the sealed later-partition accessor and is never imported by the search side.
"""
