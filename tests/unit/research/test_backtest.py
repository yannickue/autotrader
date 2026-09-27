from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from monitoring.metrics import TradeOutcome
from research.backtest import BacktestEvent, run_backtest

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_backtest_exposes_only_information_available_at_decision_time() -> None:
    events = (
        BacktestEvent(
            event_id="late-first",
            instrument="BTCUSDT-PERP",
            event_time=NOW,
            available_at=NOW + timedelta(seconds=2),
            price=Decimal("100"),
        ),
        BacktestEvent(
            event_id="early-second",
            instrument="BTCUSDT-PERP",
            event_time=NOW + timedelta(seconds=1),
            available_at=NOW + timedelta(seconds=1),
            price=Decimal("101"),
        ),
    )
    seen: list[tuple[str, tuple[str, ...]]] = []

    def strategy(event: BacktestEvent, history: tuple[BacktestEvent, ...]):
        seen.append((event.event_id, tuple(item.event_id for item in history)))
        if event.event_id == "late-first":
            return TradeOutcome(gross_pnl=Decimal("1"), notional=Decimal("100"))
        return None

    result = run_backtest(events, strategy, initial_equity=Decimal("1000"), split="oos")

    assert seen == [
        ("early-second", ("early-second",)),
        ("late-first", ("early-second", "late-first")),
    ]
    assert result.split == "oos"
    assert result.metrics.trade_count == 1
    assert result.to_json() == result.to_json()


def test_backtest_rejects_events_available_before_they_occur() -> None:
    with pytest.raises(ValueError, match="available_at cannot precede event_time"):
        BacktestEvent(
            event_id="future-leak",
            instrument="BTCUSDT-PERP",
            event_time=NOW,
            available_at=NOW - timedelta(microseconds=1),
            price=Decimal("100"),
        )


def test_backtest_requires_explicit_in_or_out_of_sample_label() -> None:
    with pytest.raises(ValueError, match="split must be 'is' or 'oos'"):
        run_backtest((), lambda _event, _history: None, split="combined")


def test_backtest_event_normalizes_aware_timestamps_to_utc() -> None:
    offset = timezone(timedelta(hours=2))
    event = BacktestEvent(
        event_id="offset",
        instrument="BTCUSDT-PERP",
        event_time=datetime(2026, 1, 1, 2, tzinfo=offset),
        available_at=datetime(2026, 1, 1, 2, 0, 1, tzinfo=offset),
        price=Decimal("100"),
    )

    assert event.event_time == datetime(2026, 1, 1, tzinfo=UTC)
    assert event.event_time.tzinfo is UTC
    assert event.available_at == datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert event.available_at.tzinfo is UTC


@pytest.mark.parametrize("price", (Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")))
def test_backtest_event_rejects_non_positive_or_non_finite_price(price) -> None:
    with pytest.raises(ValueError, match="price must be finite and positive"):
        BacktestEvent(
            event_id="invalid-price",
            instrument="BTCUSDT-PERP",
            event_time=NOW,
            available_at=NOW,
            price=price,
        )
