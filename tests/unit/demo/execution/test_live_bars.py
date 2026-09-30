# ruff: noqa: E501
"""LiveBarSource: closed-M5 frames, quote, DST-safe server time, fail-closed ambiguity."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from demo.execution.stack_port import StackFailClosed
from demo.opportunity.bar_source import BarSource, validate_frame
from tests.unit.demo.execution.stack_harness import (
    berlin_offset_s,
    build_broker,
    make_stack,
)
from tests.unit.nautilus_mt5.harness import make_rates

BERLIN = ZoneInfo("Europe/Berlin")


def rows_between(first_open: datetime, count: int, *, base: float = 25000.0):
    """M5 rows whose server-clock epochs are the Berlin WALL clock (as the real terminal)."""
    out = []
    for i in range(count):
        opened = first_open + timedelta(minutes=5 * i)
        wall = opened.astimezone(BERLIN).replace(tzinfo=UTC)  # wall clock encoded as epoch
        px = base + i
        out.append((int(wall.timestamp()), px, px + 3, px - 3, px + 1, 100 + i, 12, 0))
    return make_rates(out)


@pytest.fixture
def started(tmp_path):
    made = []

    def factory(now, *, broker=None):
        broker = broker or build_broker()
        clock = now if callable(now) else (lambda: now)
        stack = make_stack(broker, tmp_path / f"s{len(made)}", now=clock)
        stack.start()
        made.append(stack)
        return broker, stack

    yield factory
    for stack in made:
        stack.stop()


def test_frame_is_closed_bars_only_utc_and_matches_the_engine_schema(started):
    now = datetime(2026, 7, 14, 10, 7, 30, tzinfo=UTC)  # summer: server = UTC+2
    broker = build_broker()
    # rows run to 08:50Z; extend so the newest row is the FORMING bar 10:05Z
    broker.rates[5] = rows_between(datetime(2026, 7, 14, 8, 0, tzinfo=UTC), 26)
    _, stack = started(now, broker=broker)
    source = stack.bar_source
    assert isinstance(source, BarSource)
    frame = source.m5_frame("GER40", 20)
    validate_frame(frame, "GER40")
    assert list(frame.columns) == [
        "ts", "open", "high", "low", "close", "tick_volume", "spread_pts",
    ]
    assert len(frame) == 20
    assert str(frame["ts"].dt.tz) == "UTC"
    # newest closed bar: opened 10:00Z, closed 10:05Z; the 10:05Z bar is still forming at 10:07:30
    assert frame["ts"].iloc[-1] == pd.Timestamp("2026-07-14T10:00:00Z")
    assert (frame["spread_pts"] == 12).all()
    assert source.last_closed_bar_open("GER40") == datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
    assert source.last_closed_bar_close_utc("GER40") == datetime(2026, 7, 14, 10, 5, tzinfo=UTC)


def test_new_bar_close_is_visible_when_it_appears(started):
    clock = {"now": datetime(2026, 7, 14, 10, 7, 30, tzinfo=UTC)}
    broker = build_broker()
    broker.rates[5] = rows_between(datetime(2026, 7, 14, 8, 0, tzinfo=UTC), 26)  # newest = 10:05
    _, stack = started(lambda: clock["now"], broker=broker)
    source = stack.bar_source
    assert source.last_closed_bar_close_utc("GER40") == datetime(2026, 7, 14, 10, 5, tzinfo=UTC)
    clock["now"] = datetime(2026, 7, 14, 10, 10, 5, tzinfo=UTC)  # the 10:05 bar has closed
    assert source.last_closed_bar_close_utc("GER40") == datetime(2026, 7, 14, 10, 10, tzinfo=UTC)
    clock["now"] = datetime(2026, 7, 14, 10, 15, 5, tzinfo=UTC)  # 10:10 closed, not published yet
    assert source.last_closed_bar_close_utc("GER40") == datetime(2026, 7, 14, 10, 10, tzinfo=UTC)
    broker.rates[5] = rows_between(datetime(2026, 7, 14, 8, 5, tzinfo=UTC), 26)  # newest = 10:10
    assert source.last_closed_bar_close_utc("GER40") == datetime(2026, 7, 14, 10, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    "when", [datetime(2026, 7, 14, 10, 7, 30, tzinfo=UTC), datetime(2026, 12, 15, 10, 7, 30, tzinfo=UTC)]
)
def test_server_time_maps_to_utc_in_summer_and_winter(started, when):
    assert berlin_offset_s(when) == (7200 if when.month == 7 else 3600)
    broker = build_broker()
    start = when.replace(minute=5, second=0) - timedelta(minutes=5 * 25)
    broker.rates[5] = rows_between(start, 26)
    _, stack = started(when, broker=broker)
    frame = stack.bar_source.m5_frame("GER40", 5)
    assert frame["ts"].iloc[-1] == pd.Timestamp(when.replace(minute=0, second=0))


def test_ambiguous_dst_hour_bars_are_dropped_never_guessed(started):
    # 2026-10-25: 03:00 CEST -> 02:00 CET. Wall clock 02:00-02:59 exists twice.
    now = datetime(2026, 10, 25, 2, 12, tzinfo=UTC)  # = 03:12 CET, unambiguous
    broker = build_broker()
    broker.rates[5] = rows_between(datetime(2026, 10, 25, 0, 0, tzinfo=UTC), 26)
    _, stack = started(now, broker=broker)
    frame = stack.bar_source.m5_frame("GER40", 100)
    assert stack.bar_source.stats["dropped_ambiguous_bars"] > 0
    for opened in frame["ts"]:
        wall = opened.tz_convert(BERLIN)
        assert not (wall.hour == 2 and opened >= pd.Timestamp("2026-10-25T00:00:00Z")
                    and opened < pd.Timestamp("2026-10-25T02:00:00Z"))
    validate_frame(frame, "GER40")


def test_ambiguous_server_time_of_the_newest_tick_fails_closed(started):
    broker = build_broker()
    broker.live_offset_s = None
    broker.server_time = int(datetime(2026, 10, 25, 2, 30, tzinfo=UTC).timestamp())
    _, stack = started(datetime(2026, 10, 25, 1, 30, tzinfo=UTC), broker=broker)
    with pytest.raises(StackFailClosed, match="ambiguous_server_time"):
        stack.bar_source.latest_quote("GER40")


def test_quote_freshness_tick_activity_and_cache(started):
    now = datetime.now(UTC)
    broker = build_broker()
    start = (now - timedelta(minutes=5 * 25)).replace(second=0, microsecond=0)
    start = start - timedelta(minutes=start.minute % 5)
    broker.rates[5] = rows_between(start, 26)
    _, stack = started(now, broker=broker)
    source = stack.bar_source
    quote = source.latest_quote("XAUUSD")
    assert quote is not None and quote.valid and quote.bid == 4169.5 and quote.ask == 4169.97
    assert abs((quote.ts_utc - now).total_seconds()) < 5
    assert source.is_fresh("XAUUSD") and source.quote_age_seconds("XAUUSD") < 5
    assert source.tick_activity("GER40") is not None
    first_fetches = source.stats["fetches"]
    source.m5_frame("GER40", 10)
    source.m5_frame("GER40", 10)
    assert source.stats["fetches"] <= first_fetches + 1  # served from the per-market cache
    broker.live_offset_s -= 100
    assert not source.is_fresh("XAUUSD")


def test_bar_source_reads_use_only_the_lane_thread(started):
    now = datetime(2026, 7, 14, 10, 7, 30, tzinfo=UTC)
    broker = build_broker()
    broker.rates[5] = rows_between(datetime(2026, 7, 14, 8, 0, tzinfo=UTC), 26)
    _, stack = started(now, broker=broker)
    caller = threading.get_ident()
    stack.bar_source.m5_frame("GER40", 5)
    stack.bar_source.latest_quote("GER40")
    assert caller not in broker.all_call_threads()
    assert broker.all_call_threads() == stack._lane.stats.worker_thread_ids


def test_start_with_a_dead_terminal_is_fail_closed_and_leaves_no_threads(tmp_path):
    broker = build_broker()
    broker.disconnect()
    stack = make_stack(broker, tmp_path)
    before = {t.name for t in threading.enumerate()}
    with pytest.raises(StackFailClosed):
        stack.start()
    leaked = {t.name for t in threading.enumerate()} - before
    assert not [n for n in leaked if n.startswith(("demo-", "mt5-lane"))]
    assert not (tmp_path / "terminal.lock").exists()
