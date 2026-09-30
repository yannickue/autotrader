# ruff: noqa: E501
"""Risk semantics in the live path: structural stop, actual risk, hard caps, netting, TCA, logging."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from demo.execution.events import Accepted, Fill, PositionClosed, Rejected
from demo.execution.sizing import RiskCaps
from tests.unit.demo.execution.stack_harness import (
    FAST,
    build_broker,
    make_intent,
    make_stack,
)


@pytest.fixture
def env(tmp_path):
    broker = build_broker()
    stack = make_stack(broker, tmp_path)
    yield broker, stack
    stack.stop()


def kinds(events):
    return [type(e).__name__ for e in events]


def reason(events):
    return next(e.reason for e in events if isinstance(e, Rejected))


def with_caps(tmp_path, broker=None, **caps):
    broker = broker or build_broker()
    stack = make_stack(broker, tmp_path, config=replace(FAST, risk_caps=RiskCaps(**caps)))
    return broker, stack


# -- structural stop / actual risk --------------------------------------------------------------


@pytest.mark.parametrize("balance", [10_000.0, 2_000.0, 500.0])
def test_structural_stop_reaches_the_broker_unchanged_whatever_the_size(tmp_path, balance):
    broker = build_broker(balance=balance)
    stack = make_stack(broker, tmp_path)
    try:
        stack.start()
        intent = make_intent(stop=24950.0, target=25150.0)
        events = stack.submit(intent)
        assert kinds(events) == ["Accepted", "Fill", "ProtectionConfirmed"], events
        assert broker.request_log[0]["sl"] == intent.stop == 24950.0
        assert broker.request_log[0]["tp"] == intent.target
        assert broker.positions_get()[0].sl == 24950.0
        assert events[0].risk_detail["structural_stop"] == Decimal("24950.0")
    finally:
        stack.stop()


def test_actual_risk_is_computed_from_the_real_stop_distance_and_contract_size(env):
    broker, stack = env
    stack.start()
    events = stack.submit(
        make_intent(market="XAUUSD", broker_symbol="GOLD", entry_ref=4170.0, stop=4150.0, target=4215.0)
    )
    accepted = events[0]
    assert isinstance(accepted, Accepted)
    d = accepted.risk_detail
    fx = Decimal(1) / Decimal("1.17")  # EUR per USD in the harness
    distance = Decimal("4169.97") - Decimal("4150.0")  # executable ask - structural stop
    assert d["stop_distance"] == distance
    expected = d["quantity"] * 100 * distance * d["fx"]
    assert abs(d["stop_risk_eur"] - expected) < Decimal("1e-20")
    assert abs(d["fx"] - fx) < Decimal("0.000001")
    assert d["equity_risk_fraction"] == d["stop_risk_eur"] / Decimal(10_000)
    assert d["loss_per_lot_at_stop"] == 100 * distance * d["fx"]
    # the broker agrees: a stop-out loses exactly that amount (harness profit = size x 100 x move x fx)
    broker.set_symbol_quote("GOLD", 4149.0, 4149.5)
    (closed,) = [e for e in stack.poll_events() if isinstance(e, PositionClosed)]
    assert abs(-closed.profit_eur - d["quantity"] * 100 * (Decimal("4169.97") - Decimal(4150)) * d["fx"]) < Decimal("0.5")


def test_accept_logs_the_complete_risk_detail(env):
    _broker, stack = env
    stack.start()
    events = stack.submit(
        make_intent(),
        context={"family": "BREAKOUT", "atr": "20.0", "confidence": 0.31, "expected_payoff_r": 1.8,
                 "win_probability": 0.44, "win_probability_uncertainty": 0.12},
    )
    d = events[0].risk_detail
    for key in (
        "structural_stop", "stop_distance", "broker_min_lot", "lot_step", "quantity", "stop_risk_eur",
        "equity_risk_fraction", "leverage", "portfolio_risk_before", "portfolio_risk_after",
        "cluster_risk_before", "cluster_risk_after", "family_risk_before", "family_risk_after",
        "concentration_after", "concentration_before", "target_risk_fraction", "desired_quantity",
        "binding_cap", "decision", "reject_code", "atr", "stop_distance_atr", "spread", "policy_id",
        "signal_inputs", "caps", "planned_r_to_target", "margin_required", "free_margin",
    ):
        assert key in d, key
    assert d["decision"] == "TRADE" and d["reject_code"] is None
    assert d["family"] == "BREAKOUT" and d["cluster"] == "INDEX"
    assert d["signal_inputs"]["win_probability"] == 0.44
    assert d["signal_inputs"]["win_probability_uncertainty"] == 0.12
    assert d["stop_distance_atr"] == d["stop_distance"] / Decimal("20.0")


def test_reject_logs_the_same_detail_with_the_exact_reason(env):
    broker, stack = env
    stack.start()
    broker.set_quote(25010.0, 25011.5)
    events = stack.submit(make_intent(), context={"family": "BREAKOUT", "confidence": 0.9})
    d = events[0].risk_detail
    assert reason(events) == "entry_overshoot"
    assert d["decision"] == "SKIP" and d["reject_code"] == "entry_overshoot"
    assert d["gate_reject_class"] == "STRUCTURAL" and d["gate_hard"] is True
    for key in ("structural_stop", "stop_distance", "portfolio_risk_before", "equity", "family", "spread"):
        assert key in d, key
    assert d["signal_inputs"] == {"confidence": 0.9}


# -- quality inputs never decide ----------------------------------------------------------------------------


def test_quality_inputs_never_reject_and_never_change_the_size(tmp_path):
    sizes = []
    for i, ctx in enumerate(
        (
            {},
            {"confidence": 0.99, "confluence": 9, "family_score": 5.0, "win_probability": 0.99},
            {"confidence": 0.0, "confluence": 0, "family_score": -3.0, "win_probability": 0.01,
             "quality_components": {"a": -1}},
        )
    ):
        broker = build_broker()
        stack = make_stack(broker, tmp_path / str(i))
        try:
            stack.start()
            events = stack.submit(make_intent(), context=ctx)
            assert kinds(events) == ["Accepted", "Fill", "ProtectionConfirmed"], (ctx, events)
            sizes.append(events[0].quantity)
        finally:
            stack.stop()
    assert len(set(sizes)) == 1  # sizing is independent of every quality input


def test_risk_budget_multiplier_is_an_explicit_hook_only(tmp_path):
    quantities = []
    for i, ctx in enumerate(({}, {"risk_budget_multiplier": 0.5})):
        broker = build_broker()
        stack = make_stack(broker, tmp_path / str(i))
        try:
            stack.start()
            quantities.append(stack.submit(make_intent(), context=ctx)[0].quantity)
        finally:
            stack.stop()
    assert quantities[1] < quantities[0]  # explicit scaling works; nothing scales automatically


# -- hard caps ---------------------------------------------------------------------------------------------------


def test_min_lot_is_accepted_whatever_its_actual_risk_unless_a_hard_cap_is_violated(tmp_path):
    small = build_broker(balance=500.0)
    stack = make_stack(small, tmp_path)
    try:
        stack.start()
        # 1 % of 500 EUR = 5 EUR is below one 0.25 lot; the min lot risks ~2.6 % at a 50-point stop.
        events = stack.submit(make_intent(intent_id="mid"))
        accepted = events[0]
        assert kinds(events) == ["Accepted", "Fill", "ProtectionConfirmed"]
        assert accepted.quantity == Decimal("0.25")
        assert Decimal("0.02") < accepted.risk_fraction < Decimal("0.05")
        assert accepted.risk_detail["min_lot_used"] is True
        assert small.request_log[0]["sl"] == 24950.0
    finally:
        stack.stop()
    wide = build_broker(balance=500.0)
    stack2 = make_stack(wide, tmp_path / "b")
    try:
        stack2.start()
        # 100-point structural stop: min lot risk ~5.2 % > the 5 % per-position cap => skip
        events = stack2.submit(make_intent(intent_id="wide", stop=24900.0, target=25200.0))
        assert kinds(events) == ["Rejected"] and reason(events) == "size_below_min"
        detail = events[0].risk_detail
        assert detail["violated_cap"] == "max_position_stop_risk_fraction"
        assert detail["cap_limit"] == Decimal("0.05") and detail["observed_at_min_lot"] > Decimal("0.05")
        assert "minimum lot" in detail["message"] and "max_position_stop_risk_fraction" in detail["message"]
        assert wide.order_send_calls == 0
    finally:
        stack2.stop()


def test_leverage_never_exceeds_thirty_and_the_quantity_is_fitted_to_it(env):
    broker, stack = env
    stack.start()
    # a 2-point structural stop would want ~50 lots: fitted to the leverage / liquidation / margin caps
    events = stack.submit(make_intent(stop=24999.0, target=25150.0))
    d = events[0].risk_detail
    assert d["leverage"] <= 30 and d["portfolio_leverage_after"] <= 30
    assert d["quantity"] < d["desired_quantity"] and d["quantity_reduced_by_cap"] is True
    assert d["binding_cap"] in {"liquidation_safe_leverage", "max_leverage", "instrument_max_leverage",
                                "max_margin_fraction_of_free_margin", "max_portfolio_leverage"}
    if kinds(events)[-1] == "ProtectionConfirmed":
        assert broker.request_log[0]["sl"] == 24999.0


def test_aggregate_open_risk_cap_is_a_configurable_hard_cap(tmp_path):
    broker, stack = with_caps(tmp_path, max_aggregate_open_stop_risk_fraction=Decimal("0.018"))
    try:
        stack.start()
        for market, sym, entry, stop, target in (
            ("XAUUSD", "GOLD", 4170.0, 4150.0, 4215.0),
            ("GER40", "Ger40", 25002.0, 24950.0, 25150.0),
        ):
            events = stack.submit(
                make_intent(intent_id=market, market=market, broker_symbol=sym, entry_ref=entry,
                            stop=stop, target=target, min_space_r=1.0)
            )
            assert kinds(events)[-1] == "ProtectionConfirmed", (market, events)
        events = stack.submit(
            make_intent(intent_id="nas", market="NAS100", broker_symbol="UsaTec", entry_ref=21002.0,
                        stop=20950.0, target=21150.0, min_space_r=1.0)
        )
        assert reason(events) == "size_below_min"
        detail = events[0].risk_detail
        assert detail["violated_cap"] == "max_aggregate_open_stop_risk_fraction"
        assert detail["portfolio_risk_before"] > 0
        assert detail["portfolio_risk_fraction_after"] > Decimal("0.018")
        assert len(broker.positions_get()) == 2
    finally:
        stack.stop()


def test_index_cluster_cap_treats_nasdaq_spx_and_dax_as_one_cluster(tmp_path):
    _broker, stack = with_caps(tmp_path, max_cluster_stop_risk_fraction=Decimal("0.018"))
    try:
        stack.start()
        for market, sym, entry, stop, target in (
            ("NAS100", "UsaTec", 21002.0, 20950.0, 21150.0),
            ("SPX500", "Usa500", 6001.0, 5980.0, 6050.0),
        ):
            events = stack.submit(
                make_intent(intent_id=market, market=market, broker_symbol=sym, entry_ref=entry,
                            stop=stop, target=target, min_space_r=1.0)
            )
            assert kinds(events)[-1] == "ProtectionConfirmed", (market, events)
        ger = stack.submit(make_intent(intent_id="ger", min_space_r=1.0))
        assert reason(ger) == "size_below_min"
        assert ger[0].risk_detail["violated_cap"] == "max_cluster_stop_risk_fraction"
        assert ger[0].risk_detail["cluster"] == "INDEX"
        cluster_before = ger[0].risk_detail["cluster_risk_before"]
        assert cluster_before > Decimal("150")  # NAS + SPX both count into the INDEX cluster
        gold = stack.submit(
            make_intent(intent_id="gold", market="XAUUSD", broker_symbol="GOLD", entry_ref=4170.0,
                        stop=4150.0, target=4215.0)
        )
        assert kinds(gold)[-1] == "ProtectionConfirmed"  # another cluster is unaffected
    finally:
        stack.stop()


def test_family_concentration_cap_stops_a_family_from_taking_more_risk_by_emitting_more(tmp_path):
    # a family may hold at most 16 % of the 10 % aggregate budget = 1.6 % of equity
    _broker, stack = with_caps(tmp_path, max_family_share_of_open_risk=Decimal("0.16"))
    try:
        stack.start()
        ok = stack.submit(make_intent(intent_id="f1"), context={"family": "BREAKOUT"})
        assert kinds(ok)[-1] == "ProtectionConfirmed"
        second = stack.submit(
            make_intent(intent_id="f2", market="XAUUSD", broker_symbol="GOLD", entry_ref=4170.0,
                        stop=4150.0, target=4215.0),
            context={"family": "BREAKOUT"},
        )
        assert kinds(second)[-1] == "ProtectionConfirmed"  # ~2 % of equity in one family
        third = stack.submit(
            make_intent(intent_id="f3", market="NAS100", broker_symbol="UsaTec", entry_ref=21002.0,
                        stop=20950.0, target=21150.0, min_space_r=1.0),
            context={"family": "BREAKOUT"},
        )
        assert reason(third) == "size_below_min"
        assert third[0].risk_detail["violated_cap"] == "max_family_share_of_open_risk"
        other = stack.submit(
            make_intent(intent_id="f4", market="NAS100", broker_symbol="UsaTec", entry_ref=21002.0,
                        stop=20950.0, target=21150.0, min_space_r=1.0),
            context={"family": "REVERSAL"},
        )
        assert kinds(other)[-1] == "ProtectionConfirmed"
        shares = other[0].risk_detail["concentration_after"]
        assert 0 < shares["family"] < 1 and shares["cluster"] > 0 and shares["market"] > 0
    finally:
        stack.stop()


def test_margin_must_fit_the_brokers_free_margin(tmp_path):
    broker = build_broker(balance=10_000.0)
    stack = make_stack(broker, tmp_path)
    try:
        stack.start()
        broker.cfg.margin_rate = 0.5  # the broker now wants 50 % margin: 1 lot costs ~12 500 EUR
        events = stack.submit(make_intent())
        d = events[0].risk_detail
        assert d["margin_required"] <= d["free_margin"] * Decimal("0.90")
        assert d["binding_cap"] in {"max_margin_fraction_of_free_margin", "liquidation_safe_leverage",
                                    "max_leverage", "instrument_max_leverage"}
    finally:
        stack.stop()


# -- netting: BROKER representation vs INTERNAL tranches ------------------------------------------------------------


def test_same_symbol_setup_is_a_temporary_limitation_not_a_risk_rule(env):
    """One net position per symbol is only the broker's representation. v1 cannot broker-protect
    two tranches, so a valid same-symbol setup is blocked ONLY by a named temporary-limitation
    code and is counted in its own funnel category."""
    broker, stack = env
    stack.start()
    stack.submit(make_intent(intent_id="a"))
    shared = stack.submit(make_intent(intent_id="b"))  # same structural stop and target
    assert reason(shared) == "ADDON_SHARED_STOP_POSSIBLE_NOT_YET_IMPLEMENTED"
    detail = shared[0].risk_detail
    assert detail["addon_classification"] == "SHARED_STOP_POSSIBLE"
    assert detail["temporary_limitation"] is True and detail["otherwise_valid"] is True
    assert detail["gate_reject_class"] == "TEMPORARY"
    assert detail["netting"] == "BROKER_ONE_NET_POSITION_PER_SYMBOL"
    assert detail["internal_model"] == "TRANCHE_LEDGER_KEYED_BY_INTENT_ID"
    independent = stack.submit(make_intent(intent_id="c", stop=24900.0, target=25200.0))
    assert reason(independent) == "ADDON_EXPOSURE_NOT_SUPPORTED_V1"
    assert independent[0].risk_detail["addon_classification"] == "INDEPENDENT_STOPS_NEEDED"
    opposite = stack.submit(
        make_intent(intent_id="d", direction=-1, entry_ref=24999.0, stop=25050.0, target=24850.0)
    )
    assert reason(opposite) == "OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1"
    assert broker.order_send_calls == 1 and len(broker.positions_get()) == 1
    funnel = stack.rejection_funnel()
    assert set(funnel) == {"TEMPORARY_LIMITATION"}
    assert funnel["TEMPORARY_LIMITATION"]["total"] == 3
    assert funnel["TEMPORARY_LIMITATION"]["otherwise_valid_blocked"] == 3
    assert "position_exists" not in str(funnel)


def test_an_invalid_same_symbol_setup_is_rejected_for_its_own_reason_not_the_limitation(env):
    broker, stack = env
    stack.start()
    stack.submit(make_intent(intent_id="a"))
    events = stack.submit(make_intent(intent_id="stale", valid_s=-5))
    assert reason(events) == "stale_signal"
    assert "TEMPORARY_LIMITATION" not in stack.rejection_funnel()
    assert broker.order_send_calls == 1


def test_two_positions_on_two_instruments_are_open_simultaneously(env):
    broker, stack = env
    stack.start()
    first = stack.submit(make_intent(intent_id="ger"))
    second = stack.submit(
        make_intent(intent_id="nas", market="NAS100", broker_symbol="UsaTec", entry_ref=21002.0,
                    stop=20950.0, target=21150.0)
    )
    assert kinds(first)[-1] == kinds(second)[-1] == "ProtectionConfirmed"
    assert {p.symbol for p in broker.positions_get()} == {"Ger40", "UsaTec"}
    assert sorted(stack.open_intents()) == ["ger", "nas"]
    d = second[0].risk_detail
    assert d["portfolio_risk_before"] > 0 and d["cluster_risk_before"] > 0
    assert d["cluster_risk_after"] == d["cluster_risk_before"] + d["stop_risk_eur"]
    assert d["portfolio_risk_after"] == d["portfolio_risk_before"] + d["stop_risk_eur"]


# -- TCA -----------------------------------------------------------------------------------------------------------------


def test_tca_fields_are_populated_on_fill_and_close(env):
    broker, stack = env
    stack.start()
    events = stack.submit(make_intent(entry_ref=25002.0, target=25150.0))
    fill = next(e for e in events if isinstance(e, Fill))
    assert fill.intended_price == Decimal("25002.0") and fill.reference_price == Decimal("25001.5")
    assert fill.bid_at_send == Decimal("25000.0") and fill.ask_at_send == Decimal("25001.5")
    assert fill.spread == Decimal("1.5") and fill.slippage == Decimal(0)
    assert fill.slippage_vs_intended == Decimal("-0.5")  # filled better than the intended entry
    assert fill.fill_vs_mid == Decimal("0.75")  # a long pays half the spread over the mid
    assert fill.fees_price_units == Decimal(0)  # the fake broker charges no commission
    assert fill.cost_price_units == Decimal("1.5")  # spread only
    assert fill.movement_to_cost == Decimal("148.5") / Decimal("1.5")  # planned move to target / cost
    assert fill.latency_total_ms is not None and fill.latency_total_ms > 0
    assert fill.latency_send_to_fill_ms is not None
    assert fill.latency_send_to_ack_ms is None and fill.latency_ack_to_fill_ms is None
    broker.set_quote(24940.0, 24941.5)
    (closed,) = [e for e in stack.poll_events() if isinstance(e, PositionClosed)]
    assert closed.entry_price == Decimal("25001.5") and closed.holding_seconds is not None
    assert closed.net_pnl_eur == closed.profit_eur + closed.commission + closed.swap
    assert closed.exit_slippage_vs_level == Decimal(0)  # filled exactly at the stop level


def test_tca_with_commission_and_slippage(tmp_path):
    broker = build_broker(commission_per_lot=6.0)
    stack = make_stack(broker, tmp_path)
    try:
        stack.start()
        broker.fill_plan = [[(1.5, 25002.5)]]  # one full fill, 1.0 worse than the quote
        events = stack.submit(make_intent(entry_ref=25004.0))
        fill = next(e for e in events if isinstance(e, Fill))
        assert fill.slippage == Decimal("1.0")
        assert fill.commission == Decimal("-9.0") and fill.fees_price_units == Decimal("9") / Decimal("1.5")
        assert fill.cost_price_units == Decimal("1.5") + Decimal("1.0") + Decimal("6")
    finally:
        stack.stop()
