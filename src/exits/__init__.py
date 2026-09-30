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
    SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED,
    ExitDecision,
    ExitEvaluation,
    ExitMarketState,
    ExitOutcome,
    ExitPolicy,
    ExitPosition,
    ExitReason,
    PositionSide,
    StopStage,
    TakeProfitStage,
    stage_target_price,
    stop_is_unchanged_or_tighter,
)

__all__ = [
    "SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED",
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
    "TakeProfitStage",
    "apply_evaluation",
    "stage_target_price",
    "stop_is_unchanged_or_tighter",
]
