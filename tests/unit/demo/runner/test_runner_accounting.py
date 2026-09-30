# ruff: noqa: E501
"""Accounting: censored (manual / external / emergency) exits, canary import, account separation."""

from __future__ import annotations

import json
import sqlite3

import pytest

from demo import report
from demo.external import ExternalTrade, import_external_trade
from demo.funnel import funnel
from demo.store import AccountMismatch, DemoStore
from demo.testing import make_pair


def _trade_and_close(env, reason, tag="a", **close_kw):
    snap, dec, intent = make_pair(tag=tag)
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    env.stack.close_position(intent.intent_id, reason=reason, **close_kw)
    env.clock.advance(minutes=35)
    r.run_cycle()
    return r, snap, intent


@pytest.mark.parametrize("reason", ["MANUAL", "EXTERNAL", "SAFETY_FLATTEN", "SOMETHING_NEW"])
def test_manual_external_and_unknown_exits_are_censored_and_excluded(env, reason):
    r, _snap, intent = _trade_and_close(env, reason)
    st = env.store
    assert st.get_outcome(intent.intent_id) is not None  # recorded ...
    tag = st.get_trade_tag(intent.intent_id)
    assert tag["trade_type"] == "STRATEGY" and tag["censored"] is True
    assert st.list_outcomes("DISCOVERY") == [] and st.count_trades("DISCOVERY") == 0  # ... but not a strategy trade
    assert len(st.list_outcomes("DISCOVERY", kind="censored")) == 1
    assert r.status()["cumulative_r"] == 0.0 and r.status()["learning_samples"] == 0
    rep = report.build_report(st, "DISCOVERY")
    assert rep["metrics"]["trades"] == 0 and rep["metrics"]["expected_r"] is None
    assert rep["censored_exits"]["n"] == 1 and rep["censored_exits"]["exit_reasons"] == {reason: 1}
    assert rep["censored_exits"]["avg_holding_s"] is not None
    md = report.render_markdown(rep)
    assert "Censored exits" in md and "DEMO_ALPHA_RESULT != LIVE_EXECUTION_PROOF" in md


@pytest.mark.parametrize("reason", ["STOP", "TARGET", "SESSION_END"])
def test_strategy_exits_count(env, reason):
    r, _snap, intent = _trade_and_close(env, reason)
    st = env.store
    assert st.get_trade_tag(intent.intent_id)["censored"] is False
    assert len(st.list_outcomes("DISCOVERY")) == 1 and st.count_trades("DISCOVERY") == 1
    assert report.build_report(st, "DISCOVERY")["censored_exits"]["n"] == 0
    assert r.status()["learning_samples"] == 1


def test_learning_dataset_excludes_censored_trades(env):
    pytest.importorskip("numpy")
    from demo.learning.dataset import build_dataset

    _trade_and_close(env, "MANUAL")
    ds = build_dataset(env.store, phase="DISCOVERY")
    assert len(ds) == 0


def _canary(**kw):
    base = dict(position_id="9001", market="GER40", broker_symbol="Ger40", direction=1, volume=0.1, entry_price=100.0,
                exit_price=101.0, open_time_utc="2026-10-01T09:30:00+00:00", close_time_utc="2026-10-01T09:40:00+00:00",
                profit_eur=10.0, commission_eur=-1.0, swap_eur=0.0, magic=740001, comment="CANARY")
    base.update(kw)
    return ExternalTrade(**base)


def test_import_external_trade_reconciles_pnl_but_never_alpha(env):
    st = env.store
    iid = import_external_trade(st, _canary())
    assert import_external_trade(st, _canary()) == iid  # idempotent
    assert st.get_state(iid) == "CLOSED" and st.get_outcome(iid).pnl_eur == pytest.approx(9.0)
    tag = st.get_trade_tag(iid)
    assert tag["trade_type"] == "EXECUTION_CANARY" and tag["censored"] is True
    assert st.list_outcomes("DISCOVERY") == [] and st.count_trades("DISCOVERY") == 0
    assert len(st.list_outcomes("DISCOVERY", kind="canary")) == 1
    rep = report.build_report(st, "DISCOVERY")
    assert rep["metrics"]["trades"] == 0 and rep["canary_trades"]["n"] == 1
    assert rep["account_pnl_reconciliation"]["canary_eur"] == pytest.approx(9.0)
    assert rep["account_pnl_reconciliation"]["total_closed_eur"] == pytest.approx(9.0)
    fun = funnel(st, None, "DISCOVERY")
    assert fun["summary"]["opportunities"] == 0  # canary never enters the opportunity funnel
    assert st.non_traded_unlabelled("DISCOVERY") == []
    with pytest.raises(ValueError):
        import_external_trade(st, _canary(position_id="9002", trade_type="STRATEGY"))


def test_import_external_trade_with_stop_defines_r_and_test_trade_type(env):
    iid = import_external_trade(env.store, _canary(position_id="9003", stop=99.0, trade_type="TEST_TRADE"))
    out = env.store.get_outcome(iid)
    assert out.gross_r == pytest.approx(1.0) and env.store.get_trade_tag(iid)["trade_type"] == "TEST_TRADE"


