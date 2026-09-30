from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from adapters.activtrades_mt5.fake_broker import FakeBrokerConfig, FakeMT5Broker
from adapters.activtrades_mt5.history import ServerTimePolicy
from demo.contracts import TradeIntent
from demo.execution.events import Accepted, Fill, PositionClosed, ProtectionConfirmed, Rejected
from demo.execution.executor import DemoExecutor, RiskApproval
from demo.execution.ports import InMemoryRecorder
from nautilus_mt5.constants import Retcode
from risk.models import ReconciliationState
from tests.unit.nautilus_mt5.conftest import real_symbol_info


class FixedRiskBridge:
    def __init__(self, quantity: str = "0.25") -> None:
        self.quantity = Decimal(quantity)
        self.calls = 0

    def size(self, *, intent, bid, ask, now):
        self.calls += 1
        return RiskApproval(quantity=self.quantity, risk_budget=Decimal("100"))


def make_intent(now: datetime, **overrides) -> TradeIntent:
    data = dict(
        opportunity_id="opp-1",
        phase="DISCOVERY",
        intent_id="intent-1",
        market="GER40",
        broker_symbol="Ger40",
        direction=1,
        entry_ref=25002.0,
        stop=24950.0,
        target=25150.0,
        min_space_r=1.5,
        valid_until_utc=(now + timedelta(minutes=1)).isoformat(),
        forced_flat_utc=(now + timedelta(hours=1)).isoformat(),
        risk_fraction=0.01,
    )
    data.update(overrides)
    return TradeIntent(**data)


def make_executor(*, config=None, bridge=None, recorder=None):
    broker = FakeMT5Broker(real_symbol_info(), config or FakeBrokerConfig())
    now = ServerTimePolicy().server_epoch_to_utc(broker.server_time)
    recorder = recorder or InMemoryRecorder()
    executor = DemoExecutor(
        broker=broker,
        risk_bridge=bridge or FixedRiskBridge(),
        recorder=recorder,
        reconciliation=lambda: ReconciliationState.RECONCILED,
        now=lambda: now,
    )
    return executor, broker, recorder, now


def test_demo_fill_has_mandatory_attached_stop_and_target():
    executor, broker, _, now = make_executor()
    events = executor.submit(make_intent(now))
    assert broker.order_send_calls == 1
    assert broker.request_log[0]["sl"] == 24950.0
    assert broker.request_log[0]["tp"] == 25150.0
    assert any(isinstance(e, Accepted) for e in events)
    assert any(isinstance(e, Fill) for e in events)
    assert any(isinstance(e, ProtectionConfirmed) for e in events)
    assert len(broker.positions) == 1


def test_duplicate_intent_sends_exactly_once():
    executor, broker, _, now = make_executor()
    first = executor.submit(make_intent(now))
    second = executor.submit(make_intent(now))
    assert broker.order_send_calls == 1
    assert second == first


@pytest.mark.parametrize(
    ("intent_changes", "reason"),
    [
        ({"valid_until_utc": "2020-01-01T00:00:00+00:00"}, "stale_signal"),
        ({"target": 25001.0}, "target_crossed_at_fill"),
        ({"stop": 25001.75}, "structural_invalidation_crossed"),
        ({"target": 25050.0, "min_space_r": 2.0}, "min_space_r"),
    ],
)
def test_parity_rejections_never_send(intent_changes, reason):
    executor, broker, _, now = make_executor()
    events = executor.submit(make_intent(now, **intent_changes))
    assert events == [Rejected(intent_id="intent-1", reason=reason)]
    assert broker.order_send_calls == 0


def test_unreconciled_and_non_demo_accounts_fail_closed():
    executor, broker, _, now = make_executor(config=FakeBrokerConfig(trade_mode=2))
    assert executor.submit(make_intent(now))[0].reason == "non_demo_account"
    assert broker.order_send_calls == 0

    broker.cfg.trade_mode = 0
    executor.set_reconciliation(lambda: ReconciliationState.NOT_RECONCILED)
    assert executor.submit(make_intent(now, intent_id="intent-2"))[0].reason == "not_reconciled"
    assert broker.order_send_calls == 0


def test_size_below_min_is_rejected_without_rounding_up():
    executor, broker, _, now = make_executor(bridge=FixedRiskBridge("0.10"))
    result = executor.submit(make_intent(now))
    assert result[0].reason == "size_below_min"
    assert broker.order_send_calls == 0


def test_broker_reject_is_not_retried():
    executor, broker, _, now = make_executor()
    broker.send_retcode_override.append(Retcode.REJECT)
    result = executor.submit(make_intent(now))
    assert result[0].reason == "broker_reject"
    assert broker.order_send_calls == 1


def test_stale_feed_and_disconnect_block_new_exposure():
    executor, broker, _, now = make_executor()
    broker.server_time -= 120
    assert executor.submit(make_intent(now))[0].reason == "stale_feed"
    assert broker.order_send_calls == 0

    executor, broker, _, now = make_executor()
    broker.disconnect()
    assert executor.submit(make_intent(now))[0].reason == "broker_disconnect"
    assert broker.order_send_calls == 0


def test_forced_flat_uses_one_reduce_only_close():
    executor, broker, _, now = make_executor()
    intent = make_intent(now, forced_flat_utc=(now + timedelta(seconds=1)).isoformat())
    executor.submit(intent)
    events = executor.on_clock(now + timedelta(seconds=2))
    assert len(broker.positions) == 0
    assert broker.order_send_calls == 2
    assert broker.request_log[-1]["position"]
    assert events == [
        PositionClosed(
            intent_id="intent-1",
            broker_position_id=events[0].broker_position_id,
            exit_reason="SESSION_END",
        )
    ]


class DropsAttachedStop(FakeMT5Broker):
    def _book_fill(self, *args, **kwargs):
        deal = super()._book_fill(*args, **kwargs)
        position = self.positions.get(deal.position_id)
        if position is not None and deal.entry == 0:
            position.sl = 0.0
        return deal


def test_unconfirmed_stop_immediately_flattens_and_halts():
    broker = DropsAttachedStop(real_symbol_info(), FakeBrokerConfig())
    now = ServerTimePolicy().server_epoch_to_utc(broker.server_time)
    executor = DemoExecutor(
        broker=broker,
        risk_bridge=FixedRiskBridge(),
        recorder=InMemoryRecorder(),
        reconciliation=lambda: ReconciliationState.RECONCILED,
        now=lambda: now,
    )
    result = executor.submit(make_intent(now))
    assert result[-1].reason == "protection_unconfirmed"
    assert len(broker.positions) == 0
    assert broker.order_send_calls == 2
    assert executor.submit(make_intent(now, intent_id="intent-2"))[0].reason == "halted"
    assert broker.order_send_calls == 2
