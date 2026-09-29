"""C3 normalized bars -> Nautilus `Bar`s -> `ParquetDataCatalog` (write/read).

Bar model (explicit, conservative, NOT tick-perfect):

- MT5 bars are BID-based OHLC. They become `...-BID-EXTERNAL` Nautilus bars.
- The matching engine also needs an ask side so that BUY fills at ask and
  SELL fills at bid. The ASK bar is SYNTHESIZED: every OHLC price shifted by
  the bar's broker-reported `spread` (points * point) -- a constant spread
  across the bar. Real intrabar spread variation is unknown. Labelled
  `ASK_SYNTHESIZED_FROM_BAR_SPREAD`.
- `ts_event`/`ts_init` = bar CLOSE time (MT5 gives OPEN time; close = open +
  timeframe), the Nautilus convention, so a strategy can never act on a bar
  before it closed.
- Volume = broker tick activity (not exchange volume).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from adapters.activtrades_mt5.history import TIMEFRAME_SECONDS
from data.parquet_store import BarRecord
from data.provenance import SemanticType

ASK_MODEL = "ASK_SYNTHESIZED_FROM_BAR_SPREAD"
_NS = 1_000_000_000


def bar_type_strings(instrument: Instrument, timeframe: str) -> tuple[str, str]:
    seconds = TIMEFRAME_SECONDS[timeframe]
    if seconds % 60:
        raise ValueError(f"unsupported timeframe {timeframe}")
    minutes = seconds // 60
    base = f"{instrument.id}-{minutes}-MINUTE"
    return f"{base}-BID-EXTERNAL", f"{base}-ASK-EXTERNAL"


def bar_close_ns(record: BarRecord, timeframe: str) -> int:
    close = record.timestamp + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    return int(close.timestamp()) * _NS


def records_to_nautilus_bars(
    records: list[BarRecord], instrument: Instrument, point: Decimal
) -> tuple[list[Bar], list[Bar]]:
    """Return (bid_bars, ask_bars). Fails closed on anything not verifiably CFD tick activity."""
    if not records:
        raise ValueError("no bar records")
    timeframe = records[0].timeframe
    bid_type_s, ask_type_s = bar_type_strings(instrument, timeframe)
    bid_type, ask_type = BarType.from_str(bid_type_s), BarType.from_str(ask_type_s)
    bid_bars: list[Bar] = []
    ask_bars: list[Bar] = []
    previous_ns = -1
    for rec in records:
        if rec.timeframe != timeframe:
            raise ValueError("mixed timeframes")
        if rec.volume_semantic_type is not SemanticType.BROKER_TICK_ACTIVITY:
            raise ValueError("CFD bar volume must be BROKER_TICK_ACTIVITY")
        if rec.spread_points is None or rec.spread_points < 0:
            raise ValueError(f"missing/negative spread at {rec.timestamp.isoformat()}")
        ts_ns = bar_close_ns(rec, timeframe)
        if ts_ns <= previous_ns:
            raise ValueError("bars not strictly increasing")
        previous_ns = ts_ns
        spread = point * rec.spread_points
        volume = instrument.make_qty(float(rec.volume))
        for target, kind, shift in ((bid_bars, bid_type, Decimal(0)), (ask_bars, ask_type, spread)):
            target.append(
                Bar(
                    bar_type=kind,
                    open=instrument.make_price(float(rec.open + shift)),
                    high=instrument.make_price(float(rec.high + shift)),
                    low=instrument.make_price(float(rec.low + shift)),
                    close=instrument.make_price(float(rec.close + shift)),
                    volume=volume,
                    ts_event=ts_ns,
                    ts_init=ts_ns,
                )
            )
    return bid_bars, ask_bars


def write_catalog(
    path: Path | str, instrument: Instrument, bid_bars: list[Bar], ask_bars: list[Bar]
) -> ParquetDataCatalog:
    catalog = ParquetDataCatalog(path)
    catalog.write_data([instrument])
    catalog.write_data(bid_bars)
    catalog.write_data(ask_bars)
    return catalog


def read_catalog(
    path: Path | str, instrument: Instrument, timeframe: str
) -> tuple[Instrument, list[Bar], list[Bar]]:
    """Read back from disk (fresh catalog handle) -- what a backtest loads."""
    catalog = ParquetDataCatalog(path)
    instruments = catalog.instruments(instrument_ids=[str(instrument.id)])
    if len(instruments) != 1:
        raise ValueError(f"expected 1 instrument in catalog, found {len(instruments)}")
    bid_s, ask_s = bar_type_strings(instrument, timeframe)
    bid = catalog.bars(bar_types=[bid_s])
    ask = catalog.bars(bar_types=[ask_s])
    return instruments[0], bid, ask
