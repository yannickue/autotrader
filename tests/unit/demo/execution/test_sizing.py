# ruff: noqa: E501
"""Pure DemoPositionSizer: structural stop, actual risk, hard caps, no quality dependence."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal as D

import pytest

from demo.execution.sizing import (
    DemoPositionSizer,
    PortfolioRisk,
    RiskCaps,
    SizingInput,
    floor_to_step,
)


def ger(**kw) -> SizingInput:
    base = dict(
        market="GER40", cluster="INDEX", family="FAM", direction=1,
        executable_price=D("25001.5"), structural_stop=D("24950"),
        contract_size=D(1), volume_min=D("0.25"), volume_step=D("0.25"), volume_max=D(250),
        fx=D(1), equity=D(10_000), target_risk_fraction=D("0.01"),
        instrument_max_leverage=D(20), account_leverage=D(30),
    )
    base.update(kw)
    return SizingInput(**base)


def gold(**kw) -> SizingInput:
    base = dict(
        market="XAUUSD", cluster="METAL", family="FAM", direction=1,
        executable_price=D("4169.97"), structural_stop=D("4150"),
        contract_size=D(100), volume_min=D("0.01"), volume_step=D("0.01"), volume_max=D(50),
        fx=D(1) / D("1.17"), equity=D(10_000), target_risk_fraction=D("0.01"),
        instrument_max_leverage=D(10), account_leverage=D(30),
    )
    base.update(kw)
    return SizingInput(**base)


sizer = DemoPositionSizer()


def test_floor_to_step_is_anchored_at_the_minimum_lot():
    assert floor_to_step(D("1.0"), D("0.2"), D("0.2")) == D("1.0")
    assert floor_to_step(D("0.99"), D("0.2"), D("0.2")) == D("0.8")
    assert floor_to_step(D("0.19"), D("0.2"), D("0.2")) == 0
    assert floor_to_step(D("1.9"), D("0.5"), D("0.5")) == D("1.5")


def test_target_size_at_the_structural_stop_rounded_down_to_the_step():
    result = sizer.size(ger())
    assert result.accepted and result.reason is None
    d = result.detail
    assert d["loss_per_lot_at_stop"] == D("51.5")
    assert d["desired_quantity"] == D(100) / D("51.5")
    assert result.quantity == D("1.75") and result.quantity % D("0.25") == 0
    assert d["stop_risk_eur"] == D("1.75") * D("51.5")
    assert d["equity_risk_fraction"] == d["stop_risk_eur"] / D(10_000) <= D("0.01")
    assert d["structural_stop"] == D("24950") and d["min_lot_used"] is False


def test_the_structural_stop_is_never_an_output_or_a_knob():
    a = sizer.size(ger(target_risk_fraction=D("0.01")))
    b = sizer.size(ger(target_risk_fraction=D("0.03")))
    c = sizer.size(ger(equity=D(500)))
    for r in (a, b, c):
        assert r.detail["structural_stop"] == D("24950")  # never tightened / widened to hit a %
        assert r.detail["stop_distance"] == D("51.5")
    assert b.quantity > a.quantity  # the QUANTITY moves, the stop does not
    assert "stop" not in {f.name for f in fields(type(a))}


@pytest.mark.parametrize(
    ("equity", "risk_pct"),
    [(D("500"), D("0.0258")), (D("400"), D("0.0322")), (D("320"), D("0.0402"))],
)
def test_min_lot_with_more_than_one_percent_actual_risk_is_not_invalid_by_itself(equity, risk_pct):
    result = sizer.size(ger(equity=equity))
    assert result.accepted and result.quantity == D("0.25")
    d = result.detail
    assert d["min_lot_used"] is True and d["stop_risk_eur"] == D("0.25") * D("51.5")
    assert abs(d["equity_risk_fraction"] - risk_pct) < D("0.0001")  # 2.6 %, 3.2 %, 4.0 % all fine


def test_actual_min_lot_risk_uses_contract_size_and_fx_for_gold():
    result = sizer.size(gold(equity=D(500)))
    assert result.accepted and result.quantity == D("0.01")
    distance = D("4169.97") - D("4150")
    expected = D("0.01") * 100 * distance * (D(1) / D("1.17"))
    assert result.detail["stop_risk_eur"] == expected
    assert abs(result.detail["equity_risk_fraction"] - expected / D(500)) == 0
    # cross-check with the tick model: EUR per tick per lot = contract x tick_size x fx
    tick_size, tick_value_eur = D("0.01"), D(100) * D("0.01") * (D(1) / D("1.17"))
    assert result.detail["loss_per_lot_at_stop"] == distance / tick_size * tick_value_eur


def test_eurusd_money_model_contract_100000_quoted_in_usd():
    result = sizer.size(
        SizingInput(
            market="EURUSD", cluster="FX", family="FAM", direction=1,
            executable_price=D("1.17005"), structural_stop=D("1.1650"),
            contract_size=D(100_000), volume_min=D("0.01"), volume_step=D("0.01"), volume_max=D(50),
            fx=D(1) / D("1.17"), equity=D(10_000), target_risk_fraction=D("0.01"),
            instrument_max_leverage=D(30), account_leverage=D(30),
        )
    )
    assert result.accepted
    loss_per_lot = D(100_000) * D("0.00505") / D("1.17")
    assert result.detail["loss_per_lot_at_stop"] == loss_per_lot
    assert result.quantity == floor_to_step(D(100) / loss_per_lot, D("0.01"), D("0.01"))


def test_hard_cap_violation_at_the_min_lot_is_rejected_and_names_the_cap():
    caps = RiskCaps(max_position_stop_risk_fraction=D("0.02"))
    result = sizer.size(ger(equity=D(500), caps=caps))  # min lot risks 2.57 % > 2 %
    assert not result.accepted and result.reason == "size_below_min"
    d = result.detail
    assert d["violated_cap"] == "max_position_stop_risk_fraction"
    assert d["cap_limit"] == D("0.02") and d["observed_at_min_lot"] > D("0.02")
    assert d["decision"] == "SKIP" and d["quantity_at_min_lot"] == D("0.25")
    assert "0.25" in d["message"] and "max_position_stop_risk_fraction" in d["message"]


def test_portfolio_and_cluster_before_after_including_the_index_cluster():
    pf = PortfolioRisk(
        total=D(250),
        by_market={"NAS100": D(120), "SPX500": D(130)},
        by_cluster={"INDEX": D(250)},  # NASDAQ + SPX are ONE cluster; DAX joins it
        by_family={"FAM": D(250)},
        gross_notional=D(60_000),
    )
    result = sizer.size(ger(portfolio=pf))
    d = result.detail
    risk = d["stop_risk_eur"]
    assert d["portfolio_risk_before"] == 250 and d["portfolio_risk_after"] == 250 + risk
    assert d["cluster_risk_before"] == 250 and d["cluster_risk_after"] == 250 + risk
    assert d["family_risk_before"] == 250 and d["family_risk_after"] == 250 + risk
    assert d["market_risk_before"] == 0 and d["market_risk_after"] == risk
    assert d["concentration_before"]["cluster"] == 1
    assert d["concentration_after"]["cluster"] == (250 + risk) / (250 + risk) == 1
    assert d["concentration_after"]["market"] == risk / (250 + risk)
    assert d["portfolio_leverage_after"] == (D(60_000) + d["notional_eur"]) / D(10_000)


def test_cluster_cap_fits_the_quantity_then_rejects_when_the_min_lot_no_longer_fits():
    caps = RiskCaps(max_cluster_stop_risk_fraction=D("0.02"))
    partial = sizer.size(ger(caps=caps, portfolio=PortfolioRisk(total=D(150), by_cluster={"INDEX": D(150)})))
    assert partial.accepted and partial.detail["quantity_reduced_by_cap"] is True
    assert partial.detail["cluster_risk_after"] <= D(200)  # 2 % of 10 000
    assert partial.detail["binding_cap"] == "max_cluster_stop_risk_fraction"
    full = sizer.size(ger(caps=caps, portfolio=PortfolioRisk(total=D(195), by_cluster={"INDEX": D(195)})))
    assert not full.accepted and full.detail["violated_cap"] == "max_cluster_stop_risk_fraction"


def test_aggregate_cap_and_family_share_cap():
    agg = RiskCaps(max_aggregate_open_stop_risk_fraction=D("0.03"))
    blocked = sizer.size(ger(caps=agg, portfolio=PortfolioRisk(total=D(295))))
    assert not blocked.accepted and blocked.detail["violated_cap"] == "max_aggregate_open_stop_risk_fraction"
    fam = RiskCaps(max_family_share_of_open_risk=D("0.30"))  # 30 % of the 10 % budget = 3 % of equity
    blocked = sizer.size(ger(caps=fam, portfolio=PortfolioRisk(total=D(300), by_family={"FAM": D(300)})))
    assert not blocked.accepted and blocked.detail["violated_cap"] == "max_family_share_of_open_risk"
    other = sizer.size(ger(caps=fam, family="OTHER", portfolio=PortfolioRisk(total=D(300), by_family={"FAM": D(300)})))
    assert other.accepted
    unknown = sizer.size(ger(caps=fam, family="UNKNOWN", portfolio=PortfolioRisk(total=D(300), by_family={"UNKNOWN": D(300)})))
    assert unknown.accepted  # an unattributed family is logged, not lumped into one capped bucket


@pytest.mark.parametrize("equity", [D(300), D(1_000), D(10_000), D(250_000)])
@pytest.mark.parametrize("stop", [D("25000.5"), D("24999"), D("24950"), D("24000"), D("20000")])
@pytest.mark.parametrize("target", [D("0.005"), D("0.01"), D("0.05")])
def test_leverage_never_exceeds_thirty_whatever_the_inputs(equity, stop, target):
    result = sizer.size(
        ger(equity=equity, structural_stop=stop, target_risk_fraction=target,
            instrument_max_leverage=D(30), maintenance_margin_rate=D("0.0333"))
    )
    if result.accepted:
        assert result.detail["leverage"] <= 30
        assert result.detail["portfolio_leverage_after"] <= 30
        assert result.quantity >= D("0.25")
    else:
        assert result.reason in {"size_below_min", "invalid_stop"}
        if result.reason == "size_below_min":
            assert result.detail["violated_cap"]


def test_margin_must_fit_free_margin_and_the_liquidation_safety_leverage():
    tight = sizer.size(ger(free_margin=D(1_000), margin_per_lot=D(1_250)))
    assert tight.accepted
    assert tight.detail["margin_required"] <= D(1_000) * D("0.9")
    assert tight.detail["binding_cap"] == "max_margin_fraction_of_free_margin"
    none_left = sizer.size(ger(free_margin=D(100), margin_per_lot=D(1_250)))
    assert not none_left.accepted and none_left.detail["violated_cap"] == "max_margin_fraction_of_free_margin"
    liq = sizer.size(ger(structural_stop=D("24999"), maintenance_margin_rate=D("0.05")))
    assert liq.detail["liquidation_safe_leverage"] < D(20)
    assert liq.detail["portfolio_leverage_after"] <= liq.detail["liquidation_safe_leverage"]


def test_sizing_is_independent_of_signal_quality_and_only_logs_volatility_and_spread():
    names = {f.name for f in fields(SizingInput)}
    assert not names & {"confidence", "confluence", "family_score", "quality", "win_probability", "signal"}
    base = sizer.size(ger())
    varied = sizer.size(ger(atr=D("500"), spread=D("9")))
    assert varied.quantity == base.quantity
    assert varied.detail["stop_distance_atr"] == D("51.5") / D(500)
    assert varied.detail["spread_to_stop"] == D(9) / D("51.5")
    assert varied.detail["sizing_independent_of_signal_quality"] is True


def test_risk_budget_multiplier_is_the_only_explicit_scaling_hook():
    one = sizer.size(ger())
    half = sizer.size(ger(risk_budget_multiplier=D("0.5")))
    assert half.quantity < one.quantity and half.detail["risk_budget_multiplier"] == D("0.5")


def test_caps_are_one_frozen_config_and_leverage_cannot_exceed_thirty():
    caps = RiskCaps()
    with pytest.raises(FrozenInstanceError):
        caps.max_leverage = D(40)  # type: ignore[misc]
    with pytest.raises(ValueError, match="max_leverage"):
        RiskCaps(max_leverage=D(31))
    with pytest.raises(ValueError):
        RiskCaps(max_position_stop_risk_fraction=D(0))
    assert caps.max_leverage == 30
    assert replace(caps, max_cluster_stop_risk_fraction=D("0.03")).max_cluster_stop_risk_fraction == D("0.03")
    assert set(caps.as_dict()) >= {
        "target_risk_fraction", "max_position_stop_risk_fraction",
        "max_aggregate_open_stop_risk_fraction", "max_cluster_stop_risk_fraction",
        "max_family_share_of_open_risk", "max_leverage", "max_daily_loss_fraction",
        "max_drawdown_fraction",
    }


def test_wrong_side_stop_and_bad_facts_are_rejected_with_machine_codes():
    assert sizer.size(ger(structural_stop=D("25100"))).reason == "invalid_stop"
    assert sizer.size(ger(equity=D(0))).reason == "equity_non_positive"
    assert sizer.size(ger(contract_size=D(0))).reason == "invalid_market_facts"
    assert sizer.size(ger(target_risk_fraction=D(0))).reason == "risk_fraction_invalid"


def test_binding_cap_only_reported_when_the_cap_actually_bound_the_size():
    free = sizer.size(ger())  # target 1 % is far below every cap
    assert free.detail["binding_cap"] is None and free.detail["tightest_cap"]
    assert not free.detail["quantity_reduced_by_cap"]
    capped = sizer.size(ger(free_margin=D(1_000), margin_per_lot=D(1_250)))
    assert capped.detail["binding_cap"] == capped.detail["tightest_cap"] == "max_margin_fraction_of_free_margin"


def test_liquidation_fit_uses_the_reference_price_in_the_protective_direction():
    short = dict(direction=-1, executable_price=D("7693.64"), structural_stop=D("7702.745"),
                 maintenance_margin_rate=D("0.05"), instrument_max_leverage=D(20))
    plain = sizer.size(ger(**short))
    ref = D("7693.64") * (1 - D("0.0005"))
    shifted = sizer.size(ger(**short, liquidation_reference_price=ref))
    ratio = (D("7702.745") - ref) / ref
    assert shifted.detail["liquidation_safe_leverage"] == 1 / (D("0.05") + D("150.1") / 10000 + ratio)
    assert shifted.detail["liquidation_safe_leverage"] < plain.detail["liquidation_safe_leverage"]
    buy = sizer.size(ger(executable_price=D("100"), structural_stop=D("99"), maintenance_margin_rate=D("0.05"),
                         liquidation_reference_price=D("100.05")))
    assert buy.detail["liquidation_safe_leverage"] == 1 / (D("0.05") + D("150.1") / 10000 + D("1.05") / D("100.05"))
