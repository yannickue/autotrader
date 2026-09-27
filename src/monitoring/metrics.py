"""Deterministic metrics shared by research reports and monitoring exporters."""

from dataclasses import asdict, dataclass
from decimal import Decimal

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeOutcome:
    """A completed trade expressed as gross PnL and explicit modeled costs."""

    gross_pnl: Decimal
    notional: Decimal
    fees: Decimal = ZERO
    spread: Decimal = ZERO
    slippage: Decimal = ZERO
    funding: Decimal = ZERO

    def __post_init__(self) -> None:
        values = {
            "gross_pnl": self.gross_pnl,
            "notional": self.notional,
            "fees": self.fees,
            "spread": self.spread,
            "slippage": self.slippage,
            "funding": self.funding,
        }
        for name, value in values.items():
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        for name in ("notional", "fees", "spread", "slippage", "funding"):
            if values[name] < ZERO:
                raise ValueError(f"{name} cannot be negative")

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.fees - self.spread - self.slippage - self.funding


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeMetrics:
    trade_count: int
    win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    expectancy: Decimal | None
    profit_factor: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    max_drawdown: Decimal
    ruined: bool
    undefined_metrics: dict[str, str]
    turnover: Decimal
    fees: Decimal
    spread: Decimal
    slippage: Decimal
    funding: Decimal
    net_pnl: Decimal

    def to_dict(self) -> dict[str, int | str | bool | dict[str, str] | None]:
        result: dict[str, int | str | bool | dict[str, str] | None] = {}
        for key, value in asdict(self).items():
            if isinstance(value, bool | int | dict):
                result[key] = value
            elif value is None:
                result[key] = None
            else:
                result[key] = str(value)
        return result


def _risk_adjusted_ratios(
    returns: tuple[Decimal, ...],
) -> tuple[Decimal | None, Decimal | None, dict[str, str]]:
    reasons: dict[str, str] = {}
    if len(returns) < 2:
        reasons["sharpe"] = "insufficient_samples"
        reasons["sortino"] = "insufficient_samples"
        return None, None, reasons

    count = Decimal(len(returns))
    mean = sum(returns, ZERO) / count
    variance = sum(((value - mean) ** 2 for value in returns), ZERO) / count
    if variance == ZERO:
        sharpe = None
        reasons["sharpe"] = "zero_variance"
    else:
        sharpe = mean / variance.sqrt() * count.sqrt()

    downside_variance = sum((min(value, ZERO) ** 2 for value in returns), ZERO) / count
    if downside_variance == ZERO:
        sortino = None
        reasons["sortino"] = "no_downside_returns"
    else:
        sortino = mean / downside_variance.sqrt() * count.sqrt()

    return sharpe, sortino, reasons


def calculate_trade_metrics(
    trades: tuple[TradeOutcome, ...], *, initial_equity: Decimal
) -> TradeMetrics:
    """Calculate cost-aware metrics without annualization assumptions.

    Metrics that are mathematically undefined (zero trades, zero wins/losses,
    zero gross loss, insufficient or zero-variance samples, or total account
    ruin) are reported as ``None`` with a matching entry in
    ``undefined_metrics`` explaining why, instead of a fake zero/NaN/inf.
    """

    if not initial_equity.is_finite():
        raise ValueError("initial_equity must be finite")
    if initial_equity <= ZERO:
        raise ValueError("initial_equity must be positive")

    net_pnls = tuple(trade.net_pnl for trade in trades)
    count = len(trades)
    wins = tuple(pnl for pnl in net_pnls if pnl > ZERO)
    losses = tuple(pnl for pnl in net_pnls if pnl < ZERO)

    undefined_metrics: dict[str, str] = {}

    if count == 0:
        win_rate = None
        undefined_metrics["win_rate"] = "zero_trades"
    else:
        win_rate = Decimal(len(wins)) / Decimal(count)

    if wins:
        average_win = sum(wins, ZERO) / Decimal(len(wins))
    else:
        average_win = None
        undefined_metrics["average_win"] = "no_winning_trades"

    if losses:
        average_loss = -sum(losses, ZERO) / Decimal(len(losses))
    else:
        average_loss = None
        undefined_metrics["average_loss"] = "no_losing_trades"

    if count == 0:
        expectancy = None
        undefined_metrics["expectancy"] = "zero_trades"
    else:
        weighted_win = average_win if average_win is not None else ZERO
        weighted_loss = average_loss if average_loss is not None else ZERO
        expectancy = win_rate * weighted_win - (ONE - win_rate) * weighted_loss

    gross_profit = sum(wins, ZERO)
    gross_loss = -sum(losses, ZERO)
    if gross_loss == ZERO:
        profit_factor = None
        undefined_metrics["profit_factor"] = "zero_trades" if count == 0 else "no_losing_trades"
    else:
        profit_factor = gross_profit / gross_loss

    equity = initial_equity
    peak = initial_equity
    max_drawdown = ZERO
    returns: list[Decimal] = []
    ruined = False
    for pnl in net_pnls:
        returns.append(pnl / equity)
        equity += pnl
        if equity <= ZERO:
            ruined = True
            max_drawdown = ONE
            break
        peak = max(peak, equity)
        if peak <= ZERO:
            raise ValueError("drawdown peak must remain positive")
        drawdown = (peak - equity) / peak
        max_drawdown = max(max_drawdown, drawdown)

    if ruined:
        sharpe = None
        sortino = None
        undefined_metrics["sharpe"] = "account_ruined"
        undefined_metrics["sortino"] = "account_ruined"
    else:
        sharpe, sortino, risk_reasons = _risk_adjusted_ratios(tuple(returns))
        undefined_metrics.update(risk_reasons)

    return TradeMetrics(
        trade_count=count,
        win_rate=win_rate,
        average_win=average_win,
        average_loss=average_loss,
        expectancy=expectancy,
        profit_factor=profit_factor,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_drawdown,
        ruined=ruined,
        undefined_metrics=undefined_metrics,
        turnover=sum((abs(trade.notional) for trade in trades), ZERO),
        fees=sum((trade.fees for trade in trades), ZERO),
        spread=sum((trade.spread for trade in trades), ZERO),
        slippage=sum((trade.slippage for trade in trades), ZERO),
        funding=sum((trade.funding for trade in trades), ZERO),
        net_pnl=sum(net_pnls, ZERO),
    )
