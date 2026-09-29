"""Compact, allocation-light Train/Validation screening for research candidates.

``cost_burden`` is the mean per-trade cost in R: ``cost_eur`` divided by the initial
cash risk (``risk_pts * qty * contract_size``).  Partition day statistics use every
Berlin day with market data, including zero-trade days.
"""

from __future__ import annotations

from collections.abc import Iterable, Set
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from alpha.common.protocol import Partition, SplitPlan
from alpha.common.sim import DEFAULT_RULES, DEFAULT_SIZING, CostScenario, SimRules, SizingSpec
from alpha.fast.sim import CandidateArrays, MarketArrays, TradeArrays, simulate_fast
from alpha.fast.spec import StrategySpec


@dataclass(frozen=True)
class PartitionScreen:
    n_trades: int
    expectancy_r: float | None
    profit_factor: float | None
    win_rate: float | None
    avg_winner_r: float | None
    avg_loser_r: float | None
    payoff: float | None
    max_drawdown_r: float | None
    max_loss_streak: int
    trades_per_day: float | None
    zero_trade_day_frac: float | None
    cost_burden: float | None
    top_3_positive_r_share: float | None
    mean_mfe_r: float | None
    mean_mae_r: float | None


@dataclass(frozen=True)
class LightScreenResult:
    train: PartitionScreen
    validation: PartitionScreen


class RejectReason(StrEnum):
    ZERO_CANDIDATES = "zero_candidates"
    TOO_FEW_TRADES = "too_few_trades"
    INVALID_STOP = "invalid_stop"
    DUPLICATE_SPEC = "duplicate_canonical_spec_hash"


def _subset(trades: TradeArrays, mask: np.ndarray) -> TradeArrays:
    return TradeArrays(
        *(
            getattr(trades, name)
            if name == "skip_counts"
            else getattr(trades, name)[mask]
            for name in trades.__dataclass_fields__
        )
    )


def _loss_streak(values: np.ndarray) -> int:
    longest = current = 0
    for value in values:
        if value < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _metrics(
    trades: TradeArrays, n_days: int, *, contract_size: float
) -> PartitionScreen:
    r = trades.r_multiple
    count = len(r)
    if not count:
        return PartitionScreen(
            0, None, None, None, None, None, None, None, 0,
            0.0 if n_days else None, 1.0 if n_days else None,
            None, None, None, None,
        )
    wins = r[r > 0]
    losses = r[r < 0]
    gross_loss = float(-losses.sum())
    cumulative = np.concatenate((np.zeros(1), np.cumsum(r)))
    drawdown = np.maximum.accumulate(cumulative) - cumulative
    traded_days = len(np.unique(trades.entry_day))
    initial_risk_eur = trades.risk_pts * trades.qty * contract_size
    valid_risk = np.isfinite(initial_risk_eur) & (initial_risk_eur > 0)
    positive_total = float(wins.sum())
    return PartitionScreen(
        n_trades=count,
        expectancy_r=float(r.mean()),
        profit_factor=float(wins.sum() / gross_loss) if gross_loss > 0 else None,
        win_rate=float((r > 0).mean()),
        avg_winner_r=float(wins.mean()) if len(wins) else None,
        avg_loser_r=float(losses.mean()) if len(losses) else None,
        payoff=float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
        max_drawdown_r=float(drawdown.max()),
        max_loss_streak=_loss_streak(r),
        trades_per_day=count / n_days if n_days else None,
        zero_trade_day_frac=(n_days - traded_days) / n_days if n_days else None,
        cost_burden=(
            float(np.mean(trades.cost_eur[valid_risk] / initial_risk_eur[valid_risk]))
            if valid_risk.any()
            else None
        ),
        top_3_positive_r_share=(
            float(np.sort(wins)[-3:].sum() / positive_total) if positive_total > 0 else None
        ),
        mean_mfe_r=float(trades.mfe_r.mean()),
        mean_mae_r=float(trades.mae_r.mean()),
    )


