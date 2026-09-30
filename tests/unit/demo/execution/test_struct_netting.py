# ruff: noqa: E501
"""Lane S: MT5 netting coherence through the REAL Mt5DemoStack against the fake broker.

Flat symbol: a signal of any STRUCT variant trades. Occupied symbol (open position OR an ACCEPTED/SENT registry row):
a later same-direction signal is ADDON_* (-> CONCURRENT_SIGNAL / ADD_ON_CANDIDATE), an opposite one OPPOSITE_SIDE_* (->
REVERSAL_CANDIDATE); neither ever creates a second broker position or flips through zero. After the close the symbol trades again."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from demo.execution import registry as reg
from demo.execution.events import PositionClosed, Rejected
from demo.opportunity.arbitration import (
    ADD_ON_CANDIDATE,
    CONCURRENT_SIGNAL,
    REVERSAL_CANDIDATE,
    classify_stack_code,
)
from tests.unit.demo.execution.stack_harness import (
    add_phase2_symbols,
    build_broker,
    make_intent,
    make_stack,
)

ADDON = "ADDON_EXPOSURE_NOT_SUPPORTED_V1"
ADDON_SHARED = "ADDON_SHARED_STOP_POSSIBLE_NOT_YET_IMPLEMENTED"
OPPOSITE = "OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1"


def btc_intent(**kw):
    kw.setdefault("entry_ref", 83746.28)
    kw.setdefault("stop", 83146.28)
    kw.setdefault("target", 85000.0)
    return make_intent(market="BTCUSD", broker_symbol="BTCUSD", **kw)


@pytest.fixture
def env(tmp_path):
    stacks = []

    def build():
        broker = build_broker()
        add_phase2_symbols(broker)
        stack = make_stack(broker, tmp_path, extra_markets=("BRENT", "BTCUSD"))
        stacks.append(stack)
        stack.start()
        return broker, stack

    yield build
    for s in stacks:
        s.stop()


def kinds(events):
    return [type(e).__name__ for e in events]


@pytest.mark.parametrize("variant_tag", ["breakout", "confirmed", "retest", "fade"])
def test_any_variant_trades_when_the_symbol_is_flat(env, variant_tag):
    broker, stack = env()
    ev = stack.submit(btc_intent(intent_id=f"intent-{variant_tag}"))
    assert kinds(ev) == ["Accepted", "Fill", "ProtectionConfirmed"], ev
    assert broker.order_send_calls == 1 and stack.has_position("BTCUSD")


def test_second_signal_while_open_is_rejected_never_a_second_position_or_a_flip(env):
    broker, stack = env()
    assert kinds(stack.submit(btc_intent(intent_id="first")))[0] == "Accepted"
    sends = broker.order_send_calls
    same = stack.submit(btc_intent(intent_id="second-same-direction"))
    assert kinds(same) == ["Rejected"] and same[0].reason in (ADDON, ADDON_SHARED)
    assert classify_stack_code(same[0].reason) == (CONCURRENT_SIGNAL, ADD_ON_CANDIDATE)  # no pyramiding
    opp = stack.submit(btc_intent(intent_id="third-opposite", direction=-1, entry_ref=83746.28, stop=84346.28, target=82500.0))
    assert kinds(opp) == ["Rejected"] and opp[0].reason == OPPOSITE
    assert classify_stack_code(opp[0].reason) == (REVERSAL_CANDIDATE,)  # never flips through zero
    assert broker.order_send_calls == sends, "no further order may reach the broker"
    assert len(broker.positions_get(symbol="BTCUSD")) == 1
    assert stack.open_intents() == ("first",)


def test_an_in_flight_accepted_or_sent_registry_row_blocks_overlap_without_a_broker_position(env):
    broker, stack = env()
    for status, same_dir, code in ((reg.ACCEPTED, True, ADDON), (reg.SENT, False, OPPOSITE)):
        rid = f"inflight-{status}"
        assert stack._registry.insert(
            intent_id=rid, client_order_id="c-" + rid, market="BTCUSD", direction=1, stop="83146.28", target=None,
            forced_flat_utc=None, status=status, created_utc=datetime.now(UTC).isoformat(),
        )
        ev = stack.submit(btc_intent(intent_id=f"new-{status}", direction=1 if same_dir else -1,
                                     **({} if same_dir else {"stop": 84346.28, "target": 82500.0})))
        assert kinds(ev) == ["Rejected"] and isinstance(ev[0], Rejected) and ev[0].reason == code, ev
        stack._registry.update(rid, status=reg.CLOSED)
    assert broker.order_send_calls == 0 and not broker.positions_get(symbol="BTCUSD")


def test_a_later_fresh_signal_after_the_position_closed_may_trade_again(env):
    broker, stack = env()
    assert kinds(stack.submit(btc_intent(intent_id="first")))[0] == "Accepted"
    assert kinds(stack.submit(btc_intent(intent_id="blocked")))[0] == "Rejected"
    broker.set_symbol_quote("BTCUSD", 82900.0, 82983.5)  # broker-side stop executes
    assert [e for e in stack.poll_events() if isinstance(e, PositionClosed)]
    assert not stack.has_position("BTCUSD")
    assert kinds(stack.submit(btc_intent(intent_id="after-flat", entry_ref=82983.5, stop=82400.0, target=84000.0)))[0] == "Accepted"
