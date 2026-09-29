"""C5C: reconciliation (restart / mismatch), protective orders, reconnect, reports, guards."""

import ast
import inspect
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus, PositionSide
from nautilus_trader.model.identifiers import VenueOrderId

import nautilus_mt5
from nautilus_mt5.execution_client import Mt5ExecClientConfig
from nautilus_mt5.reconciliation import DiscrepancyKind
from nautilus_mt5.session import SessionState
from risk.models import ReconciliationState, RuntimeMode
from tests.unit.nautilus_mt5.harness import IID, ExecHarness

BUY, SELL = OrderSide.BUY, OrderSide.SELL
LENIENT = Mt5ExecClientConfig(autostart_sync=False, require_attached_protection=False)


@pytest.fixture
def h(broker, tmp_path):
    harness = ExecHarness(broker, tmp_path)
    yield harness
    harness.shutdown()


def kinds(h):
    return {d.kind for d in h.client.recon.discrepancies}


def open_long(h, sl=24_900.0):
    entry, stop = h.submit_bracket(BUY, "0.25", sl)
    assert h.order(entry).status is OrderStatus.FILLED
    return entry, stop


# -- restart
# ----------------------------------------------------------------------


def test_restart_reconciles_against_the_real_broker_snapshot_not_the_connection(broker, tmp_path):
    first = ExecHarness(broker, tmp_path)
    entry, _ = open_long(first)
    row = first.store.by_client_order_id(str(entry.client_order_id))
    ingested = first.store.ingested_count()
    first.session.disconnect()  # release the single-owner lock
    first.shutdown()

    second = ExecHarness(broker, tmp_path, live_engine=True)  # fresh process, empty cache, same DB
    try:
        # identity survived the restart (no volatile in-memory mapping)
        again = second.store.by_client_order_id(str(entry.client_order_id))
        assert (again.order_ticket, again.token, again.position_ticket) == (
            row.order_ticket,
            row.token,
            row.position_ticket,
        )
        # connect is NOT reconciliation: broker holds a position Nautilus does not know yet
        assert second.client.recon.state is ReconciliationState.MISMATCH
        assert kinds(second) == {DiscrepancyKind.UNEXPECTED_BROKER_POSITION}
        assert second.client.recon.runtime is RuntimeMode.HALTED
        assert second.store.ingested_count() == ingested  # old deals were NOT re-booked

        # Nautilus' own reconciliation consumes the adapter's reports ...
        mass = second.run(second.client.generate_mass_status())
        assert len(mass.position_reports) == 1
        result = second.engine.reconcile_execution_mass_status(mass)
        if inspect.isawaitable(result):
            second.run(result)
        second.pump(0.05)
        assert second.net_qty() == Decimal("0.25")
        # ... and only THEN does a fresh venue-snapshot comparison grant RECONCILED
        assert second.client.reconcile().state is ReconciliationState.RECONCILED
        assert second.client.recon.source.value == "venue_snapshot"
    finally:
        second.session.disconnect()
        second.shutdown()


def test_restart_with_matching_flat_state_reconciles_immediately(broker, tmp_path):
    first = ExecHarness(broker, tmp_path)
    first.session.disconnect()
    first.shutdown()
    second = ExecHarness(broker, tmp_path)
    try:
        assert second.client.recon.state is ReconciliationState.RECONCILED
    finally:
        second.session.disconnect()
        second.shutdown()


# -- mismatch detection
# ----------------------------------------------------------------------


def test_external_broker_trade_is_ingested_as_truth_and_halts(broker, tmp_path):
    h = ExecHarness(broker, tmp_path, live_engine=True)  # Nautilus' own report reconciliation
    try:
        broker.external_market_fill(is_buy=False, volume=0.5, comment="manual desktop trade")
        h.client.sync_once()
        h.pump(0.05)
        assert h.client.ingest_stats.external == 1
        assert h.net_qty() == Decimal("-0.50")  # inbound truth is in Nautilus (never ignored)
        assert h.client.recon.state is ReconciliationState.MISMATCH
        assert h.client.recon.runtime is RuntimeMode.HALTED
        assert DiscrepancyKind.EXTERNAL_ACTIVITY in kinds(h)
    finally:
        h.shutdown()


