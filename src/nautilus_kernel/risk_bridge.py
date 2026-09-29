"""THIN bridge: Nautilus portfolio/cache state -> pure risk/sizing -> approved intent.

    market/account inputs (read from Nautilus)  ->  RiskPolicyEvaluator (pure)
    ->  ProposedOrderIntent  ->  (caller builds the Nautilus order)

What this bridge is NOT: it holds no orders, fills, positions, portfolio or
PnL state. Everything it feeds the evaluator is READ from Nautilus' cache /
portfolio at call time. The only bridge-local values are derived research
metadata: `peak_equity` (max equity observed, needed for the drawdown gate)
and the fixed policy/limits configuration.

Legacy runtime pieces (reservations, persistence, halt latch, recovery) are
NOT part of C4: `RuntimeRiskState` is fixed READY and `pending` exposure is
empty because the v1 proof strategy allows at most one order in flight.

Simulation semantics (explicit, not a broker reconciliation):
`ReconciliationState.RECONCILED` is asserted because Nautilus IS the sole
ledger in a backtest -- there is no external truth to diverge from. This must
never be reused for a live/demo account (C5 needs VENUE_SNAPSHOT reconciliation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from nautilus_trader.model.identifiers import InstrumentId

from data.models import DataQuality, MarketSnapshot
from nautilus_kernel.queries import closed_position_lifecycles
from risk.models import (
    MAX_SYSTEM_LEVERAGE,
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    ReconciliationState,
    RiskPolicy,
    RiskSide,
    RuntimeMode,
    RuntimeRiskState,
)
from risk.policy import PolicyRejection, ProposedOrderIntent, RiskPolicyEvaluator
from risk.trading_day import RealizedPnlEntry, TradingDayPolicy, realized_pnl_today

ZERO = Decimal(0)
# Assumption (documented): index CFD liquidity is treated as ample for the
# technical proof; the real book depth is unknown. Sizing is still capped by
# RiskPolicy gross/net/leverage limits.
ASSUMED_AVAILABLE_LIQUIDITY_NOTIONAL = Decimal("1000000")

CANONICAL = "GER40"


def technical_risk_policy() -> RiskPolicy:
    """Explicit technical-backtest limits (NOT calibrated to the broker or any live account)."""
    from datetime import timedelta

    return RiskPolicy(
        policy_id="c4-technical-backtest-v1",
        risk_fraction=Decimal("0.005"),
        max_leverage=Decimal("20"),
        max_gross_notional=Decimal("200000"),
        max_net_notional=Decimal("200000"),
        max_daily_loss=Decimal("500"),
        max_drawdown=Decimal("1500"),
        max_consecutive_losses=5,
        liquidity_fraction=Decimal("0.1"),
        max_data_age=timedelta(minutes=10),
        max_signal_age=timedelta(minutes=10),
        estimated_cost_bps=Decimal("3"),
        reference_price_slippage_bps=Decimal("5"),
    )


def technical_instrument_limits() -> InstrumentRiskLimits:
    """From broker symbol_info: step/min 0.25, max 250; the rest are explicit assumptions."""
    return InstrumentRiskLimits(
        instrument=CANONICAL,
        max_leverage=Decimal("20"),
        quantity_step=Decimal("0.25"),
        min_quantity=Decimal("0.25"),
        min_notional=ZERO,
        max_notional=Decimal("7500000"),
        max_spread_bps=Decimal("10"),
        maintenance_margin_rate=Decimal("0.025"),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketInputs:
    """Strategy-observed market state for one decision (all from the closed bar)."""

    now: datetime
    bid: Decimal
    ask: Decimal
    volume: Decimal


def _dec(value: Any) -> Decimal:
    return Decimal(str(float(value)))


class NautilusRiskBridge:
    def __init__(
        self,
        *,
        instrument_id: InstrumentId,
        policy: RiskPolicy | None = None,
        limits: InstrumentRiskLimits | None = None,
        trading_day: TradingDayPolicy | None = None,
        evaluator: RiskPolicyEvaluator | None = None,
        account_leverage_cap: Decimal = MAX_SYSTEM_LEVERAGE,
    ) -> None:
        self._instrument_id = instrument_id
        self._policy = policy or technical_risk_policy()
        self._limits = limits or technical_instrument_limits()
        self._trading_day = trading_day or TradingDayPolicy()
        self._evaluator = evaluator or RiskPolicyEvaluator(self._policy)
        self._account_leverage_cap = account_leverage_cap  # from broker account info when known
        self._peak_equity: Decimal | None = None
        self.decisions = 0

    @property
    def policy(self) -> RiskPolicy:
        return self._policy

    # -- Nautilus -> risk inputs ------------------------------------------

    def _equity(self, portfolio: Any, cache: Any) -> Decimal:
        account = portfolio.account(self._instrument_id.venue)
        currency = account.base_currency
        balance = _dec(account.balance_total(currency).as_decimal())
        unrealized = portfolio.unrealized_pnl(self._instrument_id)
        return balance + (_dec(unrealized.as_decimal()) if unrealized is not None else ZERO)

    def account_state(self, *, portfolio: Any, cache: Any, now: datetime) -> AccountRiskState:
        equity = self._equity(portfolio, cache)
        self._peak_equity = equity if self._peak_equity is None else max(self._peak_equity, equity)

        unrealized_money = portfolio.unrealized_pnl(self._instrument_id)
        unrealized = _dec(unrealized_money.as_decimal()) if unrealized_money is not None else ZERO

        closed = closed_position_lifecycles(cache, self._instrument_id)
        entries = [
            RealizedPnlEntry(
                timestamp=datetime.fromtimestamp(p.ts_closed / 1e9, tz=UTC),
                realized_pnl=_dec(p.realized_pnl.as_decimal()),
                fee=ZERO,  # Nautilus realized_pnl already reflects commissions (0 here)
            )
            for p in closed
        ]
        pnl_today = realized_pnl_today(entries, now=now, policy=self._trading_day)

        losses = 0
        for p in reversed(closed):
            if p.realized_pnl.as_decimal() < 0:
                losses += 1
            else:
                break

        positions: dict[str, Decimal] = {}
        notionals: dict[str, Decimal] = {}
        gross = net = ZERO
        open_positions = cache.positions_open(instrument_id=self._instrument_id)
        if open_positions:
            signed = sum((_dec(p.signed_qty) for p in open_positions), ZERO)
            exposure = portfolio.net_exposure(self._instrument_id)
            notional = abs(_dec(exposure.as_decimal())) if exposure is not None else ZERO
            positions[CANONICAL] = signed
            notionals[CANONICAL] = notional
            gross = notional
            net = notional if signed > 0 else -notional

        return AccountRiskState(
            state_version=f"nautilus-backtest@{now.isoformat()}",
            known=True,
            reconciliation=ReconciliationState.RECONCILED,  # see module docstring
            equity=equity,
            peak_equity=self._peak_equity,
            realized_pnl_today=pnl_today,
            pnl_window_start=self._trading_day.trading_day_start(now),
            unrealized_pnl=unrealized,
            gross_notional=gross,
            net_notional=net,
            instrument_notionals=notionals,
            positions=positions,
            leverage_cap=self._account_leverage_cap,
            consecutive_losses=losses,
        )

    # -- decision ----------------------------------------------------------

    def evaluate_entry(
        self,
        *,
        portfolio: Any,
        cache: Any,
        market: MarketInputs,
        side: RiskSide,
        stop_price: Decimal,
        signal_id: str,
    ) -> ProposedOrderIntent | PolicyRejection:
        self.decisions += 1
        account = self.account_state(portfolio=portfolio, cache=cache, now=market.now)
        mid = (market.bid + market.ask) / 2
        snapshot = MarketSnapshot(
            instrument=CANONICAL,
            timestamp=market.now,
            bid=market.bid,
            ask=market.ask,
            last=mid,  # CFD has no distinct last price
            volume=market.volume,
            volatility=None,
            liquidity=None,
            source="ACTIVTRADES_MT5_CFD",
            quality=DataQuality.LIVE,  # simulation clock == data clock; see docstring
            metadata={"model": "backtest", "ask": "synthesized_from_bar_spread"},
        )
        # Conservative strategy entry hint: the executable side of the bar.
        entry_price = market.ask if side == RiskSide.BUY else market.bid
        request = PositionSizingRequest(
            signal_id=signal_id,
            instrument=CANONICAL,
            timestamp=market.now,
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            confidence=Decimal("1"),
            available_liquidity_notional=ASSUMED_AVAILABLE_LIQUIDITY_NOTIONAL,
            metadata={},
        )
        runtime = RuntimeRiskState(
            state_version="c4-fixed-ready",
            mode=RuntimeMode.READY,
            risk_ready=True,
            kill_switch=False,
        )
        return self._evaluator.evaluate_entry(
            request=request,
            snapshot=snapshot,
            account=account,
            runtime=runtime,
            instrument=self._limits,
            now=market.now,
        )