def test_cli_record_canary_imports_without_touching_the_broker(env, tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys
    from pathlib import Path

    spec_path = Path(__file__).resolve().parents[4] / "scripts" / "demo_trader.py"
    spec = importlib.util.spec_from_file_location("demo_trader_cli", spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["demo_trader_cli"] = mod
    spec.loader.exec_module(mod)
    f = tmp_path / "canary.json"
    f.write_text(json.dumps([{
        "position_id": "1", "market": "GER40", "broker_symbol": "Ger40", "direction": -1, "volume": 0.1,
        "entry_price": 100.0, "exit_price": 99.0, "open_time_utc": "2026-10-01T09:30:00+00:00",
        "close_time_utc": "2026-10-01T09:40:00+00:00", "profit_eur": 10.0, "commission_eur": -1.0}]))
    db = tmp_path / "cli.sqlite"
    DemoStore(db).close()
    monkeypatch.setenv("MT5_ALLOW_ACCOUNT_LOGIN", "0")
    assert mod.main(["--record-canary", str(f), "--db", str(db), "--artifacts", str(tmp_path)]) == 0
    with DemoStore(db) as s:
        assert s.count_trades("DISCOVERY", kind="canary") == 1
    assert "imported" in capsys.readouterr().out


# ------------------------------------------------------------------------------ account separation
def test_first_attach_binds_hash_and_phase_and_reports_state_them(env):
    r = env.build(account_phase="SMALL_ACCOUNT_FEASIBILITY")
    r.start()
    r.run_cycle()
    assert env.store.account_info() == {"account_id_hash": "h", "account_phase": "SMALL_ACCOUNT_FEASIBILITY", "legacy_backfill": False}
    hb = r.status(env.clock())
    assert hb["account_phase"] == "SMALL_ACCOUNT_FEASIBILITY" and hb["account_id_hash"] == "h"
    assert hb["disclaimer"] == "DEMO_ALPHA_RESULT != LIVE_EXECUTION_PROOF"
    rep = report.build_report(env.store, "DISCOVERY")
    assert rep["account"]["account_phase"] == "SMALL_ACCOUNT_FEASIBILITY" and "LIVE_EXECUTION_PROOF" in rep["disclaimer"]
    # the artifacts dir remembers the phase: a later runner without --account-phase inherits it
    r2 = env.build()
    r2.start()
    assert r2.account_info["account_phase"] == "SMALL_ACCOUNT_FEASIBILITY" and r2.fail_reason is None


def test_default_account_phase(env):
    r = env.build()
    r.start()
    assert r.account_info["account_phase"] == "ALPHA_EXECUTION_DISCOVERY"


def test_store_of_another_account_is_refused_fail_closed_at_start(env):
    r1 = env.build()
    r1.start()
    env.stack.set_account(account_id_hash="other-account")
    r2 = env.build()
    r2.start()
    assert r2.fail_reason is not None and r2.fail_reason.startswith("account_mismatch")
    assert env.stack.submits == []
    assert r2.run(max_cycles=1) == 7


def test_artifacts_dir_of_another_account_is_refused_even_with_a_fresh_store(env, tmp_path):
    r1 = env.build()
    r1.start()  # writes artifacts/account_meta.json for account 'h'
    env.stack.set_account(account_id_hash="other-account")
    fresh = DemoStore(tmp_path / "fresh.sqlite", clock=lambda: env.clock().isoformat())
    r2 = env.build(store=fresh)
    r2.start()
    assert r2.fail_reason is not None and "artifacts dir belongs to account" in r2.fail_reason
    fresh.close()


def test_explicit_phase_that_differs_from_the_store_is_refused(env):
    env.build(account_phase="SMALL_ACCOUNT_FEASIBILITY").start()
    r2 = env.build(account_phase="ALPHA_EXECUTION_DISCOVERY")
    r2.start()
    assert r2.fail_reason is not None and "account_phase" in r2.fail_reason


def test_legacy_db_without_account_meta_is_recorded_not_refused(env, tmp_path):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    with env.store._tx() as c:  # simulate a DB written before this feature
        c.execute("DELETE FROM meta WHERE key IN ('account_id_hash','account_phase','account_bound_utc')")
    (tmp_path / "art" / "account_meta.json").unlink()
    r2 = env.build()
    r2.start()
    assert r2.fail_reason is None and env.store.account_info()["legacy_backfill"] is True
    assert any("legacy" in w for w in r2.status()["warnings"])


def test_old_database_without_new_tables_opens_and_works(tmp_path):
    p = tmp_path / "old.sqlite"
    DemoStore(p).close()
    con = sqlite3.connect(p)
    for t in ("bar_pointers", "trade_tags", "outcome_extra", "counterfactual_meta", "scan_errors"):
        con.execute(f"DROP TABLE {t}")
    con.commit()
    con.close()
    with DemoStore(p) as s:  # tables are re-created empty; legacy rows would be STRATEGY
        assert s.get_bar_pointer("GER40") is None and s.list_outcomes() == [] and s.count_trades() == 0
        assert s.bind_account("abc", "custom-phase")["status"] == "BOUND"
        with pytest.raises(AccountMismatch):
            s.bind_account("zzz")


def test_bar_pointer_is_monotonic_and_restart_safe(tmp_path):
    p = tmp_path / "bp.sqlite"
    with DemoStore(p) as s:
        assert s.set_bar_pointer("GER40", "2026-10-01T09:05:00+00:00") is True
        assert s.set_bar_pointer("GER40", "2026-10-01T09:00:00+00:00") is False  # never backwards
        assert s.set_bar_pointer("GER40", "2026-10-01T09:10:00+00:00") is True
    with DemoStore(p) as s:
        assert s.get_bar_pointer("GER40") == "2026-10-01T09:10:00+00:00"
        assert s.get_bar_pointer("GER40", "M1") is None
