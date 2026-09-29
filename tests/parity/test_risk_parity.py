"""C6: RiskPolicy / PositionSizer parity. The Nautilus bridge path (inputs read from Nautilus
portfolio + cache) and the legacy RiskEngine are fed the IDENTICAL inputs; every decision,
size, reference price and cap must agree. Also pins the 30x ceiling semantics."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import product
from types import SimpleNamespace

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from nautilus_kernel.risk_bridge import (
    MarketInputs,
    NautilusRiskBridge,
)
from risk.engine import RiskEngine
from risk.models import MAX_SYSTEM_LEVERAGE, RiskPolicy, RiskSide
from risk.policy import PolicyRejection, RiskPolicyEvaluator

D = Decimal
IID = InstrumentId.from_str("GER40.ACTIVTRADES")
NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


class Money:
    def __init__(self, value):
        self._v = D(str(value))

    def as_decimal(self):
        return self._v


class RecordingEvaluator(RiskPolicyEvaluator):
    """Captures the exact inputs the Nautilus bridge derived, so legacy can be fed the same."""

    captured: dict | None = None

    def evaluate_entry(self, **kwargs):
        self.captured = kwargs
        return super().evaluate_entry(**kwargs)


def policy(max_leverage="30", risk_fraction="0.005"):
    return RiskPolicy(
        policy_id="parity",
        risk_fraction=D(risk_fraction),
        max_leverage=D(max_leverage),
        max_gross_notional=D("100000000"),
        max_net_notional=D("100000000"),
        max_daily_loss=D("100000"),
        max_drawdown=D("100000"),
        max_consecutive_losses=5,
        liquidity_fraction=D("0.5"),
        max_data_age=timedelta(minutes=10),
        max_signal_age=timedelta(minutes=10),
        estimated_cost_bps=D("3"),
    )


def nautilus_world(balance, unrealized, closed, open_signed):
    account = SimpleNamespace(base_currency="EUR", balance_total=lambda ccy: Money(balance))
    portfolio = SimpleNamespace(
        account=lambda venue: account,
        unrealized_pnl=lambda iid: Money(unrealized),
        net_exposure=lambda iid: Money("30000"),
    )
    opened = [SimpleNamespace(signed_qty=D(open_signed))] if open_signed else []
    cache = SimpleNamespace(
        position_snapshots=lambda: list(closed),
        positions_closed=lambda instrument_id=None: [],
        positions_open=lambda instrument_id=None: opened,
    )
    return portfolio, cache


def closed_losses(n):
    return [
        SimpleNamespace(
            id=f"P{i}",
            instrument_id=IID,
            ts_opened=int((NOW - timedelta(minutes=90 - 10 * i)).timestamp() * 1e9),
            ts_closed=int((NOW - timedelta(minutes=85 - 10 * i)).timestamp() * 1e9),
            realized_pnl=Money("-15"),
        )
        for i in range(n)
    ]


GRID = list(
    product(
        ["10", "20", "30"],  # policy max leverage (<= ceiling)
        [D("2000"), D("10000"), D("100000")],  # balance
        [D("0"), D("-80")],  # unrealized pnl
        [0, 2, 5],  # trailing losses in the Nautilus history
        [RiskSide.BUY, RiskSide.SELL],
        [D("15"), D("60"), D("400")],  # stop distance (index points)
        [D("1.5"), D("12")],  # spread (index points)
    )
)


def outcome_of(result):
    if isinstance(result, PolicyRejection):
        return ("REJECT", str(result.reason_code))
    return (
        "APPROVE",
        result.quantity,
        result.notional,
        result.leverage,
        result.max_leverage,
        result.reference_price,
    )


def run_case(case):
    max_lev, balance, unrealized, losses, side, stop_dist, spread = case
    evaluator = RecordingEvaluator(policy(max_lev))
    bridge = NautilusRiskBridge(
        instrument_id=IID,
        policy=policy(max_lev),
        limits=_limits(),
        evaluator=evaluator,
    )
    portfolio, cache = nautilus_world(balance, unrealized, closed_losses(losses), None)
    bid = D("25000.00")
    ask = bid + spread
    stop = bid - stop_dist if side == RiskSide.BUY else ask + stop_dist
    nautilus = bridge.evaluate_entry(
        portfolio=portfolio,
        cache=cache,
        market=MarketInputs(now=NOW, bid=bid, ask=ask, volume=D("100")),
        side=side,
        stop_price=stop,
        signal_id="s1",
    )
    inputs = evaluator.captured
    legacy_decision = RiskEngine(policy(max_lev)).evaluate(
        request=inputs["request"],
        snapshot=inputs["snapshot"],
        account=inputs["account"],
        runtime=inputs["runtime"],
        instrument=inputs["instrument"],
        now=inputs["now"],
    )
    if legacy_decision.approved:
        legacy = (
            "APPROVE",
            legacy_decision.quantity,
            legacy_decision.notional,
            legacy_decision.leverage,
            legacy_decision.max_leverage,
            D(legacy_decision.metadata["risk_reference_price"]),
        )
    else:
        legacy = ("REJECT", str(legacy_decision.reason_code))
    return legacy, outcome_of(nautilus)


def _limits():
    from risk.models import InstrumentRiskLimits

    return InstrumentRiskLimits(
        instrument="GER40",
        max_leverage=D("30"),
        quantity_step=D("0.25"),
        min_quantity=D("0.25"),
        min_notional=D(0),
        max_notional=D("100000000"),
        max_spread_bps=D("100"),
        maintenance_margin_rate=D("0.025"),
    )


def test_bridge_path_and_legacy_engine_agree_on_every_case_of_the_grid():
    approved = rejected = 0
    disagreements = []
    for case in GRID:
        legacy, nautilus = run_case(case)
        if legacy != nautilus:
            disagreements.append((case, legacy, nautilus))
        approved += legacy[0] == "APPROVE"
        rejected += legacy[0] == "REJECT"
    assert disagreements == [], disagreements[:3]
    assert approved > 50 and rejected > 20  # the grid genuinely exercises both outcomes


def test_leverage_never_exceeds_the_30x_ceiling_and_is_never_a_default():
    leverages = []
    for case in GRID:
        _, nautilus = run_case(case)
        if nautilus[0] == "APPROVE":
            leverages.append(nautilus[3])
            assert nautilus[3] <= MAX_SYSTEM_LEVERAGE and nautilus[4] <= MAX_SYSTEM_LEVERAGE
    assert D("30") == MAX_SYSTEM_LEVERAGE
    assert max(leverages) <= D("30")
    # risk budget, not the ceiling, sizes ordinary trades: the typical position is far below 30x
    assert sorted(leverages)[len(leverages) // 2] < D("10")


def test_a_policy_above_the_ceiling_cannot_be_constructed():
    with pytest.raises(ValueError):
        policy("31")
    policy("30")


def test_reference_price_is_the_executable_side_plus_buffer_in_both_paths():
    (legacy, nautilus) = run_case(("30", D("10000"), D("0"), 0, RiskSide.BUY, D("60"), D("1.5")))
    assert legacy[0] == nautilus[0] == "APPROVE" and legacy[5] == nautilus[5]
    assert nautilus[5] > D("25001.50")  # BUY: ask + slippage buffer, never entry or bid
    (legacy, nautilus) = run_case(("30", D("10000"), D("0"), 0, RiskSide.SELL, D("60"), D("1.5")))
    assert nautilus[5] < D("25000.00") and legacy[5] == nautilus[5]  # SELL: bid - buffer


def test_stop_on_the_wrong_side_is_rejected_by_both():
    evaluator = RecordingEvaluator(policy())
    bridge = NautilusRiskBridge(
        instrument_id=IID, policy=policy(), limits=_limits(), evaluator=evaluator
    )
    portfolio, cache = nautilus_world(D("10000"), D("0"), [], None)
    result = bridge.evaluate_entry(
        portfolio=portfolio,
        cache=cache,
        market=MarketInputs(now=NOW, bid=D("25000"), ask=D("25001.5"), volume=D(1)),
        side=RiskSide.BUY,
        stop_price=D("25100"),
        signal_id="s2",
    )
    inputs = evaluator.captured
    legacy = RiskEngine(policy()).evaluate(
        request=inputs["request"],
        snapshot=inputs["snapshot"],
        account=inputs["account"],
        runtime=inputs["runtime"],
        instrument=inputs["instrument"],
        now=inputs["now"],
    )
    assert isinstance(result, PolicyRejection) and str(result.reason_code) == "INVALID_STOP"
    assert not legacy.approved and str(legacy.reason_code) == "INVALID_STOP"
