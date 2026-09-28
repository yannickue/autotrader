from decimal import Decimal

from execution.events import TradeEvent
from execution.models import OrderSide, OrderType, TimeInForce
from execution.orders import HaltCode, OrderStatus, RejectCode
from execution.paper import EngineMode, PaperExecutionEngine

from .conftest import INSTRUMENT, make_decision, make_quote, make_request


def _rest_limit(engine, now, quantity="3"):
    decision = make_decision(quantity=quantity)
    request = make_request(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        side=OrderSide.BUY,
        quantity=quantity,
    )
    engine.submit(request, decision, make_quote(), now)
    return request


def test_cancel_resting_order(engine, now):
    _rest_limit(engine, now)
    result = engine.cancel("client-1", now)
    assert result.accepted is True
    assert result.status == OrderStatus.CANCELED


def test_cancel_is_idempotent_noop_on_terminal_order(engine, now):
    _rest_limit(engine, now)
    engine.cancel("client-1", now)
    second = engine.cancel("client-1", now)
    assert second.accepted is True
    assert second.status == OrderStatus.CANCELED


def test_canceled_limit_order_does_not_fill_on_later_trade_event(engine, now):
    """A CANCELED order must never match a later TradeEvent. In this paper
    simulator, a late fill can only be booked through the explicit
    venue-reported `report_fill` hook (see chaos tests), never through the
    ordinary trade-event crossing path."""
    _rest_limit(engine, now, quantity="1")
    engine.cancel("client-1", now)

    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("98"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)

    order = engine._orders["client-1"]
    assert order.status == OrderStatus.CANCELED
    assert order.filled_quantity == Decimal("0")
    assert INSTRUMENT not in engine.portfolio.positions
    assert engine.mode == EngineMode.READY


def test_expired_order_does_not_fill_on_later_trade_event(engine, now):
    from datetime import timedelta

    _rest_limit(engine, now, quantity="1")
    engine.on_time(now + timedelta(hours=1))
    assert engine._orders["client-1"].status == OrderStatus.EXPIRED

    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("98"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now + timedelta(hours=1))

    order = engine._orders["client-1"]
    assert order.status == OrderStatus.EXPIRED
    assert order.filled_quantity == Decimal("0")
    assert INSTRUMENT not in engine.portfolio.positions


def test_cancel_replace_links_new_client_id_and_cancels_original(engine, now):
    _rest_limit(engine, now, quantity="3")
    result = engine.cancel_replace("client-1", "client-1-r1", now, new_price=Decimal("98"))
    assert result.accepted is True
    original = engine._orders["client-1"]
    replacement = engine._orders["client-1-r1"]
    assert original.status == OrderStatus.CANCELED
    assert original.replaced_by_client_order_id == "client-1-r1"
    assert replacement.replaces_client_order_id == "client-1"
    assert replacement.limit_price == Decimal("98")
    assert replacement.status == OrderStatus.ACCEPTED


def test_cancel_replace_cannot_increase_quantity_beyond_remaining(engine, now):
    _rest_limit(engine, now, quantity="3")
    result = engine.cancel_replace("client-1", "client-1-r1", now, new_quantity=Decimal("5"))
    assert result.accepted is False
    assert result.reject_code == RejectCode.CANCEL_REPLACE_EXCEEDS_REMAINING
    assert "client-1-r1" not in engine._orders
    assert engine._orders["client-1"].status == OrderStatus.ACCEPTED


