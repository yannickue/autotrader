# ruff: noqa: E501
"""Public entry points of the fast temporal evaluator (compile target of the V2 engine).

``evaluate_temporal`` returns the ``CandidateArrays`` that ``simulate_fast`` / ``light_screen``
consume; ``evaluate_temporal_full`` adds the evidence trails; ``evaluate_temporal_many`` evaluates
a batch with exact prefix sharing (see ``batch``).  Semantics are pinned to
``alpha.temporal.reference.evaluate_reference`` by ``tests/temporal/test_kernel_reference_parity.py``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alpha.fast.sim import CandidateArrays
from alpha.temporal import batch
from alpha.temporal.batch import BatchStats, PrefixCache, SharedFrame
from alpha.temporal.kernel import TemporalResult, TemporalTrails
from alpha.temporal.reference import MarketFrame
from alpha.temporal.spec import StateMachineStrategySpec

__all__ = [
    "BatchStats",
    "PrefixCache",
    "SharedFrame",
    "TemporalResult",
    "TemporalTrails",
    "evaluate_temporal",
    "evaluate_temporal_full",
    "evaluate_temporal_many",
]


def evaluate_temporal_full(spec: StateMachineStrategySpec, frame: MarketFrame) -> TemporalResult:
    """Candidates + evidence trails for one spec (no cache, single process)."""
    return batch.evaluate_many_local([spec], frame, use_cache=False)[0]


def evaluate_temporal(spec: StateMachineStrategySpec, frame: MarketFrame) -> CandidateArrays:
    """The V2 compile target: ``CandidateArrays`` (EXIT_FIXED_R) for ``simulate_fast``."""
    return evaluate_temporal_full(spec, frame).candidates


def evaluate_temporal_many(
    specs: Sequence[StateMachineStrategySpec], frame: MarketFrame, *, workers: int = 1,
    use_cache: bool = True, cache: PrefixCache | None = None,
    cache_bytes: int = batch.DEFAULT_CACHE_BYTES, stats: BatchStats | None = None,
    executor=None, shared: SharedFrame | None = None,
) -> list[TemporalResult]:
    """Evaluate many specs with prefix sharing; order-, cache- and worker-count-independent."""
    return batch.evaluate_many(
        specs, frame, workers=workers, use_cache=use_cache, cache=cache,
        cache_bytes=cache_bytes, stats=stats, executor=executor, shared=shared,
    )
