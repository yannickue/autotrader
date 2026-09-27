"""Deterministic metrics shared by research reports and monitoring exporters."""

from dataclasses import asdict, dataclass
from decimal import Decimal

ZERO = Decimal("0")


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
    win_rate: Decimal
    average_win: Decimal
    average_loss: Decimal
    expectancy: Decimal
    profit_factor: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    max_drawdown: Decimal
    turnover: Decimal
    fees: Decimal
    spread: Decimal
    slippage: Decimal
    funding: Decimal
    net_pnl: Decimal

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            key: value if isinstance(value, int) else (None if value is None else str(value))
            for key, value in asdict(self).items()
        }


def _risk_adjusted_ratios(returns: tuple[Decimal, ...]) -> tuple[Decimal | None, Decimal | None]:
    if len(returns) < 2:
        return None, None
    count = Decimal(len(returns))
    mean = sum(returns, ZERO) / count
    variance = sum(((value - mean) ** 2 for value in returns), ZERO) / count
    sharpe = None if variance == ZERO else mean / variance.sqrt() * count.sqrt()
    downside_variance = sum((min(value, ZERO) ** 2 for value in returns), ZERO) / count
    sortino = None if downside_variance == ZERO else mean / downside_variance.sqrt() * count.sqrt()
    return sharpe, sortino


def calculate_trade_metrics(
    trades: tuple[TradeOutcome, ...], *, initial_equity: Decimal
) -> TradeMetrics:
    """Calculate cost-aware metrics without annualization assumptions."""

    if not initial_equity.is_finite():
        raise ValueError("initial_equity must be finite")
    if initial_equity <= ZERO:
        raise ValueError("initial_equity must be positive")

    net_pnls = tuple(trade.net_pnl for trade in trades)
    cumulative_equity = initial_equity
    for pnl in net_pnls:
        cumulative_equity += pnl
        if cumulative_equity <= ZERO:
            raise ValueError("cumulative equity must remain positive")

    wins = tuple(pnl for pnl in net_pnls if pnl > ZERO)
    losses = tuple(pnl for pnl in net_pnls if pnl < ZERO)
    count = len(trades)
    win_rate = Decimal(len(wins)) / Decimal(count) if count else ZERO
    average_win = sum(wins, ZERO) / Decimal(len(wins)) if wins else ZERO
    average_loss = -sum(losses, ZERO) / Decimal(len(losses)) if losses else ZERO
    expectancy = win_rate * average_win - (Decimal("1") - win_rate) * average_loss
    gross_profit = sum(wins, ZERO)
    gross_loss = -sum(losses, ZERO)
    profit_factor = gross_profit / gross_loss if gross_loss else None

    equity = initial_equity
    peak = initial_equity
    max_drawdown = ZERO
    returns: list[Decimal] = []
    for pnl in net_pnls:
        returns.append(pnl / equity)
        equity += pnl
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak
        max_drawdown = max(max_drawdown, drawdown)
    sharpe, sortino = _risk_adjusted_ratios(tuple(returns))

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
        turnover=sum((abs(trade.notional) for trade in trades), ZERO),
        fees=sum((trade.fees for trade in trades), ZERO),
        spread=sum((trade.spread for trade in trades), ZERO),
        slippage=sum((trade.slippage for trade in trades), ZERO),
        funding=sum((trade.funding for trade in trades), ZERO),
        net_pnl=sum(net_pnls, ZERO),
    )
