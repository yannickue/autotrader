"""C5C: reduce-only, inbound truth, partial fills, dedupe, unknown outcomes (real engine)."""

from decimal import Decimal

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus
from nautilus_trader.model.objects import Price, Quantity

from risk.models import ReconciliationState, RuntimeMode
from tests.unit.nautilus_mt5.harness import IID, ExecHarness

BUY, SELL = OrderSide.BUY, OrderSide.SELL


@pytest.fixture
def h(broker, tmp_path):
    harness = ExecHarness(broker, tmp_path)
    yield harness
    harness.shutdown()


def status_of(h, order):
    return h.order(order).status


def open_long(h, sl=24_900.0):
    entry, stop = h.submit_bracket(BUY, "0.25", sl)
    assert status_of(h, entry) is OrderStatus.FILLED
    return entry, stop


# -- reduce-only
# ----------------------------------------------------------------------


def test_reduce_only_close_realizes_pnl_in_nautilus_and_retires_the_protective_stop(h, broker):
    _, stop = open_long(h)
    broker.set_quote(25_010.0, 25_011.5)
    close = h.market(SELL, "0.25", reduce_only=True)
    h.submit(close)
    assert status_of(h, close) is OrderStatus.FILLED
    assert h.cache.positions_open() == [] and broker.positions_get() == ()
    assert h.order(stop).status is OrderStatus.CANCELED  # protection died with the position
    (closed,) = h.cache.positions_closed()
    nautilus_pnl = float(closed.realized_pnl.as_decimal())
    assert abs(nautilus_pnl - (25_010.0 - 25_001.5) * 0.25) < 0.01  # computed by Nautilus
    assert abs(nautilus_pnl - broker.history_deals_get()[-1].profit) < 0.01  # broker agrees


def test_reduce_only_never_clamps_and_checks_side_and_position(h, broker):
    open_long(h)
    too_big = h.market(SELL, "0.5", reduce_only=True)
    wrong_side = h.market(BUY, "0.25", reduce_only=True)
    for order in (too_big, wrong_side):
        h.submit(order)
        assert status_of(h, order) is OrderStatus.DENIED
    assert "REDUCE_ONLY" in h.denial(too_big) and broker.positions_get()[0].volume == 0.25


def test_reduce_only_without_a_broker_position_is_denied(h):
    order = h.market(SELL, "0.25", reduce_only=True)
    h.submit(order)
    assert status_of(h, order) is OrderStatus.DENIED and "REDUCE_ONLY" in h.denial(order)


def test_reduce_only_blocked_when_local_and_broker_positions_disagree(h, broker):
    open_long(h)
    broker.positions_get()[0].volume = 0.5  # corrupt/unknown position at the broker
    order = h.market(SELL, "0.25", reduce_only=True)
    h.submit(order)
    assert "LOCAL_BROKER_POSITION_MISMATCH" in h.denial(order)


@pytest.mark.parametrize(
    "state",
    [
        ReconciliationState.NOT_RECONCILED,
        ReconciliationState.RECONCILING,
        ReconciliationState.MISMATCH,
    ],
)
def test_reduce_only_blocked_unless_reconciled(h, state):
    open_long(h)
    h.client.recon.state = state
    order = h.market(SELL, "0.25", reduce_only=True)
    h.submit(order)
    assert status_of(h, order) is OrderStatus.DENIED and "NOT_VENUE_RECONCILED" in h.denial(order)


def test_reduce_only_allowed_when_halted_but_reconciled_new_exposure_is_not(h):
    open_long(h)
    h.client.recon.runtime = RuntimeMode.HALTED  # HALTED + RECONCILED
    close = h.market(SELL, "0.25", reduce_only=True)
    h.submit(close)
    assert status_of(h, close) is OrderStatus.FILLED
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert status_of(h, entry) is OrderStatus.DENIED and "RUNTIME_HALTED" in h.denial(entry)


# -- inbound truth is always ingested
# ----------------------------------------------------------------------


def test_broker_side_stop_execution_is_booked_even_while_halted_and_unreconciled(h, broker):
    _, stop = open_long(h, sl=24_900.0)
    h.client.recon.complete_mismatch(())  # MISMATCH + HALTED: outbound is shut
    broker.set_quote(24_890.0, 24_891.5)  # the broker executes the attached SL by itself
    assert broker.positions_get() == ()

    h.client.sync_once()  # INBOUND: never consults the gates
    assert h.order(stop).status is OrderStatus.FILLED  # the SL deal maps to the stop order
    assert h.cache.positions_open() == [] and len(h.cache.positions_closed()) == 1
    sl_deal = broker.history_deals_get()[-1]
    assert sl_deal.reason == 4 and h.store.is_ingested(sl_deal.ticket)
    (closed,) = h.cache.positions_closed()
    assert closed.realized_pnl.as_decimal() < 0
    assert h.client.recon.state is ReconciliationState.MISMATCH  # ingestion faked no recovery


def test_take_profit_execution_retires_the_stop_order(h, broker):
    _, stop = open_long(h)
    position = broker.positions_get()[0]
    position.tp = 25_100.0
    tp_order = h._add(
        h.factory.limit(IID, SELL, Quantity.from_str("0.25"), Price(25_100.0, 2), reduce_only=True)
    )
    h.store.record_intent(
        client_order_id=str(tp_order.client_order_id),
        strategy_id="S-001",
        instrument_id=str(IID),
        kind="PROTECT_TP",
        side="SELL",
        quantity="0.25",
        position_ticket=position.ticket,
    )
    h.store.update_order(str(tp_order.client_order_id), status="ACCEPTED", venue_order_id="TP:1")
    broker.set_quote(25_100.5, 25_102.0)
    h.client.sync_once()
    assert h.cache.positions_open() == []
    assert h.order(stop).status is OrderStatus.CANCELED  # position closed by TP


