"""C4: thin risk bridge maps Nautilus state faithfully; no competing state ownership."""

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.model.identifiers import InstrumentId

import nautilus_kernel
from nautilus_kernel.risk_bridge import MarketInputs, NautilusRiskBridge
from risk.models import RiskSide
from risk.policy import PolicyRejection, ProposedOrderIntent

IID = InstrumentId.from_str("GER40.ACTIVTRADES")
NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


class Money:
    def __init__(self, value):
        self._v = Decimal(str(value))

    def as_decimal(self):
        return self._v


def closed_position(pnl: str, closed_at: datetime, idx: int):
    return SimpleNamespace(
        id=f"P{idx}",
        instrument_id=IID,
        ts_opened=int((closed_at - timedelta(minutes=5)).timestamp() * 1e9),
        ts_closed=int(closed_at.timestamp() * 1e9),
        realized_pnl=Money(pnl),
    )


def fakes(balance="10000", unrealized="0", closed=(), open_positions=()):
    account = SimpleNamespace(base_currency="EUR", balance_total=lambda ccy: Money(balance))
    portfolio = SimpleNamespace(
        account=lambda venue: account,
        unrealized_pnl=lambda iid: Money(unrealized),
        net_exposure=lambda iid: Money("31000"),
    )
    cache = SimpleNamespace(
        position_snapshots=lambda: list(closed),
        positions_closed=lambda instrument_id=None: [],
        positions_open=lambda instrument_id=None: list(open_positions),
    )
    return portfolio, cache


def market(bid="25000.00", ask="25001.50"):
    return MarketInputs(now=NOW, bid=Decimal(bid), ask=Decimal(ask), volume=Decimal(100))


def test_approved_intent_uses_conservative_reference_and_broker_step():
    bridge = NautilusRiskBridge(instrument_id=IID)
    portfolio, cache = fakes()
    out = bridge.evaluate_entry(
        portfolio=portfolio,
        cache=cache,
        market=market(),
        side=RiskSide.BUY,
        stop_price=Decimal("24960"),
        signal_id="s1",
    )
    assert isinstance(out, ProposedOrderIntent)
    ask = Decimal("25001.50")
    assert out.reference_price > ask  # BUY sized off ask + slippage buffer, never entry/bid
    assert out.quantity % Decimal("0.25") == 0 and out.quantity >= Decimal("0.25")
    assert out.leverage <= Decimal("20") and out.metadata["policy_id"].startswith("c4-")


def test_sell_reference_is_bid_minus_buffer():
    bridge = NautilusRiskBridge(instrument_id=IID)
    portfolio, cache = fakes()
    out = bridge.evaluate_entry(
        portfolio=portfolio,
        cache=cache,
        market=market(),
        side=RiskSide.SELL,
        stop_price=Decimal("25050"),
        signal_id="s2",
    )
    assert isinstance(out, ProposedOrderIntent)
    assert out.reference_price < Decimal("25000.00")


def test_invalid_stop_is_rejected_by_pure_policy():
    bridge = NautilusRiskBridge(instrument_id=IID)
    portfolio, cache = fakes()
    out = bridge.evaluate_entry(
        portfolio=portfolio,
        cache=cache,
        market=market(),
        side=RiskSide.BUY,
        stop_price=Decimal("25100"),  # stop above entry for a BUY
        signal_id="s3",
    )
    assert isinstance(out, PolicyRejection) and str(out.reason_code) == "INVALID_STOP"


def test_consecutive_losses_come_from_nautilus_history_and_block_entries():
    losses = [closed_position("-20", NOW - timedelta(minutes=60 - 10 * i), i) for i in range(5)]
    bridge = NautilusRiskBridge(instrument_id=IID)
    portfolio, cache = fakes(closed=losses)
    state = bridge.account_state(portfolio=portfolio, cache=cache, now=NOW)
    assert state.consecutive_losses == 5 and state.realized_pnl_today == Decimal("-100")
    out = bridge.evaluate_entry(
        portfolio=portfolio,
        cache=cache,
        market=market(),
        side=RiskSide.BUY,
        stop_price=Decimal("24960"),
        signal_id="s4",
    )
    assert isinstance(out, PolicyRejection)
    assert str(out.reason_code) == "CONSECUTIVE_LOSS_LIMIT"