def test_unknown_working_order_at_the_broker_is_a_mismatch(h, broker):
    broker.orders[9001] = SimpleNamespace(
        ticket=9001,
        symbol="Ger40",
        type=2,
        state=1,
        volume_initial=0.25,
        volume_current=0.25,
        price_open=24_000.0,
        price_current=25_000.0,
        sl=0.0,
        tp=0.0,
        time_setup=1,
        time_expiration=0,
        magic=0,
        position_id=0,
        comment="foreign",
        external_id="",
    )
    assert h.client.reconcile().state is ReconciliationState.MISMATCH
    assert DiscrepancyKind.UNKNOWN_BROKER_ORDER in kinds(h)


def test_position_closed_at_the_broker_behind_our_back_is_a_missing_position(h, broker):
    open_long(h)
    broker.positions.clear()  # closed externally, no deal visible (yet)
    h.client.reconcile()
    assert DiscrepancyKind.MISSING_BROKER_POSITION in kinds(h)
    assert h.client.recon.state is ReconciliationState.MISMATCH


def test_quantity_mismatch_is_detected(h, broker):
    open_long(h)
    broker.positions_get()[0].volume = 0.75
    h.client.reconcile()
    assert DiscrepancyKind.POSITION_QTY_MISMATCH in kinds(h)


def test_a_later_clean_comparison_clears_the_halt(h, broker):
    open_long(h)
    broker.positions_get()[0].volume = 0.75
    h.client.reconcile()
    assert h.client.recon.runtime is RuntimeMode.HALTED
    broker.positions_get()[0].volume = 0.25
    assert h.client.reconcile().state is ReconciliationState.RECONCILED
    assert h.client.recon.runtime is RuntimeMode.READY


# -- protective orders
# ----------------------------------------------------------------------


def test_entry_that_fills_remotely_can_still_be_protected_without_local_reconciliation(h, broker):
    """The race: entry exists at the broker, local state lost, no stop attached."""
    deal = broker.external_market_fill(is_buy=True, volume=0.25, comment="lost-entry")
    h.client.recon.invalidate("local reconciliation lost")
    ticket = deal.position_id
    assert broker.positions_get()[0].sl == 0.0  # unprotected at the broker

    # Nautilus cannot even submit a reduce-only stop (it knows no position), so the adapter offers
    # a tighten-only emergency action that needs BROKER evidence of the ticket, not RECONCILED.
    assert h.client.emergency_protect(ticket, Decimal("24900")) is None
    assert broker.positions_get()[0].sl == 24_900.0
    # never a loosening (that would increase risk) while unreconciled
    reason = h.client.emergency_protect(ticket, Decimal("24800"))
    assert reason is not None and "NOT_VENUE_RECONCILED" in reason
    assert broker.positions_get()[0].sl == 24_900.0
    # and only for tickets the broker actually has, with valid stops
    assert h.client.emergency_protect(424242, Decimal("24900")) == "PROTECT_TICKET_NOT_AT_BROKER"
    assert "INVALID_STOPS" in h.client.emergency_protect(ticket, Decimal("25000.5"))


def test_new_exposure_is_blocked_while_a_position_is_unprotected_at_the_broker(broker, tmp_path):
    h = ExecHarness(broker, tmp_path, cfg=LENIENT)
    try:
        bare = h.market(BUY, "0.25")
        h.submit(bare)
        assert h.order(bare).status is OrderStatus.FILLED and broker.positions_get()[0].sl == 0.0
        h.client._cfg = replace(h.client._cfg, require_attached_protection=True)
        h.client.reconcile()
        assert h.client.recon.state is ReconciliationState.RECONCILED
        assert h.client.recon.unprotected_positions is True
        blocked = h.submit_bracket(BUY, "0.25", 24_900.0)[0]
        assert "UNPROTECTED_POSITION_AT_BROKER" in h.denial(blocked)
    finally:
        h.session.disconnect()
        h.shutdown()


