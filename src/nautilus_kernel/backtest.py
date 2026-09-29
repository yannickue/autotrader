"""Technical Nautilus BacktestEngine run over catalog data (C4).

Label on every result: TECHNICAL_BACKTEST / NOT_YET_BROKER_CALIBRATED.
M5 bars are NOT tick-perfect: fills happen at the (synthesized-ask / bid) bar
prices the Nautilus matching engine derives from bars. ActivTrades costs,
slippage, financing and session rules are not calibrated.

Nautilus owns orders, fills, positions, portfolio and PnL; this module only
configures the engine, runs it and READS results back from the Nautilus
cache/portfolio/reports.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import FillModel, LatencyModel, StandardMarginModel
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import EUR
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money

from nautilus_kernel.catalog import bar_type_strings, read_catalog
from nautilus_kernel.instrument import VENUE
from nautilus_kernel.proof_strategy import Evidence, ProofStrategy, ProofStrategyConfig
from nautilus_kernel.queries import closed_position_lifecycles
from nautilus_kernel.risk_bridge import NautilusRiskBridge

LABELS = ("TECHNICAL_BACKTEST", "NOT_YET_BROKER_CALIBRATED")
STARTING_BALANCE_EUR = 10_000
NS = 1_000_000_000


@dataclass(slots=True)
class BacktestResult:
    labels: tuple[str, ...]
    metrics: dict[str, Any]
    evidence: Evidence
    orders_report: list[dict[str, Any]] = field(default_factory=list)
    fills_report: list[dict[str, Any]] = field(default_factory=list)
    positions_report: list[dict[str, Any]] = field(default_factory=list)
    account_report: list[dict[str, Any]] = field(default_factory=list)


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None or len(frame) == 0:
        return []
    return frame.reset_index().to_dict(orient="records")


def run_technical_backtest(
    *,
    catalog_path: Path | str,
    instrument: Any,
    timeframe: str,
    spread_points_by_close_ns: dict[int, int],
    point: Decimal,
    bridge: NautilusRiskBridge | None = None,
    strategy_overrides: dict[str, Any] | None = None,
) -> BacktestResult:
    inst, bid_bars, ask_bars = read_catalog(catalog_path, instrument, timeframe)
    bid_type_s, _ = bar_type_strings(inst, timeframe)
    bridge = bridge or NautilusRiskBridge(instrument_id=inst.id)
    evidence = Evidence()

    engine = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("C4-PROOF-001"),
            logging=LoggingConfig(log_level="ERROR", bypass_logging=True),
        )
    )
    try:
        engine.add_venue(
            venue=VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=EUR,
            starting_balances=[Money(STARTING_BALANCE_EUR, EUR)],
            default_leverage=Decimal(20),
            margin_model=StandardMarginModel(),
            # Conservative: every marketable fill slips one tick (deterministic seed).
            fill_model=FillModel(prob_fill_on_limit=1.0, prob_slippage=1.0, random_seed=42),
            # Execution model EXECUTION_DELAYED_ONE_BAR: every command reaches the venue one
            # bar + 1 ns after the decision and fills at THAT later bar's close (executable
            # side, +1 tick). Verified alternatives: no latency => fills against the PREVIOUS
            # bar's book; latency <= 1 bar => fills at the decision bar's own close, a price
            # not executable once the close is known (look-ahead-favourable). Pessimistic,
            # not next-open (bars carry one timestamp; Nautilus cannot fill at a bar open).
            latency_model=LatencyModel(base_latency_nanos=_tf_ns(timeframe) + 1),
            bar_execution=True,
        )
        engine.add_instrument(inst)
        engine.add_data(bid_bars)
        engine.add_data(ask_bars)
        strategy = ProofStrategy(
            ProofStrategyConfig(
                instrument_id=inst.id,
                bid_bar_type=BarType.from_str(bid_type_s),
                spread_points=spread_points_by_close_ns,
                point=float(point),
                **(strategy_overrides or {}),
            ),
            bridge=bridge,
            evidence=evidence,
        )
        engine.add_strategy(strategy)
        evidence.record(
            "DATA_LOADED",
            0,
            bid_bars=len(bid_bars),
            ask_bars=len(ask_bars),
            first_close_ns=bid_bars[0].ts_event,
            last_close_ns=bid_bars[-1].ts_event,
        )
        engine.run()

        result = _collect(
            engine, inst, evidence, bid_bars, spread_points_by_close_ns, point, timeframe
        )
    finally:
        engine.dispose()
    return result


def _collect(
    engine: BacktestEngine,
    inst: Any,
    evidence: Evidence,
    bid_bars: list[Any],
    spread_points_by_close_ns: dict[int, int],
    point: Decimal,
    timeframe: str,
) -> BacktestResult:
    trader = engine.trader
    cache = engine.cache
    fills = _records(trader.generate_order_fills_report())
    orders = _records(trader.generate_orders_report())
    positions = _records(trader.generate_positions_report())
    account = _records(trader.generate_account_report(VENUE))

    closed = closed_position_lifecycles(cache, inst.id)
    pnls = [Decimal(str(p.realized_pnl.as_decimal())) for p in closed]
    commissions = Decimal(0)
    for p in closed:
        for money in p.commissions():
            commissions += Decimal(str(money.as_decimal()))

    sorted_ts = sorted(spread_points_by_close_ns)
    half_spread_cost = Decimal(0)
    for order in cache.orders():
        for event in order.events:
            if type(event).__name__ != "OrderFilled":
                continue
            idx = bisect.bisect_right(sorted_ts, event.ts_event) - 1
            if idx < 0:
                continue
            spread = Decimal(spread_points_by_close_ns[sorted_ts[idx]]) * point
            half_spread_cost += Decimal(str(event.last_qty.as_double())) * spread / 2

    net = sum(pnls, Decimal(0))
    account_obj = engine.portfolio.account(VENUE)
    metrics = {
        "labels": list(LABELS),
        "execution_model": "EXECUTION_DELAYED_ONE_BAR_AT_LATER_BAR_CLOSE_PLUS_1_TICK",
        "instrument": str(inst.id),
        "broker_symbol": str(inst.raw_symbol),
        "timeframe": timeframe,
        "data_range_utc": [
            _iso(bid_bars[0].ts_event - _tf_ns(timeframe)),
            _iso(bid_bars[-1].ts_event - _tf_ns(timeframe)),
        ],
        "bars": len(bid_bars),
        "orders": len(cache.orders()),
        "fills": len(fills),
        "completed_trades": len(closed),
        "winners": sum(1 for x in pnls if x > 0),
        "losers": sum(1 for x in pnls if x < 0),
        "net_pnl_eur": str(net),
        "commissions_eur": str(commissions),
        "spread_cost_estimate_eur": str(half_spread_cost),
        "gross_pnl_mid_estimate_eur": str(net + commissions + half_spread_cost),
        "max_position_size": str(max((p.peak_qty.as_decimal() for p in closed), default=0)),
        "final_balance_eur": str(account_obj.balance_total(EUR).as_decimal()),
        "risk_decisions": evidence_count(evidence, "RISK_APPROVED")
        + evidence_count(evidence, "RISK_REJECTED"),
        "risk_rejections": evidence_count(evidence, "RISK_REJECTED"),
    }
    return BacktestResult(
        labels=LABELS,
        metrics=metrics,
        evidence=evidence,
        orders_report=orders,
        fills_report=fills,
        positions_report=positions,
        account_report=account,
    )


def evidence_count(evidence: Evidence, event: str) -> int:
    return len(evidence.of(event))


def _tf_ns(timeframe: str) -> int:
    from adapters.activtrades_mt5.history import TIMEFRAME_SECONDS

    return TIMEFRAME_SECONDS[timeframe] * NS


def _iso(ts_ns: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts_ns / NS, tz=UTC).isoformat()
