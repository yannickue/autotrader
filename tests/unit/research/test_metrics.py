import json
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
    assert metrics.win_rate is None
    assert metrics.average_win is None
    assert metrics.average_loss is None
    assert metrics.expectancy is None
    assert metrics.profit_factor is None
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.max_drawdown == Decimal("0")
    assert metrics.ruined is False
    assert metrics.undefined_metrics == {
        "win_rate": "zero_trades",
        "average_win": "no_winning_trades",
        "average_loss": "no_losing_trades",
        "expectancy": "zero_trades",
        "profit_factor": "zero_trades",
        "sharpe": "insufficient_samples",
        "sortino": "insufficient_samples",
    }


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
def test_total_account_loss_is_reported_as_ruin_not_a_crash(terminal_pnl) -> None:
    trades = (
        TradeOutcome(gross_pnl=terminal_pnl, notional=Decimal("100")),
        TradeOutcome(gross_pnl=Decimal("1"), notional=Decimal("100")),
    )

    metrics = calculate_trade_metrics(trades, initial_equity=Decimal("100"))

    assert metrics.ruined is True
    assert metrics.max_drawdown == Decimal("1")
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.undefined_metrics["sharpe"] == "account_ruined"
    assert metrics.undefined_metrics["sortino"] == "account_ruined"


def test_metrics_never_divide_by_a_zero_or_negative_drawdown_peak() -> None:
    # Ruin on the very first trade: equity hits zero immediately, so peak
    # tracking must stop safely instead of dividing by a non-positive peak.
    trades = (TradeOutcome(gross_pnl=Decimal("-100"), notional=Decimal("100")),)

    metrics = calculate_trade_metrics(trades, initial_equity=Decimal("100"))

    assert metrics.ruined is True
    assert metrics.max_drawdown == Decimal("1")


def test_zero_wins_leaves_average_win_undefined_but_average_loss_defined() -> None:
    trades = (
        TradeOutcome(gross_pnl=Decimal("-5"), notional=Decimal("100")),
        TradeOutcome(gross_pnl=Decimal("-3"), notional=Decimal("100")),
    )

    metrics = calculate_trade_metrics(trades, initial_equity=Decimal("100"))

    assert metrics.win_rate == Decimal("0")
    assert metrics.average_win is None
    assert metrics.average_loss == Decimal("4")
    assert metrics.undefined_metrics["average_win"] == "no_winning_trades"
    # gross_loss is nonzero here, so profit_factor is well-defined (zero
    # gross profit over nonzero gross loss), unlike the "no losses" case.
    assert metrics.profit_factor == Decimal("0")
    assert "profit_factor" not in metrics.undefined_metrics
    # expectancy stays well-defined: the zero win-rate weight zeroes out the
    # undefined average_win term instead of propagating it.
    assert metrics.expectancy == Decimal("-4")


def test_zero_losses_leaves_average_loss_undefined_and_profit_factor_undefined() -> None:
    trades = (
        TradeOutcome(gross_pnl=Decimal("5"), notional=Decimal("100")),
        TradeOutcome(gross_pnl=Decimal("3"), notional=Decimal("100")),
    )

    metrics = calculate_trade_metrics(trades, initial_equity=Decimal("100"))

    assert metrics.win_rate == Decimal("1")
    assert metrics.average_loss is None
    assert metrics.profit_factor is None
    assert metrics.undefined_metrics["average_loss"] == "no_losing_trades"
    assert metrics.undefined_metrics["profit_factor"] == "no_losing_trades"
    assert metrics.expectancy == Decimal("4")


def test_single_sample_returns_are_insufficient_for_sharpe_and_sortino() -> None:
    trades = (TradeOutcome(gross_pnl=Decimal("5"), notional=Decimal("100")),)

    metrics = calculate_trade_metrics(trades, initial_equity=Decimal("100"))

    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.undefined_metrics["sharpe"] == "insufficient_samples"
    assert metrics.undefined_metrics["sortino"] == "insufficient_samples"


def test_zero_variance_returns_leave_sharpe_and_sortino_undefined() -> None:
    # Equal per-trade RETURNS (not equal PnL): 5/100 == 5.25/105 == 0.05, so
    # the return series has zero variance even though equity compounds.
    trades = (
        TradeOutcome(gross_pnl=Decimal("5"), notional=Decimal("100")),
        TradeOutcome(gross_pnl=Decimal("5.25"), notional=Decimal("100")),
    )

    metrics = calculate_trade_metrics(trades, initial_equity=Decimal("100"))

    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.undefined_metrics["sharpe"] == "zero_variance"
    assert metrics.undefined_metrics["sortino"] == "no_downside_returns"


def test_undefined_metrics_summary_serializes_to_clean_json_without_nan_or_infinity() -> None:
    metrics = calculate_trade_metrics((), initial_equity=Decimal("100"))

    payload = json.dumps(metrics.to_dict(), allow_nan=False, sort_keys=True)
    decoded = json.loads(payload)

    assert decoded["win_rate"] is None
    assert decoded["profit_factor"] is None
    assert decoded["sharpe"] is None
    assert decoded["sortino"] is None
    assert decoded["ruined"] is False
    assert decoded["undefined_metrics"]["win_rate"] == "zero_trades"
    assert "NaN" not in payload
    assert "Infinity" not in payload