def test_standalone_reduce_only_stop_becomes_a_broker_side_position_stop(broker, tmp_path):
    h = ExecHarness(broker, tmp_path, cfg=LENIENT)
    try:
        h.submit(h.market(BUY, "0.25"))
        stop = h.stop(SELL, "0.25", 24_900.0)
        h.submit(stop)
        assert h.order(stop).status is OrderStatus.ACCEPTED
        assert broker.positions_get()[0].sl == 24_900.0  # SL on the POSITION, not a pending order
        assert broker.orders_get() == ()  # nothing that could open a reverse position
        assert str(h.order(stop).venue_order_id).startswith("SL:")

        partial = h.stop(SELL, "0.5", 24_800.0)
        h.submit(partial)
        assert h.order(partial).status is OrderStatus.DENIED  # MT5 stops are per position
        assert "PROTECTIVE_QUANTITY_MUST_EQUAL_POSITION" in h.denial(partial)
        wrong_side = h.stop(BUY, "0.25", 24_800.0)
        h.submit(wrong_side)
        assert "PROTECTIVE_WRONG_SIDE" in h.denial(wrong_side)
    finally:
        h.session.disconnect()
        h.shutdown()


def modify(h, order, trigger):
    return SimpleNamespace(
        client_order_id=order.client_order_id,
        strategy_id=order.strategy_id,
        instrument_id=IID,
        venue_order_id=h.order(order).venue_order_id or VenueOrderId("SL:0"),
        trigger_price=__import__("nautilus_trader.model.objects", fromlist=["Price"]).Price(
            trigger, 2
        ),
        price=None,
        quantity=None,
    )


def test_tightening_a_stop_is_allowed_loosening_needs_reconciliation(h, broker):
    _, stop = open_long(h, sl=24_900.0)
    h.run(h.client._modify_order(modify(h, stop, 24_950.0)))  # tighten
    assert broker.positions_get()[0].sl == 24_950.0

    h.client.recon.invalidate("test")
    h.run(h.client._modify_order(modify(h, stop, 24_900.0)))  # loosen while unreconciled
    assert broker.positions_get()[0].sl == 24_950.0  # refused
    h.run(h.client._modify_order(modify(h, stop, 24_970.0)))  # tighten still fine
    assert broker.positions_get()[0].sl == 24_970.0

    h.client.reconcile()
    h.run(h.client._modify_order(modify(h, stop, 24_900.0)))  # loosen once reconciled
    assert broker.positions_get()[0].sl == 24_900.0


def test_cancelling_protection_is_risk_increasing_and_needs_reconciliation(h, broker):
    _, stop = open_long(h)
    cancel = SimpleNamespace(
        client_order_id=stop.client_order_id,
        strategy_id=stop.strategy_id,
        instrument_id=IID,
        venue_order_id=h.order(stop).venue_order_id,
    )
    h.client.recon.invalidate("test")
    h.run(h.client._cancel_order(cancel))
    assert broker.positions_get()[0].sl == 24_900.0 and h.order(stop).status is OrderStatus.ACCEPTED
    h.client.reconcile()
    h.run(h.client._cancel_order(cancel))
    assert broker.positions_get()[0].sl == 0.0 and h.order(stop).status is OrderStatus.CANCELED


def test_cancel_of_unknown_or_terminal_orders_is_rejected_not_silently_accepted(h):
    entry, _ = open_long(h)
    cancel = SimpleNamespace(
        client_order_id=entry.client_order_id,
        strategy_id=entry.strategy_id,
        instrument_id=IID,
        venue_order_id=None,
    )
    h.run(h.client._cancel_order(cancel))
    assert h.order(entry).status is OrderStatus.FILLED  # a filled market order cannot be cancelled


# -- reconnect
# ----------------------------------------------------------------------


def test_disconnect_reconnect_requires_reconciliation_before_any_new_exposure(h, broker):
    broker.disconnect()
    from nautilus_mt5.session import Mt5CallError

    with pytest.raises(Mt5CallError):
        h.client.sync_once()
    assert h.session.state is SessionState.DEGRADED
    denied, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert h.order(denied).status is OrderStatus.DENIED and "SESSION_DEGRADED" in h.denial(denied)

    broker.reconnect()
    assert h.session.reconnect().success and h.session.state is SessionState.CONNECTED
    assert all(call["login"] is None for call in broker.initialize_calls)  # never logs in
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert h.order(entry).status is OrderStatus.DENIED
    assert "NOT_VENUE_RECONCILED" in h.denial(entry)  # reconnect did NOT imply RECONCILED

    assert h.client.reconcile().state is ReconciliationState.RECONCILED
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert h.order(entry).status is OrderStatus.FILLED


