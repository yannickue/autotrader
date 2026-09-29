"""C6.5: the MT5 execution lane -- one dedicated thread, no overlapping IPC, per-symbol
serialization, and 'timeout != rejected'. Real threads; the loop is pumped by the harness."""

import asyncio
import threading
import time
from decimal import Decimal

import pytest
from nautilus_trader.model.enums import OrderSide, OrderStatus

from nautilus_mt5.execution_client import Mt5ExecClientConfig
from nautilus_mt5.executor import LaneTimeout, LaneViolation, LoopBridge, Mt5Executor
from nautilus_mt5.session import Mt5CallError
from nautilus_mt5.state import Mt5StateStore
from risk.models import ReconciliationState
from tests.unit.nautilus_mt5.harness import ExecHarness

BUY, SELL = OrderSide.BUY, OrderSide.SELL


@pytest.fixture
def lane():
    executor = Mt5Executor("test-lane")
    yield executor
    executor.shutdown()


@pytest.fixture
def h(broker, tmp_path):
    harness = ExecHarness(broker, tmp_path, lane=True)
    yield harness
    harness.shutdown()


# -- the lane itself --


def test_every_call_runs_on_one_dedicated_non_main_thread(lane):
    seen = {lane.run_sync(threading.get_ident) for _ in range(20)}
    assert len(seen) == 1 and threading.get_ident() not in seen
    assert lane.stats.worker_thread_ids == seen and lane.in_lane is False


def test_calls_are_fifo_and_never_overlap_even_from_many_threads(lane):
    active, peak, order = [0], [0], []
    guard = threading.Lock()

    def work(i):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.005)
        order.append(i)
        with guard:
            active[0] -= 1

    futures = []
    for i in range(30):
        futures.append(lane.submit(work, i))
    for f in futures:
        f.result(5)
    assert peak[0] == 1 and lane.stats.max_concurrent == 1
    assert order == list(range(30))  # strict FIFO


