"""C7 demo vertical slice, end to end, against the stateful FAKE broker (zero real MT5).

The same `assemble / connect_and_verify / run_slice` code runs against the real ActivTrades demo
terminal through scripts/c7_demo_vertical_slice.py (user-executed). Here it proves: real Nautilus
Trader + Strategy + RiskEngine + ExecutionEngine + our lane-backed MT5 clients -> min-lot bracket
entry with a broker-side stop -> controlled reduce-only close -> Nautilus PnL == broker PnL ->
fresh VENUE_SNAPSHOT reconciliation -> restart proof."""

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from adapters.activtrades_mt5.fake_broker import FakeBrokerConfig, FakeMT5Broker
from adapters.config import MT5ConnectionConfig
from nautilus_mt5.data_client import Mt5DataClientConfig
from nautilus_mt5.demo_slice import (
    DemoSliceConfig,
    SliceAbort,
    assemble,
    connect_and_verify,
    run_slice,
)
from nautilus_mt5.execution_client import Mt5ExecClientConfig
from nautilus_mt5.executor import LaneViolation
from risk.models import ReconciliationState
from tests.unit.nautilus_mt5.conftest import real_symbol_info


def live_broker(**config) -> FakeMT5Broker:
    broker = FakeMT5Broker(real_symbol_info(), FakeBrokerConfig(balance=500.0, **config))
    broker.live_offset_s = 7200  # server clock follows the wall clock (CEST offset)
    broker.bid, broker.ask = 25_000.0, 25_001.75
    return broker


def connection(broker) -> MT5ConnectionConfig:
    return MT5ConnectionConfig(login=broker.cfg.login, password="x", server="s", terminal_path="t")


def build(broker, tmp_path, *, state="state.db", auto_reconcile=True):
    loop = asyncio.new_event_loop()
    asm = assemble(
        client=broker,
        connection=connection(broker),
        loop=loop,
        state_path=tmp_path / state,
        lock_path=tmp_path / "lock",
        exec_config=Mt5ExecClientConfig(
            autostart_sync=True, sync_interval_secs=0.05, auto_reconcile_on_connect=auto_reconcile
        ),
        data_config=Mt5DataClientConfig(poll_interval_secs=0.05),
    )
    return loop, asm


def close(loop, asm):
    async def shutdown():
        await asm.adapter.exec_client._disconnect()
        await asm.adapter.data_client._disconnect()

    try:
        loop.run_until_complete(shutdown())
    finally:
        asm.adapter.store.close()
        asm.adapter.lane.shutdown()
        loop.close()


SLICE = DemoSliceConfig(hold_seconds=0.8, fill_timeout_s=5, close_timeout_s=5, max_quote_age_s=60)


def test_full_demo_slice_against_fake_broker(tmp_path):
    broker = live_broker()
    loop, asm = build(broker, tmp_path)
    try:
        facts = loop.run_until_complete(connect_and_verify(asm))
        assert facts["trade_mode"] == 0 and facts["positions"] == 0 and facts["working_orders"] == 0
        m = loop.run_until_complete(run_slice(asm, SLICE))
        assert m.state == "DONE" and m.failure is None, (m.state, m.failure, m.events)
        adapter_threads = set(
            broker.all_call_threads()
        )  # before the test itself touches the broker

        # risk/sizing approved the broker minimum lot, sized by the caps (not the 30x ceiling)
        risk = m.values["risk"]
        assert Decimal(risk["quantity"]) == Decimal("0.25")
        assert Decimal(risk["leverage"]) < Decimal("30") and risk["max_leverage"] == "30"

        # the entry carried a broker-side stop in the SAME order_send; a controlled close followed
        sends = [r for r in broker.request_log]
        assert len(sends) == 2 and sends[0]["sl"] > 0 and sends[0]["volume"] == 0.25
        assert "position" in sends[1] and sends[1]["type"] == 1  # reduce-only close bound to ticket
        assert broker.positions_get() == () and asm.cache.positions_open() == []

        # Nautilus PnL == broker PnL (long 0.25 @ ask, sold @ bid)
        (closed,) = asm.cache.positions_closed()
        broker_pnl = [d.profit for d in broker.visible_deals()][-1]
        assert abs(float(closed.realized_pnl.as_decimal()) - broker_pnl) < 0.01
        assert float(closed.realized_pnl.as_decimal()) == pytest.approx(-0.25 * 1.75, abs=0.01)

        # fresh venue-snapshot reconciliation after the lifecycle
        result = loop.run_until_complete(asm.adapter.exec_client.reconcile_async())
        assert result.state is ReconciliationState.RECONCILED
        assert result.source.value == "venue_snapshot"
        assert broker.max_concurrency == 1
        assert adapter_threads == asm.adapter.lane.stats.worker_thread_ids
        assert all(c["login"] is None for c in broker.initialize_calls)
    finally:
        close(loop, asm)


