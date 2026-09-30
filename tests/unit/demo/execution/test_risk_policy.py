# ruff: noqa: E501
"""demo-discovery-policy-v1 gate: evaluator safety gates + DemoPositionSizer, full risk detail."""

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
    RiskOutcome,
    build_policy,
)
from demo.execution.sizing import RiskCaps
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


def size(acct=None, mkt=None, itn=None, bid="25000", ask="25001", gate=None, **kw) -> RiskOutcome:
    return (gate or DemoRiskGate()).size(
        intent=itn or intent(), market=mkt or ger40(), account=acct or account(),
        bid=D(bid), ask=D(ask), quote_time=NOW, now=NOW, **kw,
    )


def test_policy_values_are_configurable_caps_not_rules():
    policy = build_policy(account(), RiskCaps())
    assert policy.policy_id == POLICY_ID == "demo-discovery-policy-v1"
    assert policy.max_leverage == MAX_SYSTEM_LEVERAGE == D(30)
    assert policy.max_daily_loss == D("600.00") and policy.max_drawdown == D("2500.00")
    assert policy.max_consecutive_losses == 8
    tweaked = build_policy(account(), RiskCaps(max_daily_loss_fraction=D("0.03")))
    assert tweaked.max_daily_loss == D("300.00")


def test_approval_carries_the_actual_risk_and_the_full_detail():
    out = size(family="BREAKOUT", atr=D(20))
    assert out.reason is None and out.approval is not None
    a = out.approval
    assert a.quantity % D("0.25") == 0 and a.risk_fraction <= D("0.01")
    assert a.risk_budget == D(100) and a.leverage < 20 and not a.min_lot_used
    d = out.detail
    assert d["decision"] == "TRADE" and d["policy_id"] == POLICY_ID
    assert d["structural_stop"] == D("24950.0") and d["stop_distance"] == D("51")
    assert d["family"] == "BREAKOUT" and d["stop_distance_atr"] == D("51") / 20
    assert d["stop_risk_eur"] == a.stop_risk_money == a.quantity * D(51)


def test_min_lot_is_accepted_with_whatever_actual_risk_it_has():
    out = size(account("500"))
    assert out.approval is not None and out.approval.min_lot_used
    assert out.approval.quantity == D("0.25")
    assert D("0.02") < out.approval.risk_fraction < D("0.05")
    assert out.approval.risk_budget == D("5.00")  # the TARGET budget; the actual risk is larger


def test_min_lot_over_a_hard_cap_is_size_below_min_naming_the_cap():
    out = size(account("500"), itn=intent(stop=24900.0))
    assert out.reason == "size_below_min" and out.approval is None
    assert out.detail["violated_cap"] == "max_position_stop_risk_fraction"
    assert out.detail["gate_reject_class"] == "SAFETY" and out.detail["decision"] == "SKIP"
    relaxed = size(account("500"), itn=intent(stop=24900.0),
                   gate=DemoRiskGate(caps=RiskCaps(max_position_stop_risk_fraction=D("0.10"))))
    assert relaxed.approval is not None  # the cap is configuration, not a universal rule


def test_daily_loss_drawdown_and_consecutive_loss_halts_remain():
    assert size(account(realized_pnl_today=D(-500))).approval is not None
    assert size(account(realized_pnl_today=D(-600))).reason == "daily_loss_limit"
    assert size(account(realized_pnl_today=D(-300), unrealized_pnl=D(-300))).reason == "daily_loss_limit"
    assert size(account("8000", peak_equity=D(10000))).approval is not None
    assert size(account("7400", peak_equity=D(10000))).reason == "drawdown_limit"
    assert size(account(consecutive_losses=7)).approval is not None
    assert size(account(consecutive_losses=8)).reason == "consecutive_loss_limit"
    out = size(account(realized_pnl_today=D(-700)))
    assert out.detail["realized_pnl_today"] == D(-700) and out.reason == "daily_loss_limit"


def test_halt_thresholds_come_from_the_caps_dataclass():
    gate = DemoRiskGate(caps=RiskCaps(max_consecutive_losses=3, max_daily_loss_fraction=D("0.02")))
    assert size(account(consecutive_losses=3), gate=gate).reason == "consecutive_loss_limit"
    assert size(account(realized_pnl_today=D(-250)), gate=gate).reason == "daily_loss_limit"


