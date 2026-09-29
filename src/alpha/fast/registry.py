"""Registry of fast candidate generators (one per strategy family).

A kernel module in ``alpha/fast/kernels/`` registers itself at import::

    @register("SESSION_SWEEP_REVERSAL", VARIANTS)      # VARIANTS: params dataclass tuple
    def generate(features: FeatureSet, params) -> CandidateArrays: ...

``generate`` is a pure function of the causal FeatureSet arrays and the params: no pandas per bar,
no Python-object construction per bar (Numba kernels over arrays). It returns AR1-compatible
``CandidateArrays`` (decision bar index = the M5 bar whose CLOSE emits the signal, strictly
increasing; direction; stop; NaN target; target_r; exit kind) that must EQUAL, field by field and
bit for bit, the arrays built from the reference candidates of the semantic strategy class.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from alpha.fast.sim import CandidateArrays
from alpha.fast.store import FeatureSet

Generate = Callable[[FeatureSet, Any], CandidateArrays]


@dataclass(frozen=True)
class FastFamily:
    strategy_id: str
    variants: tuple[Any, ...]
    generate: Generate


GENERATORS: dict[str, FastFamily] = {}


def register(strategy_id: str, variants: Sequence[Any]) -> Callable[[Generate], Generate]:
    def wrap(function: Generate) -> Generate:
        if strategy_id in GENERATORS:
            raise ValueError(f"duplicate fast generator for {strategy_id}")
        GENERATORS[strategy_id] = FastFamily(strategy_id, tuple(variants), function)
        return function

    return wrap


def discover() -> dict[str, FastFamily]:
    """Import every module under alpha.fast.kernels so its generators register."""
    import alpha.fast.kernels as kernels

    for info in pkgutil.iter_modules(kernels.__path__):
        importlib.import_module(f"{kernels.__name__}.{info.name}")
    return dict(GENERATORS)
