# ruff: noqa: E501
"""Shared harness for the Mt5DemoStack tests: a 5-market fake broker + stack factory.

ZERO real MT5: every test talks to ``FakeMT5Broker`` (multi-symbol mode). The stack is started for
real (real Nautilus kernel thread, real MT5 lane thread) against it.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from adapters.activtrades_mt5.fake_broker import FakeBrokerConfig, FakeMT5Broker
from adapters.config import MT5ConnectionConfig
from demo.contracts import TradeIntent
from demo.execution.live import Mt5DemoStack, StackConfig
from tests.unit.nautilus_mt5.conftest import real_symbol_info
from tests.unit.nautilus_mt5.harness import make_rates

EURUSD_MID = 1.17
FX_USD_TO_EUR = 1.0 / EURUSD_MID

QUOTES = {
    "Ger40": (25000.0, 25001.5),
    "UsaTec": (21000.0, 21001.0),
    "Usa500": (6000.0, 6000.8),
    "GOLD": (4169.5, 4169.97),
    "EURUSD": (1.16995, 1.17005),
}


def berlin_offset_s(moment: datetime | None = None) -> int:
    moment = moment or datetime.now(UTC)
    return int(moment.astimezone(ZoneInfo("Europe/Berlin")).utcoffset().total_seconds())


def symbol(name: str, path: str, **over) -> SimpleNamespace:
    info = copy.deepcopy(real_symbol_info())
    info.name, info.path = name, path
    for key, value in over.items():
        setattr(info, key, value)
    return info


def build_broker(*, balance: float = 10_000.0, **cfg) -> FakeMT5Broker:
    broker = FakeMT5Broker(real_symbol_info(), FakeBrokerConfig(balance=balance, **cfg))
    broker.live_offset_s = berlin_offset_s()  # server clock follows the wall clock
    broker.bid, broker.ask = QUOTES["Ger40"]
    common = dict(trade_stops_level=10, trade_tick_value=0.01, trade_tick_value_loss=0.01,
                  trade_tick_value_profit=0.01)
    broker.add_symbol(
        symbol("UsaTec", "Cash Indices\\UsaTec", volume_min=0.2, volume_step=0.2,
               volume_max=200.0, description="US Tech 100 Cash Index", **common),
        *QUOTES["UsaTec"],
    )
    broker.add_symbol(
        symbol("Usa500", "Cash Indices\\Usa500", volume_min=0.5, volume_step=0.5,
               volume_max=500.0, description="SP 500 Cash Index", **common),
        *QUOTES["Usa500"],
    )
    broker.add_symbol(
        symbol("GOLD", "Metals\\GOLD", trade_contract_size=100.0, volume_min=0.01,
               volume_step=0.01, volume_max=50.0, currency_base="XAU", currency_profit="USD",
               currency_margin="USD", description="Gold", **common),
        *QUOTES["GOLD"],
        profit_fx=FX_USD_TO_EUR,
    )
    broker.add_symbol(
        symbol("EURUSD", "Forex\\Majors\\EURUSD", digits=5, point=1e-05, trade_tick_size=1e-05,
               trade_contract_size=100000.0, volume_min=0.01, volume_step=0.01, volume_max=50.0,
               currency_base="EUR", currency_profit="USD", currency_margin="EUR",
               description="Euro vs US Dollar", trade_stops_level=10,
               trade_tick_value=1.0, trade_tick_value_loss=1.0, trade_tick_value_profit=1.0),
        *QUOTES["EURUSD"],
        profit_fx=FX_USD_TO_EUR,
    )
    return broker


def connection(broker: FakeMT5Broker) -> MT5ConnectionConfig:
    return MT5ConnectionConfig(
        login=broker.cfg.login, password="x", server=broker.cfg.server, terminal_path="t"
    )


FAST = StackConfig(
    submit_wait_s=8.0, exposure_timeout_s=6.0, flatten_wait_s=8.0, sync_interval_s=0.05,
    start_timeout_s=30.0, lock_heartbeat_s=0.2, bar_min_refetch_s=0.0, reconcile_retry_s=0.0,
    disconnect_grace_s=0.5, close_grace_s=0.5,
)


def make_stack(
    broker: FakeMT5Broker, tmp_path: Path, *, dry_run: bool = False, config: StackConfig = FAST,
    now=None,
) -> Mt5DemoStack:
    return Mt5DemoStack(
        client=broker,
        connection=connection(broker),
        state_dir=tmp_path / "state",
        lock_path=tmp_path / "terminal.lock",
        dry_run=dry_run,
        config=config,
        now=now,
    )


def make_intent(
    now: datetime | None = None,
    *,
    intent_id: str = "intent-1",
    market: str = "GER40",
    broker_symbol: str = "Ger40",
    direction: int = 1,
    entry_ref: float = 25002.0,
    stop: float = 24950.0,
    target: float | None = 25150.0,
    min_space_r: float = 1.5,
    valid_s: int = 90,
    flat_in_s: int | None = 3600,
    risk_fraction: float = 0.01,
) -> TradeIntent:
    now = now or datetime.now(UTC)
    return TradeIntent(
        opportunity_id="opp-" + intent_id,
        phase="DISCOVERY",
        intent_id=intent_id,
        market=market,
        broker_symbol=broker_symbol,
        direction=direction,
        entry_ref=entry_ref,
        stop=stop,
        target=target,
        min_space_r=min_space_r,
        valid_until_utc=(now + timedelta(seconds=valid_s)).isoformat(),
        forced_flat_utc=None if flat_in_s is None else (now + timedelta(seconds=flat_in_s)).isoformat(),
        risk_fraction=risk_fraction,
    )


def m5_rows(broker: FakeMT5Broker, *, n: int = 40, base: float = 25000.0, end: datetime | None = None):
    """n M5 rows ending with the FORMING bar (as the real terminal returns them)."""
    end = end or datetime.now(UTC)
    offset = berlin_offset_s(end)
    last_open = int(end.timestamp() // 300 * 300)
    rows = []
    for i in range(n):
        t = last_open - (n - 1 - i) * 300 + offset  # server-clock epoch
        px = base + i
        rows.append((t, px, px + 3, px - 3, px + 1, 100 + i, 12, 0))
    return make_rates(rows)


def inject_closed_trade(broker: FakeMT5Broker, *, profit: float, symbol: str = "Ger40") -> None:
    """A finished round trip TODAY (broker-truth deals) that realised ``profit`` (balance moves)."""
    pid = broker._ticket()
    for entry, kind, pnl in ((0, 0, 0.0), (1, 1, profit)):
        broker.deals.append(
            SimpleNamespace(
                ticket=broker._ticket(), order=broker._ticket(), time=broker.server_time,
                time_msc=broker.server_time * 1000, type=kind, entry=entry, magic=740_003,
                position_id=pid, reason=3, volume=0.25, price=25000.0, commission=0.0, swap=0.0,
                profit=pnl, fee=0.0, symbol=symbol, comment="injected", external_id="",
            )
        )
    broker.balance += profit
