"""Regression tests for the post-C7-prep Codex review (account pinning, duplicate ClientOrderId,
protective-cancel unknown outcome, protective level reconciliation, explicit leverage cap)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus

from nautilus_mt5.constants import Retcode
from nautilus_mt5.execution_client import Mt5ExecClientConfig
from nautilus_mt5.reconciliation import DiscrepancyKind
from risk.models import ReconciliationState
from tests.unit.nautilus_mt5.harness import IID, ExecHarness

BUY = OrderSide.BUY
DEMO_ONLY = Mt5ExecClientConfig(autostart_sync=False, require_demo_account=True)


@pytest.fixture
def h(broker, tmp_path):
    harness = ExecHarness(broker, tmp_path, cfg=DEMO_ONLY)
    yield harness
    harness.shutdown()


def test_account_switched_after_connect_blocks_the_next_send(h, broker):
    broker.cfg.login = 424242  # the terminal is now attached to a different account
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert h.order(entry).status is OrderStatus.REJECTED
    assert "ACCOUNT_IDENTITY_CHANGED" in h.denial(entry)
    assert broker.order_send_calls == 0
    assert h.client.recon.state is not ReconciliationState.RECONCILED


def test_a_non_demo_account_blocks_the_send_even_with_the_right_login(h, broker):
    real_info = broker.account_info

    def as_real_money():
        info = real_info()
        info.trade_mode = 2
        return info

    broker.account_info = as_real_money
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert h.order(entry).status is OrderStatus.REJECTED
    assert "ACCOUNT_IS_NOT_DEMO" in h.denial(entry)
    assert broker.order_send_calls == 0


def test_identity_is_rechecked_before_stop_changes_too(h, broker):
    entry, stop = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert h.order(entry).status is OrderStatus.FILLED
    ticket = broker.positions_get()[0].ticket
    broker.cfg.login = 1
    reason = h.client.emergency_protect(ticket, Decimal("24950"))
    assert reason == "ACCOUNT_IDENTITY_CHANGED" and broker.positions_get()[0].sl == 24_900.0
    assert stop is not None


def test_redelivered_client_order_id_never_reaches_the_broker_twice(h, broker):
    from nautilus_trader.model.objects import Price, Quantity

    entry = h.market(BUY, "0.25")
    stop = h._add(
        h.factory.stop_market(
            IID, OrderSide.SELL, Quantity.from_str("0.25"), Price(24_900.0, 2), reduce_only=True
        )
    )
    h.client.submit_sync(entry, stop_loss=stop)
    assert broker.order_send_calls == 1
    h.client.submit_sync(entry, stop_loss=stop)  # the very same ClientOrderId delivered again
    assert broker.order_send_calls == 1
    assert any("DUPLICATE_CLIENT_ORDER_ID" in line for line in h.client.audit)


def _cancel(h, stop):
    return SimpleNamespace(
        client_order_id=stop.client_order_id,
        strategy_id=stop.strategy_id,
        instrument_id=IID,
        venue_order_id=h.order(stop).venue_order_id,
    )


def test_protective_cancel_with_a_raised_send_is_unknown_not_a_silent_cancel_rejection(h, broker):
    _, stop = h.submit_bracket(BUY, "0.25", 24_900.0)
    broker.raise_on_send.append(TimeoutError("ipc"))
    h.run(h.client._cancel_order(_cancel(h, stop)))
    assert h.order(stop).status is OrderStatus.ACCEPTED  # SL verified still present
    assert broker.positions_get()[0].sl == 24_900.0
    assert h.client.recon.state is not ReconciliationState.RECONCILED  # authority dropped


def test_protective_cancel_whose_response_was_lost_is_resolved_from_the_broker(h, broker):
    _, stop = h.submit_bracket(BUY, "0.25", 24_900.0)
    real_send = broker.order_send

    def executed_but_response_lost(request):
        real_send(request)
        return None

    broker.order_send = executed_but_response_lost
    h.run(h.client._cancel_order(_cancel(h, stop)))
    assert broker.positions_get()[0].sl == 0.0  # the broker DID remove it
    assert h.order(stop).status is OrderStatus.CANCELED  # verified against the broker, not guessed
    assert h.client.recon.state is not ReconciliationState.RECONCILED


def test_a_definite_broker_refusal_of_the_cancel_keeps_authority(h, broker):
    _, stop = h.submit_bracket(BUY, "0.25", 24_900.0)
    broker.send_retcode_override.append(int(Retcode.REJECT))
    h.run(h.client._cancel_order(_cancel(h, stop)))
    assert h.order(stop).status is OrderStatus.ACCEPTED
    assert broker.positions_get()[0].sl == 24_900.0


def test_broker_stop_that_differs_from_the_nautilus_stop_is_a_mismatch(h, broker):
    entry, stop = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert h.order(entry).status is OrderStatus.FILLED
    assert h.client.reconcile().state is ReconciliationState.RECONCILED
    broker.positions_get()[0].sl = 24_800.0  # modified behind our back (e.g. from the terminal)
    result = h.client.reconcile()
    kinds = {d.kind for d in result.discrepancies}
    assert result.state is ReconciliationState.MISMATCH
    assert DiscrepancyKind.PROTECTION_LEVEL_MISMATCH in kinds
    broker.positions_get()[0].sl = 24_900.0
    assert h.client.reconcile().state is ReconciliationState.RECONCILED
    assert stop is not None


def test_tp_missing_at_the_broker_is_a_mismatch(h, broker):
    from nautilus_trader.model.objects import Price, Quantity

    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert h.order(entry).status is OrderStatus.FILLED
    position = broker.positions_get()[0]
    tp_order = h._add(
        h.factory.limit(
            IID, OrderSide.SELL, Quantity.from_str("0.25"), Price(25_200.0, 2), reduce_only=True
        )
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
    result = h.client.reconcile()  # Nautilus believes a TP exists; the broker has none
    assert DiscrepancyKind.PROTECTION_LEVEL_MISMATCH in {d.kind for d in result.discrepancies}


def test_demo_slice_uses_the_broker_reported_account_leverage_as_the_cap(tmp_path):
    from nautilus_mt5.demo_slice import connect_and_verify, run_slice
    from tests.unit.nautilus_mt5.test_c7_demo_slice import SLICE, build, close, live_broker

    broker = live_broker()
    loop, asm = build(broker, tmp_path / "a")
    try:
        loop.run_until_complete(connect_and_verify(asm))
        m = loop.run_until_complete(run_slice(asm, SLICE, account_leverage=Decimal("20")))
        assert m.state == "DONE" and m.values["risk"]["max_leverage"] == "20"  # not the 30x ceiling
    finally:
        close(loop, asm)

    broker = live_broker()  # a 10x account cap cannot fit the minimum lot on 500 EUR: vetoed
    loop, asm = build(broker, tmp_path / "b")
    try:
        loop.run_until_complete(connect_and_verify(asm))
        m = loop.run_until_complete(run_slice(asm, SLICE, account_leverage=Decimal("10")))
        assert m.state == "FAILED" and "RISK_REJECTED" in (m.failure or "")
        assert broker.order_send_calls == 0
    finally:
        close(loop, asm)
