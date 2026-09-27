"""Sprint 1 integration: full MarketSnapshot -> Universe -> Strategy -> Signal ->
RiskDecision -> ExecutionRequest -> Paper Fill -> Portfolio -> Metrics path.

Every test below is a thin pytest wrapper around a `scenario_*` function in
`e2e_scenarios.py`; the scenario function does the real work (and its
assertions), so `scripts/sprint1_e2e_evidence.py` can call the exact same
function and get the exact same guarantees outside of pytest.
"""

from __future__ import annotations

from tests.integration import e2e_scenarios as scenarios


def test_accepted_long_opens_exactly_one_long_position() -> None:
    evidence = scenarios.scenario_accepted_long()
    assert evidence["execution_status"] == "filled"
    assert float(evidence["position_qty"]) > 0


def test_accepted_short_opens_exactly_one_short_position() -> None:
    evidence = scenarios.scenario_accepted_short()
    assert evidence["execution_status"] == "filled"
    assert float(evidence["position_qty"]) < 0


def test_risk_reject_wide_spread_creates_zero_exposure() -> None:
    evidence = scenarios.scenario_risk_reject_wide_spread()
    assert evidence["risk_reason"] == "SPREAD_TOO_WIDE"
    assert evidence["execution_status"] is None
    assert evidence["position_qty"] == "0"


def test_halt_runtime_and_execution_create_zero_exposure() -> None:
    evidence = scenarios.scenario_halt()
    assert evidence["runtime_kill_switch"]["result"]["risk_reason"] == "HALTED"
    assert evidence["risk_engine_halt"]["result"]["risk_reason"] == "HALTED"
    assert evidence["execution_engine_halted"]["result"]["risk_reason"] == "ACCOUNT_UNRECONCILED"
    assert (
        evidence["execution_engine_halted_direct_submit"]["result"]["execution_reject_code"]
        == "NOT_READY"
    )
    for variant in (
        "runtime_kill_switch",
        "risk_engine_halt",
        "execution_engine_halted",
        "execution_engine_halted_direct_submit",
    ):
        assert evidence[variant]["position_qty_before"] == evidence[variant]["position_qty_after"]
        assert evidence[variant]["gross_notional_after"] == "0"


def test_stale_signal_is_rejected_with_zero_exposure() -> None:
    evidence = scenarios.scenario_stale_signal()
    assert evidence["risk_reason"] == "SIGNAL_STALE"
    assert evidence["position_qty"] == "0"


def test_duplicate_request_does_not_double_the_position() -> None:
    evidence = scenarios.scenario_duplicate_request()
    assert evidence["first"]["signal_id"] == evidence["second"]["signal_id"]
    assert evidence["first"]["position_qty"] == evidence["second"]["position_qty"]
    assert evidence["qty_after_resubmit"] == evidence["first"]["position_qty"]


def test_duplicate_fill_is_counted_once() -> None:
    evidence = scenarios.scenario_duplicate_fill()
    assert evidence["qty_after_first_trade"] == evidence["qty_after_duplicate_trade"]
    assert evidence["qty_after_report_fill"] == evidence["qty_after_duplicate_report"]


def test_leverage_cap_binds_at_20x_and_policy_over_cap_raises() -> None:
    evidence = scenarios.scenario_leverage_cap()
    assert float(evidence["decision1_leverage"]) <= 20
    assert evidence["decision1_binding_constraint"] in {
        "leverage_cap",
        "portfolio_leverage_capacity",
    }
    assert float(evidence["account_leverage_after_first"]) <= 20
    assert float(evidence["account_leverage_after_second"]) <= 20
    assert evidence["raised_over_20x_policy"] is True


def test_gross_exposure_is_marked_and_visible_after_open() -> None:
    evidence = scenarios.scenario_gross_exposure_marked_after_open()
    assert float(evidence["qty"]) > 0
    assert float(evidence["gross_notional"]) > 0
    assert float(evidence["gross_notional"]) >= float(evidence["qty"]) * float(
        evidence["latest_last"]
    )


def test_second_long_cannot_exceed_max_gross_notional() -> None:
    evidence = scenarios.scenario_exposure_limit_blocks_second_long()
    tolerance = float(evidence["max_gross_notional"]) * 1.02
    assert float(evidence["gross_after_second"]) <= tolerance


def test_nan_and_inf_cannot_produce_exposure() -> None:
    evidence = scenarios.scenario_nan_inf_rejected()
    assert evidence["market_snapshot_rejects_nan"] is True
    assert evidence["risk_decision_reason"] == "INVALID_INPUT"
    assert evidence["position_qty"] == "0"


def test_invalid_stop_distance_is_rejected() -> None:
    evidence = scenarios.scenario_invalid_stop()
    assert evidence["stop_equals_entry_reason"] == "INVALID_STOP"
    assert evidence["wrong_side_reason"] == "INVALID_STOP"


def test_unknown_account_state_blocks_exposure() -> None:
    evidence = scenarios.scenario_account_unknown()
    assert evidence["risk_reason"] == "ACCOUNT_UNKNOWN"
    assert evidence["position_qty"] == "0"


def test_execution_cannot_bypass_risk_decision() -> None:
    evidence = scenarios.scenario_execution_cannot_bypass_risk()
    assert evidence["none_decision_reject"] == "RISK_NOT_APPROVED"
    assert evidence["rejected_decision_reject"] == "RISK_NOT_APPROVED"
    assert evidence["mismatch_id_reject"] == "RISK_MISMATCH"
    assert evidence["mismatch_side_reject"] == "RISK_MISMATCH"
    assert evidence["over_size_reject"] == "EXCEEDS_APPROVED_SIZE"
    assert evidence["position_qty"] == "0"


def test_metrics_after_long_open_and_close_are_finite_and_json_serializable() -> None:
    evidence = scenarios.scenario_metrics_json_serializable()
    assert evidence["trade_count"] == 1
    assert evidence["json_length"] > 0