def test_concurrent_threads_submitting_are_serialized(lane):
    peak = [0]
    active = [0]
    guard = threading.Lock()

    def work():
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.002)
        with guard:
            active[0] -= 1

    def submitter():
        for _ in range(10):
            lane.run_sync(work)

    threads = [threading.Thread(target=submitter) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak[0] == 1 and lane.stats.completed >= 60


def test_symbol_sections_order_execution_changing_work_per_symbol(lane):
    events = []

    def op(tag):
        with lane.symbol_section("Ger40"):
            events.append(("start", tag))
            time.sleep(0.003)
            events.append(("end", tag))

    threads = [threading.Thread(target=lambda t=t: op(t)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    # no interleaving: every start is immediately followed by its own end
    for i in range(0, len(events), 2):
        assert events[i][0] == "start" and events[i + 1] == ("end", events[i][1])


def test_reentrant_use_inside_the_lane_does_not_deadlock(lane):
    def outer():
        return lane.run_sync(lambda: "inner")

    assert lane.run_sync(outer, timeout=2) == "inner"


def test_timeout_is_reported_as_unknown_not_as_failure_and_work_continues(lane):
    done = threading.Event()

    def slow():
        time.sleep(0.3)
        done.set()
        return "finished"

    with pytest.raises(LaneTimeout):
        lane.run_sync(slow, timeout=0.05)
    assert lane.stats.timeouts == 1
    assert done.wait(2)  # the call was NOT cancelled; a timeout carries no verdict


def test_lane_exceptions_propagate_and_lane_stays_usable(lane):
    with pytest.raises(ZeroDivisionError):
        lane.run_sync(lambda: 1 / 0)
    assert lane.run_sync(lambda: 7) == 7 and lane.stats.failed == 1


def test_async_run_awaits_without_blocking_the_loop(lane):
    async def scenario():
        started = time.perf_counter()
        ticks = []

        async def ticker():
            for _ in range(5):
                ticks.append(time.perf_counter())
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        await lane.run(time.sleep, 0.08)
        await task
        return started, ticks

    loop = asyncio.new_event_loop()
    try:
        started, ticks = loop.run_until_complete(scenario())
    finally:
        loop.close()
    assert len(ticks) == 5 and ticks[-1] - started < 0.2  # the loop kept running meanwhile


def test_loop_bridge_runs_callables_on_the_loop_thread():
    loop = asyncio.new_event_loop()
    bridge = LoopBridge(loop)
    bridge.bind_current_thread()
    main = threading.get_ident()
    result = {}

    async def scenario():
        def in_worker():
            return bridge.call(threading.get_ident)

        result["thread"] = await loop.run_in_executor(None, in_worker)

    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
    assert result["thread"] == main and bridge.calls_marshalled == 1


# -- the guard: MT5 IPC outside the lane is refused ------------------------------------------------


def test_session_refuses_mt5_calls_from_outside_the_lane(h, broker):
    with pytest.raises(LaneViolation):
        h.session.client.order_send({})  # raw access from the main thread
    with pytest.raises(LaneViolation):
        h.session.call("positions_get", h.session.client.positions_get)
    with pytest.raises(LaneViolation):
        h.session.reconnect()
    assert broker.order_send_calls == 0


# -- full stack through the lane --


def test_full_stack_all_broker_calls_on_the_lane_thread_and_nautilus_stays_on_the_loop(h, broker):
    main = threading.get_ident()
    entry, stop = h.submit_bracket(BUY, "0.25", 24_900.0)
    h.until(lambda: h.order(entry).status is OrderStatus.FILLED)
    assert h.order(stop).status is OrderStatus.ACCEPTED
    assert h.net_qty() == Decimal("0.25")
    threads = broker.all_call_threads()
    assert threads == h.lane.stats.worker_thread_ids and main not in threads
    assert broker.max_concurrency == 1
    assert h.client._bridge.calls_marshalled > 0  # events/cache went through the loop bridge


def test_concurrent_entries_reconcile_and_sync_never_overlap_and_never_duplicate_exposure(
    h, broker
):
    broker.call_latency_s = 0.002  # widen any race window
    first = h.factory.market(h_iid(), BUY, qty("0.25"))
    del first
    e1, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    e2, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    e3, _ = h.submit_bracket(BUY, "0.25", 24_900.0)

    async def hammer():
        await asyncio.gather(
            h.client.reconcile_async(),
            h.client._lane_run(h.client.sync_once),
            h.client.reconcile_async(),
        )

    h.run(hammer())
    h.until(
        lambda: all(
            h.order(o).status in (OrderStatus.FILLED, OrderStatus.DENIED) for o in (e1, e2, e3)
        )
    )
    statuses = sorted(h.order(o).status.name for o in (e1, e2, e3))
    assert statuses.count("FILLED") == 1 and statuses.count("DENIED") == 2
    assert broker.order_send_calls == 1  # exactly one exposure-changing request reached the broker
    assert broker.positions_get()[0].volume == 0.25 and h.net_qty() == Decimal("0.25")
    assert broker.max_concurrency == 1 and h.lane.stats.max_concurrent == 1


def test_state_store_is_safe_across_the_lane_and_loop_threads(tmp_path):
    store = Mt5StateStore(tmp_path / "s.db")
    errors = []

    def writer(n):
        try:
            for i in range(25):
                store.record_intent(
                    client_order_id=f"O-{n}-{i}",
                    strategy_id="S",
                    instrument_id="X",
                    kind="MARKET",
                    side="BUY",
                    quantity="1",
                )
                store.mark_ingested(n * 1000 + i, None)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert errors == [] and len(store.all_orders()) == 125 and store.ingested_count() == 125
    assert len({r.token for r in store.all_orders()}) == 125  # tokens stay unique
    store.close()


# -- timeout / unknown outcome through the executor layer --


def test_timeout_of_order_send_is_unknown_never_rejected_and_never_resent(broker, tmp_path):
    broker.send_delay_s = 0.6  # the order_send call outlives the caller's patience
    cfg = Mt5ExecClientConfig(autostart_sync=False, exposure_timeout_secs=0.15)
    harness = ExecHarness(broker, tmp_path, cfg=cfg, lane=True)
    try:
        entry, _stop = harness.submit_bracket(BUY, "0.25", 24_900.0)
        harness.pump(0.4)  # caller timed out; the lane thread is still inside order_send
        assert harness.order(entry).status is OrderStatus.SUBMITTED  # NOT rejected
        assert harness.client.recon.state is not ReconciliationState.RECONCILED
        assert any("LANE_TIMEOUT" in line for line in harness.client.audit)
        assert harness.store.by_client_order_id(str(entry.client_order_id)).status == "IN_DOUBT"

        harness.until(lambda: broker.positions_get() != (), timeout=5)  # the send DID execute
        harness.pump(0.9)  # let the in-flight lane call finish
        assert broker.order_send_calls == 1  # never blindly resent
        assert broker.positions_get()[0].volume == 0.25
        assert harness.order(entry).status is not OrderStatus.REJECTED
        # the timeout degraded the session and dropped authority: the rest of that call failed
        # closed; only an attach-only reconnect + explicit venue comparison restores it
        assert harness.client.recon.state is not ReconciliationState.RECONCILED
        assert harness.lane.run_sync(harness.session.reconnect).success
        result = harness.run(harness.client.reconcile_async())
        harness.pump(0.1)
        assert harness.order(entry).status is OrderStatus.FILLED
        assert result.state is ReconciliationState.RECONCILED, [
            (r.kind, r.status, r.order_ticket) for r in harness.store.all_orders()
        ] + list(harness.client.audit)
        assert broker.order_send_calls == 1
    finally:
        harness.shutdown()


def test_lost_response_through_the_lane_is_in_doubt_and_reconciles_without_resend(h, broker):
    broker.lose_response_after_execute = 1
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    h.pump(0.2)
    assert h.order(entry).status is OrderStatus.SUBMITTED
    assert h.store.by_client_order_id(str(entry.client_order_id)).status == "IN_DOUBT"
    assert h.lane.run_sync(h.session.reconnect).success
    h.run(h.client.reconcile_async())
    h.pump(0.1)
    assert h.order(entry).status is OrderStatus.FILLED and broker.order_send_calls == 1
    assert h.client.recon.state is ReconciliationState.RECONCILED


def test_lane_timeout_surfaces_as_call_error_never_as_a_verdict(h, broker):
    broker.call_latency_s = 0.3
    with pytest.raises(LaneTimeout):
        h.lane.run_sync(
            h.session.call, "positions_get", h.session.client.positions_get, timeout=0.05
        )
    broker.call_latency_s = 0.0
    h.lane.run_sync(lambda: None)  # queue drained
    assert isinstance(Mt5CallError("x", None), RuntimeError)


def h_iid():
    from tests.unit.nautilus_mt5.harness import IID

    return IID


def qty(text):
    from nautilus_trader.model.objects import Quantity

    return Quantity.from_str(text)


def test_exposure_changing_work_runs_inside_the_per_symbol_section(h, monkeypatch):
    entered = []
    original = h.lane.symbol_section

    def spy(symbol):
        entered.append(symbol)
        return original(symbol)

    monkeypatch.setattr(h.lane, "symbol_section", spy)
    entry, _ = h.submit_bracket(BUY, "0.25", 24_900.0)
    h.until(lambda: h.order(entry).status is OrderStatus.FILLED)
    h.run(h.client.reconcile_async())
    assert entered and set(entered) == {"Ger40"}
    assert len(entered) >= 2  # the submit and the reconcile both took the section


def test_data_client_polls_through_the_same_lane(broker, tmp_path):
    from decimal import Decimal as D
    from types import SimpleNamespace

    from adapters.config import MT5ConnectionConfig
    from nautilus_mt5.data_client import Mt5DataClientConfig
    from nautilus_mt5.factory import build_mt5_adapter
    from nautilus_mt5.instruments import InstrumentAssumptions
    from tests.unit.nautilus_mt5.harness import IID, Harness

    harness = Harness()
    harness.capture("DataEngine.process")
    adapter = build_mt5_adapter(
        client=broker,
        connection=MT5ConnectionConfig(
            login=broker.cfg.login, password="x", server="s", terminal_path="t"
        ),
        assumptions=InstrumentAssumptions(margin_init=D("0.05"), margin_maint=D("0.05")),
        loop=harness.loop,
        msgbus=harness.msgbus,
        cache=harness.cache,
        clock=harness.clock,
        state_path=tmp_path / "s.db",
        lock_path=tmp_path / "l",
        data_config=Mt5DataClientConfig(autostart_poller=False),
    )
    try:
        harness.run(adapter.data_client._connect())
        harness.run(adapter.data_client._subscribe_quote_ticks(SimpleNamespace(instrument_id=IID)))
        broker.server_time = 1_790_000_000
        published = harness.run(adapter.data_client._lane_run(adapter.data_client.poll_once))
        assert published == 1
        assert broker.all_call_threads() == adapter.lane.stats.worker_thread_ids
        assert threading.get_ident() not in broker.all_call_threads()
        assert broker.max_concurrency == 1
        harness.run(adapter.data_client._disconnect())
    finally:
        adapter.store.close()
        adapter.lane.shutdown()
        harness.close()