# -- partial fills
# ----------------------------------------------------------------------


def test_progressive_partial_fills_converge_to_broker_truth_without_double_counting(h, broker):
    broker.progressive = True
    broker.fill_plan.append([(0.25, 25_001.5), (0.25, 25_001.75), (0.5, 25_002.0)])
    entry, _ = h.submit_bracket(BUY, "1.00", 24_900.0)
    assert status_of(h, entry) is OrderStatus.ACCEPTED and h.cache.positions_open() == []

    tickets = sorted(d.ticket for d in broker.deals)
    broker.release_deals(tickets[0])
    h.client.sync_once()
    assert h.order(entry).status is OrderStatus.PARTIALLY_FILLED
    assert h.net_qty() == Decimal("0.25")

    broker.release_deals(tickets[1])
    h.client.sync_once()
    h.client.sync_once()  # repeated observation must not add quantity
    assert h.order(entry).filled_qty == 0.5 and h.net_qty() == Decimal("0.50")

    broker.release_deals(tickets[2])
    h.client.sync_once()
    filled = h.order(entry)
    assert filled.status is OrderStatus.FILLED and filled.filled_qty == 1.0
    assert h.net_qty() == Decimal("1.00")
    expected_avg = (0.25 * 25_001.5 + 0.25 * 25_001.75 + 0.5 * 25_002.0) / 1.0
    assert abs(float(filled.avg_px) - expected_avg) < 1e-6
    assert h.client.ingest_stats.ingested == 3 and h.client.ingest_stats.failed == 0


def test_ioc_partial_fill_cancels_the_remainder(h, broker):
    broker.fill_plan.append([(0.25, 25_001.5)])
    entry, _ = h.submit_bracket(BUY, "1.00", 24_900.0)
    order = h.order(entry)
    assert order.status is OrderStatus.CANCELED and order.filled_qty == 0.25
    assert h.net_qty() == Decimal("0.25") and broker.positions_get()[0].volume == 0.25


# -- deal dedupe
# ----------------------------------------------------------------------


def test_same_deal_observed_many_times_is_booked_exactly_once(h, broker):
    entry, _ = open_long(h)
    broker.show_deal_twice(broker.deals[0].ticket)  # the broker even lists it twice
    for _ in range(5):
        h.client.sync_once()
    assert h.order(entry).filled_qty == 0.25 and h.net_qty() == Decimal("0.25")
    assert h.store.ingested_count() == 1 and h.client.ingest_stats.ingested == 1
    assert h.client.ingest_stats.duplicates >= 5


def test_booking_failure_does_not_lose_the_deal(h, broker, monkeypatch):
    broker.progressive = True
    broker.fill_plan.append([(0.25, 25_001.5)])
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    ticket = broker.deals[0].ticket
    broker.release_deals(ticket)

    original = h.client.generate_order_filled
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated failure while booking")
        return original(**kwargs)

    monkeypatch.setattr(h.client, "generate_order_filled", flaky)
    h.client.sync_once()
    assert h.client.ingest_stats.failed == 1 and not h.store.is_ingested(ticket)  # NOT marked
    assert h.cache.positions_open() == []

    h.client.sync_once()  # the next pass books it: nothing was lost
    assert h.store.is_ingested(ticket) and h.net_qty() == Decimal("0.25")
    assert h.order(entry).status is OrderStatus.FILLED
    h.client.sync_once()
    assert h.net_qty() == Decimal("0.25")  # and never twice


# -- unknown outcome (IN_DOUBT)
# ----------------------------------------------------------------------


def test_lost_response_is_in_doubt_not_rejected_and_resolves_after_reconcile(h, broker):
    broker.lose_response_after_execute = 1
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert status_of(h, entry) is OrderStatus.SUBMITTED  # NOT rejected: exposure may exist
    assert len(broker.positions_get()) == 1  # and it does
    assert h.client.recon.state is ReconciliationState.NOT_RECONCILED
    assert h.store.by_client_order_id(str(entry.client_order_id)).status == "IN_DOUBT"

    assert h.session.reconnect().success
    h.client.reconcile()
    assert h.order(entry).status is OrderStatus.FILLED  # adopted from the broker deal via token
    assert h.net_qty() == Decimal("0.25")
    assert broker.positions_get()[0].sl == 24_900.0  # protected at the broker throughout


def test_exception_before_execution_is_rejected_only_after_grace_with_no_broker_trace(h, broker):
    broker.raise_on_send.append(TimeoutError("ipc timeout"))
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert status_of(h, entry) is OrderStatus.SUBMITTED
    assert h.session.reconnect().success
    h.client.reconcile()
    assert status_of(h, entry) is OrderStatus.SUBMITTED  # too early to call it never-executed
    assert h.client.recon.state is not ReconciliationState.RECONCILED  # unresolved blocks authority

    broker.server_time += 600  # past the in-doubt grace period, still no trace at the broker
    h.client.reconcile()
    assert status_of(h, entry) is OrderStatus.REJECTED
    assert h.client.recon.state is ReconciliationState.RECONCILED
