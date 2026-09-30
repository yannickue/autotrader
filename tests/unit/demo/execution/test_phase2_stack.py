# ruff: noqa: E501
"""Lane M2: Brent + BTCUSD through the REAL Mt5DemoStack (Nautilus kernel + MT5 lane threads) against the fake broker.

Facts come from the live probe (docs/evidence/phase2_symbol_probe.json). The five core markets must behave
exactly as before when no Phase-2 market is opted in."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from demo.execution.events import Accepted, Fill, PositionClosed, ProtectionConfirmed, Rejected
from tests.unit.demo.execution.stack_harness import (
    PHASE2_QUOTES,
    add_phase2_symbols,
    berlin_offset_s,
    build_broker,
    make_intent,
    make_stack,
)

BOTH = ("BRENT", "BTCUSD")


def brent_intent(**kw):
    kw.setdefault("entry_ref", 97.78)
    kw.setdefault("stop", 96.78)  # structural stop 1.00 USD below the ask: never tightened by the stack
    kw.setdefault("target", 100.3)
    return make_intent(market="BRENT", broker_symbol="Brent", **kw)


def btc_intent(**kw):
    kw.setdefault("entry_ref", 83746.28)
    kw.setdefault("stop", 83146.28)
    kw.setdefault("target", 85000.0)
    return make_intent(market="BTCUSD", broker_symbol="BTCUSD", **kw)


@pytest.fixture
def make_env(tmp_path):
    stacks = []

    def build(extra=BOTH, *, now=None, **symbols):
        broker = build_broker()
        add_phase2_symbols(broker, **symbols)
        stack = make_stack(broker, tmp_path, extra_markets=extra, now=now)
        stacks.append(stack)
        return broker, stack

    yield build
    for s in stacks:
        s.stop()


def kinds(events):
    return [type(e).__name__ for e in events]


# ------------------------------------------------------------------------ five-market regression
def test_default_stack_is_the_five_markets_and_rejects_phase2_intents(make_env):
    broker, stack = make_env(extra=())
    stack.start()
    assert set(stack.markets) == {"GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD"}
    assert stack.disabled_markets == {}
    ev = stack.submit(brent_intent())
    assert kinds(ev) == ["Rejected"] and ev[0].reason == "unknown_market"
    assert "disabled_markets" in stack.account_snapshot().extra and stack.account_snapshot().extra["disabled_markets"] == {}
    assert broker.order_send_calls == 0


# ------------------------------------------------------------------------ enabled markets end to end
@pytest.mark.parametrize(
    "intent_fn,symbol,stop,target,exit_quote",
    [
        (brent_intent, "Brent", Decimal("96.78"), Decimal("100.3"), (96.5, 96.57)),
        (btc_intent, "BTCUSD", Decimal("83146.28"), Decimal("85000.0"), (82900.0, 82983.5)),
    ],
)
def test_phase2_market_entry_protection_and_stop_exit(make_env, intent_fn, symbol, stop, target, exit_quote):
    broker, stack = make_env()
    snap = stack.start()
    assert snap.reconciliation == "RECONCILED"
    assert stack.disabled_markets == {}
    assert set(stack.markets) >= {"BRENT", "BTCUSD", "GER40"}
    notes = snap.extra["start_notes"]["phase2_preflight"]
    assert notes["BRENT"]["verdict"] == "GREEN" and notes["BTCUSD"]["verdict"] == "GREEN"
    intent = intent_fn()
    events = stack.submit(intent)
    assert kinds(events) == ["Accepted", "Fill", "ProtectionConfirmed"], events
    accepted, fill, protection = events
    assert isinstance(accepted, Accepted) and isinstance(fill, Fill) and isinstance(protection, ProtectionConfirmed)
    request = broker.request_log[0]
    assert request["symbol"] == symbol
    assert Decimal(str(request["sl"])) == stop and Decimal(str(request["tp"])) == target  # structural stop as given
    assert protection.stop == stop
    assert fill.spread == Decimal(str(round(PHASE2_QUOTES[symbol][1] - PHASE2_QUOTES[symbol][0], 2)))  # TCA spread, USD
    assert fill.quantity == accepted.quantity and Decimal("0.01") <= fill.quantity
    assert accepted.leverage is not None and accepted.leverage <= Decimal(30)
    assert stack.has_position(intent.market) and intent.intent_id in stack.open_intents()
    assert stack.account_snapshot().all_positions_protected
    broker.set_symbol_quote(symbol, *exit_quote)  # broker-side stop executes
    closed = [e for e in stack.poll_events() if isinstance(e, PositionClosed)]
    assert len(closed) == 1 and closed[0].exit_reason == "STOP"
    assert closed[0].profit_eur is not None and closed[0].profit_eur < 0
    assert not stack.has_position(intent.market) and stack.open_intents() == ()


def test_margin_uses_the_observed_instrument_leverage_and_never_exceeds_30x(make_env):
    _broker, stack = make_env()
    stack.start()
    ev = stack.submit(btc_intent())
    accepted = next(e for e in ev if isinstance(e, Accepted))
    # BTCUSD broker margin is 2x: the position leverage is capped by the INSTRUMENT, far below the 30x ceiling
    assert accepted.leverage <= Decimal("2.0001")
    ev = stack.submit(brent_intent(intent_id="intent-2"))
    accepted2 = next(e for e in ev if isinstance(e, Accepted))
    assert accepted2.leverage <= Decimal("30")
    assert stack._markets["BTCUSD"].spec.max_leverage == Decimal("2.0")
    assert stack._markets["BRENT"].spec.max_leverage == Decimal("10.0")


def test_open_phase2_position_does_not_break_other_markets_cluster_books(make_env):
    """ENERGY / CRYPTO positions are cluster-limited through ALL_CLUSTERS (no KeyError / unknown cluster)."""
    _broker, stack = make_env()
    stack.start()
    assert kinds(stack.submit(btc_intent())) == ["Accepted", "Fill", "ProtectionConfirmed"]
    assert kinds(stack.submit(brent_intent(intent_id="intent-2"))) == ["Accepted", "Fill", "ProtectionConfirmed"]
    ev = stack.submit(make_intent(intent_id="intent-3"))  # GER40 with BTC + Brent open
    assert kinds(ev)[0] == "Accepted", ev
    clusters = {t.market: t.cluster for t in stack._ledger.tranches()}
    assert clusters["BTCUSD"] == "CRYPTO" and clusters["BRENT"] == "ENERGY"


# ------------------------------------------------------------------------ per-market fail closed
def test_market_not_tradable_is_disabled_alone_and_its_intents_are_rejected(make_env):
    broker, stack = make_env(brent_over={"trade_mode": 0})
    stack.start()  # does NOT raise: only Brent is disabled
    assert set(stack.disabled_markets) == {"BRENT"}
    assert "tradable" in stack.disabled_markets["BRENT"]  # refused by the instrument provider (trade_mode != FULL)
    assert stack.account_snapshot().extra["disabled_markets"].keys() == {"BRENT"}
    ev = stack.submit(brent_intent())
    assert kinds(ev) == ["Rejected"] and ev[0].reason == "unknown_market"
    assert broker.order_send_calls == 0
    assert kinds(stack.submit(btc_intent(intent_id="intent-2"))) == ["Accepted", "Fill", "ProtectionConfirmed"]
    assert kinds(stack.submit(make_intent(intent_id="intent-3")))[0] == "Accepted"  # core market untouched


def test_implied_leverage_above_30x_disables_that_market(make_env):
    broker, stack = make_env(observed_margin=False)
    broker.order_calc_margin = lambda action, sym, volume, price: 0.5  # absurdly small margin => implied leverage >> 30x
    stack.start()
    assert set(stack.disabled_markets) == set(BOTH)
    assert all("margin_calc" in why and "30" in why for why in stack.disabled_markets.values())


def test_stops_level_larger_than_the_structural_stop_disables_the_market(make_env):
    broker, stack = make_env(brent_over={"trade_stops_level": 400})  # 4.00 USD > the 1.00 reference structural stop
    stack.start()
    assert set(stack.disabled_markets) == {"BRENT"} and "structural_sl_vs_stops_level" in stack.disabled_markets["BRENT"]
    ev = stack.submit(brent_intent())  # loaded but preflight-RED: stays known (positions stay manageable), entries blocked
    assert kinds(ev) == ["Rejected"] and ev[0].reason == "market_disabled"
    assert broker.order_send_calls == 0


def test_symbol_missing_at_the_broker_disables_only_that_market(make_env):
    _broker, stack = make_env(brent=False)
    stack.start()
    assert set(stack.disabled_markets) == {"BRENT"} and stack.disabled_markets["BRENT"].startswith("instrument_load_failed")
    assert "BRENT" not in stack.markets and "BTCUSD" in stack.markets
    ev = stack.submit(brent_intent())
    assert kinds(ev) == ["Rejected"] and ev[0].reason == "unknown_market"
    assert kinds(stack.submit(btc_intent(intent_id="intent-2")))[0] == "Accepted"


def test_wrong_path_or_contract_facts_disable_only_that_market(make_env):
    _broker, stack = make_env(btc_over={"path": "Stocks" + chr(92) + "BTCUSD"}, brent_over={"trade_contract_size": 100.0})
    stack.start()
    assert set(stack.disabled_markets) == set(BOTH)
    assert stack.disabled_markets["BTCUSD"].startswith("instrument_load_failed")
    assert stack.disabled_markets["BRENT"].startswith("start_check_failed: spec_mismatch:BRENT:contract_size")
    assert set(stack.markets) == {"GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD"}  # the five core markets are intact


# ------------------------------------------------------------------------ closed market = idle, not a fault
def _frozen_clock(broker, now: datetime, stale_s: float):
    """Broker server clock pinned to ``now`` minus ``stale_s`` (every tick is that old)."""
    broker.live_offset_s = None
    broker.server_time = int(now.timestamp()) + berlin_offset_s(now) - int(stale_s)


def test_closed_market_stale_quote_enabled_and_open_market_stale_quote_disabled(tmp_path):
    saturday = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
    wednesday = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)  # 11:00 London (BST): inside Brent's cash session
    results = {}
    for label, now in (("closed", saturday), ("open", wednesday)):
        broker = build_broker()
        add_phase2_symbols(broker)
        _frozen_clock(broker, now, 16200)
        stack = make_stack(broker, tmp_path / label, now=lambda now=now: now, extra_markets=BOTH)
        try:
            stack.start()
            results[label] = dict(stack.disabled_markets)
        finally:
            stack.stop()
    assert results["closed"] == {}  # closed/idle is not a fault: both markets stay enabled
    assert set(results["open"]) == set(BOTH)
    assert all("quote_fresh" in why and "OPEN" in why for why in results["open"].values())


def test_btcusd_friday_break_is_outside_the_entry_calendar():
    from markets.phase2 import load_phase2_spec
    from markets.preflight import calendar_open

    spec = load_phase2_spec("BTCUSD")
    assert calendar_open(spec, datetime(2026, 10, 2, 12, 0, tzinfo=UTC))  # Friday midday: open
    assert not calendar_open(spec, datetime(2026, 10, 2, 20, 45, tzinfo=UTC))  # after the 20:30 forced flat / 20:55 break
    assert not calendar_open(spec, datetime(2026, 10, 3, 12, 0, tzinfo=UTC))  # Saturday: no automatic 24/7
    assert spec.calendar.forced_flat_min == 20 * 60 + 30  # stays before the Friday 20:55 UTC broker break


# ------------------------------------------------------------------------ registry / opt-in wiring
def test_optional_markets_follow_the_registry_not_a_hardcoded_list(make_env):
    from nautilus_mt5.symbols import demo_registry

    assert [m.canonical for m in demo_registry(extra_markets=BOTH).all()][-2:] == list(BOTH)
    _, only_btc = make_env(extra=("BTCUSD",))
    only_btc.start()
    assert "BTCUSD" in only_btc.markets and "BRENT" not in only_btc.markets
    assert isinstance(only_btc.submit(brent_intent())[0], Rejected)


def test_shadow_mode_never_sends_for_phase2_markets(tmp_path):
    broker = build_broker()
    add_phase2_symbols(broker)
    stack = make_stack(broker, tmp_path, dry_run=True, extra_markets=BOTH)
    try:
        stack.start()
        assert stack.disabled_markets == {}
        ev = stack.submit(btc_intent())
        assert broker.order_send_calls == 0 and not any(isinstance(e, Fill) for e in ev)
    finally:
        stack.stop()
