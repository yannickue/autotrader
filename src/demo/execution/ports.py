"""Small structural ports and deterministic in-memory test adapters."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from demo.contracts import TradeIntent
from demo.execution.events import ExecutionEvent


class OpportunitySource(Protocol):
    def poll(self) -> Iterable[TradeIntent]: ...


class Recorder(Protocol):
    def record(self, event: ExecutionEvent) -> None: ...


class InMemoryOpportunitySource:
    def __init__(self, intents: Iterable[TradeIntent] = ()) -> None:
        self._intents = list(intents)

    def poll(self) -> list[TradeIntent]:
        result, self._intents = self._intents, []
        return result


class InMemoryRecorder:
    def __init__(self) -> None:
        self.events: list[ExecutionEvent] = []

    def record(self, event: ExecutionEvent) -> None:
        self.events.append(event)
