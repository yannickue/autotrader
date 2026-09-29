"""Risk contracts 15-17: reduce-only, kill switch / halt, no same-tick flip.

Parametrized over the `admission` fixture (pure policy + legacy engine) except
where a test is explicitly marked LEGACY-ONLY (state that only the legacy
engine owns: the reduce-only reservation ledger).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from risk.engine import RiskEngine
from risk.models import ReconciliationState, RiskReason, RiskSide, RuntimeMode
from tests.contracts.conftest import (
    INSTRUMENT,
    NOW,
    EntryAdmission,
    Scenario,
    make_account,
    make_policy,
    make_runtime,
)

D = Decimal


def _long(qty: str = "2"):
    return make_account(positions={INSTRUMENT: D(qty)})


def _short(qty: str = "2"):
    return make_account(positions={INSTRUMENT: -D(qty)})


# -- 15. reduce-only never increases exposure ---------------------------------------


def test_c15_reduce_only_against_a_long_is_a_sell_and_adds_no_exposure(
    admission: EntryAdmission,
) -> None:
    outcome = admission.admit_reduce_only(account=_long(), side=RiskSide.SELL, quantity=D("1"))

    assert outcome.approved is True
    assert outcome.reduce_only is True
    assert outcome.quantity == D("1")
    assert outcome.notional == 0  # never adds exposure
    assert outcome.leverage == 0


def test_c15_reduce_only_against_a_short_is_a_buy(admission: EntryAdmission) -> None:
    outcome = admission.admit_reduce_only(account=_short(), side=RiskSide.BUY, quantity=D("2"))
    assert outcome.approved is True
    assert outcome.notional == 0


def test_c15_reduce_only_may_close_exactly_the_whole_position(admission: EntryAdmission) -> None:
    outcome = admission.admit_reduce_only(account=_long(), side=RiskSide.SELL, quantity=D("2"))
    assert outcome.approved is True


def test_c15_reduce_only_cannot_exceed_the_position(admission: EntryAdmission) -> None:
    outcome = admission.admit_reduce_only(account=_long(), side=RiskSide.SELL, quantity=D("3"))
    assert outcome.approved is False
    assert outcome.reason_code == str(RiskReason.QUANTITY_INVALID)


def test_c15_reduce_only_on_the_position_side_is_rejected(admission: EntryAdmission) -> None:
    same_side_long = admission.admit_reduce_only(
        account=_long(), side=RiskSide.BUY, quantity=D("1")
    )
    same_side_short = admission.admit_reduce_only(
        account=_short(), side=RiskSide.SELL, quantity=D("1")
    )
    for outcome in (same_side_long, same_side_short):
        assert outcome.approved is False
        assert outcome.reason_code == str(RiskReason.SIDE_MISMATCH)


def test_c15_reduce_only_with_no_position_is_rejected(admission: EntryAdmission) -> None:
    outcome = admission.admit_reduce_only(
        account=make_account(positions={}), side=RiskSide.SELL, quantity=D("1")
    )
    assert outcome.approved is False
    assert outcome.reason_code == str(RiskReason.NO_POSITION)


@pytest.mark.parametrize("quantity", ["0", "-1"])
def test_c15_non_positive_quantity_is_rejected(admission: EntryAdmission, quantity: str) -> None:
    outcome = admission.admit_reduce_only(
        account=_long(), side=RiskSide.SELL, quantity=D(quantity)
    )
    assert outcome.approved is False
    assert outcome.reason_code == str(RiskReason.QUANTITY_INVALID)


def test_c15_reduce_only_still_requires_a_known_account(admission: EntryAdmission) -> None:
    account = make_account(known=False, positions={INSTRUMENT: D("2")})
    outcome = admission.admit_reduce_only(account=account, side=RiskSide.SELL, quantity=D("1"))
    assert outcome.approved is False
    assert outcome.reason_code == str(RiskReason.ACCOUNT_UNKNOWN)


@pytest.mark.parametrize(
    "state",
    [
        ReconciliationState.NOT_RECONCILED,
        ReconciliationState.RECONCILING,
        ReconciliationState.MISMATCH,
    ],
)
def test_c15_reduce_only_requires_a_reconciled_account(
    admission: EntryAdmission, state: ReconciliationState
) -> None:
    account = make_account(reconciliation=state, positions={INSTRUMENT: D("2")})
    outcome = admission.admit_reduce_only(account=account, side=RiskSide.SELL, quantity=D("1"))
    assert outcome.approved is False
    assert outcome.reason_code == str(RiskReason.ACCOUNT_UNRECONCILED)


# -- 16. kill switch and latched halt: no NEW exposure, reduce-only still allowed ----


def test_c16_kill_switch_blocks_new_exposure(admission: EntryAdmission) -> None:
    outcome = admission.admit(Scenario(runtime=make_runtime(kill_switch=True)))
    assert outcome.approved is False
    assert outcome.reason_code == str(RiskReason.HALTED)
    assert outcome.quantity == 0 and outcome.notional == 0


def test_c16_halted_runtime_mode_blocks_new_exposure(admission: EntryAdmission) -> None:
    outcome = admission.admit(Scenario(runtime=make_runtime(mode=RuntimeMode.HALTED)))
    assert outcome.approved is False
    assert outcome.reason_code == str(RiskReason.HALTED)


def test_c16_latched_halt_blocks_new_exposure(admission: EntryAdmission) -> None:
    outcome = admission.admit(Scenario(latched_halt=True))
    assert outcome.approved is False
    assert outcome.reason_code == str(RiskReason.HALTED)


@pytest.mark.parametrize(
    "runtime",
    [
        make_runtime(mode=RuntimeMode.STARTING),
        make_runtime(mode=RuntimeMode.RECONCILING),
        make_runtime(mode=RuntimeMode.DEGRADED),
        make_runtime(risk_ready=False),
    ],
    ids=["starting", "reconciling", "degraded", "risk_not_ready"],
)
def test_c16_runtime_not_ready_blocks_new_exposure(
    admission: EntryAdmission, runtime
) -> None:
    outcome = admission.admit(Scenario(runtime=runtime))
    assert outcome.approved is False
    assert outcome.reason_code == str(RiskReason.RUNTIME_NOT_READY)


def test_c16_reduce_only_is_still_allowed_under_kill_switch_and_halt(
    admission: EntryAdmission,
) -> None:
    outcome = admission.admit_reduce_only(
        account=_long(),
        side=RiskSide.SELL,
        quantity=D("1"),
        runtime=make_runtime(kill_switch=True, mode=RuntimeMode.HALTED),
        latched_halt=True,
    )
    assert outcome.approved is True
    assert outcome.notional == 0


def test_c16_reduce_only_under_halt_still_needs_known_reconciled_account(
    admission: EntryAdmission,
) -> None:
    halted = make_runtime(kill_switch=True, mode=RuntimeMode.HALTED)
    unknown = admission.admit_reduce_only(
        account=make_account(known=False, positions={INSTRUMENT: D("2")}),
        side=RiskSide.SELL,
        quantity=D("1"),
        runtime=halted,
        latched_halt=True,
    )
    mismatch = admission.admit_reduce_only(
        account=make_account(
            reconciliation=ReconciliationState.MISMATCH, positions={INSTRUMENT: D("2")}
        ),
        side=RiskSide.SELL,
        quantity=D("1"),
        runtime=halted,
        latched_halt=True,
    )
    assert unknown.approved is False and unknown.reason_code == str(RiskReason.ACCOUNT_UNKNOWN)
    assert mismatch.approved is False
    assert mismatch.reason_code == str(RiskReason.ACCOUNT_UNRECONCILED)


# -- 17. no same-tick flip through zero ---------------------------------------------


def test_c17_reduce_only_larger_than_position_is_rejected_not_clamped(
    admission: EntryAdmission,
) -> None:
    outcome = admission.admit_reduce_only(
        account=make_account(positions={INSTRUMENT: D("1")}),
        side=RiskSide.SELL,
        quantity=D("1.5"),
    )
    assert outcome.approved is False
    assert outcome.reason_code == str(RiskReason.QUANTITY_INVALID)
    assert outcome.quantity == 0


def test_c17_already_reserved_reduce_only_quantity_counts_against_the_position(
    admission: EntryAdmission,
) -> None:
    """Position 1, 1 already reserved by an approved-but-unfilled reduce-only:
    a second reduce-only of 1 would jointly flip the position -> rejected."""
    account = make_account(positions={INSTRUMENT: D("1")})
    second = admission.admit_reduce_only(
        account=account, side=RiskSide.SELL, quantity=D("1"), reserved_quantity=D("1")
    )
    assert second.approved is False
    assert second.reason_code == str(RiskReason.QUANTITY_INVALID)

    remainder = admission.admit_reduce_only(
        account=make_account(positions={INSTRUMENT: D("2")}),
        side=RiskSide.SELL,
        quantity=D("1"),
        reserved_quantity=D("1"),
    )
    assert remainder.approved is True  # 1 reserved + 1 = 2 <= position 2


def test_c17_legacy_only_concurrent_reduce_only_reservations_cannot_jointly_flip() -> None:
    """LEGACY-ONLY: the reservation ledger is state only the legacy `RiskEngine`
    owns (the pure evaluator takes `reserved_reduce_only_quantity` as an input,
    covered above for both). This drives the real sequence: approve, reserve,
    reject the second, release, approve again."""
    engine = RiskEngine(make_policy())
    account = make_account(positions={INSTRUMENT: D("1")})

    def reduce(request_id: str):
        return engine.evaluate_reduce_only(
            request_id=request_id,
            instrument=INSTRUMENT,
            side=RiskSide.SELL,
            quantity=D("1"),
            account=account,
            runtime=make_runtime(),
            now=NOW,
        )

    first = reduce("flip-1")
    assert first.approved is True

    second = reduce("flip-2")
    assert second.approved is False
    assert second.reason_code == RiskReason.QUANTITY_INVALID

    engine.release(first.decision_id)
    assert reduce("flip-3").approved is True
