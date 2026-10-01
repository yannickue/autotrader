# ruff: noqa: E501
"""Lane M3: fixes for the M2 audit findings (manage-only markets with open exposure, per-market clock reference,
strict enablement flags, margin headroom info)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from demo.execution.events import PositionClosed
from demo.execution.live import StackFailClosed
from markets import phase2
from markets.spec import MarketSpecError
from tests.unit.demo.execution.stack_harness import (
    add_phase2_symbols,
    build_broker,
    make_intent,
    make_stack,
)

BOTH = ("BRENT", "BTCUSD")


def kinds(events):
    return [type(e).__name__ for e in events]


def btc_intent(**kw):
    kw.setdefault("entry_ref", 83746.28)
    kw.setdefault("stop", 83146.28)
    kw.setdefault("target", 85000.0)
    return make_intent(market="BTCUSD", broker_symbol="BTCUSD", **kw)


def open_btc_then_restart(tmp_path, *, extra_after=()):
    """First stack (BTC enabled) opens a BTC position and dies; returns (broker, second stack, not yet started)."""
    broker = build_broker()
    add_phase2_symbols(broker)
    first = make_stack(broker, tmp_path, extra_markets=("BTCUSD",))
    first.start()
    assert kinds(first.submit(btc_intent(flat_in_s=600)))[0] == "Accepted"
    first.stop()
    assert len(broker.positions_get()) == 1
    return broker, make_stack(broker, tmp_path, extra_markets=extra_after)


# (a) flag off after restart with an open position ---------------------------------------------------------
def test_flag_off_restart_keeps_open_btc_position_managed_and_core_markets_trading(tmp_path):
    broker, second = open_btc_then_restart(tmp_path)
    try:
        snap = second.start()
        assert snap.reconciliation == "RECONCILED" and snap.open_positions == 1
        assert second.halted_reason is None and second._foreign == ()
        assert "BTCUSD" in second.markets and "manage_only" in second.disabled_markets["BTCUSD"]
        assert second.account_snapshot().extra["start_notes"]["manage_only_markets"] == ["BTCUSD"]
        assert second.open_intents() == ("intent-1",) and second.has_position("BTCUSD")
        second.poll_events()
        assert second.halted_reason is None and second._foreign == ()  # poll does not classify it foreign
        ev = second.submit(btc_intent(intent_id="intent-2"))  # entries on the disabled market are rejected
        assert kinds(ev) == ["Rejected"] and ev[0].reason == "market_disabled"
        assert kinds(second.submit(make_intent(intent_id="intent-3")))[0] == "Accepted"  # core market keeps trading
        (closed,) = second.on_clock(datetime.now(UTC) + timedelta(seconds=700))  # forced flat works, no KeyError
        assert isinstance(closed, PositionClosed) and closed.exit_reason == "SESSION_END" and closed.intent_id == "intent-1"
        assert all(p.symbol != "BTCUSD" for p in broker.positions_get())
        assert second.halted_reason is None
    finally:
        second.stop()


# (b) failed start check / failed preflight with an open registry row ---------------------------------------
def test_failed_preflight_with_open_row_keeps_the_market_managed(tmp_path):
    broker, second = open_btc_then_restart(tmp_path, extra_after=("BTCUSD",))
    broker._info("BTCUSD").trade_stops_level = 100000  # preflight RED (stops level) on the restart
    try:
        second.start()
        assert "structural_sl_vs_stops_level" in second.disabled_markets["BTCUSD"]
        assert second.halted_reason is None and "BTCUSD" in second.markets
        assert kinds(second.submit(btc_intent(intent_id="intent-2")))[0] == "Rejected"
        (closed,) = second.on_clock(datetime.now(UTC) + timedelta(seconds=700))
        assert closed.exit_reason == "SESSION_END"
    finally:
        second.stop()


@pytest.mark.parametrize("extra_after", [(), ("BTCUSD",)])
def test_unverifiable_market_with_open_row_refuses_to_start_with_a_clear_reason(tmp_path, extra_after):
    broker, second = open_btc_then_restart(tmp_path, extra_after=extra_after)
    broker._info("BTCUSD").trade_contract_size = 7.0  # facts no longer match the checked-in config
    with pytest.raises(StackFailClosed, match="open_exposure_on_unverifiable_market:BTCUSD"):
        second.start()


# (c) no exposure + disabled: not registered as before ---------------------------------------------------------
def test_no_open_exposure_and_flag_off_market_is_not_registered(tmp_path):
    broker = build_broker()
    add_phase2_symbols(broker)
    first = make_stack(broker, tmp_path, extra_markets=("BTCUSD",))
    first.start()
    first.submit(btc_intent(flat_in_s=600))
    (closed,) = first.on_clock(datetime.now(UTC) + timedelta(seconds=700))
    assert isinstance(closed, PositionClosed)
    first.stop()
    second = make_stack(broker, tmp_path)
    try:
        second.start()
        assert "BTCUSD" not in second.markets and second.disabled_markets == {}
        ev = second.submit(btc_intent(intent_id="intent-2"))
        assert ev[0].reason == "unknown_market"
    finally:
        second.stop()


# M1: per-market clock reference -----------------------------------------------------------------------------
def test_one_failing_tick_does_not_blank_the_clock_reference_and_disabled_markets_are_skipped(tmp_path):
    broker = build_broker()
    add_phase2_symbols(broker)
    stack = make_stack(broker, tmp_path, extra_markets=BOTH)
    try:
        stack.start()
        assert stack._on_lane(stack._lane_snapshot)[0].server_time is not None
        polled: list[str] = []
        real = broker.symbol_info_tick

        def tick(symbol):
            polled.append(symbol)
            return None if symbol == "Ger40" else real(symbol)  # one market's tick fails

        broker.symbol_info_tick = tick
        assert stack._on_lane(stack._lane_snapshot)[0].server_time is not None
        assert "Ger40" in polled
        stack.disabled_markets["BRENT"] = "preflight_red: test"
        polled.clear()
        stack._on_lane(stack._lane_snapshot)
        assert "Brent" not in polled and "BTCUSD" in polled
    finally:
        stack.stop()


def test_clock_reference_window_must_stay_below_the_skew_bound(tmp_path):
    from demo.runner import RunnerConfig

    cfg = RunnerConfig(artifacts_dir=tmp_path)
    assert 0 < cfg.clock_reference_window_s < cfg.max_clock_skew_s
    with pytest.raises(ValueError, match="clock_reference_window_s"):
        RunnerConfig(artifacts_dir=tmp_path, clock_reference_window_s=300.0, max_clock_skew_s=300.0)


# M2-doc: information-only margin headroom line ------------------------------------------------------------------
def test_preflight_notes_carry_a_free_margin_headroom_line(tmp_path):
    broker = build_broker()
    add_phase2_symbols(broker)
    stack = make_stack(broker, tmp_path, extra_markets=BOTH)
    try:
        snap = stack.start()
        note = snap.extra["start_notes"]["phase2_preflight"]["BTCUSD"]
        assert note["free_margin_eur"] > 0 and note["min_lot_margin_pct_of_free_margin"] == pytest.approx(
            100.0 * note["margin_min_lot_eur"] / note["free_margin_eur"], abs=0.1
        )
        assert stack.disabled_markets == {}  # information only: no new gate
    finally:
        stack.stop()


# LOW: strict enablement flag ------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ['"false"', '"true"', "1", "0"])
def test_enablement_rejects_non_boolean_values(tmp_path, bad):
    (tmp_path / "enablement.toml").write_text(f"[BTCUSD]\nenabled = {bad}\n", encoding="utf-8")
    with pytest.raises(MarketSpecError, match="TOML boolean"):
        phase2.load_enablement(tmp_path)


def test_committed_enablement_is_the_approved_production_state():
    # 2026-10-01 deployment decision (user approval): both Phase-2 markets on, still behind the code-side GREEN preflight gate
    assert phase2.load_enablement() == {"BRENT": True, "BTCUSD": True}