def test_restart_proof_reattaches_and_reconciles_from_a_real_snapshot(tmp_path):
    broker = live_broker()
    loop, asm = build(broker, tmp_path)
    try:
        loop.run_until_complete(connect_and_verify(asm))
        m = loop.run_until_complete(run_slice(asm, SLICE))
        assert m.state == "DONE"
        ingested = asm.adapter.store.ingested_count()
    finally:
        close(loop, asm)

    # --- controlled restart: brand-new runtime, same durable state, terminal already logged in
    loop2, asm2 = build(broker, tmp_path, auto_reconcile=False)
    try:

        async def restart():
            await asm2.adapter.exec_client._connect()
            asm2.adapter.exec_client._set_connected(True)
            first = asm2.adapter.exec_client.recon.state  # before any comparison
            mass = await asm2.adapter.exec_client.generate_mass_status()
            result = await asm2.adapter.exec_client.reconcile_async()
            return first, mass, result

        first, mass, result = loop2.run_until_complete(restart())
        assert first is ReconciliationState.NOT_RECONCILED  # never inherited from before
        assert len(mass.position_reports) == 1  # FLAT report from the broker snapshot
        assert result.state is ReconciliationState.RECONCILED
        assert asm2.adapter.store.ingested_count() == ingested  # old deals not re-booked
        assert asm2.cache.positions_open() == [] and broker.positions_get() == ()
    finally:
        close(loop2, asm2)


def test_slice_aborts_before_any_order_on_every_stop_condition(tmp_path):
    # non-demo account
    broker = live_broker()
    real_account_info = broker.account_info

    def real_money():
        info = real_account_info()
        info.trade_mode = 2
        return info

    broker.account_info = real_money
    loop, asm = build(broker, tmp_path / "a")
    try:
        with pytest.raises(SliceAbort, match="not a DEMO"):
            loop.run_until_complete(connect_and_verify(asm))
        assert broker.order_send_calls == 0 and broker.check_log == []
    finally:
        close(loop, asm)

    # book not flat
    broker = live_broker()
    broker.external_market_fill(is_buy=True, volume=0.25)
    loop, asm = build(broker, tmp_path / "b")
    try:
        with pytest.raises((SliceAbort, ConnectionError, RuntimeError)):
            loop.run_until_complete(connect_and_verify(asm))
        assert broker.order_send_calls == 0 and broker.check_log == []  # nothing was sent
    finally:
        close(loop, asm)

    # hedging account fails closed at connect
    broker = live_broker()
    broker.cfg.margin_mode = 2
    loop, asm = build(broker, tmp_path / "c")
    try:
        with pytest.raises(Exception, match="RETAIL_HEDGING"):
            loop.run_until_complete(connect_and_verify(asm))
    finally:
        close(loop, asm)


def test_stale_or_wide_quotes_never_produce_an_entry(tmp_path):
    broker = live_broker()
    broker.live_offset_s = 7200 - 3600  # quotes look an hour old
    loop, asm = build(broker, tmp_path)
    try:
        loop.run_until_complete(connect_and_verify(asm))
        cfg = DemoSliceConfig(
            hold_seconds=0.1, fill_timeout_s=0.2, close_timeout_s=0.2, max_quote_age_s=15
        )
        started = time.perf_counter()

        # shorten the wait: no valid quote ever arrives
        async def go():
            return await asyncio.wait_for(run_slice(asm, cfg), timeout=1.5)

        with pytest.raises(TimeoutError):
            loop.run_until_complete(go())
        assert broker.order_send_calls == 0 and broker.check_log == []
        assert time.perf_counter() - started < 5
    finally:
        close(loop, asm)


def test_risk_gate_can_veto_the_entry_and_nothing_reaches_the_broker(tmp_path):
    broker = live_broker()
    broker.cfg.balance = 5.0  # equity far too small for even one minimum lot
    broker.balance = 5.0
    loop, asm = build(broker, tmp_path)
    try:
        loop.run_until_complete(connect_and_verify(asm))
        m = loop.run_until_complete(run_slice(asm, SLICE))
        assert m.state == "FAILED" and "RISK_REJECTED" in (m.failure or ""), m.failure
        assert broker.order_send_calls == 0 and broker.check_log == []
    finally:
        close(loop, asm)