def test_win_resets_streak_and_previous_day_pnl_is_excluded():
    old_loss = closed_position("-300", NOW - timedelta(days=1, hours=2), 0)
    recent = [
        closed_position("-10", NOW - timedelta(minutes=30), 1),
        closed_position("+5", NOW - timedelta(minutes=20), 2),
    ]
    bridge = NautilusRiskBridge(instrument_id=IID)
    portfolio, cache = fakes(closed=[old_loss, *recent])
    state = bridge.account_state(portfolio=portfolio, cache=cache, now=NOW)
    assert state.consecutive_losses == 0
    assert state.realized_pnl_today == Decimal("-5")  # trading-day window, not all-time


def test_peak_equity_is_tracked_and_open_exposure_is_read_from_portfolio():
    bridge = NautilusRiskBridge(instrument_id=IID)
    p1, c1 = fakes(balance="10500")
    bridge.account_state(portfolio=p1, cache=c1, now=NOW)
    long_open = SimpleNamespace(signed_qty=Decimal("1.25"))
    p2, c2 = fakes(balance="10200", open_positions=[long_open])
    state = bridge.account_state(portfolio=p2, cache=c2, now=NOW)
    assert state.peak_equity == Decimal("10500") and state.equity == Decimal("10200")
    assert state.gross_notional == Decimal("31000") and state.net_notional == Decimal("31000")
    assert state.positions == {"GER40": Decimal("1.25")}


FORBIDDEN_STATE_OWNERS = ("execution", "portfolio", "persistence", "pipeline", "margin", "exits")
FORBIDDEN_VENUE = ("MetaTrader5", "adapters.activtrades_mt5.real_client")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.mark.parametrize("path", sorted(Path(nautilus_kernel.__file__).parent.glob("*.py")))
def test_kernel_package_has_no_competing_state_owner_and_no_venue_access(path):
    for module in _imports(path):
        root = module.split(".")[0]
        assert root not in FORBIDDEN_STATE_OWNERS, f"{path.name} imports legacy runtime {module}"
        assert not any(module.startswith(f) for f in FORBIDDEN_VENUE), f"{path.name}: {module}"


def test_strategy_never_calls_venue_or_legacy_execution():
    source = (Path(nautilus_kernel.__file__).parent / "proof_strategy.py").read_text()
    assert "PaperExecutionEngine" not in source and "order_send" not in source


def test_bridge_is_generalised_per_instrument_and_refuses_mismatched_limits():
    from risk.models import InstrumentRiskLimits

    nas = InstrumentId.from_str("NAS100.ACTIVTRADES")
    limits = InstrumentRiskLimits(
        instrument="NAS100",
        max_leverage=Decimal("20"),
        quantity_step=Decimal("0.2"),
        min_quantity=Decimal("0.2"),
        min_notional=Decimal(0),
        max_notional=Decimal("4000000"),
        max_spread_bps=Decimal("10"),
        maintenance_margin_rate=Decimal("0.05"),
    )
    bridge = NautilusRiskBridge(instrument_id=nas, limits=limits)
    portfolio, cache = fakes()
    out = bridge.evaluate_entry(
        portfolio=portfolio,
        cache=cache,
        market=market("21000.00", "21001.00"),
        side=RiskSide.BUY,
        stop_price=Decimal("20950"),
        signal_id="nas",
    )
    assert isinstance(out, ProposedOrderIntent) and out.instrument == "NAS100"
    assert out.quantity % Decimal("0.2") == 0
    with pytest.raises(ValueError, match="instrument limits are for"):
        NautilusRiskBridge(instrument_id=nas)  # default limits describe GER40
