from collections.abc import Callable, Iterable
from decimal import Decimal

from signals.models import Signal


class SignalFusion:
    """Rank and select signals using deterministic, non-LLM policy."""

    def __init__(
        self,
        *,
        min_confidence: Decimal = Decimal("0"),
        veto_conflicting_directions: bool = False,
        veto: Callable[[Signal], bool] | None = None,
    ) -> None:
        self.min_confidence = min_confidence
        self.veto_conflicting_directions = veto_conflicting_directions
        self.veto = veto

    def rank(self, signals: Iterable[Signal | None]) -> tuple[Signal, ...]:
        eligible = (
            signal
            for signal in signals
            if signal is not None
            and signal.confidence >= self.min_confidence
            and (self.veto is None or not self.veto(signal))
        )
        return tuple(
            sorted(
                eligible,
                key=lambda signal: (
                    -signal.confidence,
                    -signal.expected_move,
                    signal.strategy_id,
                    signal.signal_id,
                ),
            )
        )

    def select(self, signals: Iterable[Signal | None]) -> Signal | None:
        ranked = self.rank(signals)
        if not ranked:
            return None
        selected = ranked[0]
        if self.veto_conflicting_directions and any(
            signal.instrument == selected.instrument and signal.direction != selected.direction
            for signal in ranked[1:]
        ):
            return None
        return selected
