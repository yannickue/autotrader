from datetime import UTC, datetime, timedelta
from decimal import Decimal

from signals.models import Direction, Signal
from strategies import SignalFusion

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def signal(
    signal_id: str,
    direction: Direction,
    confidence: str,
    expected_move: str,
) -> Signal:
    return Signal(
        signal_id=signal_id,
        instrument="BTCUSDT-PERP",
        direction=direction,
        timestamp=NOW,
        strategy_id=f"strategy-{signal_id}",
        entry_zone=(Decimal("99"), Decimal("101")),
        invalidation_level=Decimal("95"),
        expected_move=Decimal(expected_move),
        expected_horizon=timedelta(minutes=15),
        confidence=Decimal(confidence),
        metadata={},
    )


def test_fusion_ranks_optional_signals_deterministically() -> None:
    low = signal("low", Direction.LONG, "0.60", "0.05")
    high_small_move = signal("high-small", Direction.LONG, "0.80", "0.03")
    high_large_move = signal("high-large", Direction.LONG, "0.80", "0.04")
    fusion = SignalFusion(min_confidence=Decimal("0.50"))

    ranked = fusion.rank([None, low, high_small_move, high_large_move])

    assert [item.signal_id for item in ranked] == ["high-large", "high-small", "low"]


def test_fusion_vetoes_conflicting_directions() -> None:
    fusion = SignalFusion(veto_conflicting_directions=True)
    long = signal("long", Direction.LONG, "0.80", "0.03")
    short = signal("short", Direction.SHORT, "0.70", "0.04")

    assert fusion.select([long, None, short]) is None


def test_fusion_applies_deterministic_veto_rule_before_selection() -> None:
    fusion = SignalFusion(veto=lambda candidate: candidate.metadata.get("blocked") is True)
    blocked = Signal(
        signal_id="blocked",
        instrument="BTCUSDT-PERP",
        direction=Direction.LONG,
        timestamp=NOW,
        strategy_id="strategy-blocked",
        entry_zone=(Decimal("99"), Decimal("101")),
        invalidation_level=Decimal("95"),
        expected_move=Decimal("0.06"),
        expected_horizon=timedelta(minutes=15),
        confidence=Decimal("0.90"),
        metadata={"blocked": True},
    )
    allowed = signal("allowed", Direction.LONG, "0.70", "0.03")

    assert fusion.select([blocked, allowed]) == allowed