def test_cluster_open_risk_is_summed_across_nasdaq_spx_and_dax():
    assert CLUSTERS["GER40"] == CLUSTERS["NAS100"] == CLUSTERS["SPX500"] == "INDEX"
    two_open = tuple(
        OpenRisk(market=m, cluster="INDEX", risk_money=D(85), family="F") for m in ("NAS100", "SPX500")
    )
    gate = DemoRiskGate(caps=RiskCaps(max_cluster_stop_risk_fraction=D("0.018")))
    out = size(account(open_risks=two_open), gate=gate)
    assert out.reason == "size_below_min" and out.detail["violated_cap"] == "max_cluster_stop_risk_fraction"
    assert out.detail["cluster_risk_before"] == 170 and out.detail["cluster"] == "INDEX"
    metal = size(
        account(open_risks=two_open),
        mkt=MarketFacts(market="XAUUSD", contract_size=D(100), volume_min=D("0.01"), volume_step=D("0.01"),
                        volume_max=D(50), max_leverage=D(10), max_spread=D("0.47"), fx=D("0.85")),
        itn=intent("XAUUSD", stop=4150.0, entry=4170.0), bid="4169.5", ask="4169.97", gate=gate,
    )
    assert metal.approval is not None and metal.detail["cluster_risk_before"] == 0


def test_money_model_gold_and_eurusd_use_contract_size_and_fx():
    gold = MarketFacts(market="XAUUSD", contract_size=D(100), volume_min=D("0.01"), volume_step=D("0.01"),
                       volume_max=D(50), max_leverage=D(10), max_spread=D("0.47"), fx=D("0.85"))
    out = size(itn=intent("XAUUSD", stop=4150.0, entry=4170.0), mkt=gold, bid="4169.5", ask="4169.97")
    assert out.approval is not None
    assert out.approval.stop_risk_money == out.approval.quantity * 100 * D("19.97") * D("0.85")
    assert out.approval.stop_risk_money <= D(100)
    fx = MarketFacts(market="EURUSD", contract_size=D(100000), volume_min=D("0.01"), volume_step=D("0.01"),
                     volume_max=D(50), max_leverage=D(30), max_spread=D("0.00053"), fx=D(1) / D("1.17"))
    r2 = size(itn=intent("EURUSD", stop=1.1650, entry=1.1700), mkt=fx, bid="1.16995", ask="1.17000")
    assert r2.approval is not None and r2.approval.stop_risk_money <= D(100)


def test_every_reject_has_a_machine_code_and_a_class():
    for acct in (account(consecutive_losses=8), account("7400", peak_equity=D(10000)),
                 account(realized_pnl_today=D(-700))):
        out = size(acct)
        assert out.reason == out.reason.lower() and " " not in out.reason
        assert out.detail["reject_code"] == out.reason and out.detail["gate_reject_class"] == "SAFETY"


def test_unknown_cluster_and_non_positive_equity_are_rejected():
    assert size(mkt=ger40(market="FOO"), itn=intent("FOO")).reason == "unknown_cluster"
    assert size(replace(account(), equity=D(0))).reason == "equity_non_positive"


@pytest.mark.parametrize("target", [0.002, 0.01, 0.03])
def test_target_risk_fraction_is_only_an_input(target):
    out = size(itn=intent(rf=target))
    assert out.approval is not None
    assert out.approval.risk_fraction <= D(str(target))
    assert out.detail["target_risk_fraction"] == D(str(target))


# ---- fitted => approved: the sizer must fit the EVALUATOR's liquidation geometry (reference price) ----------

MARGIN_REJECTS = {"margin_stop_too_close_to_liquidation", "margin_stop_beyond_liquidation", "sizer_evaluator_mismatch"}


def spx500(**kw) -> MarketFacts:
    base = dict(
        market="SPX500", contract_size=D(1), volume_min=D("0.5"), volume_step=D("0.5"),
        volume_max=D(1000), max_leverage=D(20), max_spread=D(50), fx=D(1),
    )
    base.update(kw)
    return MarketFacts(**base)


def spx_intent(direction, stop, rf="0.05"):
    return intent("SPX500", stop=float(stop), entry=7693.64, rf=float(rf), direction=direction, broker_symbol="SPX500")


