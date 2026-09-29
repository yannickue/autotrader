"""C2: the pure RiskPolicyEvaluator/PositionSizer vs the legacy RiskEngine.

The legacy `RiskEngine` is now a thin stateful wrapper (halt latch, decision
cache, reservation ledger) around the pure policy, and remains a shadow oracle.
These tests pin (a) parity of every decision field over a sweep of scenarios,
(b) purity: no owned mutable state, no I/O-ish imports, inputs never mutated,
(c) the reconciliation-state contract on the entry and reduce-only paths.
"""

import ast
import copy
import itertools
from decimal import Decimal
from pathlib import Path

import pytest

from risk.engine import RiskEngine
from risk.models import ReconciliationState, RiskReason, RiskSide
from risk.policy import (
    PendingExposure,
    PolicyRejection,
    ProposedOrderIntent,
    RiskPolicyEvaluator,
)
from risk.sizing import PositionSizer
from tests.unit.risk.test_engine import (
    NOW,
    _account,
    _limits,
    _policy,
    _request,
    _runtime,
    _snapshot,
)

SRC = Path(__file__).resolve().parents[3] / "src"


def _entry_kwargs(**overrides):
    kwargs = {
        "request": _request(),
        "snapshot": _snapshot(),
        "account": _account(),
        "runtime": _runtime(),
        "instrument": _limits(),
        "now": NOW,
    }
    kwargs.update(overrides)
    return kwargs


def _scenarios():
    sides = (RiskSide.BUY, RiskSide.SELL)
    accounts = (
        {},
        {"realized_pnl_today": Decimal("-60")},
        {"realized_pnl_today": Decimal("-100")},
        {"equity": Decimal("900"), "peak_equity": Decimal("1000")},
        {"equity": Decimal("800"), "peak_equity": Decimal("1000")},
        {"consecutive_losses": 2},
        {"consecutive_losses": 3},
        {"gross_notional": Decimal("4000"), "net_notional": Decimal("3000")},
        {"reconciliation": ReconciliationState.NOT_RECONCILED},
        {"reconciliation": ReconciliationState.MISMATCH},
        {"known": False},
    )
    snapshots = (
        {},
        {"bid": Decimal("90"), "ask": Decimal("110")},  # wide spread
        {"volatility": Decimal("0.5")},
    )
    runtimes = ({}, {"kill_switch": True})
    policies = ({}, {"max_leverage": Decimal("3")}, {"risk_fraction": Decimal("0.5")})
    return list(itertools.product(sides, accounts, snapshots, runtimes, policies))


@pytest.mark.parametrize(("side", "acct", "snap", "rt", "pol"), _scenarios())
def test_entry_decision_parity_pure_vs_legacy(side, acct, snap, rt, pol) -> None:
    stop = Decimal("95") if side is RiskSide.BUY else Decimal("105")
    kwargs = _entry_kwargs(
        request=_request(side=side, stop_price=stop),
        account=_account(**acct),
        snapshot=_snapshot(**snap),
        runtime=_runtime(**rt),
    )
    policy = _policy(**pol)
    legacy = RiskEngine(policy).evaluate(**kwargs)
    pure = RiskPolicyEvaluator(policy).evaluate_entry(**kwargs)

    if isinstance(pure, PolicyRejection):
        assert legacy.approved is False
        assert legacy.reason_code == pure.reason_code
        assert legacy.reason == pure.reason
    else:
        assert legacy.approved is True
        assert (legacy.quantity, legacy.notional, legacy.leverage) == (
            pure.quantity,
            pure.notional,
            pure.leverage,
        )
        assert legacy.max_leverage == pure.max_leverage
        assert legacy.risk_budget == pure.risk_budget
        assert legacy.stop_price == pure.stop_price
        assert dict(legacy.metadata) == pure.metadata
        assert pure.reduce_only is False


def test_pending_exposure_is_an_input_and_matches_legacy_reservations() -> None:
    policy = _policy()
    engine = RiskEngine(policy)
    first = engine.evaluate(**_entry_kwargs(request=_request(signal_id="s1")))
    assert first.approved
    second_legacy = engine.evaluate(**_entry_kwargs(request=_request(signal_id="s2")))

    pure = RiskPolicyEvaluator(policy)
    second_pure = pure.evaluate_entry(
        **_entry_kwargs(request=_request(signal_id="s2")),
        pending=PendingExposure(gross=first.notional, net=first.notional),
    )
    without_pending = pure.evaluate_entry(**_entry_kwargs(request=_request(signal_id="s2")))

    assert isinstance(second_pure, ProposedOrderIntent)
    assert second_legacy.quantity == second_pure.quantity
    assert dict(second_legacy.metadata) == second_pure.metadata
    # pending exposure genuinely changes the outcome (it is not ignored)
    assert without_pending.metadata != second_pure.metadata


