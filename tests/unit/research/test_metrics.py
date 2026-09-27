from decimal import Decimal

import pytest

from monitoring.metrics import TradeOutcome, calculate_trade_metrics


def test_trade_metrics_include_returns_risk_and_all_costs() -> None:
    trades = (
        TradeOutcome(
            gross_pnl=Decimal("12"),
            notional=Decimal("100"),
            fees=Decimal("1"),
            spread=Decimal("1"),
            slippage=Decimal("0"),
            funding=Decimal("0"),
        ),
        TradeOutcome(
            gross_pnl=Decimal("-4"),
            notional=Decimal("50"),
            fees=Decimal("1"),
            spread=Decimal("0"),
            slippage=Decimal("1"),
            funding=Decimal("0"),
        ),
    )

    metrics = calculate_trade_metrics(trades, initial_equity=Decimal("100"))

    assert metrics.trade_count == 2
    assert metrics.win_rate == Decimal("0.5")
    assert metrics.average_win == Decimal("10")
    assert metrics.average_loss == Decimal("6")
    assert metrics.expectancy == Decimal("2")
    assert metrics.profit_factor == Decimal("1.666666666666666666666666667")
    assert metrics.max_drawdown == Decimal("0.05454545454545454545454545455")
    assert metrics.turnover == Decimal("150")
    assert metrics.fees == Decimal("2")
    assert metrics.spread == Decimal("1")
    assert metrics.slippage == Decimal("1")
    assert metrics.funding == Decimal("0")
    assert metrics.sharpe is not None
    assert metrics.sortino is not None


def test_empty_trade_metrics_are_defined_without_fake_performance() -> None:
    metrics = calculate_trade_metrics((), initial_equity=Decimal("100"))

    assert metrics.trade_count == 0
    assert metrics.win_rate == Decimal("0")
    assert metrics.profit_factor is None
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.max_drawdown == Decimal("0")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("gross_pnl", Decimal("NaN")),
        ("gross_pnl", Decimal("Infinity")),
        ("notional", Decimal("-Infinity")),
        ("fees", Decimal("NaN")),
        ("spread", Decimal("Infinity")),
        ("slippage", Decimal("-Infinity")),
        ("funding", Decimal("NaN")),
    ),
)
def test_trade_outcome_rejects_every_non_finite_decimal(field, value) -> None:
    values = {"gross_pnl": Decimal("1"), "notional": Decimal("100"), field: value}

    with pytest.raises(ValueError, match=f"{field} must be finite"):
        TradeOutcome(**values)


@pytest.mark.parametrize("field", ("notional", "fees", "spread", "slippage", "funding"))
def test_trade_outcome_rejects_negative_notional_and_costs(field) -> None:
    values = {"gross_pnl": Decimal("1"), "notional": Decimal("100"), field: Decimal("-0.01")}

    with pytest.raises(ValueError, match=f"{field} cannot be negative"):
        TradeOutcome(**values)


@pytest.mark.parametrize("initial_equity", (Decimal("NaN"), Decimal("Infinity")))
def test_metrics_reject_non_finite_initial_equity(initial_equity) -> None:
    with pytest.raises(ValueError, match="initial_equity must be finite"):
        calculate_trade_metrics((), initial_equity=initial_equity)


@pytest.mark.parametrize("terminal_pnl", (Decimal("-100"), Decimal("-101")))
def test_metrics_fail_clearly_when_cumulative_equity_is_not_positive(terminal_pnl) -> None:
    trades = (
        TradeOutcome(gross_pnl=terminal_pnl, notional=Decimal("100")),
        TradeOutcome(gross_pnl=Decimal("1"), notional=Decimal("100")),
    )

    with pytest.raises(ValueError, match="cumulative equity must remain positive"):
        calculate_trade_metrics(trades, initial_equity=Decimal("100"))
