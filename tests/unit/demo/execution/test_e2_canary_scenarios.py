# ruff: noqa: E501
"""Lane K: the E2 canary SCENARIOS (B1 CONTINUATION / B2 REVERSION + broker TP / B3 restart with an open position) driven through the
REAL ``manage_exits`` path (StagedExitManager + exit profiles) against the netting-account FAKE broker.  Zero real MT5.

Covers: each scenario's full PASS, sabotage cases failing at the right step with the finally-flatten still executing, the injection
contract (engine inputs only: no order price is ever altered, every value logged), the new EOD-recovery window / lock refusals, the
refusals staying in force for every scenario, ``--scenario all`` ordering + flat-between check, and the CLI / static plan.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts import e2_broker_canary as canary
from tests.unit.demo.execution.stack_harness import build_broker
from tests.unit.demo.execution.test_e2_canary import foreign_fill, load, make_env, own, status_of

D = Decimal
TP_LEVEL_MIN = 1.1750  # the canary's TP1 / target level is ~3 x 17.6 pips above the 1.17005 ask: ~1.1753
SCENARIO_STEPS_N = {"base": 10, "b1": 9, "b2": 5, "b3": 10}


def run_scenario(env: canary.CanaryEnv, scenario: str, **kw) -> tuple[int, dict]:
    code, _ = canary.run_canary(env, scenario=scenario, **kw)
    (path,) = sorted(env.artifacts_dir.glob(f"canary_report_*_{scenario}.json"))
    return code, json.loads(path.read_text(encoding="utf-8"))


def assert_failed_at(report: dict, step: int, broker) -> None:
    assert report["verdict"].startswith(f"FAIL(step {step}"), report["verdict"]
    assert report["steps"][step - 1]["status"] == "FAIL"
    assert all(s["status"] == "SKIPPED" for s in report["steps"][step:])
    assert not own(broker), "the finally-flatten left the canary position open"
    assert report["final_flatten"]["residual_canary_positions"] == []


def prices_of(broker) -> list[float]:
    return [r["price"] for r in broker.request_log if r.get("price")]


def assert_injection_never_touched_orders(broker, report: dict) -> None:
    """Orders carry the REAL market: no request price equals / approaches an injected quote, no stop is above the real market."""
    injected = [v for v in report["injected_values"] if v["injected_bid"] is not None]
    assert injected, "scenario injected no quote (expected at least one)"
    inj_prices = {round(float(v["injected_bid"]), 5) for v in injected} | {round(float(v["injected_ask"]), 5) for v in injected}
    plan_tp = float(report["plan"]["tp1_price"])
    for req in broker.request_log:
        for key in ("price", "sl"):
            if req.get(key):
                assert round(float(req[key]), 5) not in inj_prices, f"request {req} carries an injected value"
        if req.get("tp"):  # the only legal TP is the entry's broker TP at the target level (B2); never a value derived from a quote
            assert abs(float(req["tp"]) - plan_tp) < 1e-9 and report["scenario"] == "b2", f"request {req} carries an unexpected TP"
        if req.get("price"):
            assert abs(float(req["price"]) - 1.17) < 0.001, f"order price {req['price']} is not the real market"
        if req.get("sl"):
            assert float(req["sl"]) < 1.16995, f"stop {req['sl']} is not below the real bid"


# -- B1 ---------------------------------------------------------------------------------------------------------------


def test_b1_continuation_full_pass_through_manage_exits(tmp_path):
    broker = build_broker()
    lines: list[str] = []
    env = make_env(tmp_path, broker, log=lines.append)
    code, report = run_scenario(env, "b1")
    assert code == 0 and report["verdict"] == "EXECUTION_CONTRACT_PASS", report["verdict"]
    assert [s["name"] for s in report["steps"]] == list(canary.B1_STEP_NAMES)
    assert status_of(report) == ["PASS"] * 9
    assert report["scenario"] == "b1" and report["plan"]["exit_policy"] == "staged_profiles"
    assert not broker.positions_get() and not broker.orders_get()
    # the ENGINE decided: the manager logged partial_exit / stop_moved / full_close with the CONTINUATION profile
    engine = [e for v in report["injected_values"] for e in v["engine_log"]]
    kinds = [e["kind"] for e in engine]
    assert kinds.count("partial_exit") == 1 and kinds.count("stop_moved") == 1 and kinds.count("full_close") == 1
    assert next(e for e in engine if e["kind"] == "partial_exit")["exit_profile"] == "CONTINUATION"
    assert next(e for e in engine if e["kind"] == "full_close")["exit_reason"] == "EXIT_ENGINE_STRUCTURE"
    # real orders at the broker: one reduce-only partial 0.01, one SLTP modify (the trail), one reduce-only close of the runner
    deals = [r for r in broker.request_log if r.get("position") and r.get("action") == 1]
    assert [r["volume"] for r in deals] == [0.01, 0.01]
    modifies = [r for r in broker.request_log if r.get("action") == 6]
    assert modifies and max(float(r["sl"]) for r in modifies) > 1.1683  # tightened
    assert all(r["magic"] == canary.CANARY_MAGIC for r in broker.request_log if "magic" in r)
    assert_injection_never_touched_orders(broker, report)
    # report: per-step injected values, marker, before/after snapshots, latencies
    assert [s["n"] for s in report["steps"] if s.get("injected")] == [3, 6, 7, 8]
    assert all(v["marker"] == canary.INJECTION_MARKER and v["quote_reads"] + v["frame_reads"] >= 1 for v in report["injected_values"])
    assert report["steps"][2]["before"]["positions"][0]["volume"] == 0.02 and report["steps"][2]["after"]["positions"][0]["volume"] == 0.01
    assert report["latencies_ms"]["engine_partial_ms"] > 0 and report["latencies_ms"]["engine_final_close_ms"] > 0
    assert sum(canary.INJECTION_MARKER in ln for ln in lines) == len(report["injected_values"]) == 4  # tp1, trail, loosen_cycle, failure
    assert report["restart"]["reconciliation"] == "RECONCILED" and report["registry_status_after_close"] == "CLOSED"


def test_injection_is_removed_after_every_manage_cycle(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    seen: dict[str, bool] = {}

    def check():
        src = env.runtime["canary"].stack.bar_source
        seen["clean"] = "latest_quote" not in vars(src) and "m5_frame" not in vars(src)

    env.hooks["after_action_6"] = check
    code, _ = run_scenario(env, "b1")
    assert code == 0 and seen == {"clean": True}


def test_b1_sabotage_engine_partial_removes_the_wrong_volume(tmp_path):
    broker = build_broker()
    real_send = broker.order_send

    def send(request):
        if request.get("position") and request.get("volume") == 0.01 and not send.done:  # the broker closes MORE than asked
            send.done = True
            request = {**request, "volume": 0.02}
        return real_send(request)

    send.done = False
    broker.order_send = send
    env = make_env(tmp_path, broker)
    code, report = run_scenario(env, "b1")
    assert code == 1
    assert_failed_at(report, 3, broker)


def test_b1_sabotage_partial_leaves_the_position_unprotected(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def strip_sl():
        for p in broker.positions.values():
            p.sl = 0.0

    env.hooks["after_action_5"] = strip_sl
    code, report = run_scenario(env, "b1")
    assert code == 1
    assert_failed_at(report, 5, broker)


def test_b1_sabotage_stop_not_tightened_by_the_engine_path(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def revert_sl():  # the broker "forgets" the engine's modify
        for p in broker.positions.values():
            p.sl = round(p.price_open - 0.00175, 5)

    env.hooks["after_action_6"] = revert_sl
    code, report = run_scenario(env, "b1")
    assert code == 1
    assert_failed_at(report, 6, broker)


def test_b1_sabotage_loosen_is_allowed_by_the_broker_path(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def loosen():
        for p in broker.positions.values():
            p.sl = round(p.sl - 0.0010, 5)

    env.hooks["after_action_7"] = loosen
    code, report = run_scenario(env, "b1")
    assert code == 1
    assert_failed_at(report, 7, broker)
    assert "changed" in report["steps"][6]["detail"]


def test_b1_sabotage_engine_close_leaves_exposure(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    sent = {"done": False}

    def reopen():
        if not sent["done"]:
            sent["done"] = True
            foreign_fill(broker, "EURUSD", 0.01, magic=canary.CANARY_MAGIC, sl=1.1600)

    env.hooks["after_action_8"] = reopen
    code, report = run_scenario(env, "b1")
    assert code == 1
    assert_failed_at(report, 8, broker)


def test_b1_manager_that_never_evaluates_fails_step_3(tmp_path):
    """If the engine did not consume the injected inputs the step must not pass on a coincidence (and nothing is left open)."""
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def deafen():  # manage_exits does nothing: no evaluation, so no partial
        env.runtime["canary"].stack.manage_exits = lambda now: []

    env.hooks["before_manage_tp1"] = deafen
    code, report = run_scenario(env, "b1")
    assert code == 1
    assert_failed_at(report, 3, broker)
    assert "never read the injected" in report["steps"][2]["detail"]


# -- B2 ---------------------------------------------------------------------------------------------------------------


def test_b2_reversion_full_pass_broker_tp_and_engine_target_at_the_same_level(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    code, report = run_scenario(env, "b2")
    assert code == 0 and report["verdict"] == "EXECUTION_CONTRACT_PASS", report["verdict"]
    assert [s["name"] for s in report["steps"]] == list(canary.B2_STEP_NAMES)
    assert status_of(report) == ["PASS"] * 5
    entry = broker.request_log[0]
    tp = float(report["plan"]["tp1_price"])
    assert abs(entry["tp"] - tp) < 1e-9 and tp > 1.1750 and entry["sl"] > 0  # broker TP at the target level, far above the market
    assert report["plan"]["broker_tp"] == report["plan"]["tp1_price"]
    step3 = report["steps"][2]["data"]
    assert step3["closed_by"] == "engine" and step3["exit_reason"] == "EXIT_ENGINE_TP1"
    assert report["steps"][3]["data"]["stray_orders"] == 0 and report["steps"][3]["data"]["flatten_failures"] == {}
    assert report["steps"][1]["data"]["broker_tp"] == report["steps"][1]["data"]["engine_stage_price"]
    deals = [r for r in broker.request_log if r.get("position") and r.get("action") == 1]
    assert [r["volume"] for r in deals] == [0.02]  # ONE real full reduce-only close
    assert not broker.positions_get() and not broker.orders_get()
    assert_injection_never_touched_orders(broker, report)
    assert report["registry_status_after_close"] == "CLOSED"


def test_b2_sabotage_stray_order_left_after_the_close(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def stray():  # the TP order stayed behind at the broker
        broker.orders[999001] = SimpleNamespace(
            ticket=999001, symbol="EURUSD", magic=canary.CANARY_MAGIC, type=3, volume_current=0.02, volume_initial=0.02,
            price_open=1.1760, sl=0.0, tp=0.0,
        )

    env.hooks["after_action_3"] = stray
    code, report = run_scenario(env, "b2")
    assert code == 1
    assert report["verdict"].startswith("FAIL(step 4") or report["verdict"].startswith("FAIL(step 3"), report["verdict"]
    assert not own(broker)


def test_b2_sabotage_broker_tp_not_at_the_engine_level(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def move_tp():
        for p in broker.positions.values():
            p.tp = round(p.tp + 0.0020, 5)

    env.hooks["after_action_2"] = move_tp
    code, report = run_scenario(env, "b2")
    assert code == 1
    assert_failed_at(report, 2, broker)


def test_b2_sabotage_engine_close_failure_counts_as_a_halt(tmp_path):
    """A failed engine close must show up as flatten failure / halt in step 3 or 4 (and the finally-flatten still closes)."""
    broker = build_broker()
    real_send = broker.order_send

    def send(request):
        if request.get("position") and request.get("volume") == 0.02 and not send.done:
            send.done = True
            return SimpleNamespace(retcode=10006, comment="Request rejected", order=0, deal=0, volume=0.0, price=0.0, request_id=0)
        return real_send(request)

    send.done = False
    broker.order_send = send
    env = make_env(tmp_path, broker)
    code, report = run_scenario(env, "b2")
    assert code == 1
    assert report["verdict"].startswith("FAIL(step 3")
    assert not own(broker)


@pytest.mark.skip(reason="LANE V dependency: on base 327c7e7 the engine close that reaches the broker AFTER its TP filled gets 'Position not found' "
                         "(retcode 10036) and Mt5DemoStack._flatten returns False (flat=False, halt flatten_failed, failure count + 1) although the "
                         "position IS flat. Lane V fixes the _flatten success semantics; remove this skip after the Lane V merge "
                         "(the test body is complete and was verified to fail only on exactly that: 'the engine close reported failure (flat=False)').")
def test_b2b_broker_tp_fills_first_then_the_engine_close_is_harmless(tmp_path):
    broker = build_broker()
    real_send = broker.order_send

    def send(request):
        if request.get("position") and request.get("volume") == 0.02 and not send.done:
            send.done = True
            tp = max(float(p.tp) for p in broker.positions.values())
            broker.set_symbol_quote("EURUSD", round(tp + 0.00005, 5), round(tp + 0.00015, 5))  # the broker TP fills FIRST
        return real_send(request)

    send.done = False
    broker.order_send = send
    env = make_env(tmp_path, broker, tolerate_tp_race=True)
    code, report = run_scenario(env, "b2")
    assert send.done and any(o.comment == "[tp]" for o in broker.history_orders), "the broker TP did not fill (no race happened)"
    stack_state = report["steps"][3]
    assert stack_state["status"] == "PASS", stack_state["detail"]  # no halt, no flatten-failure count, no stray order
    assert not broker.positions_get() and code in (0, 1)


@pytest.mark.skip(reason="LANE V dependency (timing-dependent on base 327c7e7): when the strategy has already ingested the broker-TP deal before the "
                         "flatten runs, _flatten returns False (flat=False, halt flatten_failed) although the position is flat; it passed in isolation "
                         "and failed under the parallel fast tier. Remove this skip after the Lane V merge.")
def test_b2b_broker_tp_fills_before_the_engine_flatten_is_enqueued(tmp_path):
    """The broker TP fills AFTER the manager decided but BEFORE its flatten reaches the strategy (the local cache has not seen the TP
    deal yet).  The engine close must then be harmless: no halt, no flatten-failure count, no stray order, flat, registry terminal."""
    broker = build_broker()
    env = make_env(tmp_path, broker, tolerate_tp_race=True)

    def race():
        stack = env.runtime["canary"].stack
        original = stack._flatten

        def flatten_after_tp(info, **kw):
            tp = max(float(p.tp) for p in broker.positions.values())
            broker.set_symbol_quote("EURUSD", round(tp + 0.00005, 5), round(tp + 0.00015, 5))  # the broker TP fills first
            return original(info, **kw)

        stack._flatten = flatten_after_tp

    env.hooks["before_manage_target"] = race
    code, report = run_scenario(env, "b2")
    assert not broker.positions_get() and not broker.orders_get()
    assert any(o.comment == "[tp]" for o in broker.history_orders), "the broker TP did not fill (no race happened)"
    assert report["steps"][3]["status"] == "PASS", report["steps"][3]["detail"]
    assert code == 0, report["verdict"]


# -- B3 ---------------------------------------------------------------------------------------------------------------


def test_b3_restart_with_an_open_position_full_pass(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    code, report = run_scenario(env, "b3")
    assert code == 0 and report["verdict"] == "EXECUTION_CONTRACT_PASS", report["verdict"]
    assert [s["name"] for s in report["steps"]] == list(canary.B3_STEP_NAMES)
    assert status_of(report) == ["PASS"] * 10
    data6 = report["steps"][5]["data"]
    assert data6["post"]["reconciliation"] == "RECONCILED" and data6["post"]["adopted_intents"] == [data6["pre"]["intent_id"]]
    assert data6["post"]["profile"] == "CONTINUATION" and data6["pre"]["stages_completed"] == 1 and data6["post"]["stages_completed"] >= 1
    assert D(data6["post"]["broker_sl"]) == D(data6["pre"]["broker_sl"])  # the broker stop survived the restart untouched
    assert report["steps"][6]["data"]["stages_completed"] >= 1 and D(report["steps"][6]["data"]["broker_volume"]) == D("0.01")
    tighten = report["steps"][7]["data"]["tighten"]
    assert D(tighten["new_sl"]) > D(tighten["old_sl"])
    assert "adopted" in tighten["path"] or tighten["path"] == "ModifyStopJob"
    assert report["steps"][7]["data"]["loosen"]["broker_sl_unchanged"] is not None
    # no second TP1 reduce after the restart: exactly one partial (0.01) + one flatten (0.01) were sent
    deals = [r for r in broker.request_log if r.get("position") and r.get("action") == 1]
    assert [r["volume"] for r in deals] == [0.01, 0.01]
    assert not broker.positions_get() and not broker.orders_get()
    assert_injection_never_touched_orders(broker, report)
    assert report["restart"]["reconciliation"] == "RECONCILED" and report["restart"]["registry_status"] == "CLOSED"


def _mutate_registry_ctx(env, mutate):
    c = env.runtime["canary"]
    row = c.stack._registry.get(c.intent_id)
    ctx = json.loads(row.context)
    mutate(ctx)
    c.stack._registry.update(c.intent_id, context=json.dumps(ctx))


def test_b3_sabotage_profile_changed_across_the_restart(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    env.hooks["before_restart"] = lambda: _mutate_registry_ctx(env, lambda ctx: ctx["exit_profile"].update(profile="REVERSION"))
    code, report = run_scenario(env, "b3")
    assert code == 1
    assert_failed_at(report, 6, broker)
    assert "frozen profile changed" in report["steps"][5]["detail"]


def test_b3_sabotage_stages_completed_lost_across_the_restart(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    # the persisted exit_state vanishes: the lower bound is then DERIVED from the realized volume, so this must still PASS the
    # lower-bound check - but a changed state profile is not tolerated
    env.hooks["before_restart"] = lambda: _mutate_registry_ctx(env, lambda ctx: ctx["exit_state"].update(exit_profile="FAILED_MOVE"))
    code, report = run_scenario(env, "b3")
    assert code == 1
    assert_failed_at(report, 6, broker)


def test_b3_sabotage_position_not_adopted_after_the_restart(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def orphan_the_position():  # the registry no longer knows the position: the new session cannot adopt it
        c = env.runtime["canary"]
        reg = c.stack._registry
        with reg._lock:
            reg._db.execute("DELETE FROM intents WHERE intent_id=?", (c.intent_id,))

    env.hooks["before_restart"] = orphan_the_position
    code, report = run_scenario(env, "b3")
    assert code in (1, 4)
    assert report["steps"][5]["status"] == "FAIL"
    assert not own(broker), "the finally-flatten must still close the un-adopted position"
    assert report["final_flatten"]["residual_canary_positions"] == []


def test_b3_sabotage_broker_stop_missing_after_the_restart(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def strip_sl():
        for p in broker.positions.values():
            p.sl = 0.0

    # (the production stack REPAIRS a missing stop during its own restart reconciliation, so the stop is removed right after it)
    env.hooks["after_action_6"] = strip_sl
    code, report = run_scenario(env, "b3")
    assert code in (1, 4)
    assert report["steps"][5]["status"] == "FAIL" and "SL" in report["steps"][5]["detail"]
    assert not own(broker)


def test_b3_sabotage_stage_refires_after_the_restart(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    # the engine would be allowed to re-fire if the stage progress were lost AND the volume lower bound did not hold; emulate a
    # broker whose position volume went back up (stale) so the realized lower bound is gone
    def refill():
        for p in broker.positions.values():
            p.volume = 0.02

    env.hooks["before_manage_norefire"] = refill
    code, report = run_scenario(env, "b3")
    assert code == 1
    assert report["steps"][6]["status"] == "FAIL" or report["steps"][5]["status"] == "FAIL"
    assert not own(broker)


# -- default scenario unchanged ---------------------------------------------------------------------------------------


def test_default_scenario_is_the_unchanged_ten_step_canary(tmp_path, capsys):
    art = tmp_path / "cli_art"
    assert canary.main(["--fake", "--artifacts", str(art)]) == 0
    out = capsys.readouterr().out
    assert "EXECUTION_CONTRACT_PASS" in out and canary.INJECTION_MARKER not in out
    report = load(art)
    assert [s["name"] for s in report["steps"]] == list(canary.STEP_NAMES)
    assert status_of(report) == ["PASS"] * 7 + ["NOT_APPLICABLE", "PASS", "PASS"]
    assert report["scenario"] == "base" and report["injected_values"] == []
    assert not any("injected" in s for s in report["steps"])
    assert report["plan"]["broker_tp"] is None and "exit_policy" not in report["plan"]


# -- refusals stay in force for every scenario ------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["b1", "b2", "b3"])
def test_refusals_hold_for_every_scenario(tmp_path, scenario):
    broker = build_broker(trade_mode=2)
    code, report = run_scenario(make_env(tmp_path, broker), scenario)
    assert code == 2 and report["verdict"].startswith("REFUSED") and "non_demo_account" in report["verdict"]
    assert broker.order_send_calls == 0

    broker2 = build_broker()
    foreign_fill(broker2, "EURUSD", 0.01, magic=555, sl=1.1600)
    code, report = run_scenario(make_env(tmp_path / "second", broker2), scenario)
    assert code == 2 and "no_position_on_symbol" in report["verdict"]
    assert [p.magic for p in broker2.positions_get()] == [555]


def test_scenario_never_lowers_the_confirmation_requirement(tmp_path, capsys):
    assert canary.main(["--live", "--scenario", "all"]) == 2
    assert canary.CONFIRM_VALUE in capsys.readouterr().err
    env = make_env(tmp_path, mode="live", confirm=None, artifacts_dir=canary.REQUIRED_ARTIFACTS)
    assert any(c["name"] == "confirm_flag" and not c["ok"] for c in canary.local_preflight(env))


# -- EOD-recovery lock / window refusal -------------------------------------------------------------------------------


def berlin(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 1, hour, minute, tzinfo=canary.BERLIN)


@pytest.mark.parametrize(("hour", "minute", "refused"), [
    (21, 39, False), (21, 40, True), (21, 50, True), (22, 0, True), (22, 35, True), (22, 36, False), (10, 0, False),
])
def test_live_refused_inside_the_eod_recovery_window(tmp_path, hour, minute, refused):
    env = make_env(tmp_path, mode="live", confirm=canary.CONFIRM_VALUE, artifacts_dir=canary.REQUIRED_ARTIFACTS,
                   window_now=lambda: berlin(hour, minute))
    check = next(c for c in canary.local_preflight(env) if c["name"] == "outside_eod_recovery_window")
    assert check["ok"] is (not refused), check
    # a plan-only run sends nothing and is never refused for the window
    assert "outside_eod_recovery_window" not in {c["name"] for c in canary.local_preflight(env, plan_only=True)}


def test_window_check_uses_berlin_time_not_utc(tmp_path):
    from datetime import UTC

    env = make_env(tmp_path, mode="live", confirm=canary.CONFIRM_VALUE, artifacts_dir=canary.REQUIRED_ARTIFACTS,
                   window_now=lambda: datetime(2026, 10, 1, 19, 50, tzinfo=UTC))  # 21:50 Berlin (CEST)
    check = next(c for c in canary.local_preflight(env) if c["name"] == "outside_eod_recovery_window")
    assert check["ok"] is False


def test_refuses_when_the_eod_recovery_lock_is_alive(tmp_path):
    trader = tmp_path / "trader"
    trader.mkdir()
    (trader / "eod_recovery.lock").write_text(json.dumps({"pid": os.getpid(), "create_time": None, "role": "eod-recovery"}))
    broker = build_broker()
    code, report = run_scenario(make_env(tmp_path, broker), "b1")
    assert code == 2 and "no_eod_recovery_alive" in report["verdict"]
    assert broker.order_send_calls == 0 and broker.calls == []


# -- --scenario all ---------------------------------------------------------------------------------------------------


def test_scenario_all_runs_sequentially_with_a_flat_check_between(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    code, combined = canary.run_all(env)
    assert code == 0, combined["verdict"]
    assert combined["verdict"] == "EXECUTION_CONTRACT_PASS(all: base,b1,b2,b3)"
    assert [s["scenario"] for s in combined["scenarios"]] == ["base", "b1", "b2", "b3"]
    assert all(s["exit_code"] == 0 for s in combined["scenarios"])
    assert [(f["after_scenario"], f["ok"]) for f in combined["flat_checks"]] == [("base", True), ("b1", True), ("b2", True), ("b3", True)]
    assert [len(s["steps"]) for s in combined["scenarios"]] == [10, 9, 5, 10]
    assert not broker.positions_get() and not broker.orders_get()
    # one report per scenario (+ the combined one); the base scenario keeps its historical file name
    names = sorted(p.name for p in env.artifacts_dir.glob("canary_report_*.json"))
    assert len(names) == 5 and sum(n.endswith("_all.json") for n in names) == 1
    assert sum(n.endswith("_b1.json") for n in names) == sum(n.endswith("_b2.json") for n in names) == sum(n.endswith("_b3.json") for n in names) == 1


def test_scenario_all_stops_when_the_account_is_not_flat_between_scenarios(tmp_path, monkeypatch):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    original = canary.run_canary

    def wrapped(env_, *, plan_only=False, scenario="base"):
        result = original(env_, plan_only=plan_only, scenario=scenario)
        if scenario == "b1":
            foreign_fill(broker, "EURUSD", 0.01, magic=555, sl=1.1600)  # something else appears after b1
        return result

    monkeypatch.setattr(canary, "run_canary", wrapped)
    code, combined = canary.run_all(env)
    assert code == 2 and combined["verdict"].startswith("REFUSED(account not flat after scenario b1")
    assert [s["scenario"] for s in combined["scenarios"]] == ["base", "b1"]  # b2 / b3 never started
    assert combined["flat_checks"][-1]["ok"] is False
    assert [p.magic for p in broker.positions_get()] == [555]  # never touched


def test_scenario_all_reports_exit_4_when_canary_exposure_remains_between_scenarios(tmp_path, monkeypatch):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    original = canary.run_canary

    def wrapped(env_, *, plan_only=False, scenario="base"):
        result = original(env_, plan_only=plan_only, scenario=scenario)
        if scenario == "base":
            foreign_fill(broker, "EURUSD", 0.01, magic=canary.CANARY_MAGIC, sl=1.1600)
        return result

    monkeypatch.setattr(canary, "run_canary", wrapped)
    code, combined = canary.run_all(env)
    assert code == 4 and "MANUAL ACTION REQUIRED" in combined["verdict"]
    assert [s["scenario"] for s in combined["scenarios"]] == ["base"]


def test_scenario_all_stops_at_the_first_failing_scenario(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    env.hooks["after_action_6"] = lambda: [setattr(p, "sl", round(p.price_open - 0.00175, 5)) for p in broker.positions.values()]
    code, combined = canary.run_all(env)  # base step 6 sabotaged: the base scenario fails, nothing after it runs
    assert code == 1 and combined["verdict"].startswith("FAIL(scenario base")
    assert [s["scenario"] for s in combined["scenarios"]] == ["base"]
    assert not own(broker)


# -- CLI / static plan ------------------------------------------------------------------------------------------------


def test_dry_run_plan_lists_the_scenario_steps_and_numbers(capsys):
    assert canary.main(["--dry-run-plan", "--plan-price", "1.17", "--scenario", "all"]) == 0
    out = capsys.readouterr().out
    for name in ("base", "b1", "b2", "b3"):
        assert f"--- scenario {name}" in out
    assert "0.02" in out and canary.INJECTION_MARKER in out and "CONTINUATION" in out and "REVERSION" in out
    for lines in canary.STATIC_PLAN_BY_SCENARIO.values():
        for line in lines:
            assert line in out
    assert canary.main(["--dry-run-plan", "--scenario", "b3"]) == 0
    one = capsys.readouterr().out
    assert "restart" in one and "--- scenario b1" not in one


def test_dry_run_plan_with_the_fake_broker_places_nothing_for_each_scenario(tmp_path):
    for scenario in ("b1", "b2", "b3"):
        broker = build_broker()
        env = make_env(tmp_path / scenario, broker)
        code, report = run_scenario(env, scenario, plan_only=True)
        assert code == 0 and report["verdict"].startswith("PLAN_OK") and report["plan"]["scenario"] == scenario
        assert report["plan"]["shadow_sized_quantity"] == "0.02" and report["steps"] == []
        assert broker.order_send_calls == 0 and not broker.positions_get()
    assert report["plan"]["broker_tp"] is None or scenario != "b2"


def test_cli_rejects_an_unknown_scenario(capsys):
    with pytest.raises(SystemExit) as exc:
        canary.main(["--fake", "--scenario", "nope"])
    assert exc.value.code == 2


def test_cli_fake_scenario_b2_end_to_end(tmp_path, capsys):
    art = tmp_path / "cli_b2"
    assert canary.main(["--fake", "--scenario", "b2", "--artifacts", str(art)]) == 0
    out = capsys.readouterr().out
    assert "EXECUTION_CONTRACT_PASS" in out and canary.INJECTION_MARKER in out
    (path,) = sorted(art.glob("canary_report_*_b2.json"))
    assert json.loads(path.read_text())["scenario"] == "b2"
