from types import SimpleNamespace

from demo.execution.strategy import DemoTraderStrategy


def test_strategy_forwards_intent_to_executor():
    calls = []
    executor = SimpleNamespace(submit=lambda intent: calls.append(intent) or ["accepted"])
    strategy = DemoTraderStrategy(executor=executor)
    intent = object()
    assert strategy.submit_intent(intent) == ["accepted"]
    assert calls == [intent]
