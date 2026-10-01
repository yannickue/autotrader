# ruff: noqa: E501
"""Bars adapter (``market_observer.bars_adapter``): one path for live + historical, ATR / segments / local clock, DST, gaps, HTF closed-bar alignment.

Catalogue items covered: session/timezone correctness through the adapter (BTCUSD no session; GER40 Berlin DST 2026-03 / 2026-10; NAS100 New York DST weeks
2026-03 / 2026-10 / 2026-11 with synthetic local arrays AND the real calendar); gap reset at segment boundaries; HTF closed-bar alignment (M15 information
never from a forming bar); live/historical parity of the adapter (BarBuffer == batch bit-exactly); alpha-free import.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market_observer import bars_adapter as BA
from market_observer import levels as L
from market_observer import swings as SW
from market_observer.schema import ObserverBars, SessionSpec

NS = 10**9
STEP = 300 * NS


@lru_cache(maxsize=1)
def ger40():
    from alpha.common.market_data import load_dev_market_frame
    from markets.spec import load_market_spec

    ms = load_market_spec("GER40")
    return ms, load_dev_market_frame(ms).iloc[-6000:].reset_index(drop=True)


def test_market_observer_package_does_not_import_alpha():
    src = Path(__file__).resolve().parents[3] / "src" / "market_observer"
    pat = re.compile(r"^\s*(?:from|import)\s+(alpha|execution|risk|exits|nautilus_mt5|nautilus_kernel|adapters)", re.M)
    for py in src.rglob("*.py"):
        assert not pat.search(py.read_text(encoding="utf-8")), f"{py} must stay alpha/execution/risk/exits-free"


# ---------------------------------------------------------------------------------------------- derived arrays
def test_atr_matches_the_alpha_atr14_definition_and_is_a_pure_window_function():
    from alpha.families.data import atr14

    ms, fr = ger40()
    b = BA.bars_from_frame("GER40", fr, point_size=ms.point_size, tick_size=ms.tick_size, session=SessionSpec("Europe/Berlin", 540, 1050))
    ref = atr14(b.h, b.l, b.c)
    assert np.array_equal(np.isnan(ref), np.isnan(b.atr))
    m = np.isfinite(ref)
    assert np.allclose(ref[m], b.atr[m], rtol=1e-12, atol=0.0)
    tr = BA.true_range(b.h, b.l, b.c)
    assert b.atr[500] == BA.atr_at(tr[480:501], 20)  # the same 14 true ranges give the bit-identical value whatever precedes them


def test_segment_ids_are_the_contiguity_facts_of_familydata():
    from alpha.families.data import _assemble
    from alpha.families.spec import MarketCalendar

    ms, fr = ger40()
    n = len(fr)
    ts = pd.DatetimeIndex(fr["ts"]).as_unit("ns").asi8.astype(np.int64)
    fd = _assemble("GER40", MarketCalendar.from_market_spec(ms), ts, *(fr[k].to_numpy(float) for k in ("open", "high", "low", "close")),
                   fr["spread_pts"].to_numpy(float) * ms.point_size, fr["tick_volume"].to_numpy(float), (1.0, 2.0), {})
    seg = BA.segment_ids(ts)
    assert np.array_equal(np.flatnonzero(np.diff(seg) > 0), np.flatnonzero(~fd.contig_next[: n - 1]))
    assert seg.max() > 5 and np.all(np.diff(seg) >= 0)


def test_local_minute_matches_alpha_local_clock_and_day_is_an_absolute_ordinal():
    from alpha.session import local_clock

    ms, fr = ger40()
    ts = pd.DatetimeIndex(fr["ts"]).as_unit("ns").asi8.astype(np.int64)
    minute, day = BA.local_clock_arrays(ts, ms.calendar.tz)
    ref_min, ref_day = local_clock(ts, ms.calendar)
    assert np.array_equal(minute, ref_min.astype(np.int64))
    assert np.array_equal(np.diff(day) != 0, np.diff(ref_day) != 0)  # same day boundaries; ours is absolute (window independent)
    assert day[0] > 20_000  # days since 1970


# ---------------------------------------------------------------------------------------------- session / DST
def _utc_ns(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").value)


def test_btcusd_has_no_session_and_core_markets_keep_their_calendar():
    from demo.opportunity.observer_hook import session_for
    from markets.spec import load_market_spec

    assert session_for(load_market_spec("BTCUSD")) == SessionSpec("UTC", None, None)
    assert session_for(load_market_spec("GER40")) == SessionSpec("Europe/Berlin", 540, 1050)
    assert session_for(load_market_spec("NAS100")) == SessionSpec("America/New_York", 570, 960)
    assert session_for(load_market_spec("BRENT")).cash_open_min == 480  # London energy open (provisional but the engine's own calendar)


@pytest.mark.parametrize(("tz", "utc_open_summer", "utc_open_winter", "minute"), [("Europe/Berlin", "07:00", "08:00", 540), ("America/New_York", "13:30", "14:30", 570)])
def test_cash_open_maps_to_the_same_local_minute_in_summer_and_winter(tz, utc_open_summer, utc_open_winter, minute):
    ts = np.array([_utc_ns(f"2026-07-15T{utc_open_summer}"), _utc_ns(f"2026-01-14T{utc_open_winter}")], dtype=np.int64)
    lm, _ld = BA.local_clock_arrays(ts, tz)
    assert list(lm) == [minute, minute]


@pytest.mark.parametrize(("tz", "days"), [
    ("Europe/Berlin", ("2026-03-23", "2026-03-24", "2026-03-25", "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02", "2026-04-03",
                       "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30")),
    ("America/New_York", ("2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13",
                          "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06")),
])
def test_orb_levels_follow_the_local_open_across_dst_weeks(tz, days):
    """Session levels depend on the market-LOCAL clock the adapter derives: on every day of the DST-transition weeks the ORB high is the high of the first
    15 local minutes of the cash session (a fixed UTC offset would pick the wrong bars on one side of the switch)."""
    open_min = 540 if tz == "Europe/Berlin" else 570
    close_min = 1050 if tz == "Europe/Berlin" else 960
    session = SessionSpec(tz, open_min, close_min)
    ts_list, hi, lo, cl = [], [], [], []
    expected: dict[int, float] = {}
    for k, d in enumerate(days):
        start = _utc_ns(f"{d}T05:00")
        for j in range(12 * 12):  # 05:00 .. 17:00 UTC
            t = start + j * STEP
            lm, _ = BA.local_clock_arrays(np.array([t], dtype=np.int64), tz)
            first15 = open_min <= lm[0] < open_min + 15
            price = 100.0 + (3.0 + k * 0.1 if first15 and lm[0] == open_min else 0.0)
            ts_list.append(t)
            hi.append(price + 0.05)
            lo.append(100.0 - 0.05)
            cl.append(100.0)
        expected[k] = 100.0 + 3.0 + k * 0.1 + 0.05
    ts = np.array(ts_list, dtype=np.int64)
    c = np.array(cl)
    bars = BA.build_observer_bars("X", ts, c, np.array(hi), np.array(lo), c, np.full(len(c), 100.0), np.zeros(len(c)), tick_size=0.01, session=session)
    reg = L.LevelRegistry(L.LevelConfig(swing_timeframes=(), struct_range_n=None))
    last_ctx_by_day: dict[int, L.LevelContext] = {}
    for i in range(len(bars)):
        ctx = reg.update(bars, i)
        if bars.local_minute[i] == open_min + 20:  # after the 15 minute opening range closed
            last_ctx_by_day[int(i // 144)] = ctx
    assert len(last_ctx_by_day) == len(days)
    for k, ctx in last_ctx_by_day.items():
        orb = [lv.price for lv in ctx.levels if lv.source.value == "ORB_HIGH"]
        assert orb == [pytest.approx(expected[k])], (days[k], orb)


def test_synthetic_local_arrays_override_the_derived_ones():
    n = 40
    ts = np.arange(n, dtype=np.int64) * STEP + _utc_ns("2026-06-10T07:00")
    c = np.full(n, 100.0)
    lm = np.arange(n, dtype=np.int64) * 5
    ld = np.full(n, 1234, dtype=np.int64)
    b = BA.build_observer_bars("X", ts, c, c + 0.1, c - 0.1, c, np.full(n, 10.0), np.zeros(n), tick_size=0.01, session=SessionSpec("Europe/Berlin", 540, 1050),
                               local_minute=lm, local_day=ld, segment_id=np.zeros(n, np.int64), atr=np.full(n, 0.5))
    assert np.array_equal(b.local_minute, lm) and np.array_equal(b.local_day, ld) and np.all(b.atr == 0.5)
    assert not b.c.flags.writeable and not b.local_day.flags.writeable


# ---------------------------------------------------------------------------------------------- gaps / segments
def test_gap_creates_a_new_segment_and_balance_does_not_span_it():
    ms, fr = ger40()
    part = fr.iloc[-120:].reset_index(drop=True)
    gap = pd.concat([part.iloc[:60], part.iloc[63:]]).reset_index(drop=True)  # three bars missing after position 59
    b = BA.bars_from_frame("GER40", gap, point_size=ms.point_size, tick_size=ms.tick_size, session=SessionSpec("Europe/Berlin", 540, 1050))
    assert b.segment_id[59] + 1 == b.segment_id[60] and np.all(np.diff(b.segment_id) <= 1)
    from market_observer.balance import balance_features

    assert balance_features(b, 60 + 10).values["range_width_atr_w24"] is None  # window crosses the break
    assert balance_features(b, 60 + 30).values["range_width_atr_w24"] is not None


# ---------------------------------------------------------------------------------------------- live == batch (BarBuffer)
def _arrays(fr, ms):
    return BA.frame_arrays(fr, ms.point_size)


def _same(a: ObserverBars, b: ObserverBars) -> bool:
    for name in ("ts_ns", "o", "h", "l", "c", "tick_volume", "spread", "atr", "local_minute", "local_day"):
        if not np.array_equal(getattr(a, name), getattr(b, name), equal_nan=True):
            return False
    # segment ids differ by an offset only when the buffer started later: compare the break structure
    return bool(np.array_equal(np.diff(a.segment_id), np.diff(b.segment_id)))


def test_bar_buffer_equals_the_batch_adapter_bit_exactly_over_sliding_frames():
    ms, fr = ger40()
    sess = SessionSpec("Europe/Berlin", 540, 1050)
    batch = BA.bars_from_frame("GER40", fr.iloc[:2000], point_size=ms.point_size, tick_size=ms.tick_size, session=sess)
    buf = BA.BarBuffer("GER40", tick_size=ms.tick_size, session=sess)
    window = 700
    assert buf.sync(**_arrays(fr.iloc[:window], ms)) == "init"
    for end in range(window + 1, 2001):
        out = buf.sync(**_arrays(fr.iloc[end - window: end], ms))  # the engine's SLIDING frame
        assert out == "append"
    assert len(buf) == 2000 and _same(buf.bars(), batch)
    # an older / identical frame changes nothing
    assert buf.sync(**_arrays(fr.iloc[1000:1500], ms)) == "noop" and len(buf) == 2000
    assert buf.sync(**_arrays(fr.iloc[1300:2000], ms)) == "noop"


def test_bar_buffer_resets_on_missed_bars_and_when_too_long_and_appends_a_hole_as_a_new_segment():
    ms, fr = ger40()
    sess = SessionSpec("Europe/Berlin", 540, 1050)
    buf = BA.BarBuffer("GER40", tick_size=ms.tick_size, session=sess, max_bars=400)
    buf.sync(**_arrays(fr.iloc[:300], ms))
    # a frame that starts well after the buffer's last bar (bars missed): reset, generation +1
    assert buf.sync(**_arrays(fr.iloc[1000:1300], ms)) == "reset" and buf.generation == 1 and len(buf) == 300
    # a hole INSIDE the overlap-free continuation (frame skips 3 bars but overlaps the last buffered bar): appended as a new segment
    hole = pd.concat([fr.iloc[1299:1301], fr.iloc[1304:1320]]).reset_index(drop=True)
    seg_before = int(buf.bars().segment_id[-1])
    assert buf.sync(**_arrays(hole, ms)) == "append"
    assert int(buf.bars().segment_id[-1]) == seg_before + 1
    # growing beyond max_bars rebuilds from the current frame
    assert buf.sync(**_arrays(fr.iloc[1300:1700], ms)) in ("reset", "append")
    big = BA.BarBuffer("GER40", tick_size=ms.tick_size, session=sess, max_bars=350)
    big.sync(**_arrays(fr.iloc[:300], ms))
    assert big.sync(**_arrays(fr.iloc[100:400], ms)) == "reset" and len(big) == 300


# ---------------------------------------------------------------------------------------------- HTF closed-bar alignment
@pytest.mark.parametrize("phase", [0, 1])
def test_m15_information_never_comes_from_a_forming_bar(phase):
    """Phase 0 / 1 = the decision bar is the first / second M5 bar of its 15-minute group, i.e. that M15 group is still FORMING. A huge spike in the HIGH
    of bar i (closed, but inside the forming group) must leave every ``m15_*`` swing column unchanged: M15 swings come from completed 15-minute groups
    only. (At phase 2 the group completes at the decision bar's own close, so M15 may legitimately include it; the M5 columns may always change because
    bar i is a closed M5 bar.)"""
    ms, fr = ger40()
    b = BA.bars_from_frame("GER40", fr, point_size=ms.point_size, tick_size=ms.tick_size, session=SessionSpec("Europe/Berlin", 540, 1050))
    minutes = (b.ts_ns // NS // 60) % 15
    cands = [i for i in range(5800, 5990) if minutes[i] == phase * 5 and b.segment_id[i] == b.segment_id[i - 40]]
    assert cands
    for i in cands[:6]:
        h = b.h.copy()
        h[i] = h[i] + 50.0 * b.atr[i]
        spike = ObserverBars(b.market, b.ts_ns, b.o, h, b.l, b.c, b.tick_volume, b.spread, b.atr, b.segment_id, b.local_minute, b.local_day, b.tick_size, b.session, b.bar_seconds)
        base = {k: v for k, v in SW.swing_features(b, i).values.items() if k.startswith("m15_")}
        got = {k: v for k, v in SW.swing_features(spike, i).values.items() if k.startswith("m15_")}
        assert base and got == base, f"M15 swing columns changed by a bar of the forming group, i={i}"


def test_adapter_bars_prefix_invariance_of_the_non_level_groups_at_every_m15_phase():
    from market_observer.acceptance import acceptance_features
    from market_observer.balance import balance_features
    from market_observer.participation import participation_features

    ms, fr = ger40()
    b = BA.bars_from_frame("GER40", fr, point_size=ms.point_size, tick_size=ms.tick_size, session=SessionSpec("Europe/Berlin", 540, 1050))
    for i in (5970, 5971, 5972):
        for fn in (balance_features, participation_features, lambda x, j: acceptance_features(x, j, None, 1), SW.swing_features):
            assert fn(b, i).values == fn(b.prefix(i + 1), i).values


def test_no_wall_clock_is_used_in_the_adapter():
    src = (Path(__file__).resolve().parents[3] / "src" / "market_observer" / "bars_adapter.py").read_text(encoding="utf-8")
    assert "datetime.now" not in src and "time.time" not in src and "perf_counter" not in src
