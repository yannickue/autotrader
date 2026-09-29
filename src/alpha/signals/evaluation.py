"""Bridge from strategy candidates into the AR1 simulator and metrics."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from alpha.common.frame import Frame
from alpha.common.metrics import compute_metrics
from alpha.common.sim import (
    DEFAULT_RULES,
    DEFAULT_SIZING,
    CostScenario,
    Signals,
    SimRules,
    SizingSpec,
    simulate,
)
from alpha.regime import REGIME_DIMENSIONS

from .candidate import SignalCandidate


@dataclass(frozen=True)
class EvaluationResult:
    trades: pd.DataFrame
    skips: dict[str, int]


def _decision_position(frame: Frame, signal_ts: pd.Timestamp) -> int:
    decision_open = pd.Timestamp(signal_ts).tz_convert("UTC") - pd.Timedelta(minutes=5)
    position = frame.ts.get_indexer([decision_open])[0]
    if position < 0:
        raise ValueError(f"signal_ts {signal_ts} does not map to an M5 close")
    return int(position)


def evaluate_candidates(
    frame: Frame,
    candidates: list[SignalCandidate],
    *,
    cost: CostScenario,
    sizing: SizingSpec = DEFAULT_SIZING,
    rules: SimRules = DEFAULT_RULES,
) -> EvaluationResult:
    """Evaluate candidates with AR1 fills while retaining complete attribution."""

    if not candidates:
        return EvaluationResult(pd.DataFrame(), {})
    grouped: dict[tuple[object, ...], list[SignalCandidate]] = {}
    for item in candidates:
        key = (item.strategy_id, item.strategy_version, item.exit_spec)
        grouped.setdefault(key, []).append(item)

    trade_frames: list[pd.DataFrame] = []
    total_skips: dict[str, int] = {}
    for items in grouped.values():
        side = np.zeros(len(frame), dtype=np.int8)
        stop = np.full(len(frame), np.nan)
        attribution: dict[int, SignalCandidate] = {}
        for item in items:
            position = _decision_position(frame, item.signal_ts)
            if position in attribution:
                raise ValueError("duplicate candidate decision within one strategy/exit group")
            side[position] = item.direction
            stop[position] = item.stop
            attribution[position] = item
        trades, skips = simulate(
            frame, Signals(side, stop), items[0].exit_spec, cost, sizing, rules
        )
        for key, value in skips.items():
            total_skips[key] = total_skips.get(key, 0) + value
        if trades.empty:
            continue
        source = [attribution[int(position)] for position in trades["decision_idx"]]
        trades = trades.copy()
        trades["strategy_id"] = [item.strategy_id for item in source]
        trades["strategy_version"] = [item.strategy_version for item in source]
        trades["instrument"] = [item.instrument for item in source]
        trades["signal_ts"] = [item.signal_ts for item in source]
        trades["entry_intent"] = [item.entry_intent for item in source]
        trades["candidate_target"] = [item.target for item in source]
        trades["h1_regime"] = [item.h1_regime for item in source]
        trades["m15_context"] = [item.m15_context for item in source]
        trades["session_phase"] = [item.session_phase for item in source]
        trades["setup_metadata"] = [item.setup_metadata for item in source]
        trades["param_fingerprint"] = [item.param_fingerprint for item in source]
        trade_frames.append(trades)
    combined = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    if not combined.empty:
        combined = combined.sort_values(["entry_ts", "strategy_id"], kind="stable").reset_index(
            drop=True
        )
    return EvaluationResult(combined, total_skips)


def grouped_metrics(
    trades: pd.DataFrame,
    *,
    trading_days: np.ndarray,
    minimum_trades: int = 30,
) -> pd.DataFrame:
    """Long-form strategy x regime-dimension x active-context AR1 metrics."""

    if minimum_trades < 1:
        raise ValueError("minimum_trades must be positive")
    rows: list[dict[str, object]] = []
    for _, trade in trades.iterrows():
        contexts = sorted(key for key, active in trade["m15_context"].items() if active) or ["NONE"]
        for dimension in REGIME_DIMENSIONS:
            for context in contexts:
                rows.append(
                    {
                        "strategy_id": trade["strategy_id"],
                        "regime_dimension": dimension,
                        "regime_value": trade["h1_regime"][dimension],
                        "context": context,
                        "date": trade["date"],
                        "r_multiple": trade["r_multiple"],
                        "pnl_eur": trade["pnl_eur"],
                    }
                )
    if not rows:
        return pd.DataFrame()
    expanded = pd.DataFrame(rows)
    output: list[dict[str, object]] = []
    keys = ["strategy_id", "regime_dimension", "regime_value", "context"]
    for values, group in expanded.groupby(keys, dropna=False, sort=True):
        counts = group.groupby("date").size().reindex(pd.Index(trading_days), fill_value=0)
        n_trades = len(group)
        pnl = group["pnl_eur"].to_numpy(float)
        losses = -pnl[pnl < 0].sum()
        output.append(
            dict(
                zip(keys, values, strict=True),
                n_trades=n_trades,
                n_trading_days=len(trading_days),
                trades_per_day=n_trades / len(trading_days) if len(trading_days) else None,
                median_trades_per_day=float(counts.median()) if len(trading_days) else None,
                zero_trade_days=int((counts == 0).sum()),
                insufficient_sample=n_trades < minimum_trades,
                expectancy_r=float(group["r_multiple"].mean()),
                win_rate=float((pnl > 0).mean()),
                profit_factor=float(pnl[pnl > 0].sum() / losses) if losses else None,
                net_pnl_eur=float(pnl.sum()),
            )
        )
    return pd.DataFrame(output)


def ar1_metrics(
    result: EvaluationResult,
    *,
    trading_days: np.ndarray,
    window_bars: int,
    sizing: SizingSpec = DEFAULT_SIZING,
    seed: int = 0,
) -> dict:
    """Expose the unchanged AR1 aggregate metric implementation for bridge results."""

    return compute_metrics(
        result.trades,
        trading_days=trading_days,
        window_bars=window_bars,
        sizing=sizing,
        seed=seed,
    )
