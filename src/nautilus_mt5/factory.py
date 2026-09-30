"""Wiring helper: one shared attach-only session, one provider, both Nautilus clients.

`build_mt5_adapter` performs NO I/O: no connect, no MT5 call. Connection happens when Nautilus
starts the clients (`_connect`). The MT5 client object is INJECTED (`MT5ClientProtocol`): tests
pass a fake; only a deliberate, separately approved runner (later phase) may pass
`adapters.activtrades_mt5.real_client.get_real_client()`. TradingNode config/factory classes are
deferred to C7 so that no configuration object can start a live connection by accident.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nautilus_trader.common.component import LiveClock, MessageBus

from adapters.config import MT5ConnectionConfig
from nautilus_mt5.data_client import Mt5DataClientConfig, Mt5LiveMarketDataClient
from nautilus_mt5.execution_client import Mt5ExecClientConfig, Mt5LiveExecutionClient
from nautilus_mt5.executor import Mt5Executor
from nautilus_mt5.instruments import InstrumentAssumptions, Mt5InstrumentProvider
from nautilus_mt5.session import Mt5Session
from nautilus_mt5.state import Mt5StateStore
from nautilus_mt5.symbols import SymbolRegistry


@dataclass(slots=True)
class Mt5Adapter:
    session: Mt5Session
    provider: Mt5InstrumentProvider
    data_client: Mt5LiveMarketDataClient
    exec_client: Mt5LiveExecutionClient
    store: Mt5StateStore
    lane: Mt5Executor | None = None


def build_mt5_adapter(
    *,
    client: Any,
    connection: MT5ConnectionConfig,
    assumptions: InstrumentAssumptions,
    loop: asyncio.AbstractEventLoop,
    msgbus: MessageBus,
    cache: Any,
    clock: LiveClock,
    state_path: Path | str,
    lock_path: Path | None = None,
    registry: SymbolRegistry | None = None,
    data_config: Mt5DataClientConfig | None = None,
    exec_config: Mt5ExecClientConfig | None = None,
    use_lane: bool = True,
    allow_multiplier_and_cross_currency: bool = False,
    optional_canonicals: frozenset[str] = frozenset(),
) -> Mt5Adapter:
    # ONE dedicated thread carries all MT5 IPC; the session refuses calls from anywhere else.
    lane = Mt5Executor() if use_lane else None
    try:
        session = Mt5Session(client, connection, lock_path=lock_path, lane=lane)  # no login opt-in
    except BaseException:
        if lane is not None:
            lane.shutdown()
        raise
    provider = Mt5InstrumentProvider(
        client,
        assumptions=assumptions,
        registry=registry,
        allow_multiplier_and_cross_currency=allow_multiplier_and_cross_currency,
        optional_canonicals=optional_canonicals,
    )
    store = Mt5StateStore(state_path)
    data_client = Mt5LiveMarketDataClient(
        loop, msgbus, cache, clock, provider, session, data_config
    )
    exec_client = Mt5LiveExecutionClient(
        loop, msgbus, cache, clock, provider, session, store, exec_config
    )
    return Mt5Adapter(
        session=session,
        provider=provider,
        data_client=data_client,
        exec_client=exec_client,
        store=store,
        lane=lane,
    )
