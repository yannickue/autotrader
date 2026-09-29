"""Regression tests for the post-C5 Codex review findings (High + Medium)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus

from adapters.config import MT5ConnectionConfig
from nautilus_mt5.constants import Retcode
from nautilus_mt5.session import Mt5Session, SessionState
from risk.models import ReconciliationState
from tests.unit.nautilus_mt5.harness import IID, ExecHarness

BUY, SELL = OrderSide.BUY, OrderSide.SELL


@pytest.fixture
def h(broker, tmp_path):
    harness = ExecHarness(broker, tmp_path)
    yield harness
    harness.shutdown()


def test_crash_between_booking_and_marking_does_not_double_book_on_replay(h, broker, monkeypatch):
    broker.progressive = True
    broker.fill_plan.append([(0.25, 25_001.5)])
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    ticket = broker.deals[0].ticket
    broker.release_deals(ticket)

    real_mark = h.store.mark_ingested
    monkeypatch.setattr(
        h.store, "mark_ingested", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError, match="crash"):
        h.client.sync_once()  # fill reached Nautilus, the "seen" mark did not
    assert h.net_qty() == Decimal("0.25") and not h.store.is_ingested(ticket)

    monkeypatch.setattr(h.store, "mark_ingested", real_mark)
    h.client.sync_once()  # replay: Nautilus already holds this trade id -> mark only
    assert h.net_qty() == Decimal("0.25") and h.store.is_ingested(ticket)
    assert h.order(entry).filled_qty == 0.25 and h.client.ingest_stats.failed == 0


def test_delayed_partial_deal_still_closes_the_cancelled_ioc_remainder(h, broker):
    broker.progressive = True
    broker.fill_plan.append([(0.25, 25_001.5)])
    entry, _ = h.submit_bracket(BUY, "1.00", 24_900.0)
    assert h.order(entry).status is OrderStatus.ACCEPTED  # no deal visible at send time
    broker.release_deals(broker.deals[0].ticket)
    broker.history_orders[0].state = 2  # the broker cancelled the IOC remainder
    h.client.sync_once()  # the partial deal arrives late
    order = h.order(entry)
    assert order.filled_qty == 0.25 and order.status is OrderStatus.CANCELED
    assert not order.is_open and h.net_qty() == Decimal("0.25")


def test_emergency_protect_reports_a_broker_rejection(h, broker):
    deal = broker.external_market_fill(is_buy=True, volume=0.25)
    broker.send_retcode_override.append(int(Retcode.REJECT))
    reason = h.client.emergency_protect(deal.position_id, Decimal("24900"))
    assert reason is not None and "ORDER_SEND_10006" in reason
    assert broker.positions_get()[0].sl == 0.0  # still unprotected, and the caller knows


def test_emergency_protect_reports_an_unverified_outcome(h, broker):
    deal = broker.external_market_fill(is_buy=True, volume=0.25)
    broker.send_retcode_override.append(int(Retcode.DONE))  # claims success, changes nothing
    reason = h.client.emergency_protect(deal.position_id, Decimal("24900"))
    assert reason == "PROTECTIVE_OUTCOME_UNKNOWN"
    assert h.client.recon.state is ReconciliationState.NOT_RECONCILED


def test_reduce_only_is_rejected_if_the_broker_position_shrinks_before_send(h, broker):
    entry, _ = h.submit_bracket(BUY, "0.50", 24_900.0)
    assert h.order(entry).status is OrderStatus.FILLED
    original = h.client._quote

    def shrink_then_quote(symbol):
        broker.positions_get()[0].volume = 0.25  # the broker moved after admission
        return original(symbol)

    h.client._quote = shrink_then_quote
    sends_before = broker.order_send_calls
    close = h.market(SELL, "0.50", reduce_only=True)
    h.submit(close)
    assert h.order(close).status is OrderStatus.REJECTED
    assert "REDUCE_ONLY_POSITION_CHANGED_BEFORE_SEND" in h.denial(close)
    assert broker.order_send_calls == sends_before  # nothing was sent


def test_fill_reports_use_the_venue_id_the_order_was_accepted_with(h, broker):
    _, stop = h.submit_bracket(BUY, "0.25", 24_900.0)
    broker.set_quote(24_890.0, 24_891.5)
    h.client.sync_once()
    reports = h.run(h.client.generate_fill_reports(SimpleNamespace(instrument_id=None)))
    sl_report = next(r for r in reports if r.client_order_id == stop.client_order_id)
    assert str(sl_report.venue_order_id) == str(h.order(stop).venue_order_id)  # "SL:<ticket>"


def test_a_transient_booking_failure_does_not_block_reconciliation_forever(h, broker, monkeypatch):
    broker.progressive = True
    broker.fill_plan.append([(0.25, 25_001.5)])
    h.submit_bracket(BUY, "0.25", 24_900.0)
    broker.release_deals(broker.deals[0].ticket)
    original = h.client.generate_order_filled
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return original(**kwargs)

    monkeypatch.setattr(h.client, "generate_order_filled", flaky)
    h.client.sync_once()
    assert h.client.ingest_stats.failed == 1
    assert h.client.reconcile().state is ReconciliationState.RECONCILED  # retried, then clean


def test_adopted_in_doubt_order_persists_its_venue_id_and_becomes_accepted(h, broker):
    broker.lose_response_after_execute = 1
    broker.progressive = True  # its deal is not visible yet
    broker.fill_plan.append([(0.25, 25_001.5)])
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert h.order(entry).status is OrderStatus.SUBMITTED
    assert h.session.reconnect().success
    h.client.reconcile()
    row = h.store.by_client_order_id(str(entry.client_order_id))
    assert row.venue_order_id == str(row.order_ticket) and row.order_ticket is not None
    assert h.order(entry).status is OrderStatus.ACCEPTED


def test_protective_cancel_is_only_reported_after_the_level_is_gone(h, broker):
    _, stop = h.submit_bracket(BUY, "0.25", 24_900.0)
    broker.send_retcode_override.append(int(Retcode.DONE))  # says done, SL stays
    cancel = SimpleNamespace(
        client_order_id=stop.client_order_id,
        strategy_id=stop.strategy_id,
        instrument_id=IID,
        venue_order_id=h.order(stop).venue_order_id,
    )
    h.run(h.client._cancel_order(cancel))
    assert h.order(stop).status is OrderStatus.ACCEPTED
    assert broker.positions_get()[0].sl == 24_900.0
    assert h.client.recon.state is not ReconciliationState.RECONCILED


def test_failed_connects_do_not_inflate_the_session_refcount(broker, tmp_path):
    session = Mt5Session(
        broker,
        MT5ConnectionConfig(login=111, password="x", server="s", terminal_path="t"),
        lock_path=tmp_path / "l",
    )
    for _ in range(3):
        assert not session.acquire().success
    assert session.state is SessionState.FAILED and session._users == 0
    session.release()
    assert session.state is SessionState.DISCONNECTED