def _partition_screen(
    trades: TradeArrays,
    market: MarketArrays,
    dates: np.ndarray,
    split: SplitPlan,
    part: Partition,
    *,
    contract_size: float,
) -> PartitionScreen:
    bar_mask = split.mask(dates, part)
    trade_mask = split.mask(dates[trades.entry_idx], part) if len(trades) else np.zeros(0, bool)
    return _metrics(
        _subset(trades, trade_mask),
        len(np.unique(market.day[bar_mask])),
        contract_size=contract_size,
    )


def screen_trades(
    trades: TradeArrays,
    market: MarketArrays,
    split: SplitPlan,
    *,
    dates: np.ndarray,
    sizing: SizingSpec = DEFAULT_SIZING,
) -> LightScreenResult:
    """Reduce already-simulated trades to embargo-aware Train/Validation metrics."""
    dates = np.asarray(dates).astype("datetime64[D]")
    return LightScreenResult(
        train=_partition_screen(
            trades, market, dates, split, split.train, contract_size=sizing.contract_size
        ),
        validation=_partition_screen(
            trades, market, dates, split, split.validation, contract_size=sizing.contract_size
        ),
    )


def light_screen(
    market: MarketArrays,
    candidates: CandidateArrays,
    cost: CostScenario,
    split: SplitPlan,
    *,
    dates: np.ndarray,
    sizing: SizingSpec = DEFAULT_SIZING,
    rules: SimRules = DEFAULT_RULES,
) -> LightScreenResult:
    """Simulate one candidate set and return compact embargo-aware Train/Validation metrics."""
    dates = np.asarray(dates).astype("datetime64[D]")
    if len(dates) != len(market.o):
        raise ValueError("dates must contain one Berlin date per market bar")
    trades = simulate_fast(market, candidates, cost, sizing, rules)
    return screen_trades(trades, market, split, dates=dates, sizing=sizing)


def light_screen_many(
    market: MarketArrays,
    candidate_sets: Iterable[CandidateArrays],
    cost: CostScenario,
    split: SplitPlan,
    *,
    dates: np.ndarray,
    sizing: SizingSpec = DEFAULT_SIZING,
    rules: SimRules = DEFAULT_RULES,
) -> list[LightScreenResult]:
    """Screen independent candidate sets without retaining their TradeArrays."""
    return [
        light_screen(market, candidates, cost, split, dates=dates, sizing=sizing, rules=rules)
        for candidates in candidate_sets
    ]


def reject_reason(
    spec: StrategySpec,
    candidates: CandidateArrays,
    *,
    min_trades: int,
    seen: Set[str] | None = None,
    market: MarketArrays | None = None,
    sizing: SizingSpec = DEFAULT_SIZING,
    cost: CostScenario | None = None,
) -> RejectReason | None:
    """Return the first cheap Stage-A rejection, without mutating ``seen``."""
    count = len(candidates.decision_idx)
    if count == 0:
        return RejectReason.ZERO_CANDIDATES
    if seen is not None and spec.spec_hash() in seen:
        return RejectReason.DUPLICATE_SPEC
    if market is not None:
        scenario = cost or CostScenario("STAGE_A", slippage_pts=0.0)
        entry_idx = candidates.decision_idx + 1
        in_range = entry_idx < len(market.o)  # a decision on the last bar has no fill bar
        if not in_range.any():
            return RejectReason.INVALID_STOP
        entry_idx = entry_idx[in_range]
        direction, stop = candidates.direction[in_range], candidates.stop[in_range]
        fill = np.where(
            direction > 0,
            market.o[entry_idx] + market.spread[entry_idx] * scenario.spread_mult
            + scenario.slippage_pts,
            market.o[entry_idx] - scenario.slippage_pts,
        )
        risk_pts = direction * (fill - stop)
        # the simulator skips individually invalid candidates; reject only impossible specs
        if np.all(
            ~np.isfinite(risk_pts)
            | (risk_pts < sizing.min_risk_pts)
            | (risk_pts > sizing.max_risk_pts)
        ):
            return RejectReason.INVALID_STOP
    if count < min_trades:
        return RejectReason.TOO_FEW_TRADES
    return None


__all__ = (
    "LightScreenResult", "PartitionScreen", "RejectReason", "light_screen",
    "light_screen_many", "reject_reason", "screen_trades",
)
