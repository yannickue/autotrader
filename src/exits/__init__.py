"""Deterministic exit engine: standalone, independently testable position-exit
decisioning (stops, targets, trailing, break-even, partial profit taking,
momentum/signal/liquidity/time/emergency exits).

Not yet wired into `src/pipeline`; see `src/exits/engine.py` module docstring
for the intended integration (reduce-only via `RiskEngine.evaluate_reduce_only`
plus `ExitEngine.notify_terminal` on every terminal execution outcome, per
`docs/OPEN_QUESTIONS.md` #24).
"""

from exits.engine import ExitEngine, RiskReleaseGate, apply_evaluation
from exits.models import (
    ExitDecision,
    ExitEvaluation,
    ExitMarketState,
    ExitOutcome,
    ExitPolicy,
    ExitPosition,
    ExitReason,
    PositionSide,
    StopStage,
)

__all__ = [
    "ExitDecision",
    "ExitEngine",
    "ExitEvaluation",
    "ExitMarketState",
    "ExitOutcome",
    "ExitPolicy",
    "ExitPosition",
    "ExitReason",
    "PositionSide",
    "RiskReleaseGate",
    "StopStage",
    "apply_evaluation",
]