def test_cancel_replace_after_partial_fill_caps_remaining(engine, now):
    _rest_limit(engine, now, quantity="3")
    event = TradeEvent(
        instrument=INSTRUMENT,
        timestamp=now,
        trade_id="t1",
        price=Decimal("98"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)  # 1 filled, 2 remaining

    ok = engine.cancel_replace("client-1", "client-1-r1", now, new_quantity=Decimal("2"))
    assert ok.accepted is True

    too_much = engine.cancel_replace("client-1-r1", "client-1-r2", now, new_quantity=Decimal("3"))
    assert too_much.accepted is False


def test_reconcile_halts_on_unknown_external_order(engine, now):
    engine.reconcile({"orders": {"ghost-order": {}}, "positions": {}}, now)
    assert engine.mode == EngineMode.HALTED


def test_reconcile_halts_on_position_mismatch(config, portfolio, cost_schedule, now):
    engine = PaperExecutionEngine(config, portfolio, cost_schedule)
    engine.reconcile({"orders": {}, "positions": {INSTRUMENT: "5"}}, now)
    assert engine.mode == EngineMode.HALTED


def test_resume_after_reconcile_returns_to_ready(engine, now):
    engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, now)
    assert engine.mode == EngineMode.HALTED

    ok = engine.resume_after_reconcile({"orders": {}, "positions": {}}, now)
    assert ok is True
    assert engine.mode == EngineMode.READY


def test_reduce_only_submit_allowed_while_halted_if_it_truly_reduces(engine, now):
    open_decision = make_decision(decision_id="d-open", quantity="2")
    open_request = make_request(
        request_id="r-open", risk_decision_id="d-open", client_order_id="c-open", quantity="2"
    )
    engine.submit(open_request, open_decision, make_quote(), now)

    # force a halt
    engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, now)
    assert engine.mode == EngineMode.HALTED

    close_decision = make_decision(
        decision_id="d-close", quantity="1", side="sell", reduce_only=True
    )
    close_request = make_request(
        request_id="r-close",
        risk_decision_id="d-close",
        client_order_id="c-close",
        side=OrderSide.SELL,
        quantity="1",
        reduce_only=True,
    )
    result = engine.submit(close_request, close_decision, make_quote(), now)
    assert result.accepted is True


def test_non_reduce_only_submit_rejected_while_halted(engine, now):
    engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, now)
    decision = make_decision()
    request = make_request()
    result = engine.submit(request, decision, make_quote(), now)
    assert result.reject_code == RejectCode.NOT_READY


def test_non_reduce_only_cancel_replace_rejected_while_halted(engine, now):
    """Regression for a review finding: cancel_replace() had no mode check
    at all (unlike submit()), so replacing a resting order while HALTED
    still created a new ACCEPTED order -- including a non-reduce-only
    replacement, which opens new exposure and bypasses the halt entirely."""
    _rest_limit(engine, now, quantity="3")

    engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, now)
    assert engine.mode == EngineMode.HALTED

    result = engine.cancel_replace("client-1", "client-1-r1", now, new_price=Decimal("97"))
    assert result.accepted is False
    assert result.reject_code == RejectCode.NOT_READY
    # the original order must still be resting, untouched, and no
    # replacement should have been created
    assert engine._orders["client-1"].status == OrderStatus.ACCEPTED
    assert "client-1-r1" not in engine._orders


def test_reduce_only_cancel_replace_still_allowed_while_halted(engine, now):
    """A reduce-only order's cancel/replace can only shrink exposure, so it
    stays allowed while HALTED -- mirrors submit()'s existing policy."""
    open_decision = make_decision(decision_id="d-open", quantity="2")
    open_request = make_request(
        request_id="r-open", risk_decision_id="d-open", client_order_id="c-open", quantity="2"
    )
    engine.submit(open_request, open_decision, make_quote(), now)

    close_decision = make_decision(
        decision_id="d-close", quantity="1", side="sell", reduce_only=True
    )
    close_request = make_request(
        request_id="r-close",
        risk_decision_id="d-close",
        client_order_id="c-close",
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        limit_price="99",
        side=OrderSide.SELL,
        quantity="1",
        reduce_only=True,
    )
    engine.submit(close_request, close_decision, make_quote(), now)

    engine.reconcile({"orders": {"ghost": {}}, "positions": {}}, now)
    assert engine.mode == EngineMode.HALTED

    result = engine.cancel_replace("c-close", "c-close-r1", now, new_price=Decimal("98"))
    assert result.accepted is True