def test_adapter_is_a_second_gate_an_invalid_stop_is_denied_before_the_broker(tmp_path):
    broker = live_broker()
    loop, asm = build(broker, tmp_path)
    try:
        loop.run_until_complete(connect_and_verify(asm))
        tight = DemoSliceConfig(
            hold_seconds=0.1,
            fill_timeout_s=2,
            close_timeout_s=2,
            max_quote_age_s=60,
            stop_distance=Decimal("0.5"),
        )
        m = loop.run_until_complete(run_slice(asm, tight))
        assert m.state == "FAILED" and "INVALID_STOPS" in (m.failure or ""), m.failure
        assert broker.order_send_calls == 0 and broker.check_log == []
    finally:
        close(loop, asm)


def test_real_client_is_unreachable_and_login_is_refused(tmp_path):
    broker = live_broker()
    with pytest.raises(Exception, match="attach-only"):
        assemble(
            client=broker,
            connection=MT5ConnectionConfig(
                login=1, password="x", server="s", terminal_path="t", allow_account_login=True
            ),
            loop=asyncio.new_event_loop(),
            state_path=tmp_path / "s.db",
        )
    assert isinstance(LaneViolation("x"), RuntimeError) and datetime.now(UTC).year >= 2026


def test_dry_run_stops_after_the_real_order_check_and_never_calls_order_send(tmp_path):
    broker = live_broker()
    loop = asyncio.new_event_loop()
    asm = assemble(
        client=broker,
        connection=connection(broker),
        loop=loop,
        state_path=tmp_path / "s.db",
        lock_path=tmp_path / "l",
        exec_config=Mt5ExecClientConfig(autostart_sync=False, dry_run=True),
        data_config=Mt5DataClientConfig(poll_interval_secs=0.05),
    )
    try:
        loop.run_until_complete(connect_and_verify(asm))
        m = loop.run_until_complete(run_slice(asm, SLICE))
        assert m.state == "FAILED" and "DRY_RUN_ORDER_CHECK_OK" in (m.failure or "")
        assert len(broker.check_log) == 1 and broker.order_send_calls == 0
        assert broker.check_log[0]["sl"] > 0  # the exact live shape was validated
        assert broker.positions_get() == () and asm.cache.positions_open() == []
    finally:
        close(loop, asm)


def test_report_contains_the_required_expected_vs_actual_measurements(tmp_path):
    from nautilus_mt5.demo_slice import build_report, collect_broker_facts, expected_margin

    broker = live_broker(commission_per_lot=0.0)
    loop, asm = build(broker, tmp_path)
    started = datetime.now(UTC)
    try:
        loop.run_until_complete(connect_and_verify(asm))
        m = loop.run_until_complete(run_slice(asm, SLICE))
        facts = loop.run_until_complete(
            asm.adapter.exec_client._lane_run(collect_broker_facts, asm.adapter, started)
        )
        margin = loop.run_until_complete(
            asm.adapter.exec_client._lane_run(expected_margin, asm.adapter, 25_001.75, 0.25)
        )
        recon = loop.run_until_complete(asm.adapter.exec_client.reconcile_async())
        report = build_report(
            m,
            facts,
            reconciliation={"state": recon.state.value, "source": recon.source.value},
            nautilus_state={
                "open_positions": len(asm.cache.positions_open()),
                "open_orders": len(asm.cache.orders_open()),
            },
            expected_margin_eur=margin,
            dry_run=False,
        )
        ev = report["expected_vs_actual"]
        assert ev["expected_quantity"] == "0.25" and ev["broker_quantity"] == 0.25
        assert ev["entry_slippage_points"] == 0.0 and ev["broker_stop"] == pytest.approx(
            float(ev["expected_stop"]), abs=0.011
        )
        assert ev["expected_margin_eur"] == pytest.approx(25_001.75 * 0.25 * 0.05, rel=1e-6)
        assert ev["broker_peak_margin_eur"] > 0 and ev["submit_to_entry_fill_ms"] > 0
        assert len(report["broker"]["deal_tickets"]) == 2 and report["broker"]["position_ticket"]
        assert all(report["verdict"].values()), report["verdict"]
    finally:
        close(loop, asm)
