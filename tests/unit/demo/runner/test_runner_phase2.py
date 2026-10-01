# ruff: noqa: E501
"""Lane M2: the runner with the Phase-2 markets (BRENT / BTCUSD): opt-in wiring, per-market disable in the heartbeat,
closed-market idling on the probe-derived calendars, and per-market funnel / TCA / report rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from demo import report
from demo import runner as rn
from demo.execution.events import Accepted, Fill, ProtectionConfirmed
from demo.runner import DemoRunner, RunnerConfig
from demo.store import DemoStore
from demo.testing import T0, FakeClock, FakeStack, ScriptedEngine, make_pair

CORE = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD")


def _factory(record):
    def make(*, dry_run, state_dir, markets):
        record.append(tuple(markets))
        return FakeStack(FakeClock(T0 + timedelta(seconds=10)), markets=tuple(markets), shadow=dry_run)

    return make


# ------------------------------------------------------------------------------- opt-in wiring / double gate
def _flags_off(monkeypatch):
    import markets.phase2 as p2

    monkeypatch.setattr(p2, "load_enablement", lambda config_dir=None: {"BRENT": False, "BTCUSD": False})


def test_default_runner_with_the_flags_off_is_the_five_markets_on_the_frozen_v1_spec(tmp_path, monkeypatch):
    _flags_off(monkeypatch)  # the safe fallback: both flags off -> five core markets, v1 bit-identical
    seen: list = []
    r = rn.build_live_runner("shadow", artifacts_dir=tmp_path / "a", stack_factory=_factory(seen), learning=False)
    try:
        assert set(r.cfg.markets) == set(CORE) and set(seen[0]) == set(CORE)
        assert r.engine.strategy_hash == "c3eae99e782888ac"  # v1, bit-identical
    finally:
        r.store.close()


def test_default_runner_with_the_committed_production_enablement_is_the_seven_markets_on_spec_v1_2(tmp_path):
    seen: list = []
    r = rn.build_live_runner("shadow", artifacts_dir=tmp_path / "a", stack_factory=_factory(seen), learning=False)
    try:
        assert set(r.cfg.markets) == set(CORE) | {"BTCUSD", "BRENT"} and set(seen[0]) >= set(CORE)
        assert r.engine.strategy_hash == "70e323157664552d"  # v1.2 (all four STRUCT variants ACTIVE_DISCOVERY_ELIGIBLE)
    finally:
        r.store.close()


def test_enabled_phase2_market_extends_the_universe_with_the_v1_2_superset(tmp_path):
    seen: list = []
    r = rn.build_live_runner("shadow", artifacts_dir=tmp_path / "a", stack_factory=_factory(seen), learning=False,
                             phase2_markets=("BTCUSD",))
    try:
        assert set(r.cfg.markets) == set(CORE) | {"BTCUSD"} and "BRENT" not in r.cfg.markets  # per-market switch
        assert r.engine.strategy_hash == "70e323157664552d"  # Lane F: v1.2 supersedes v1.1 in the selector
    finally:
        r.store.close()


def test_explicit_market_list_cannot_bypass_the_enablement_flag(tmp_path, monkeypatch):
    _flags_off(monkeypatch)
    with pytest.raises(rn.LiveStackRefused, match="not enabled"):
        rn.build_live_runner("shadow", artifacts_dir=tmp_path / "a", stack_factory=_factory([]), markets=("GER40", "BRENT"))
    with pytest.raises(rn.LiveStackRefused, match="unknown Phase-2"):
        rn.build_live_runner("shadow", artifacts_dir=tmp_path / "b", stack_factory=_factory([]), phase2_markets=("DOGEUSD",))


def test_enablement_flag_file_drives_the_default(tmp_path, monkeypatch):
    import markets.phase2 as p2

    monkeypatch.setattr(p2, "load_enablement", lambda config_dir=None: {"BRENT": True, "BTCUSD": False})
    seen: list = []
    r = rn.build_live_runner("shadow", artifacts_dir=tmp_path / "a", stack_factory=_factory(seen), learning=False)
    try:
        assert set(r.cfg.markets) == set(CORE) | {"BRENT"}
    finally:
        r.store.close()


# ------------------------------------------------------------------------------- per-market disable in the heartbeat
def _rig(tmp_path, start, markets, **cfg):
    clock = FakeClock(start)
    store = DemoStore(tmp_path / "w.sqlite", clock=lambda: clock().isoformat())
    stack = FakeStack(clock, markets=markets)
    stack.bar_source.n = 200
    eng = ScriptedEngine()
    kw = dict(mode="demo-auto", markets=tuple(markets), artifacts_dir=tmp_path / "a", poll_interval_s=5.0,
              min_disk_free_bytes=1, all_stale_grace_s=900.0, all_stale_exit_s=7200.0)
    kw.update(cfg)
    r = DemoRunner(stack, eng, store, config=RunnerConfig(**kw), clock=clock, sleep=lambda s: None, disk_free=lambda: 10**12)
    return clock, store, stack, eng, r


def test_stack_disabled_market_lands_in_the_heartbeat_and_is_never_scanned(tmp_path):
    start = datetime(2026, 9, 30, 10, 0, 10, tzinfo=UTC)  # Wednesday: both markets inside their calendars
    _clock, store, stack, eng, r = _rig(tmp_path, start, ("GER40", "BRENT", "BTCUSD"))
    stack.disabled_markets = {"BRENT": "preflight_red: quote_fresh: FAIL - quote age 500s > 30s while the calendar says OPEN"}
    r.start()
    r.run_cycle()
    hb = json.loads((tmp_path / "a" / "heartbeat.json").read_text(encoding="utf-8"))
    assert list(hb["disabled_markets"]) == ["BRENT"] and "stack_preflight" in hb["disabled_markets"]["BRENT"]
    assert {m for m, _ in eng.calls} == {"GER40", "BTCUSD"}  # the disabled market is not evaluated, the others are
    assert r.fail_reason is None and hb["market_state"]["BTCUSD"] in ("FRESH", "OPEN_OUT_OF_SESSION")
    store.close()


def test_runner_fails_closed_only_when_every_market_is_disabled(tmp_path):
    start = datetime(2026, 9, 30, 10, 0, 10, tzinfo=UTC)
    _clock, store, stack, _eng, r = _rig(tmp_path, start, ("BRENT", "BTCUSD"))
    stack.disabled_markets = {"BRENT": "x", "BTCUSD": "y"}
    r.start()
    assert r.fail_reason is not None and "all markets disabled" in r.fail_reason
    store.close()


# ------------------------------------------------------------------------------- closed markets are idle
def test_brent_and_btc_weekend_break_is_idle_not_a_fault_and_resumes_monday(tmp_path):
    fri_close = datetime(2026, 10, 2, 20, 30, tzinfo=UTC)  # after BTC cash close and after Brent London close
    mon_open = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    clock, store, stack, _eng, r = _rig(tmp_path, fri_close - timedelta(hours=10), ("BRENT", "BTCUSD"))
    r.start()
    r.run_cycle()
    stack.bar_source.frozen.update({"BRENT": fri_close, "BTCUSD": fri_close})  # broker feed stops at the break
    clock.now = fri_close + timedelta(minutes=30)
    for _ in range(14):
        clock.advance(hours=4)
        r.run_cycle()
        assert r.fail_reason is None and r.halt_reason is None
        st = r.status()
        assert st["market_state"]["BRENT"] == "CLOSED_IDLE" and st["market_state"]["BTCUSD"] == "CLOSED_IDLE"
        if clock.now >= mon_open - timedelta(hours=4):
            break
    assert st["idle_all_markets_closed"] is True and stack.submits == []
    stack.bar_source.frozen.clear()
    clock.now = mon_open + timedelta(minutes=12, seconds=10)
    r.run_cycle()
    assert r.fail_reason is None and not r._idle_all
    assert r._market_state["BTCUSD"] == "FRESH"
    store.close()


# ------------------------------------------------------------------------------- funnel / store / TCA / report rows
def _scripted_fill(intent, px):
    return [
        Accepted(intent.intent_id, Decimal("0.2"), Decimal("10000"), Decimal("0.01"), Decimal("100"), Decimal("2")),
        Fill(intent.intent_id, Decimal(px), Decimal("0.2"), Decimal("70"), Decimal("0"), Decimal("-1"), Decimal("0"),
             "ord-1", "pos-1", reference_price=Decimal("83746.28")),
        ProtectionConfirmed(intent.intent_id, "pos-1", Decimal(str(intent.stop)), Decimal(str(intent.target))),
    ]


def test_new_market_rows_reach_store_funnel_tca_and_report(tmp_path):
    start = datetime(2026, 9, 30, 10, 0, 10, tzinfo=UTC)
    clock, store, stack, eng, r = _rig(tmp_path, start, ("GER40", "BTCUSD"))
    snap, dec, intent = make_pair(market="BTCUSD", entry=83746.28, risk=600.0, signal_ts=start - timedelta(seconds=10))
    rej = make_pair(market="BTCUSD", entry=83746.28, risk=600.0, accepted=False, tag="b", signal_ts=start - timedelta(seconds=10))
    stack.script[intent.intent_id] = _scripted_fill(intent, "83816.28")
    eng.push("BTCUSD", (snap, dec, intent), (rej[0], rej[1], None))
    r.start()
    r.run_cycle()
    assert store.get_tca(intent.intent_id, "ENTRY")["actual_fill_price"] == pytest.approx(83816.28)
    assert {d.opportunity_id for d in store.list_decisions()} >= {snap.opportunity_id, rej[0].opportunity_id}
    stack.close_position(intent.intent_id, reason="TARGET", exit_price=84946.28)
    clock.advance(minutes=35)
    r.run_cycle()
    rep = report.build_report(store, "DISCOVERY")
    by_market = rep["breakdowns"]["market"]
    assert "BTCUSD" in by_market and by_market["BTCUSD"]["n"] == 1
    hb = json.loads((tmp_path / "a" / "heartbeat.json").read_text(encoding="utf-8"))
    assert "BTCUSD" in hb["market_state"]
    funnel = r.funnel_summary(clock.now, max_age_s=0)
    assert funnel is not None
    store.close()