def test_late_fill_on_replaced_order_plus_replacement_fill_cannot_exceed_approved_size(
    engine, now
):
    """Regression for an independently confirmed review finding: cancel_replace()
    gives the replacement the original's remaining quantity but never reduces
    the original order's own `.quantity` field. report_fill() has no
    is_terminal() guard, so a late fill on the CANCELED original plus a fill
    on its replacement could each pass the per-order overfill check
    individually while together exceeding what the risk decision approved --
    violating EXECUTION_CONTRACT.md's "does not... increase approved size"."""
    _rest_limit(engine, now, quantity="2")  # decision-1 approves exactly 2

    replace_result = engine.cancel_replace("client-1", "client-1-r1", now, new_price=Decimal("98"))
    assert replace_result.accepted is True

    # Late fill on the now-CANCELED original: within the decision's approved
    # total (0 + 2 <= 2), so this one legitimately books.
    engine.report_fill("client-1", "late-trade", Decimal("99"), Decimal("2"), now)
    assert engine.mode == EngineMode.READY
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("2")

    # A further fill on the replacement would push cumulative fills for
    # decision-1 to 4, double the approved 2 -- must halt, not silently book.
    engine.report_fill("client-1-r1", "trade-2", Decimal("98"), Decimal("2"), now)
    assert engine.mode == EngineMode.HALTED
    assert engine.halt_reason is not None and "OVERFILL" in engine.halt_reason
    # position must still reflect only the first, legitimate fill
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("2")


