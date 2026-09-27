"""A small deterministic event-loop for strategy research."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from monitoring.metrics import TradeMetrics, TradeOutcome, calculate_trade_metrics


@dataclass(frozen=True, slots=True, kw_only=True)
class BacktestEvent:
    event_id: str
    instrument: str
    event_time: datetime
    available_at: datetime
    price: Decimal

    def __post_init__(self) -> None:
        if self.event_time.utcoffset() is None or self.available_at.utcoffset() is None:
            raise ValueError("event timestamps must be timezone-aware")
        event_time = self.event_time.astimezone(UTC)
        available_at = self.available_at.astimezone(UTC)
        if available_at < event_time:
            raise ValueError("available_at cannot precede event_time")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("price must be finite and positive")
        object.__setattr__(self, "event_time", event_time)
        object.__setattr__(self, "available_at", available_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class BacktestResult:
    split: str
    event_count: int
    metrics: TradeMetrics

    def to_json(self) -> str:
        return json.dumps(
            {
                "event_count": self.event_count,
                "metrics": self.metrics.to_dict(),
                "split": self.split,
            },
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )


Strategy = Callable[[BacktestEvent, tuple[BacktestEvent, ...]], TradeOutcome | None]


def run_backtest(
    events: tuple[BacktestEvent, ...],
    strategy: Strategy,
    *,
    initial_equity: Decimal = Decimal("1"),
    split: str,
) -> BacktestResult:
    if split not in {"is", "oos"}:
        raise ValueError("split must be 'is' or 'oos'")
    ordered = tuple(
        sorted(events, key=lambda event: (event.available_at, event.event_time, event.event_id))
    )
    history: list[BacktestEvent] = []
    trades: list[TradeOutcome] = []
    for event in ordered:
        history.append(event)
        trade = strategy(event, tuple(history))
        if trade is not None:
            trades.append(trade)
    return BacktestResult(
        split=split,
        event_count=len(ordered),
        metrics=calculate_trade_metrics(tuple(trades), initial_equity=initial_equity),
    )
