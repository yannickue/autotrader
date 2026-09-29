"""Risk contracts 1-14: new-exposure admission.

Every test takes the `admission` fixture and therefore runs against BOTH the pure
`RiskPolicyEvaluator` and the legacy `RiskEngine` (parity oracle). Assertions pin
behavior (reason codes, arithmetic), never engine structure.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from risk.models import (
    MAX_SYSTEM_LEVERAGE,
    ReconciliationState,
    RiskReason,
    RiskSide,
)
from tests.contracts.conftest import (
    NOW,
    EntryAdmission,
    Scenario,
    make_account,
    make_limits,
    make_policy,
    make_request,
    make_snapshot,
)

D = Decimal


def _rejects(outcome, reason: RiskReason) -> None:
    assert outcome.approved is False, f"expected {reason}, got approval"
    assert outcome.reason_code == str(reason)
    assert outcome.quantity == 0
    assert outcome.notional == 0


# -- 1. hard system leverage ceiling ------------------------------------------------


def test_c01_policy_and_instrument_construction_reject_leverage_above_20x() -> None:
    assert D("20") == MAX_SYSTEM_LEVERAGE
    with pytest.raises(ValueError):
        make_policy(max_leverage=D("21"))
    with pytest.raises(ValueError):
        make_limits(max_leverage=D("21"))
    with pytest.raises(ValueError):
        make_policy(max_leverage=D("0"))


def test_c01_leverage_20x_is_never_exceeded_even_if_account_cap_allows_more(
    admission: EntryAdmission,
) -> None:
    # Policy and instrument are at the ceiling; the account cap (which has no
    # construction-time check) claims 50x. Sizing is driven to the leverage cap.
    scenario = Scenario(
        policy=make_policy(
            risk_fraction=D("1"),
            max_leverage=D("20"),
            max_gross_notional=D("50000"),
            max_net_notional=D("50000"),
        ),
        limits=make_limits(max_leverage=D("20"), max_notional=D("50000")),
        account=make_account(leverage_cap=D("50")),
        request=make_request(
            stop_price=D("99"), available_liquidity_notional=D("1000000")
        ),
    )
    outcome = admission.admit(scenario)

    assert outcome.approved is True, outcome.reason_code
    assert outcome.max_leverage == D("20")
    assert outcome.leverage <= D("20")
    assert outcome.binding_constraint == "leverage_cap"
    # ...and the account-level leverage is bounded by the same ceiling.
    assert outcome.notional / scenario.account.equity <= D("20")


def test_c01_effective_cap_is_the_minimum_of_policy_instrument_and_account(
    admission: EntryAdmission,
) -> None:
    outcome = admission.admit(Scenario())  # policy 10, instrument 8, account 5
    assert outcome.approved is True
    assert outcome.max_leverage == D("5")


# -- 2. position sizing -------------------------------------------------------------


def test_c02_sizing_risk_budget_arithmetic_with_cost_allowance(
    admission: EntryAdmission,
) -> None:
    outcome = admission.admit(Scenario())

    assert outcome.approved is True
    assert outcome.reason_code == str(RiskReason.APPROVED)
    # risk_budget = equity 1000 * 1% = 10
    assert outcome.risk_budget == D("10.00")
    # reference 101.0505; per-unit loss = stop distance 5 + round-trip cost
    # (101.0505 * 10bps * 2 = 0.202101) -> 10 / 5.202101 = 1.9223 -> 1.9
    assert outcome.quantity == D("1.9")
    assert outcome.notional == D("191.99595")
    assert outcome.leverage == D("0.19199595")
    assert outcome.binding_constraint == "risk_budget"


def test_c02_cost_allowance_shrinks_the_position(admission: EntryAdmission) -> None:
    free = admission.admit(Scenario(policy=make_policy(estimated_cost_bps=D("0"))))
    costly = admission.admit(Scenario(policy=make_policy(estimated_cost_bps=D("10"))))

    assert free.approved and costly.approved
    assert free.quantity == D("2.0")  # 10 / 5 exactly
    assert costly.quantity < free.quantity


def test_c02_quantity_is_rounded_down_to_the_step_never_up(admission: EntryAdmission) -> None:
    limits = make_limits(quantity_step=D("0.5"), min_quantity=D("0.5"))
    outcome = admission.admit(Scenario(limits=limits))

    # unrounded 1.9223 -> 1.5 (down), never 2.0
    assert outcome.approved is True
    assert outcome.quantity == D("1.5")
    assert outcome.quantity % D("0.5") == 0


def test_c02_confidence_never_influences_size(admission: EntryAdmission) -> None:
    low = admission.admit(Scenario(request=make_request(confidence=D("0"))))
    high = admission.admit(Scenario(request=make_request(confidence=D("1"))))
    assert low.quantity == high.quantity and low.notional == high.notional


def test_c02_size_below_instrument_minimum_is_rejected(admission: EntryAdmission) -> None:
    outcome = admission.admit(Scenario(policy=make_policy(risk_fraction=D("0.0001"))))
    _rejects(outcome, RiskReason.SIZE_BELOW_MINIMUM)


# -- 3. stop-distance sizing and invalid stop side ---------------------------------


def test_c03_size_scales_with_stop_distance(admission: EntryAdmission) -> None:
    near = admission.admit(Scenario(request=make_request(stop_price=D("95"))))
    far = admission.admit(Scenario(request=make_request(stop_price=D("90"))))

    # far: 10 / (10 + 0.202101) = 0.98 -> 0.9
    assert near.quantity == D("1.9")
    assert far.approved is True
    assert far.quantity == D("0.9")


@pytest.mark.parametrize(
    "side, entry, stop",
    [
        (RiskSide.BUY, "100", "105"),  # BUY stop above entry
        (RiskSide.BUY, "100", "100"),  # zero distance
        (RiskSide.SELL, "100", "95"),  # SELL stop below entry
        (RiskSide.SELL, "100", "100"),
    ],
)
def test_c03_invalid_stop_side_is_rejected(
    admission: EntryAdmission, side: RiskSide, entry: str, stop: str
) -> None:
    request = make_request(side=side, entry_price=D(entry), stop_price=D(stop))
    _rejects(admission.admit(Scenario(request=request)), RiskReason.INVALID_STOP)


def test_c03_stop_between_reference_price_and_entry_is_rejected(
    admission: EntryAdmission,
) -> None:
    # Wide market: reference = ask 110 + 5bps = 110.055; entry 115 is inside the
    # deviation tolerance; stop 112 is below entry but ABOVE the reference price
    # (would trigger immediately) -> INVALID_STOP.
    scenario = Scenario(
        limits=make_limits(max_spread_bps=D("3000"), max_notional=D("100000")),
        snapshot=make_snapshot(bid=D("90"), ask=D("110"), volatility=D("0.05")),
        request=make_request(entry_price=D("115"), stop_price=D("112")),
    )
    _rejects(admission.admit(scenario), RiskReason.INVALID_STOP)


# -- 4. executable reference price --------------------------------------------------


def test_c04_buy_reference_price_is_ask_plus_slippage(admission: EntryAdmission) -> None:
    outcome = admission.admit(Scenario())
    assert outcome.reference_price == D("101.0505")  # ask 101 + 5bps
    assert outcome.notional == outcome.quantity * D("101.0505")


def test_c04_sell_reference_price_is_bid_minus_slippage(admission: EntryAdmission) -> None:
    request = make_request(side=RiskSide.SELL, entry_price=D("100"), stop_price=D("105"))
    outcome = admission.admit(Scenario(request=request))
    assert outcome.approved is True
    assert outcome.reference_price == D("98.9505")  # bid 99 - 5bps


def test_c04_reference_price_never_comes_from_request_entry_price(
    admission: EntryAdmission,
) -> None:
    a = admission.admit(Scenario(request=make_request(entry_price=D("100"))))
    b = admission.admit(Scenario(request=make_request(entry_price=D("100.5"))))
    assert a.approved and b.approved
    assert a.reference_price == b.reference_price == D("101.0505")


def test_c04_sell_entry_price_far_below_market_is_rejected(admission: EntryAdmission) -> None:
    request = make_request(side=RiskSide.SELL, entry_price=D("10"), stop_price=D("110"))
    _rejects(admission.admit(Scenario(request=request)), RiskReason.ENTRY_PRICE_DEVIATION)


def test_c04_buy_entry_price_far_above_market_is_rejected(admission: EntryAdmission) -> None:
    request = make_request(entry_price=D("1000"), stop_price=D("900"))
    _rejects(admission.admit(Scenario(request=request)), RiskReason.ENTRY_PRICE_DEVIATION)


def test_c04_deviation_tolerance_widens_with_spread_and_volatility(
    admission: EntryAdmission,
) -> None:
    scenario = Scenario(
        limits=make_limits(max_spread_bps=D("3000"), max_notional=D("100000")),
        snapshot=make_snapshot(bid=D("90"), ask=D("110"), volatility=D("0.05")),
        request=make_request(entry_price=D("95"), stop_price=D("80")),
    )
    assert admission.admit(scenario).approved is True


# -- 5. spread guard ----------------------------------------------------------------


def test_c05_spread_wider_than_instrument_limit_is_rejected(admission: EntryAdmission) -> None:
    wide = make_snapshot(bid=D("90"), ask=D("110"))
    _rejects(admission.admit(Scenario(snapshot=wide)), RiskReason.SPREAD_TOO_WIDE)


def test_c05_spread_guard_boundary_is_strictly_greater_than(admission: EntryAdmission) -> None:
    # Snapshot spread is exactly 200 bps (99/101 around mid 100).
    at_limit = admission.admit(Scenario(limits=make_limits(max_spread_bps=D("200"))))
    below_limit = admission.admit(Scenario(limits=make_limits(max_spread_bps=D("199"))))
    assert at_limit.approved is True
    _rejects(below_limit, RiskReason.SPREAD_TOO_WIDE)


# -- 6. stale data ------------------------------------------------------------------


def test_c06_stale_snapshot_is_rejected(admission: EntryAdmission) -> None:
    stale = make_snapshot(timestamp=NOW - timedelta(seconds=10))
    _rejects(admission.admit(Scenario(snapshot=stale)), RiskReason.DATA_STALE)


def test_c06_future_snapshot_is_rejected(admission: EntryAdmission) -> None:
    future = make_snapshot(timestamp=NOW + timedelta(seconds=10))
    _rejects(admission.admit(Scenario(snapshot=future)), RiskReason.DATA_STALE)


def test_c06_data_age_boundary_is_inclusive_of_the_limit(admission: EntryAdmission) -> None:
    exactly = make_snapshot(timestamp=NOW - timedelta(seconds=5))
    just_over = make_snapshot(timestamp=NOW - timedelta(seconds=5, microseconds=1))
    assert admission.admit(Scenario(snapshot=exactly)).approved is True
    _rejects(admission.admit(Scenario(snapshot=just_over)), RiskReason.DATA_STALE)


def test_c06_non_live_data_is_rejected(admission: EntryAdmission) -> None:
    from data.models import DataQuality

    delayed = make_snapshot(quality=DataQuality.DELAYED)
    _rejects(admission.admit(Scenario(snapshot=delayed)), RiskReason.DATA_NOT_LIVE)


# -- 7. stale signal ----------------------------------------------------------------


def test_c07_stale_signal_is_rejected(admission: EntryAdmission) -> None:
    stale = make_request(timestamp=NOW - timedelta(seconds=10))
    _rejects(admission.admit(Scenario(request=stale)), RiskReason.SIGNAL_STALE)


def test_c07_future_signal_is_rejected(admission: EntryAdmission) -> None:
    future = make_request(timestamp=NOW + timedelta(seconds=10))
    _rejects(admission.admit(Scenario(request=future)), RiskReason.SIGNAL_STALE)


def test_c07_signal_age_boundary(admission: EntryAdmission) -> None:
    exactly = make_request(timestamp=NOW - timedelta(seconds=5))
    just_over = make_request(timestamp=NOW - timedelta(seconds=5, microseconds=1))
    assert admission.admit(Scenario(request=exactly)).approved is True
    _rejects(admission.admit(Scenario(request=just_over)), RiskReason.SIGNAL_STALE)


# -- 8. account-known requirement ---------------------------------------------------


def test_c08_unknown_account_blocks_new_exposure(admission: EntryAdmission) -> None:
    _rejects(
        admission.admit(Scenario(account=make_account(known=False))),
        RiskReason.ACCOUNT_UNKNOWN,
    )


# -- 9. account-reconciled requirement ----------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        ReconciliationState.NOT_RECONCILED,
        ReconciliationState.RECONCILING,
        ReconciliationState.MISMATCH,
    ],
)
def test_c09_every_non_reconciled_state_blocks_new_exposure(
    admission: EntryAdmission, state: ReconciliationState
) -> None:
    outcome = admission.admit(Scenario(account=make_account(reconciliation=state)))
    _rejects(outcome, RiskReason.ACCOUNT_UNRECONCILED)


def test_c09_reconciled_account_is_admitted(admission: EntryAdmission) -> None:
    outcome = admission.admit(
        Scenario(account=make_account(reconciliation=ReconciliationState.RECONCILED))
    )
    assert outcome.approved is True


def test_c09_unknown_takes_precedence_over_unreconciled(admission: EntryAdmission) -> None:
    account = make_account(known=False, reconciliation=ReconciliationState.MISMATCH)
    _rejects(admission.admit(Scenario(account=account)), RiskReason.ACCOUNT_UNKNOWN)


# -- 10. daily-loss gate ------------------------------------------------------------


def test_c10_daily_loss_limit_rejects(admission: EntryAdmission) -> None:
    account = make_account(realized_pnl_today=D("-60"), unrealized_pnl=D("-50"))
    _rejects(admission.admit(Scenario(account=account)), RiskReason.DAILY_LOSS_LIMIT)


def test_c10_daily_loss_boundary_is_inclusive(admission: EntryAdmission) -> None:
    at_limit = make_account(realized_pnl_today=D("-100"))
    just_inside = make_account(realized_pnl_today=D("-99.99"))
    _rejects(admission.admit(Scenario(account=at_limit)), RiskReason.DAILY_LOSS_LIMIT)
    assert admission.admit(Scenario(account=just_inside)).approved is True


def test_c10_unrealized_loss_counts_toward_the_daily_limit(admission: EntryAdmission) -> None:
    account = make_account(realized_pnl_today=D("0"), unrealized_pnl=D("-100"))
    _rejects(admission.admit(Scenario(account=account)), RiskReason.DAILY_LOSS_LIMIT)


def test_c10_daily_pnl_window_must_be_a_single_trading_day(admission: EntryAdmission) -> None:
    # An all-time realized figure must not be smuggled in as "today".
    old_window = make_account(pnl_window_start=NOW - timedelta(days=3))
    _rejects(admission.admit(Scenario(account=old_window)), RiskReason.INVALID_INPUT)
    future_window = make_account(pnl_window_start=NOW + timedelta(hours=1))
    _rejects(admission.admit(Scenario(account=future_window)), RiskReason.INVALID_INPUT)


# -- 11. drawdown gate --------------------------------------------------------------


def test_c11_drawdown_limit_rejects(admission: EntryAdmission) -> None:
    account = make_account(equity=D("799"), peak_equity=D("1000"))
    _rejects(admission.admit(Scenario(account=account)), RiskReason.DRAWDOWN_LIMIT)


def test_c11_drawdown_boundary_is_inclusive(admission: EntryAdmission) -> None:
    at_limit = make_account(equity=D("800"), peak_equity=D("1000"))
    inside = make_account(equity=D("801"), peak_equity=D("1000"))
    _rejects(admission.admit(Scenario(account=at_limit)), RiskReason.DRAWDOWN_LIMIT)
    assert admission.admit(Scenario(account=inside)).approved is True


# -- 12. consecutive-loss limit and risk reduction ----------------------------------


def test_c12_consecutive_loss_limit_rejects(admission: EntryAdmission) -> None:
    account = make_account(consecutive_losses=3)
    _rejects(admission.admit(Scenario(account=account)), RiskReason.CONSECUTIVE_LOSS_LIMIT)


def test_c12_loss_streak_below_limit_reduces_risk(admission: EntryAdmission) -> None:
    baseline = admission.admit(Scenario())
    streak = admission.admit(Scenario(account=make_account(consecutive_losses=2)))

    assert baseline.reduce_risk is False
    assert streak.approved is True
    assert streak.reduce_risk is True
    assert streak.risk_budget == baseline.risk_budget / 2  # multiplier 0.5
    assert streak.quantity < baseline.quantity


def test_c12_drawdown_below_limit_reduces_risk(admission: EntryAdmission) -> None:
    # drawdown 100 >= 0.5 * max_drawdown(200) but < the 200 hard limit
    account = make_account(equity=D("900"), peak_equity=D("1000"))
    outcome = admission.admit(Scenario(account=account))
    assert outcome.approved is True
    assert outcome.reduce_risk is True
    assert outcome.risk_budget == D("900") * D("0.01") * D("0.5")


def test_c12_daily_loss_below_limit_reduces_risk(admission: EntryAdmission) -> None:
    account = make_account(realized_pnl_today=D("-60"))  # >= 50% of 100, < 100
    outcome = admission.admit(Scenario(account=account))
    assert outcome.approved is True
    assert outcome.reduce_risk is True


# -- 13. liquidity constraint -------------------------------------------------------


def test_c13_no_available_liquidity_rejects(admission: EntryAdmission) -> None:
    request = make_request(available_liquidity_notional=D("0"))
    _rejects(admission.admit(Scenario(request=request)), RiskReason.LIQUIDITY_INSUFFICIENT)


def test_c13_size_never_exceeds_the_liquidity_fraction(admission: EntryAdmission) -> None:
    request = make_request(available_liquidity_notional=D("500"))
    outcome = admission.admit(Scenario(request=request))

    assert outcome.approved is True
    assert outcome.binding_constraint == "liquidity"
    # liquidity_fraction 0.10 of 500 = 50 notional at most
    assert outcome.notional <= D("50")


# -- 14. gross / net exposure limits (incl. pending exposure) -----------------------


def test_c14_gross_limit_rejects_when_already_full(admission: EntryAdmission) -> None:
    account = make_account(gross_notional=D("10000"))
    _rejects(admission.admit(Scenario(account=account)), RiskReason.EXPOSURE_LIMIT)


def test_c14_net_limit_rejects_same_direction_only(admission: EntryAdmission) -> None:
    long_book = make_account(net_notional=D("5000"))
    _rejects(admission.admit(Scenario(account=long_book)), RiskReason.EXPOSURE_LIMIT)

    # A SELL against a fully-long net book REDUCES net exposure and is allowed.
    sell = make_request(side=RiskSide.SELL, entry_price=D("100"), stop_price=D("105"))
    assert admission.admit(Scenario(account=long_book, request=sell)).approved is True

    short_book = make_account(net_notional=D("-5000"))
    _rejects(admission.admit(Scenario(account=short_book, request=sell)), RiskReason.EXPOSURE_LIMIT)


def test_c14_pending_gross_exposure_counts_toward_the_limit(admission: EntryAdmission) -> None:
    _rejects(
        admission.admit(Scenario(pending_gross=D("10000"), pending_net=D("0"))),
        RiskReason.EXPOSURE_LIMIT,
    )


def test_c14_pending_net_exposure_counts_toward_the_limit(admission: EntryAdmission) -> None:
    _rejects(
        admission.admit(Scenario(pending_gross=D("5000"), pending_net=D("5000"))),
        RiskReason.EXPOSURE_LIMIT,
    )


def test_c14_size_is_capped_by_remaining_gross_capacity(admission: EntryAdmission) -> None:
    policy = make_policy(max_gross_notional=D("250"))
    scenario = Scenario(policy=policy, pending_gross=D("100"), pending_net=D("0"))
    outcome = admission.admit(scenario)

    assert outcome.approved is True
    assert scenario.account.gross_notional + scenario.pending_gross + outcome.notional <= D("250")
    assert outcome.binding_constraint == "gross_capacity"


def test_c14_size_is_capped_by_remaining_net_capacity(admission: EntryAdmission) -> None:
    policy = make_policy(max_net_notional=D("250"))
    account = make_account(net_notional=D("100"))
    outcome = admission.admit(Scenario(policy=policy, account=account))

    assert outcome.approved is True
    assert account.net_notional + outcome.notional <= D("250")
    assert outcome.binding_constraint == "net_capacity"
