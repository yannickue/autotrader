"""Internal deterministic helpers shared by strategy families."""

from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any

from data.models import MarketSnapshot
from signals.models import Direction, Signal


def validate_snapshots(snapshots: Sequence[MarketSnapshot]) -> None:
    if not snapshots:
        return
    instruments = {snapshot.instrument for snapshot in snapshots}
    if len(instruments) != 1:
        raise ValueError("all snapshots must use the same instrument")
    if any(
        snapshot.timestamp.utcoffset() is None
        or snapshot.timestamp.utcoffset() != timedelta(0)
        for snapshot in snapshots
    ):
        raise ValueError("snapshot timestamps must be UTC")


def bounded_confidence(strength: Decimal, threshold: Decimal) -> Decimal:
    """Return a ranking score, not a probability estimate."""
    return min(Decimal("1"), strength / (threshold * Decimal("2")))


def build_signal(
    *,
    snapshot: MarketSnapshot,
    strategy_id: str,
    family: str,
    direction: Direction,
    invalidation_level: Decimal,
    expected_move: Decimal,
    expected_horizon: timedelta,
    confidence: Decimal,
    features: Mapping[str, Any],
) -> Signal:
    identity = "|".join(
        (
            strategy_id,
            snapshot.instrument,
            snapshot.timestamp.isoformat(),
            direction.value,
            str(sorted(features.items())),
        )
    )
    signal_id = f"{strategy_id}:{sha256(identity.encode()).hexdigest()}"
    return Signal(
        signal_id=signal_id,
        instrument=snapshot.instrument,
        direction=direction,
        timestamp=snapshot.timestamp,
        strategy_id=strategy_id,
        entry_zone=(snapshot.bid, snapshot.ask),
        invalidation_level=invalidation_level,
        expected_move=expected_move,
        expected_horizon=expected_horizon,
        confidence=confidence,
        metadata={
            "schema_version": "1",
            "family": family,
            "source": snapshot.source,
            "feature_attribution": dict(features),
            "confidence_semantics": "ranking_score_not_probability",
        },
    )