def test_account_mismatch_at_connect_fails_closed(broker, tmp_path):
    broker.cfg.login = 555  # terminal is logged into a different account than expected
    from adapters.config import MT5ConnectionConfig
    from nautilus_mt5.session import Mt5Session

    harness = ExecHarness(broker, tmp_path, connect=False)
    harness.session = Mt5Session(
        broker,
        MT5ConnectionConfig(login=111, password="x", server="s", terminal_path="t"),
        lock_path=tmp_path / "other.lock",
    )
    harness.client._session = harness.session
    try:
        with pytest.raises(ConnectionError):
            harness.run(harness.client._connect())
        assert harness.session.state is SessionState.FAILED
        assert all(call["login"] is None for call in broker.initialize_calls)
    finally:
        harness.shutdown()


# -- MT5 request details
# ----------------------------------------------------------------------


def test_filling_mode_follows_the_symbol_and_unsupported_modes_are_refused(h, broker):
    broker.symbol_info_obj.filling_mode = 1  # FOK only
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert (
        broker.request_log[-1]["type_filling"] == 0 and h.order(entry).status is OrderStatus.FILLED
    )
    h.submit(h.market(SELL, "0.25", reduce_only=True))
    broker.symbol_info_obj.filling_mode = 0  # nothing allowed
    refused, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    assert "UNSUPPORTED_FILLING" in h.denial(refused)


def test_deviation_comes_from_config(broker, tmp_path):
    h = ExecHarness(
        broker, tmp_path, cfg=Mt5ExecClientConfig(autostart_sync=False, deviation_points=35)
    )
    try:
        h.submit_bracket(BUY, "0.25", 24_900.0)
        assert broker.request_log[0]["deviation"] == 35
    finally:
        h.session.disconnect()
        h.shutdown()


# -- reports
# ----------------------------------------------------------------------


def test_reports_expose_broker_truth_with_deal_ticket_identity(h, broker):
    entry, _ = open_long(h)
    positions = h.run(
        h.client.generate_position_status_reports(SimpleNamespace(instrument_id=None))
    )
    assert len(positions) == 1 and positions[0].position_side is PositionSide.LONG
    assert float(positions[0].quantity) == 0.25 and float(positions[0].avg_px_open) == 25_001.5
    fills = h.run(h.client.generate_fill_reports(SimpleNamespace(instrument_id=None)))
    assert [str(f.trade_id) for f in fills] == [str(broker.deals[0].ticket)]
    assert str(fills[0].client_order_id) == str(entry.client_order_id)
    orders = h.run(
        h.client.generate_order_status_reports(SimpleNamespace(instrument_id=None, open_only=False))
    )
    assert {str(o.client_order_id) for o in orders} >= {str(entry.client_order_id)}
    mass = h.run(h.client.generate_mass_status())
    assert len(mass.position_reports) == 1 and len(mass.fill_reports) == 1


def test_flat_position_report_lets_nautilus_close_a_stale_local_position(h, broker):
    positions = h.run(
        h.client.generate_position_status_reports(SimpleNamespace(instrument_id=None))
    )
    assert [p.position_side for p in positions] == [PositionSide.FLAT]


def test_account_state_uses_broker_margin_not_c4_assumptions(h, broker):
    open_long(h)
    h.client.sync_once()
    account = h.cache.account(h.client.account_id)
    used = broker.account_info().margin
    assert used > 0
    assert float(account.balance_locked().as_decimal()) == pytest.approx(used, rel=1e-6)
    assert float(account.balance_total().as_decimal()) == pytest.approx(broker.balance, rel=1e-6)


# -- guards
# ----------------------------------------------------------------------


def test_adapter_never_imports_metatrader5_or_legacy_state_owners():
    banned_roots = {"MetaTrader5", "execution", "portfolio", "persistence", "pipeline"}
    for path in sorted(Path(nautilus_mt5.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in banned_roots, f"{path.name} imports {name}"
                assert "real_client" not in name, f"{path.name} imports {name}"


def test_no_login_credentials_ever_reach_initialize(h, broker):
    open_long(h)
    h.session.reconnect()
    assert all(c["login"] is None and c["server"] is None for c in broker.initialize_calls)
