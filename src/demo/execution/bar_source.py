"""Rolling closed-M5 data source over the single-owner MT5 IPC lane."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import pandas as pd

from adapters.activtrades_mt5.history import ServerTimePolicy, validate_rates_schema
from demo.execution.market_config import DemoMarketSpec, load_demo_market_specs

MT5_TIMEFRAME_M5 = 5


class BarSource(Protocol):
    def frame(self, market: str) -> pd.DataFrame: ...

    def quote(self, market: str) -> tuple[float, float, datetime]: ...

    def last_bar_close_utc(self, market: str) -> datetime | None: ...


class Mt5BarSource:
    def __init__(
        self,
        client: Any,
        *,
        lookback: int = 500,
        markets: dict[str, DemoMarketSpec] | None = None,
        server_time: ServerTimePolicy | None = None,
        now: Any = None,
    ) -> None:
        self._client = client
        self._lookback = lookback
        self._markets = markets or load_demo_market_specs()
        self._server_time = server_time or ServerTimePolicy()
        self._now = now or (lambda: datetime.now(UTC))
        self._frames: dict[str, pd.DataFrame] = {}

    def _symbol(self, market: str) -> str:
        try:
            return self._markets[market].broker_symbol
        except KeyError:
            raise KeyError(f"unknown DEMO market {market}") from None

    def frame(self, market: str) -> pd.DataFrame:
        # start_pos=1 excludes the still-forming current M5 bar.
        rows = self._client.copy_rates_from_pos(
            self._symbol(market), MT5_TIMEFRAME_M5, 1, self._lookback
        )
        if rows is None:
            raise RuntimeError(f"MT5 copy_rates failed for {market}")
        validate_rates_schema(rows)
        records = []
        for row in rows:
            opened = self._server_time.server_epoch_to_utc(float(row["time"]))
            records.append(
                {
                    "ts_utc": opened,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "tick_activity": int(row["tick_volume"]),
                    "spread_points": int(row["spread"]),
                }
            )
        result = pd.DataFrame.from_records(records).set_index("ts_utc")
        self._frames[market] = result
        return result.copy()

    def quote(self, market: str) -> tuple[float, float, datetime]:
        tick = self._client.symbol_info_tick(self._symbol(market))
        if tick is None:
            raise RuntimeError(f"MT5 quote failed for {market}")
        return (
            float(tick.bid),
            float(tick.ask),
            self._server_time.server_epoch_to_utc(float(tick.time_msc) / 1000),
        )

    def last_bar_close_utc(self, market: str) -> datetime | None:
        frame = self._frames.get(market)
        if frame is None:
            frame = self.frame(market)
        if frame.empty:
            return None
        return frame.index[-1].to_pydatetime() + timedelta(minutes=5)

    def freshness(self, market: str) -> timedelta | None:
        closed = self.last_bar_close_utc(market)
        return None if closed is None else self._now().astimezone(UTC) - closed
