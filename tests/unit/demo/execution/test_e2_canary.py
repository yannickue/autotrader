# ruff: noqa: E501
"""E2 execution-contract canary (scripts/e2_broker_canary.py) against the netting-account FAKE broker.

Zero real MT5.  Covers: the full PASS path, every assertion step failing correctly when sabotaged (with the
finally-flatten still executed), the refusal conditions, the report schema / no secrets, and timeout -> flatten.
"""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from scripts import e2_broker_canary as canary
from tests.unit.demo.execution.stack_harness import build_broker, connection

D = Decimal
FAST_OVERRIDES = dict(
    sync_interval_s=0.05, lock_heartbeat_s=0.2, bar_min_refetch_s=0.0, reconcile_retry_s=0.0,
    disconnect_grace_s=0.5, close_grace_s=0.5, start_timeout_s=30.0, submit_wait_s=8.0, exposure_timeout_s=6.0,
    flatten_wait_s=8.0,
)


def make_env(tmp_path: Path, broker=None, **over) -> canary.CanaryEnv:
    broker = broker or build_broker()
    art = tmp_path / "artifacts"
    values = dict(
        mode="fake", client=broker, connection=connection(broker), artifacts_dir=art,
        trader_artifacts_dir=tmp_path / "trader", mt5_lock_path=tmp_path / "terminal.lock",
        expected_account_hash=canary.login_hash(broker.cfg.login), expected_server=None, market="EURUSD",
        timeout_s=90.0, stack_overrides=dict(FAST_OVERRIDES), login_env=None, log=lambda _m: None,
    )
    values.update(over)
    return canary.CanaryEnv(**values)


def foreign_fill(broker, symbol: str, volume: float, *, magic: int, sl: float, buy: bool = True) -> None:
    """A deal nobody requested (manual trade / another strategy) on ``symbol``."""
    from nautilus_mt5.constants import Filling, TradeAction

    bid, ask = broker._bidask(symbol)
    price = ask if buy else bid
    broker.fill_plan.insert(0, [(volume, price)])
    broker._execute_deal({
        "symbol": symbol, "type": 0 if buy else 1, "volume": volume, "comment": "foreign", "magic": magic, "sl": sl, "tp": 0.0,
        "type_filling": int(Filling.IOC), "action": TradeAction.DEAL,
    })


def run(env: canary.CanaryEnv, **kw) -> tuple[int, dict]:
    """Run the canary and return the report AS WRITTEN TO DISK (what the lead / CI will read)."""
    code, _ = canary.run_canary(env, **kw)
    return code, load(env.artifacts_dir)


def own(broker):
    return [p for p in broker.positions_get() if p.magic == canary.CANARY_MAGIC]