def test_spx500_short_liquidation_capped_size_is_fitted_to_the_evaluator_reference_price():
    # live regression: stop 7702.745 vs bid 7693.64, mm = 1/20. The evaluator measures the distance from
    # R = bid x (1 - 5 bps); a sizer that measured it from the bid chose 110.5 lots and was then rejected.
    out = size(account("55946"), spx500(), spx_intent(-1, "7702.745"), bid="7693.64", ask="7694.14")
    assert out.reason is None and out.approval is not None
    assert out.approval.quantity == D("109.0")
    assert out.detail["binding_cap"] == "liquidation_safe_leverage"
    assert out.detail["liquidation_reference_price"] == D("7693.64") * (1 - D("0.0005"))


def test_spx500_long_symmetric_case_is_fitted_and_approved():
    out = size(account("55946"), spx500(), spx_intent(1, "7684.535"), bid="7693.14", ask="7693.64")
    assert out.reason is None and out.approval is not None
    assert out.detail["binding_cap"] == "liquidation_safe_leverage"
    assert out.detail["liquidation_reference_price"] == D("7693.64") * (1 + D("0.0005"))


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("price", ["7693.64", "25000", "4170.5", "1.1725"])
@pytest.mark.parametrize("stop_bps", [12, 25, 60, 100, 150, 400])
@pytest.mark.parametrize("leverage", [10, 20, 30])
@pytest.mark.parametrize("equity", ["9000", "100000", "333333"])
@pytest.mark.parametrize("gross_x", ["0", "3"])
def test_property_fitted_quantity_is_never_rejected_by_the_evaluator_margin_check(
    direction, price, stop_bps, leverage, equity, gross_x
):
    p = D(price)
    bid, ask = (p, p + p / 20000) if direction == -1 else (p - p / 20000, p)
    executable = bid if direction == -1 else ask
    stop = executable - direction * executable * D(stop_bps) / D(10000)
    mkt = spx500(max_leverage=D(leverage), volume_min=D("0.01"), volume_step=D("0.01"), max_spread=p)
    gross = D(equity) * D(gross_x)
    acct = account(equity, gross_notional=gross, net_notional=gross)
    itn = intent("SPX500", stop=float(stop), entry=float(executable), rf=0.05, direction=direction, broker_symbol="SPX500")
    out = size(acct, mkt, itn, bid=str(bid), ask=str(ask))
    assert out.reason not in MARGIN_REJECTS, (out.reason, out.detail.get("liquidation_safe_leverage"))


def test_a_residual_evaluator_margin_rejection_after_sizing_is_the_distinct_mismatch_code(monkeypatch):
    # simulate the OLD bug (sizer measuring from the executable price): the evaluator then rejects a
    # fitted quantity - that must surface as sizer_evaluator_mismatch, not as a plain margin reject.
    import demo.execution.risk_policy as rp

    real = rp.DemoPositionSizer.size

    def old_geometry(self, inp):
        return real(self, replace(inp, liquidation_reference_price=None))

    monkeypatch.setattr(rp.DemoPositionSizer, "size", old_geometry)
    out = size(account("55946"), spx500(), spx_intent(-1, "7702.745"), bid="7693.64", ask="7694.14")
    assert out.reason == "sizer_evaluator_mismatch" and out.approval is None
    assert out.detail["evaluator_reject_reason"] == "margin_stop_too_close_to_liquidation"
    assert out.detail["gate_reject_class"] == "SAFETY"


def test_stopout_fraction_is_passed_to_fit_and_evaluator_alike():
    base = size(account("55946"), spx500(), spx_intent(-1, "7702.745"), bid="7693.64", ask="7694.14")
    half = size(account("55946"), spx500(), spx_intent(-1, "7702.745"), bid="7693.64", ask="7694.14",
                gate=DemoRiskGate(stopout_fraction_of_initial_margin=D("0.5")))
    assert half.approval is not None and base.approval is not None
    assert half.detail["liquidation_safe_leverage"] > base.detail["liquidation_safe_leverage"]
    assert half.approval.quantity > base.approval.quantity  # fitted leverage raised accordingly, still approved


def test_binding_constraint_is_target_risk_when_no_cap_bound():
    out = size()
    assert out.approval is not None and out.detail["binding_cap"] is None
    assert out.approval.binding_constraint == "target_risk" and out.detail["tightest_cap"]
