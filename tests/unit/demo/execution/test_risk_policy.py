# ruff: noqa: E501
"""demo-discovery-policy-v1: pure risk gate (sizing, min-lot rule, cluster and loss limits)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from demo.contracts import TradeIntent
from demo.execution.risk_policy import (
    CLUSTERS,
    POLICY_ID,
    DemoRiskGate,
    GateAccount,
    MarketFacts,
    OpenRisk,
    SizedApproval,
    build_policy,
)
from risk.models import MAX_SYSTEM_LEVERAGE

NOW = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
DAY = datetime(2026, 10, 1, tzinfo=UTC)


def ger40(**kw) -> MarketFacts:
    base = dict(
        market="GER40", contract_size=D(1), volume_min=D("0.25"), volume_step=D("0.25"),
        volume_max=D(250), max_leverage=D(20), max_spread=D(8), fx=D(1),
    )
    base.update(kw)
    return MarketFacts(**base)


def account(equity="10000", **kw) -> GateAccount:
    eq = D(equity)
    base = dict(
        equity=eq, balance=eq, peak_equity=eq, start_of_day_equity=eq, realized_pnl_today=D(0),
        unrealized_pnl=D(0), consecutive_losses=0, gross_notional=D(0), net_notional=D(0),
        notionals={}, signed_units={}, account_leverage=D(30), day_start=DAY,
    )
    base.update(kw)
    return GateAccount(**base)


def intent(market="GER40", *, stop=24950.0, entry=25000.0, rf=0.01, direction=1, **kw):
    data = dict(
        opportunity_id="opp-1", phase="DISCOVERY", intent_id="i-1", market=market,
        broker_symbol="Ger40", direction=direction, entry_ref=entry, stop=stop, target=None,
        min_space_r=1.5, valid_until_utc=(NOW + timedelta(minutes=1)).isoformat(),
        forced_flat_utc=None, risk_fraction=rf,
    )
    data.update(kw)
    return TradeIntent(**data)


def size(acct=None, mkt=None, itn=None, bid="25000", ask="25001"):
    return DemoRiskGate().size(
        intent=itn or intent(), market=mkt or ger40(), account=acct or account(),
        bid=D(bid), ask=D(ask), quote_time=NOW, now=NOW,
    )


def test_one_percent_risk_sizing_on_the_broker_step_and_policy_values():
    result = size()
    assert isinstance(result, SizedApproval)
    assert result.policy_id == POLICY_ID == "demo-discovery-policy-v1"
    assert result.quantity % D("0.25") == 0
    assert D("0.009") < result.risk_fraction <= D("0.01")
    assert result.risk_budget == D("100.00")
    assert result.leverage < D("20") and not result.min_lot_override
    policy = build_policy(account())
    assert policy.max_leverage == MAX_SYSTEM_LEVERAGE == D(30)
    assert policy.max_daily_loss == D("600.00")  # 6 % of start-of-day equity
    assert policy.max_drawdown == D("2500.00")  # 25 % of peak equity
    assert policy.max_consecutive_losses == 8


def test_min_lot_is_used_only_if_risk_at_most_two_percent():
    # equity 500: 1 % = 5 EUR -> below 0.25 lot; min lot with a tight 20-point stop risks ~1.9 %
    ok = size(account("500"), itn=intent(stop=24981.0))
    assert isinstance(ok, SizedApproval) and ok.min_lot_override
    assert ok.quantity == D("0.25")
    assert D("0.01") < ok.risk_fraction <= D("0.02")
    assert ok.risk_budget == ok.risk_fraction * D(500)  # actual risk recorded, not the 1 % budget


def test_size_below_min_when_min_lot_risk_exceeds_two_percent():
    assert size(account("500"), itn=intent(stop=24950.0)) == "size_below_min"


def test_min_lot_never_bypasses_the_leverage_cap():
    # min lot 0.25 x 25000 = 6250 notional vs equity 150 -> 41x > 30x hard ceiling
    assert size(account("150"), itn=intent(stop=24995.0)) == "size_below_min"


def test_leverage_is_capped_at_the_hard_ceiling_never_a_target():
    result = size(account("1000"), itn=intent(stop=24990.0), mkt=ger40(max_leverage=D(30)))
    assert isinstance(result, SizedApproval)
    assert result.leverage <= D(30)


def test_risk_fraction_above_policy_cap_is_rejected():
    assert size(itn=intent(rf=0.02)) == "risk_fraction_above_cap"


def test_daily_loss_stop_is_six_percent_of_start_of_day_equity():
    ok = size(account(realized_pnl_today=D(-500)))
    assert isinstance(ok, SizedApproval)
    assert size(account(realized_pnl_today=D(-600))) == "daily_loss_limit"
    # floating loss counts as well
    assert size(account(realized_pnl_today=D(-300), unrealized_pnl=D(-300))) == "daily_loss_limit"


def test_max_drawdown_halt_is_25_percent_of_peak():
    assert isinstance(size(account("8000", peak_equity=D(10000))), SizedApproval)
    assert size(account("7400", peak_equity=D(10000))) == "drawdown_limit"


def test_eight_consecutive_losses_halt():
    assert isinstance(size(account(consecutive_losses=7)), SizedApproval)
    assert size(account(consecutive_losses=8)) == "consecutive_loss_limit"


def test_index_cluster_is_one_cluster_not_independent_positions():
    assert CLUSTERS["GER40"] == CLUSTERS["NAS100"] == CLUSTERS["SPX500"] == "INDEX"
    assert CLUSTERS["XAUUSD"] == "METAL" and CLUSTERS["EURUSD"] == "FX"
    two_open = tuple(OpenRisk(market=m, cluster="INDEX", risk_money=D(125)) for m in ("NAS100", "SPX500"))
    # 250 EUR open (2.5 %) + ~1 % new > 3 % cluster cap, though far below the 4 % total cap
    assert size(account(open_risks=two_open)) == "cluster_risk_limit"
    # the same open risk does not block another cluster
    metal = size(
        account(open_risks=two_open),
        mkt=MarketFacts(
            market="XAUUSD", contract_size=D(100), volume_min=D("0.01"), volume_step=D("0.01"),
            volume_max=D(50), max_leverage=D(10), max_spread=D("0.47"), fx=D("0.85"),
        ),
        itn=intent("XAUUSD", stop=4150.0, entry=4170.0),
        bid="4169.5", ask="4169.97",
    )
    assert isinstance(metal, SizedApproval)


def test_total_open_risk_cap_is_four_percent():
    opens = (
        OpenRisk(market="NAS100", cluster="INDEX", risk_money=D(150)),
        OpenRisk(market="XAUUSD", cluster="METAL", risk_money=D(150)),
        OpenRisk(market="EURUSD", cluster="FX", risk_money=D(60)),
    )
    # 360 open + ~100 new > 400 total cap, while every single cluster is fine
    assert size(account(open_risks=opens)) == "total_open_risk_limit"


def test_money_model_gold_and_eurusd_use_contract_size_and_fx():
    gold = MarketFacts(
        market="XAUUSD", contract_size=D(100), volume_min=D("0.01"), volume_step=D("0.01"),
        volume_max=D(50), max_leverage=D(10), max_spread=D("0.47"), fx=D("0.85"),
    )
    result = size(
        itn=intent("XAUUSD", stop=4150.0, entry=4170.0), mkt=gold, bid="4169.5", ask="4169.97"
    )
    assert isinstance(result, SizedApproval)
    # stop risk in EUR = lots x 100 oz x 19.97 USD x 0.85 EUR/USD must be within 1 % of 10000
    assert result.stop_risk_money == result.quantity * 100 * D("19.97") * D("0.85")
    assert result.stop_risk_money <= D(100)

    fx = MarketFacts(
        market="EURUSD", contract_size=D(100000), volume_min=D("0.01"), volume_step=D("0.01"),
        volume_max=D(50), max_leverage=D(30), max_spread=D("0.00053"), fx=D(1) / D("1.17"),
    )
    r2 = size(
        itn=intent("EURUSD", stop=1.1650, entry=1.1700), mkt=fx, bid="1.16995", ask="1.17000"
    )
    assert isinstance(r2, SizedApproval)
    assert r2.stop_risk_money <= D(100) and r2.quantity >= D("0.01")


def test_every_reject_has_a_machine_reason_code():
    for acct in (
        account(consecutive_losses=8),
        account("7400", peak_equity=D(10000)),
        account(realized_pnl_today=D(-700)),
    ):
        reason = size(acct)
        assert isinstance(reason, str) and reason == reason.lower() and " " not in reason


def test_unknown_cluster_is_rejected():
    assert size(mkt=ger40(market="FOO"), itn=intent("FOO")) == "unknown_cluster"


@pytest.mark.parametrize("bad", [D(0), D(-1)])
def test_non_positive_equity_rejected(bad):
    assert size(replace(account(), equity=bad)) == "equity_non_positive"