def load(report_dir: Path) -> dict:
    (path,) = sorted(report_dir.glob("canary_report_*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def status_of(report: dict) -> list[str]:
    return [s["status"] for s in report["steps"]]


# -- full PASS ---------------------------------------------------------------------------------------------------


def test_full_pass_on_the_fake_broker(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    code, report = run(env)
    assert code == 0 and report["verdict"] == "EXECUTION_CONTRACT_PASS", report["verdict"]
    assert status_of(report) == ["PASS"] * 7 + ["NOT_APPLICABLE", "PASS", "PASS"]
    assert not broker.positions_get() and not broker.orders_get()
    assert report["final_flatten"]["residual_canary_positions"] == []
    # everything carried the canary magic, 2 x min lot, a stop, no TP
    sends = [r for r in broker.request_log if r.get("action") == 1 and not r.get("position")]
    assert sends and all(r["magic"] == canary.CANARY_MAGIC for r in broker.request_log if "magic" in r)
    assert report["plan"]["total_lots"] == "0.02" and report["plan"]["partial_lots"] == "0.01"
    assert report["plan"]["broker_tp"] is None
    # the report has the broker snapshots, latencies and execution quality
    assert report["steps"][2]["before"]["positions"][0]["volume"] == 0.02
    assert report["steps"][2]["after"]["positions"][0]["volume"] == 0.01
    assert report["steps"][5]["after"]["positions"][0]["sl"] > report["steps"][5]["before"]["positions"][0]["sl"]
    for key in ("order_fill_ms", "partial_1_ms", "modify_ms", "final_close_ms"):
        assert report["latencies_ms"][key] is not None and report["latencies_ms"][key] >= 0
    entry = report["execution_quality"]["entry"]
    assert {"slippage_adverse_positive", "spread", "bid_at_send", "ask_at_send"} <= set(entry)
    assert report["execution_quality"]["partial_1"]["slippage_adverse_positive"] is not None
    assert report["restart"]["reconciliation"] == "RECONCILED" and report["restart"]["registry_status"] == "CLOSED"
    assert report["registry_status_after_close"] == "CLOSED"
    # EXECUTION_CANARY accounting record is produced (record-canary compatible)
    (trade_file,) = sorted(env.artifacts_dir.glob("canary_trade_*.json"))
    trade = json.loads(trade_file.read_text())
    assert trade["trade_type"] == "EXECUTION_CANARY" and trade["magic"] == canary.CANARY_MAGIC


def test_report_schema_and_no_secrets(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    run(env)
    raw = next(env.artifacts_dir.glob("canary_report_*.json")).read_text(encoding="utf-8")
    report = json.loads(raw)
    assert {"schema", "generated_utc", "mode", "market", "preflight", "plan", "steps", "latencies_ms", "execution_quality",
            "final_flatten", "verdict", "exit_code", "canary_magic", "timeout_s"} <= set(report)
    assert report["schema"] == "e2_canary_report/1" and report["demo_only"] is True
    assert [s["n"] for s in report["steps"]] == list(range(1, 11))
    for s in report["steps"]:
        assert {"n", "name", "status", "detail", "before", "after", "duration_ms", "data"} <= set(s)
    assert "password" not in raw.lower()
    keys, values = set(), set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                keys.add(k.lower())
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        else:
            values.add(str(o))

    walk(report)
    assert not [k for k in keys if any(w in k for w in ("password", "token", "secret", "login", "api_key"))]
    assert str(broker.cfg.login) not in values  # only the hashed account id appears
    assert report["account_id_hash"] == canary.login_hash(broker.cfg.login)


# -- sabotage: every step fails correctly and the finally-flatten still runs --------------------------------------


def assert_failed_at(report: dict, step: int, broker) -> None:
    assert report["verdict"].startswith(f"FAIL(step {step}"), report["verdict"]
    assert report["steps"][step - 1]["status"] == "FAIL"
    assert all(s["status"] == "SKIPPED" for s in report["steps"][step:])
    assert not own(broker), "the finally-flatten left the canary position open"
    assert report["final_flatten"]["residual_canary_positions"] == []


def test_step1_partial_entry_fill_fails_and_is_flattened(tmp_path):
    broker = build_broker()
    broker.fill_plan = [[(0.01, 1.17005)]]  # the broker fills only half of the 0.02
    env = make_env(tmp_path, broker)
    code, report = run(env)
    assert code == 1
    assert_failed_at(report, 1, broker)


def test_step2_missing_stop_is_detected(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def strip_sl():
        for p in broker.positions.values():
            p.sl = 0.0

    env.hooks["after_action_2"] = strip_sl
    code, report = run(env)
    assert code == 1
    assert_failed_at(report, 2, broker)
    assert "stop-loss missing" in report["steps"][1]["detail"]


def test_step3_rejected_partial_fails_and_flatten_still_closes(tmp_path):
    broker = build_broker()
    real_send = broker.order_send
    state = {"n": 0}

    def send(request):
        # reject only the FIRST reduce-only close (volume 0.01 on a 0.02 position); the safety flatten must still work
        if request.get("position") and request.get("volume") == 0.01 and state["n"] == 0:
            state["n"] += 1
            return SimpleNamespace(retcode=10006, comment="Request rejected", order=0, deal=0, volume=0.0, price=0.0, request_id=0)
        return real_send(request)

    broker.order_send = send
    env = make_env(tmp_path, broker)
    code, report = run(env)
    assert code == 1
    assert_failed_at(report, 3, broker)


def test_step4_broker_local_volume_mismatch_is_detected(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def shrink():  # the broker still shows the original volume (stale)
        for p in broker.positions.values():
            p.volume = 0.02

    env.hooks["after_action_4"] = shrink
    code, report = run(env)
    assert code == 1
    assert_failed_at(report, 4, broker)


def test_step5_stale_or_missing_protection_after_partial_is_detected(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def strip_sl():
        for p in broker.positions.values():
            p.sl = 0.0

    env.hooks["after_action_5"] = strip_sl
    code, report = run(env)
    assert code == 1
    assert_failed_at(report, 5, broker)


def test_step6_stop_that_does_not_tighten_is_detected(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def revert_sl():  # the broker "forgets" the modification
        for p in broker.positions.values():
            p.sl = round(p.price_open - 0.0020, 5)

    env.hooks["after_action_6"] = revert_sl
    code, report = run(env)
    assert code == 1
    assert_failed_at(report, 6, broker)


def test_step7_loosening_not_refused_is_detected(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def loosen_at_broker():  # a broker/adapter that accepted the widening
        for p in broker.positions.values():
            p.sl = round(p.sl - 0.0010, 5)

    env.hooks["after_action_7"] = loosen_at_broker
    code, report = run(env)
    assert code == 1
    assert_failed_at(report, 7, broker)
    assert "changed by a refused widening" in report["steps"][6]["detail"]


def test_step9_position_not_flat_is_detected_and_flattened(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    sent = {"done": False}

    def reopen():  # an opposite / leftover exposure appears after the final close
        if not sent["done"]:
            sent["done"] = True
            foreign_fill(broker, "EURUSD", 0.01, magic=canary.CANARY_MAGIC, sl=1.1600)

    env.hooks["after_action_9"] = reopen
    code, report = run(env)
    assert code == 1
    assert_failed_at(report, 9, broker)


def test_step10_restart_sees_a_mismatch(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def foreign_position():  # something else is at the broker when the new stack attaches
        foreign_fill(broker, "EURUSD", 0.01, magic=555, sl=1.1600)

    env.hooks["before_restart"] = foreign_position
    code, report = run(env)
    assert code in (1, 4)
    assert report["steps"][9]["status"] == "FAIL"
    assert report["verdict"].startswith("FAIL(step 10") or report["exit_code"] == 4
    assert not own(broker)  # the canary's own position is long gone; the foreign one is never touched
    assert any(p.magic == 555 for p in broker.positions_get())


def test_unexpected_exception_in_a_step_is_a_fail_and_flattens(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)

    def boom():
        raise RuntimeError("synthetic failure after the entry")

    env.hooks["after_action_1"] = boom
    code, report = run(env)
    assert code == 1
    assert_failed_at(report, 1, broker)
    assert "synthetic failure" in report["steps"][0]["detail"]


# -- timeout ------------------------------------------------------------------------------------------------------


def test_hard_timeout_triggers_the_flatten(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker, timeout_s=6.0)

    def stall():
        time.sleep(12.0)  # the sequence hangs (e.g. a stuck broker call) while the position is open

    env.hooks["after_action_3"] = stall
    t0 = time.monotonic()
    code, report = run(env)
    assert code == 1 and "timeout" in report["verdict"].lower() + " ".join(s["detail"] for s in report["steps"]).lower()
    assert not own(broker), "timeout did not flatten"
    assert report["final_flatten"]["residual_canary_positions"] == []
    assert time.monotonic() - t0 < 60


# -- refusals -----------------------------------------------------------------------------------------------------


def refused(code, report, fragment: str) -> None:
    assert code == 2 and report["verdict"].startswith("REFUSED"), report["verdict"]
    assert fragment in report["verdict"], report["verdict"]


def test_refuses_non_demo_account(tmp_path):
    broker = build_broker(trade_mode=2)
    env = make_env(tmp_path, broker)
    code, report = run(env)
    refused(code, report, "non_demo_account")
    assert broker.order_send_calls == 0


def test_refuses_wrong_account_hash(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker, expected_account_hash="deadbeefdeadbeef")
    code, report = run(env)
    refused(code, report, "account_hash_matches_trader")
    assert broker.order_send_calls == 0


def test_refuses_unknown_account_binding(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker, expected_account_hash=None)
    code, report = run(env)
    refused(code, report, "expected_account_hash_known")
    assert broker.order_send_calls == 0


def test_refuses_when_a_runner_lock_is_alive(tmp_path):
    trader = tmp_path / "trader"
    trader.mkdir()
    (trader / "runner.lock").write_text(json.dumps({"pid": os.getpid(), "create_time": None, "role": "runner:demo-auto"}))
    broker = build_broker()
    code, report = run(make_env(tmp_path, broker))
    refused(code, report, "no_runner_alive")
    assert broker.order_send_calls == 0 and broker.calls == []


def test_refuses_on_a_fresh_trader_heartbeat(tmp_path):
    from datetime import UTC, datetime

    trader = tmp_path / "trader"
    trader.mkdir()
    (trader / "heartbeat.json").write_text(json.dumps({"process_alive": True, "updated_utc": datetime.now(UTC).isoformat()}))
    broker = build_broker()
    code, report = run(make_env(tmp_path, broker))
    refused(code, report, "no_fresh_trader_heartbeat")
    assert broker.calls == []


def test_refuses_when_the_mt5_terminal_lock_is_busy(tmp_path):
    (tmp_path / "terminal.lock").write_text("12345")
    broker = build_broker()
    code, report = run(make_env(tmp_path, broker))
    refused(code, report, "mt5_terminal_lock_free")
    assert broker.calls == []


def test_refuses_a_preexisting_position_on_the_symbol(tmp_path):
    broker = build_broker()
    foreign_fill(broker, "EURUSD", 0.01, magic=555, sl=1.1600)
    code, report = run(make_env(tmp_path, broker))
    refused(code, report, "no_position_on_symbol")
    assert broker.order_send_calls == 0
    assert [p.magic for p in broker.positions_get()] == [555]  # never touched


def test_refuses_a_preexisting_foreign_position_elsewhere(tmp_path):
    broker = build_broker()
    foreign_fill(broker, "Ger40", 0.25, magic=740_003, sl=24000.0)
    code, report = run(make_env(tmp_path, broker))
    refused(code, report, "account_flat_no_orders")
    assert broker.order_send_calls == 0


def test_refuses_when_the_market_is_closed(tmp_path):
    broker = build_broker()
    broker.market_open = False
    code, report = run(make_env(tmp_path, broker))
    assert code == 2 and report["verdict"].startswith("REFUSED") and "shadow_order_check_ok" in report["verdict"]
    assert broker.order_send_calls == 0 and not broker.positions_get()


def test_refuses_a_stale_quote(tmp_path):
    broker = build_broker()
    broker.live_offset_s = None  # the server clock stops following the wall clock => the tick is old
    code, report = run(make_env(tmp_path, broker))
    assert code == 2 and report["verdict"].startswith("REFUSED")
    assert broker.order_send_calls == 0


def test_refuses_the_login_environment_variable(tmp_path, capsys):
    broker = build_broker()
    code, report = run(make_env(tmp_path, broker, login_env="1"))
    refused(code, report, "no_account_login_env")
    assert broker.calls == []
    os.environ["MT5_ALLOW_ACCOUNT_LOGIN"] = "1"
    try:
        assert canary.main(["--fake"]) == 2
        assert "MT5_ALLOW_ACCOUNT_LOGIN" in capsys.readouterr().err
    finally:
        del os.environ["MT5_ALLOW_ACCOUNT_LOGIN"]


def test_live_requires_the_exact_confirmation_phrase(tmp_path, capsys):
    assert canary.main(["--live"]) == 2
    assert canary.CONFIRM_VALUE in capsys.readouterr().err
    assert canary.main(["--live", "--confirm-demo-canary=nope"]) == 2
    env = make_env(tmp_path, mode="live", confirm=None, artifacts_dir=canary.REQUIRED_ARTIFACTS)
    checks = canary.local_preflight(env)
    assert any(c["name"] == "confirm_flag" and not c["ok"] for c in checks)


def test_live_artifacts_dir_is_pinned(tmp_path):
    env = make_env(tmp_path, mode="live", confirm=canary.CONFIRM_VALUE)  # artifacts under tmp_path, not artifacts/e2_canary
    checks = canary.local_preflight(env)
    assert any(c["name"] == "artifacts_dir" and not c["ok"] for c in checks)


# -- plan mode / helpers -------------------------------------------------------------------------------------------


def test_dry_run_plan_alone_is_static_and_touches_no_broker(capsys):
    assert canary.main(["--dry-run-plan", "--plan-price", "1.17"]) == 0
    out = capsys.readouterr().out
    assert "STATIC PLAN" in out and "0.02" in out
    for n in range(1, 11):
        assert f"  {n} " in out or f"  {n:<2}" in out


def test_dry_run_plan_with_the_fake_broker_places_nothing(tmp_path):
    broker = build_broker()
    env = make_env(tmp_path, broker)
    code, report = run(env, plan_only=True)
    assert code == 0 and report["verdict"].startswith("PLAN_OK")
    assert report["plan"]["shadow_sized_quantity"] == "0.02"
    assert broker.order_send_calls == 0 and not broker.positions_get()
    assert report["steps"] == []


def test_geometry_and_tighten_helpers():
    geo = canary.compute_geometry(bid=D("1.16995"), ask=D("1.17005"), tick=D("0.00001"), point=D("0.00001"), stops_level_pts=10)
    assert geo["stop_distance"] >= max(10 * D("0.0001"), D("0.0001") * 3, D("0.0015") * D("1.17005")) - D("0.00001")
    assert geo["initial_stop"] < D("1.17005")
    new = canary.compute_tighten_stop(
        bid=D("1.16995"), current_stop=geo["initial_stop"], initial_distance=geo["stop_distance"], tick=D("0.00001"),
        point=D("0.00001"), stops_level_pts=10, freeze_level_pts=0, spread=D("0.0001"),
    )
    assert new is not None and geo["initial_stop"] < new < D("1.16995") - D("0.0001")
    assert canary.compute_tighten_stop(  # market fell through: no valid tighter level
        bid=geo["initial_stop"] + D("0.0002"), current_stop=geo["initial_stop"], initial_distance=geo["stop_distance"],
        tick=D("0.00001"), point=D("0.00001"), stops_level_pts=10, freeze_level_pts=0, spread=D("0.0001"),
    ) is None


def test_scrub_removes_secret_looking_keys():
    cleaned = canary._scrub({"a": 1, "password": "x", "nested": [{"token": "t", "ok": 2}], "login": 5})
    assert cleaned == {"a": 1, "nested": [{"ok": 2}]}


def test_live_plan_only_needs_no_confirmation_phrase(tmp_path):
    env = make_env(tmp_path, mode="live", confirm=None, artifacts_dir=canary.REQUIRED_ARTIFACTS)
    names = {c["name"] for c in canary.local_preflight(env, plan_only=True)}
    assert "confirm_flag" not in names  # nothing can be sent in a plan-only run
    assert "confirm_flag" in {c["name"] for c in canary.local_preflight(env)}


def test_cli_fake_end_to_end(tmp_path, capsys):
    art = tmp_path / "cli_art"
    assert canary.main(["--fake", "--artifacts", str(art)]) == 0
    out = capsys.readouterr().out
    assert "EXECUTION_CONTRACT_PASS" in out and "step  1" in out and "step 10" in out
    assert load(art)["verdict"] == "EXECUTION_CONTRACT_PASS"
