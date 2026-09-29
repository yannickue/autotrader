"""Candidate strategy abstraction and AR1 evaluation bridge."""

from .candidate import (
    ENTRY_INTENT,
    CandidateStrategy,
    CandidateStrategyBase,
    SignalCandidate,
    assert_truncation_invariant,
    generate_candidates,
)
from .conflicts import ConflictRecord, record_conflicts
from .evaluation import EvaluationResult, ar1_metrics, evaluate_candidates, grouped_metrics

__all__ = [
    "ENTRY_INTENT",
    "CandidateStrategy",
    "CandidateStrategyBase",
    "ConflictRecord",
    "EvaluationResult",
    "SignalCandidate",
    "ar1_metrics",
    "assert_truncation_invariant",
    "evaluate_candidates",
    "generate_candidates",
    "grouped_metrics",
    "record_conflicts",
]
