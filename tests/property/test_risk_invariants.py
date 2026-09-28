"""Property-style invariant checks over a deterministic grid of inputs.

No hypothesis dependency is installed in this environment, so we sweep a
deterministic grid of plausible values with plain loops instead.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from data.models import DataQuality, MarketSnapshot
from risk.engine import RiskEngine
from risk.models import (
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    RiskPolicy,
    RiskSide,
    RuntimeMode,
    RuntimeRiskState,
)

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _policy(**changes: object) -> RiskPolicy:
    values: dict[str, object] = {
        "policy_id": "risk-v1",
        "risk_fraction": Decimal("0.02"),
        "max_leverage": Decimal("10"),
        "max_gross_notional": Decimal("10000"),
        "max_net_notional": Decimal("5000"),
        "max_daily_loss": Decimal("100"),
        "max_drawdown": Decimal("200"),
        "max_consecutive_losses": 3,
        "liquidity_fraction": Decimal("0.10"),
        "max_data_age": timedelta(seconds=5),
        "max_signal_age": timedelta(seconds=5),
    }
    values.update(changes)
    return RiskPolicy(**values)


def _limits(**changes: object) -> InstrumentRiskLimits:
    values: dict[str, object] = {
        "instrument": "BTCUSDT-PERP",
        "max_leverage": Decimal("8"),
        "quantity_step": Decimal("0.001"),
        "min_quantity": Decimal("0.001"),
        "min_notional": Decimal("1"),
        "max_notional": Decimal("4000"),
        "max_spread_bps": Decimal("250"),
        "maintenance_margin_rate": Decimal("0.005"),
    }
    values.update(changes)
    return InstrumentRiskLimits(**values)


def _account(**changes: object) -> AccountRiskState:
    values: dict[str, object] = {
        "state_version": "account-1",
        "known": True,
        "reconciled": True,
        "equity": Decimal("1000"),
        "peak_equity": Decimal("1000"),
        "realized_pnl_today": Decimal("0"),
        "unrealized_pnl": Decimal("0"),
        "gross_notional": Decimal("0"),
        "net_notional": Decimal("0"),
        "instrument_notionals": {},
        "positions": {},
        "leverage_cap": Decimal("5"),
        "consecutive_losses": 0,
    }
    values.update(changes)
    return AccountRiskState(**values)


def _runtime(**changes: object) -> RuntimeRiskState:
    values: dict[str, object] = {
        "state_version": "runtime-1",
        "mode": RuntimeMode.READY,
        "risk_ready": True,
        "kill_switch": False,
    }
    values.update(changes)
    return RuntimeRiskState(**values)


def _snapshot(**changes: object) -> MarketSnapshot:
    values: dict[str, object] = {
        "instrument": "BTCUSDT-PERP",
        "timestamp": NOW,
        "bid": Decimal("99"),
        "ask": Decimal("101"),
        "last": Decimal("100"),
        "volume": Decimal("1000"),
        "volatility": Decimal("0.02"),
        "liquidity": Decimal("1"),
        "source": "venue-a",
        "quality": DataQuality.LIVE,
    }
    values.update(changes)
    return MarketSnapshot(**values)


def _request(**changes: object) -> PositionSizingRequest:
    values: dict[str, object] = {
        "signal_id": "signal-1",
        "instrument": "BTCUSDT-PERP",
        "timestamp": NOW,
        "side": RiskSide.BUY,
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "confidence": Decimal("0.2"),
        "available_liquidity_notional": Decimal("10000"),
        "metadata": {},
    }
    values.update(changes)
    return PositionSizingRequest(**values)


def test_sizing_never_breaches_leverage_or_exposure_limits() -> None:
    entries = [Decimal("50"), Decimal("100"), Decimal("500"), Decimal("2000")]
    stop_fracs = [Decimal("0.01"), Decimal("0.05"), Decimal("0.2")]
    equities = [Decimal("100"), Decimal("1000"), Decimal("50000")]
    sides = [RiskSide.BUY, RiskSide.SELL]
    leverage_caps = [Decimal("1"), Decimal("5"), Decimal("20")]
    gross_used_fracs = [Decimal("0"), Decimal("0.3"), Decimal("0.8")]

    signal_counter = 0
    for entry in entries:
        for stop_frac in stop_fracs:
            for equity in equities:
                for side in sides:
                    for leverage_cap in leverage_caps:
                        for gross_used_frac in gross_used_fracs:
                            signal_counter += 1
                            policy = _policy(max_leverage=Decimal("20"))
                            instrument = _limits(max_leverage=Decimal("20"))
                            max_gross = Decimal("100000")
                            max_net = Decimal("100000")
                            policy = _policy(
                                max_leverage=Decimal("20"),
                                max_gross_notional=max_gross,
                                max_net_notional=max_net,
                            )
                            account = _account(
                                equity=equity,
                                peak_equity=equity,
                                leverage_cap=leverage_cap,
                                gross_notional=max_gross * gross_used_frac,
                                net_notional=max_net * gross_used_frac
                                if side == RiskSide.BUY
                                else -max_net * gross_used_frac,
                            )
                            stop_distance = entry * stop_frac
                            if side == RiskSide.BUY:
                                stop_price = entry - stop_distance
                            else:
                                stop_price = entry + stop_distance
                            request = _request(
                                signal_id=f"prop-{signal_counter}",
                                entry_price=entry,
                                stop_price=stop_price,
                                side=side,
                            )
                            engine = RiskEngine(policy)
                            decision = engine.evaluate(
                                request=request,
                                snapshot=_snapshot(),
                                account=account,
                                runtime=_runtime(),
                                instrument=instrument,
                                now=NOW,
                            )

                            max_leverage = min(
                                Decimal("20"),
                                policy.max_leverage,
                                instrument.max_leverage,
                                leverage_cap,
                            )
                            assert decision.leverage <= max_leverage
                            assert decision.max_leverage <= Decimal("20")

                            if decision.approved:
                                gross_after = (
                                    account.gross_notional + decision.notional
                                )
                                assert gross_after <= max_gross + Decimal("0.0001")
                                account_gross_leverage_after = Decimal(
                                    decision.metadata["account_gross_leverage_after"]
                                )
                                tolerance = Decimal("0.0001")
                                assert account_gross_leverage_after <= max_leverage + tolerance

                                if side == RiskSide.BUY:
                                    net_after = account.net_notional + decision.notional
                                    assert net_after <= max_net + Decimal("0.0001")
                                else:
                                    net_after = account.net_notional - decision.notional
                                    assert net_after >= -max_net - Decimal("0.0001")