def test_latched_halt_is_an_input_not_owned_state() -> None:
    pure = RiskPolicyEvaluator(_policy())
    blocked = pure.evaluate_entry(**_entry_kwargs(), latched_halt=True)
    assert isinstance(blocked, PolicyRejection) and blocked.reason_code is RiskReason.HALTED
    # ... and the evaluator did not latch anything: the next call is unaffected.
    assert isinstance(pure.evaluate_entry(**_entry_kwargs()), ProposedOrderIntent)


def test_evaluator_is_deterministic_stateless_and_does_not_mutate_inputs() -> None:
    pure = RiskPolicyEvaluator(_policy())
    kwargs = _entry_kwargs()
    frozen_inputs = copy.deepcopy(kwargs)
    state_before = {k: repr(v) for k, v in vars(pure).items()}

    first = pure.evaluate_entry(**kwargs)
    second = pure.evaluate_entry(**kwargs)

    assert first == second
    assert kwargs == frozen_inputs
    assert {k: repr(v) for k, v in vars(pure).items()} == state_before
    assert not any("reserv" in name or "halt" in name for name in vars(pure))
    assert not hasattr(pure, "release") and not hasattr(pure, "export_state")


def test_position_sizer_is_pure_and_matches_documented_arithmetic() -> None:
    sizer = PositionSizer(_policy())
    snapshot = _snapshot()
    ref = sizer.reference_price(side=RiskSide.BUY, snapshot=snapshot)
    assert ref == Decimal("101.0505")  # ask + 5 bps, never request.entry_price
    assert sizer.reference_price(side=RiskSide.SELL, snapshot=snapshot) == Decimal("98.9505")
    assert sizer.max_leverage(instrument=_limits(), account=_account()) == Decimal("5")
    reduce_risk, fraction = sizer.risk_reduction(_account(consecutive_losses=2))
    assert reduce_risk is True and fraction == Decimal("0.005")
    assert sizer.risk_reduction(_account())[0] is False


@pytest.mark.parametrize("module", ["policy.py", "sizing.py", "trading_day.py"])
def test_pure_modules_import_no_runtime_io_or_state_owners(module: str) -> None:
    forbidden = {
        "adapters",
        "MetaTrader5",
        "nautilus_trader",
        "execution",
        "portfolio",
        "persistence",
        "pipeline",
        "exits",
        "sqlite3",
        "socket",
        "requests",
        "os",
        "subprocess",
    }
    tree = ast.parse((SRC / "risk" / module).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(forbidden), imported & forbidden


# -- reconciliation-state contract on both paths -----------------------------


@pytest.mark.parametrize(
    "state",
    [
        ReconciliationState.NOT_RECONCILED,
        ReconciliationState.RECONCILING,
        ReconciliationState.MISMATCH,
    ],
)
def test_any_non_reconciled_state_blocks_new_exposure_and_reduce_only(state) -> None:
    pure = RiskPolicyEvaluator(_policy())
    account = _account(reconciliation=state, positions={"BTCUSDT-PERP": Decimal("2")})

    entry = pure.evaluate_entry(**_entry_kwargs(account=account))
    reduce = pure.evaluate_reduce_only(
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("1"),
        account=account,
        now=NOW,
    )

    for result in (entry, reduce):
        assert isinstance(result, PolicyRejection)
        assert result.reason_code is RiskReason.ACCOUNT_UNRECONCILED
        assert state.value in result.reason  # the text names the state
    legacy = RiskEngine(_policy()).evaluate_reduce_only(
        request_id="r1",
        instrument="BTCUSDT-PERP",
        side=RiskSide.SELL,
        quantity=Decimal("1"),
        account=account,
        runtime=_runtime(),
        now=NOW,
    )
    assert legacy.approved is False and legacy.reason_code is RiskReason.ACCOUNT_UNRECONCILED


def test_reduce_only_never_adds_exposure_and_ignores_halt_but_not_reconciliation() -> None:
    pure = RiskPolicyEvaluator(_policy())
    account = _account(positions={"BTCUSDT-PERP": Decimal("2")})
    ok = pure.evaluate_reduce_only(
        instrument="BTCUSDT-PERP", side=RiskSide.SELL, quantity=Decimal("2"), account=account,
        now=NOW,
    )
    assert isinstance(ok, ProposedOrderIntent)
    assert ok.reduce_only is True and ok.notional == 0 and ok.leverage == 0

    for kwargs, reason in (
        ({"quantity": Decimal("2.1")}, RiskReason.QUANTITY_INVALID),  # would flip through zero
        ({"quantity": Decimal("1"), "side": RiskSide.BUY}, RiskReason.SIDE_MISMATCH),
        (
            {"quantity": Decimal("1"), "reserved_reduce_only_quantity": Decimal("1.5")},
            RiskReason.QUANTITY_INVALID,
        ),
        ({"quantity": Decimal("1"), "instrument": "OTHER"}, RiskReason.NO_POSITION),
    ):
        args = {"instrument": "BTCUSDT-PERP", "side": RiskSide.SELL, "account": account, "now": NOW}
        args.update(kwargs)
        result = pure.evaluate_reduce_only(**args)
        assert isinstance(result, PolicyRejection) and result.reason_code is reason