def test_decision_level_cap_does_not_penalize_normal_single_order_fills(engine, now):
    """Sanity check: the new decision-level cap must not interfere with the
    ordinary case of one order (no cancel/replace) filling up to its full
    approved quantity, possibly via multiple partial fills."""
    decision = make_decision(decision_id="d-normal", quantity="3")
    request = make_request(
        request_id="r-normal", risk_decision_id="d-normal", client_order_id="c-normal",
        quantity="3",
    )
    result = engine.submit(request, decision, make_quote(), now)
    assert result.accepted is True

    event1 = TradeEvent(
        instrument=INSTRUMENT, timestamp=now, trade_id="t1", price=Decimal("100"),
        quantity=Decimal("2"),
    )
    engine.on_trade(event1, now)
    event2 = TradeEvent(
        instrument=INSTRUMENT, timestamp=now, trade_id="t2", price=Decimal("100"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event2, now)

    assert engine.mode == EngineMode.READY
    assert engine._orders["c-normal"].filled_quantity == Decimal("3")


def test_protective_stop_fill_not_double_counted_against_entry_decision_cap(engine, now):
    """A protective STOP order shares its parent entry's decision_id but is
    reduce_only=True; its fill closes exposure and must not be checked
    against (or consume) the entry decision's approved-quantity cap, which
    would otherwise immediately look "overfilled" the moment the position
    that was just opened gets closed by its own stop."""
    decision = make_decision(decision_id="d-entry", quantity="1", stop_price="90")
    request = make_request(
        request_id="r-entry", risk_decision_id="d-entry", client_order_id="c-entry",
        quantity="1",
    )
    engine.submit(request, decision, make_quote(), now)  # MARKET order, fills immediately

    assert engine.mode == EngineMode.READY
    stop_order = engine._orders["c-entry:stop"]
    assert stop_order.reduce_only is True
    assert stop_order.decision_id == "d-entry"

    # Trade crosses the stop trigger -> stop fills, closing the position.
    event = TradeEvent(
        instrument=INSTRUMENT, timestamp=now, trade_id="t-stop", price=Decimal("89"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)

    assert engine.mode == EngineMode.READY  # must NOT have halted as a false OVERFILL
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("0")


def test_unknown_order_fill_report_halts(engine, now):
    engine.report_fill("no-such-order", "t1", Decimal("100"), Decimal("1"), now)
    assert engine.mode == EngineMode.HALTED


def test_overfill_halts(engine, now):
    _rest_limit(engine, now, quantity="1")
    engine.report_fill("client-1", "t1", Decimal("99"), Decimal("5"), now)
    assert engine.mode == EngineMode.HALTED


def test_export_import_checkpoint_roundtrip(engine, now):
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1", metadata={"take_profit": Decimal("120")})
    engine.submit(request, decision, make_quote(), now)

    checkpoint = engine.export_checkpoint()

    restored = PaperExecutionEngine(engine.config, engine.portfolio, engine._cost_schedule)
    restored.import_checkpoint(checkpoint)

    # The live engine was READY when checkpointed, but a restart must never
    # resume straight into READY -- import_checkpoint always forces
    # RECONCILING (unless the persisted mode was HALTED, which stays HALTED).
    assert checkpoint["mode"] == str(EngineMode.READY)
    assert restored.mode == EngineMode.RECONCILING

    restored_checkpoint = restored.export_checkpoint()
    assert {k: v for k, v in restored_checkpoint.items() if k != "mode"} == {
        k: v for k, v in checkpoint.items() if k != "mode"
    }
    assert "client-1:stop" in restored._orders
    assert "client-1:take_profit" in restored._orders

    # restart flow: reconcile against matching venue snapshot
    venue_orders = {cid: {} for cid, o in restored._orders.items() if not o.is_terminal()}
    venue_positions = {
        instrument: str(view.quantity) for instrument, view in restored.portfolio.positions.items()
    }
    ok = restored.reconcile({"orders": venue_orders, "positions": venue_positions}, now)
    assert ok is True
    assert restored.mode == EngineMode.READY


def test_checkpoint_export_is_deterministic_for_identical_state(engine, now):
    decision = make_decision()
    request = make_request()
    engine.submit(request, decision, make_quote(), now)
    a = engine.export_checkpoint()
    b = engine.export_checkpoint()
    assert a == b


def test_restart_never_resumes_ready_regardless_of_persisted_mode(engine, now):
    """A restored engine must always require reconciliation before new
    exposure is permitted (EXECUTION_CONTRACT.md), never silently resume
    READY straight from a checkpoint -- regardless of what mode was
    persisted at checkpoint time."""
    for persisted_mode in (EngineMode.STARTING, EngineMode.RECONCILING, EngineMode.READY):
        engine.mode = persisted_mode
        checkpoint = engine.export_checkpoint()
        restored = PaperExecutionEngine(engine.config, engine.portfolio, engine._cost_schedule)
        restored.import_checkpoint(checkpoint)
        assert restored.mode == EngineMode.RECONCILING, persisted_mode


def test_restart_preserves_halted_mode(engine, now):
    """A HALTED checkpoint must stay HALTED on restart -- a restart alone
    must never silently downgrade/auto-recover a halt."""
    engine._halt(HaltCode.INTERNAL_ERROR, "test halt")
    checkpoint = engine.export_checkpoint()
    assert checkpoint["mode"] == str(EngineMode.HALTED)

    restored = PaperExecutionEngine(engine.config, engine.portfolio, engine._cost_schedule)
    restored.import_checkpoint(checkpoint)
    assert restored.mode == EngineMode.HALTED
    assert restored.halt_reason == checkpoint["halt_reason"]


def test_checkpoint_round_trips_order_updated_at(engine, now):
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1")
    engine.submit(request, decision, make_quote(), now)
    order = engine._orders["client-1"]
    assert order.updated_at == now  # sanity: the live order really has this set

    checkpoint = engine.export_checkpoint()
    assert checkpoint["orders"]["client-1"]["updated_at"] == now.isoformat()

    restored = PaperExecutionEngine(engine.config, engine.portfolio, engine._cost_schedule)
    restored.import_checkpoint(checkpoint)
    assert restored._orders["client-1"].updated_at == now


def test_checkpoint_exports_every_dedup_map(engine, now):
    """Every internal dedup map the engine relies on to reject a replayed
    request/fill as a duplicate after restart must survive a checkpoint
    round-trip -- a restart must never silently lose duplicate protection."""
    decision = make_decision(quantity="1")
    request = make_request(quantity="1")
    engine.submit(request, decision, make_quote(), now)

    checkpoint = engine.export_checkpoint()
    for key in (
        "requests",
        "decision_usage",
        "decision_approved_quantity",
        "decision_filled_quantity",
        "client_id_owner",
        "seen_fill_keys",
        "orders",
    ):
        assert key in checkpoint, key

    assert checkpoint["requests"]["request-1"]["client_order_id"] == "client-1"
    assert checkpoint["decision_usage"]["decision-1"] == "client-1"
    assert checkpoint["decision_approved_quantity"]["decision-1"] == "1"
    assert checkpoint["client_id_owner"]["client-1"] == "request-1"

    restored = PaperExecutionEngine(engine.config, engine.portfolio, engine._cost_schedule)
    restored.import_checkpoint(checkpoint)

    # Re-submitting the exact same (already-consumed) request after restart
    # must return the identical remembered result, not process it anew.
    duplicate_result = restored.submit(request, decision, make_quote(), now)
    original_result = engine.submit(request, decision, make_quote(), now)
    assert duplicate_result == original_result


def test_cancel_replace_of_unfilled_entry_preserves_protective_prices(engine, now):
    """Regression: cancel_replace() dropped trigger_price/take_profit_price
    on the replacement. For an ENTRY order replaced before it fills, this
    meant the position opened with NO protective stop/take-profit at all
    once the replacement filled -- silently, no error, no halt."""
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(
        order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, limit_price="99",
        quantity="1", metadata={"take_profit": Decimal("120")},
    )
    engine.submit(request, decision, make_quote(), now)
    assert engine._orders["client-1"].status == OrderStatus.ACCEPTED  # resting, unfilled

    result = engine.cancel_replace("client-1", "client-1-r1", now, new_price=Decimal("98"))
    assert result.accepted is True
    replacement = engine._orders["client-1-r1"]
    assert replacement.trigger_price == Decimal("90")
    assert replacement.take_profit_price == Decimal("120")

    # Fill the replacement -> protective stop/take-profit must now be created.
    event = TradeEvent(
        instrument=INSTRUMENT, timestamp=now, trade_id="t1", price=Decimal("98"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)
    assert replacement.status == OrderStatus.FILLED
    assert "client-1-r1:stop" in engine._orders
    assert "client-1-r1:take_profit" in engine._orders
    assert engine._orders["client-1-r1:stop"].trigger_price == Decimal("90")


def test_cancel_replace_of_resting_stop_preserves_trigger_price(engine, now):
    """Regression: replacing an already-resting protective STOP order lost
    its trigger_price on the replacement (defaulted to None), so the very
    next crossing trade hit `_try_cross_protective`'s
    `assert order.trigger_price is not None` and HALTed with
    INTERNAL_ERROR -- a confusing failure mode, with the position left
    unprotected regardless."""
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1", metadata={"take_profit": Decimal("120")})
    engine.submit(request, decision, make_quote(), now)  # MARKET order, fills immediately

    stop = engine._orders["client-1:stop"]
    tp = engine._orders["client-1:take_profit"]
    assert stop.trigger_price == Decimal("90")
    assert stop.oco_sibling_id == tp.client_order_id

    result = engine.cancel_replace(
        "client-1:stop", "client-1:stop-r1", now, new_price=Decimal("91")
    )
    assert result.accepted is True
    replacement = engine._orders["client-1:stop-r1"]
    assert replacement.trigger_price == Decimal("90")  # preserved, not lost
    assert replacement.role == stop.role

    # The OCO link is re-pointed both ways: the take-profit's sibling now
    # points at the replacement stop, not the dead original.
    assert replacement.oco_sibling_id == tp.client_order_id
    assert tp.oco_sibling_id == "client-1:stop-r1"

    # A crossing trade must fill the replacement normally -- no assertion
    # error, no false INTERNAL_ERROR halt.
    event = TradeEvent(
        instrument=INSTRUMENT, timestamp=now, trade_id="t1", price=Decimal("89"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)
    assert engine.mode == EngineMode.READY
    assert replacement.status == OrderStatus.FILLED
    # OCO cancel now correctly hits the take-profit sibling via the
    # re-pointed link.
    assert tp.status == OrderStatus.CANCELED


def test_cancel_replace_stop_then_take_profit_fills_cancels_replacement_stop(engine, now):
    """The other direction of the OCO re-link: after replacing the stop,
    filling the (unchanged) take-profit sibling must cancel the
    *replacement* stop, not silently no-op against the dead original."""
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(quantity="1", metadata={"take_profit": Decimal("120")})
    engine.submit(request, decision, make_quote(), now)

    tp = engine._orders["client-1:take_profit"]
    engine.cancel_replace("client-1:stop", "client-1:stop-r1", now, new_price=Decimal("91"))
    replacement_stop = engine._orders["client-1:stop-r1"]

    event = TradeEvent(
        instrument=INSTRUMENT, timestamp=now, trade_id="t1", price=Decimal("121"),
        quantity=Decimal("1"),
    )
    engine.on_trade(event, now)

    assert tp.status == OrderStatus.FILLED
    assert replacement_stop.status == OrderStatus.CANCELED


def test_late_fill_on_replaced_entry_still_creates_protective_orders(engine, now):
    """Regression confirmed by independent Codex review: the decision-level
    fill cap fix (Finding #1) correctly bounds a late fill on a replaced
    (terminal) entry order, but _apply_fill returned before ever reaching
    the ENTRY protective-sync call -- so exposure increased with NO
    protective stop/take-profit at all."""
    decision = make_decision(quantity="1", stop_price="90")
    request = make_request(
        order_type=OrderType.LIMIT, time_in_force=TimeInForce.GTC, limit_price="99",
        quantity="1", metadata={"take_profit": Decimal("120")},
    )
    engine.submit(request, decision, make_quote(), now)
    engine.cancel_replace("client-1", "client-1-r1", now, new_price=Decimal("98"))

    # Late fill on the now-CANCELED original -- within the approved decision
    # total, so it legitimately books and increases real exposure.
    engine.report_fill("client-1", "late-trade", Decimal("99"), Decimal("1"), now)

    assert engine.mode == EngineMode.READY
    assert engine.portfolio.positions[INSTRUMENT].quantity == Decimal("1")
    assert "client-1:stop" in engine._orders
    assert "client-1:take_profit" in engine._orders
    assert engine._orders["client-1:stop"].trigger_price == Decimal("90")


def test_replaced_stop_gets_resized_and_relinked_on_later_partial_fill(engine, now):
    """Regression confirmed by independent Codex review: _sync_protective_orders
    and _ensure_protective looked protective children up by their fixed
    deterministic id (f"{entry_id}:stop"), not by following the
    replacement chain. After cancel_replace()'d the stop, a later partial
    fill of the entry re-ran _sync_protective_orders, which resized/
    relinked the dead original id instead of the live replacement --
    leaving the replacement stop's quantity stale and re-pointing the
    take-profit's OCO link back at the dead original."""
    decision = make_decision(quantity="2", stop_price="90")
    request = make_request(quantity="2", metadata={"take_profit": Decimal("120")})
    engine.submit(request, decision, make_quote(), now)  # MARKET, fills fully at qty=2

    stop = engine._orders["client-1:stop"]
    tp = engine._orders["client-1:take_profit"]
    assert stop.quantity == Decimal("2")

    result = engine.cancel_replace(
        "client-1:stop", "client-1:stop-r1", now, new_price=Decimal("91")
    )
    assert result.accepted is True
    replacement = engine._orders["client-1:stop-r1"]
    assert replacement.quantity == Decimal("2")

    # Re-run protective sync the way a later partial fill of the entry
    # would (same entry, same filled_quantity -- simulates the sync call
    # that used to corrupt the live replacement's linkage).
    engine._sync_protective_orders(engine._orders["client-1"], now)

    # The dead original must NOT be resurrected/resized.
    assert stop.is_terminal()
    assert stop.quantity == Decimal("2")  # untouched
    # The live replacement keeps its size and gets the OCO link, not the
    # dead original.
    assert replacement.quantity == Decimal("2")
    assert replacement.oco_sibling_id == tp.client_order_id
    assert tp.oco_sibling_id == "client-1:stop-r1"
