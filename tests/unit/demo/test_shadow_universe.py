# ruff: noqa: E501
"""Lane U2 (B): ShadowUniverseScanner / OutOfWindowShadow controller / read-only source / spec mechanism plumbing.

Zero MT5: fakes only. Family generators are stubbed (``stub_generate``) where a deterministic signal is needed."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "opportunity"))

from alpha.fast.sim import CandidateArrays
from demo.execution.stack_port import StackFailClosed
from demo.opportunity import engine as eng
from demo.opportunity.bar_source import M5_SECONDS, Quote
from demo.opportunity.engine import InMemorySeenStore
from demo.shadow_universe import (
    OutOfWindowShadow,
    ShadowReadError,
    ShadowUniverseError,
    ShadowUniverseScanner,
    make_shadow_live_source,
    select_shadow_markets,
    shadow_production_set,
    shadow_to_market_spec,
)
from markets.shadow import load_shadow_specs
from markets.spec import MarketSpecError, load_market_spec

NOW = datetime(2026, 10, 1, 12, 7, 30, tzinfo=UTC)  # Thursday; newest closed bar OPENS 12:00
LAST_OPEN = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _frame(last_open: datetime, point: float, n: int = 700, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.DatetimeIndex([last_open - timedelta(seconds=M5_SECONDS * k) for k in range(n - 1, -1, -1)], tz="UTC").as_unit("ns")
    close = 1.1 + np.cumsum(rng.normal(0, 0.0004, n))
    return pd.DataFrame({
        "ts": ts, "open": close - 0.0001, "high": close + 0.0006, "low": close - 0.0006, "close": close,
        "tick_volume": np.full(n, 50.0), "spread_pts": np.full(n, 6.0),
    })


class FakeSource:
    def __init__(self, specs, *, last_open=LAST_OPEN, quote_age_s=10.0) -> None:
        self.specs = specs
        self.last_open = {m: last_open for m in specs}
        self.fail: set[str] = set()
        self.frame_calls: list[str] = []
        self.quote_age_s = quote_age_s

    def m5_frame(self, market, n=None):
        self.frame_calls.append(market)
        if market in self.fail:
            raise ShadowReadError("rates_unavailable")
        return _frame(self.last_open[market], self.specs[market].point_size)

    def latest_quote(self, market):
        return Quote(ts_utc=NOW - timedelta(seconds=self.quote_age_s), bid=1.1, ask=1.1001)


@pytest.fixture
def stub_generate(monkeypatch):
    def gen(data, spec, thr):
        i = len(data) - 2
        return CandidateArrays(
            np.array([i], dtype=np.int64), np.array([1], dtype=np.int8), np.array([float(data.c[i]) - 0.002]),
            np.array([np.nan]), np.array([1.5]), np.array([0], dtype=np.int8),
        )

    monkeypatch.setattr(eng, "generate_candidates", gen)


@pytest.fixture(scope="module")
def all_specs():
    return load_shadow_specs()


def _scanner(specs, source, **kw):
    return ShadowUniverseScanner(
        specs=specs, source=source, phase="DISCOVERY", seen_store=InMemorySeenStore(), commit="t", **kw,
    )


def test_selection_all_ready_and_list_and_pending(all_specs):
    sel, pending = select_shadow_markets("all-ready")
    assert set(sel) == set(all_specs) and pending and pending < set(sel)
    assert all("quote_freshness_unverified_market_closed_at_scan" in sel[m].data_quality_flags for m in pending)
    sel2, _ = select_shadow_markets("AUDUSD, GBPUSD")
    assert set(sel2) == {"AUDUSD", "GBPUSD"}
    with pytest.raises(ShadowUniverseError):
        select_shadow_markets("NOT_A_MARKET")


def test_shadow_market_is_never_a_production_market_spec(all_specs):
    sp = all_specs["AUDUSD"]
    ms = shadow_to_market_spec(sp)
    assert ms.trading_enabled is False and ms.research_only is True and ms.asset_class == "fx_cfd"
    with pytest.raises(MarketSpecError):
        ms.validate()  # the production validator refuses every canonical outside the production universe
    with pytest.raises(MarketSpecError):
        load_market_spec("AUDUSD")  # not loadable through the production loader either


def test_family_set_is_fit_free_struct_only(all_specs):
    ps = shadow_production_set(["AUDUSD", "ETHUSD"])
    for m in ("AUDUSD", "ETHUSD"):
        fss = ps.specs_for(m)
        assert {f.family for f in fss} == {"STRUCT"} and len(fss) == 4
        assert all(f.thr_values == () for f in fss)  # no fitted parameter
    assert ps.strategy_hash.startswith("shadow-struct-")


def test_hard_guard_refuses_registry_overlap(all_specs):
    sel, _ = select_shadow_markets("AUDUSD")
    with pytest.raises(ShadowUniverseError):
        _scanner(sel, FakeSource(sel), forbidden=("AUDUSD",))
    with pytest.raises(ShadowUniverseError):
        _scanner(sel, FakeSource(sel), forbidden=("AUDUSD",))  # broker symbol collision is covered the same way


def test_scan_records_rejected_shadow_universe_never_accepted_never_intent(all_specs, stub_generate):
    sel, _ = select_shadow_markets("AUDUSD,ETHUSD")
    sc = _scanner(sel, FakeSource(sel))
    pairs = sc.scan_cycle(NOW)
    assert len(pairs) == 8  # 4 STRUCT variants x 2 markets
    for snap, dec in pairs:
        assert dec.accepted is False and dec.reasons[0] == "SHADOW_UNIVERSE"
        assert snap.signal["origin"] == "SHADOW_UNIVERSE" and snap.signal["shadow_only"] is True
        assert snap.signal["cluster"] in ("FX", "CRYPTO") and snap.signal["family_set"] == "STRUCT_FIT_FREE"
    assert sc.engine.last_intents == [] and sc.engine.intents_for(pairs) == []
    st = sc.stats()
    assert st["symbols_total"] == 2 and st["scanned_last_cycle"] == 2 and st["opportunities_recorded"] == 8 and st["errors"] == 0
    assert st["by_cluster"] == {"FX": 4, "CRYPTO": 4}


def test_bounded_round_robin_covers_everything_without_repeats(all_specs, stub_generate):
    names = sorted(all_specs)[:40]
    sel = {n: all_specs[n] for n in names}
    src = FakeSource(sel)
    sc = _scanner(sel, src, max_symbols_per_cycle=5, budget_s=60.0)
    done: list[str] = []
    for _ in range(8):
        sc.scan_cycle(NOW)
        assert sc.scanned_last_cycle <= 5
    done = list(dict.fromkeys(src.frame_calls))
    assert set(done) == set(names)  # 8 cycles x 5 = all 40, each visited
    assert len(src.frame_calls) == 80  # scanner + engine read of the same (cached in production) frame: one evaluation per symbol per bar boundary
    sc.scan_cycle(NOW)
    assert sc.scanned_last_cycle == 0  # nothing due until the next M5 boundary


def test_wall_time_budget_bounds_a_cycle(all_specs, stub_generate):
    names = sorted(all_specs)[:20]
    sel = {n: all_specs[n] for n in names}
    t = {"v": 0.0}

    def clock():
        t["v"] += 0.6  # every clock read advances 0.6 s: a scan of one symbol costs > budget share
        return t["v"]

    sc = _scanner(sel, FakeSource(sel), max_symbols_per_cycle=50, budget_s=2.0, clock=clock)
    sc.scan_cycle(NOW)
    assert 1 <= sc.scanned_last_cycle <= 4 and sc.stats()["deferred_total"] >= 16


def test_per_symbol_failure_isolation_and_counting(all_specs, stub_generate):
    sel, _ = select_shadow_markets("AUDUSD,GBPUSD,ETHUSD")
    src = FakeSource(sel)
    src.fail.add("GBPUSD")
    sc = _scanner(sel, src)
    pairs = sc.scan_cycle(NOW)
    assert {s.market for s, _ in pairs} == {"AUDUSD", "ETHUSD"}
    st = sc.stats()
    assert st["errors"] == 1 and st["symbols_with_errors"] == {"GBPUSD": 1} and "GBPUSD" in st["last_error"]
    calls = len(src.frame_calls)
    sc.scan_cycle(NOW)
    assert len(src.frame_calls) == calls  # the failed symbol is retried at the NEXT boundary, not every cycle


def test_stack_fail_closed_is_not_swallowed(all_specs):
    sel, _ = select_shadow_markets("AUDUSD")

    class Boom(FakeSource):
        def m5_frame(self, market, n=None):
            raise StackFailClosed("mt5_lane_timeout")

    with pytest.raises(StackFailClosed):
        _scanner(sel, Boom(sel)).scan_cycle(NOW)


def test_pending_spec_needs_a_fresh_quote_first(all_specs, stub_generate):
    sel, pending = select_shadow_markets("all-ready")
    name = sorted(pending)[0]
    one = {name: sel[name]}
    stale = FakeSource(one, quote_age_s=3600.0)
    sc = _scanner(one, stale, pending={name})
    assert sc.scan_cycle(NOW) == [] and stale.frame_calls == [] and sc.stats()["pending_not_fresh"] == 1
    fresh = FakeSource(one, quote_age_s=5.0)
    sc2 = _scanner(one, fresh, pending={name})
    assert sc2.scan_cycle(NOW) and sc2.stats()["pending_validated"] == 1 and fresh.frame_calls == [name, name]


def test_closed_market_with_stale_last_bar_is_idle_not_evaluated(all_specs, stub_generate):
    sel, _ = select_shadow_markets("AUDUSD")
    src = FakeSource(sel, last_open=LAST_OPEN - timedelta(hours=40))  # weekend: newest bar is far older than one bar
    sc = _scanner(sel, src)
    assert sc.scan_cycle(NOW) == [] and sc.stats()["idle_stale_bar"] == 1
    assert sc.frame_rows("AUDUSD")  # still available to the counterfactual labeller


def test_frame_rows_are_price_unit_bars_for_the_labeller(all_specs, stub_generate):
    sel, _ = select_shadow_markets("AUDUSD")
    sc = _scanner(sel, FakeSource(sel))
    sc.scan_cycle(NOW)
    rows = sc.frame_rows("AUDUSD")
    t, _o, h, lo, _c, sp = rows[-1]
    assert t == LAST_OPEN and h > lo and sp == pytest.approx(6.0 * sel["AUDUSD"].point_size)


# ------------------------------------------------------------------ OutOfWindowShadow controller
class _Eng:
    supports_shadow_scan = True

    def __init__(self, ms, specs, raises=None):
        self.ms, self.specs, self.raises, self.calls = ms, specs, raises, []

    def market_spec(self, m):
        return self.ms

    def specs_for(self, m):
        return self.specs

    def on_m5_close(self, market, now, *, shadow=None):
        self.calls.append((market, shadow))
        if self.raises:
            raise self.raises
        return []

    def release_seen(self):
        pass


def _ger():
    from demo.opportunity.production_spec import load_production_spec

    return load_market_spec("GER40"), load_production_spec().specs_for("GER40")


def test_oow_controller_gates_tradable_window_and_budget():
    ms, specs = _ger()
    e = _Eng(ms, specs)
    c = OutOfWindowShadow()
    out_close = datetime(2026, 6, 10, 22, 5, tzinfo=UTC)  # 00:05 Berlin: every window closed
    in_close = datetime(2026, 6, 10, 8, 5, tzinfo=UTC)  # 10:05 Berlin: windows open
    c.begin_cycle()
    c.scan(e, "GER40", out_close, out_close, tradable=False)
    assert e.calls == [] and c.counters["skipped_not_tradable"] == 1
    c.scan(e, "GER40", in_close, in_close, tradable=True)
    assert e.calls == [] and c.counters["skipped_window_open"] == 1
    c.scan(e, "GER40", out_close, out_close, tradable=True)
    assert len(e.calls) == 1 and e.calls[0][1].relax_window is True and e.calls[0][1].code == "OUT_OF_WINDOW_SHADOW"
    t = {"v": 0.0}
    c2 = OutOfWindowShadow(budget_s=1.0, clock=lambda: t["v"])
    c2.begin_cycle()
    t["v"] = 5.0  # budget already burnt by earlier markets in this cycle
    e2 = _Eng(ms, specs)
    c2.scan(e2, "GER40", out_close, out_close, tradable=True)
    assert e2.calls == [] and c2.counters["skipped_budget"] == 1


def test_oow_controller_contains_engine_exceptions_and_counts_them():
    ms, specs = _ger()
    c = OutOfWindowShadow()
    c.begin_cycle()
    close = datetime(2026, 6, 10, 22, 5, tzinfo=UTC)
    assert c.scan(_Eng(ms, specs, raises=ValueError("boom")), "GER40", close, close, tradable=True) == []
    assert c.counters["errors"] == 1 and "boom" in c.last_error
    with pytest.raises(StackFailClosed):  # a real stack failure is not a measurement error
        c.scan(_Eng(ms, specs, raises=StackFailClosed("x")), "GER40", close, close, tradable=True)


def test_oow_controller_dedupe_cap_per_zone_and_day():
    from alpha.families.spec import EffectiveWindow
    from demo.opportunity.policy import Candidate

    c = OutOfWindowShadow(cap_per_zone_day=2)

    def cand(stop=99.0, day=10):
        sig = datetime(2026, 6, day, 22, 5, tzinfo=UTC)
        return Candidate(
            market="GER40", broker_symbol="Ger40", family="STRUCT", strategy_id="s", spec_hash="h", direction=1, signal_ts=sig,
            bar_open_ts=sig - timedelta(minutes=5), close=100.0, atr=1.0, bar_spread=0.1, stop=stop, target=float("nan"),
            target_r=1.5, exit_kind=0, min_space_r=float("nan"), window=EffectiveWindow(0, 1440, 1440),
        )

    assert [c._admit(cand()) for _ in range(3)] == [True, True, False]  # capped at 2 per zone/day
    assert c._admit(cand(stop=90.0)) is True  # another zone
    assert c._admit(cand(day=11)) is True  # next day resets
    assert c.counters["capped"] == 1


def test_oow_controller_unsupported_engine_is_a_noop():
    c = OutOfWindowShadow()
    close = datetime(2026, 6, 10, 22, 5, tzinfo=UTC)
    assert c.scan(SimpleNamespace(), "GER40", close, close, tradable=True) == []
    assert c.counters["skipped_engine_unsupported"] == 1


# -------------------------------------------------------------------------- read-only live source
def _rates(last_open_server: int, n: int) -> np.ndarray:
    dt = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                   ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])
    a = np.zeros(n, dtype=dt)
    a["time"] = last_open_server - 300 * np.arange(n - 1, -1, -1)
    a["open"] = a["close"] = 1.1
    a["high"], a["low"], a["spread"], a["tick_volume"] = 1.1005, 1.0995, 7, 40
    return a


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def copy_rates_from_pos(self, sym, tf, pos, count):
        self.calls.append(("copy_rates_from_pos", sym))
        if sym == "BAD":
            raise RuntimeError("terminal_no_data")
        server_epoch = int((NOW - timedelta(minutes=7, seconds=30)).timestamp()) + 7200  # Berlin CEST = UTC+2
        return _rates(server_epoch, count)

    def symbol_info_tick(self, sym):
        self.calls.append(("symbol_info_tick", sym))
        return SimpleNamespace(time_msc=int(NOW.timestamp() * 1000) + 7200_000, bid=1.1, ask=1.1001)

    def symbol_select(self, *a, **k):  # the source must NEVER touch Market Watch
        raise AssertionError("symbol_select must not be called")


class _Session:
    def __init__(self, client) -> None:
        from adapters.activtrades_mt5.history import ServerTimePolicy

        self.client, self.time_policy = client, ServerTimePolicy()

    def call(self, what, fn, *a):
        return fn(*a)


class _Stack:
    def __init__(self) -> None:
        self._cfg = SimpleNamespace(bar_settle_s=1.0, bar_min_refetch_s=2.0, bar_lookback=600)
        self.client = _Client()
        self._sess = _Session(self.client)
        self.lane_calls: list[dict] = []

    def _now(self):
        return NOW

    def _session_or_fail(self):
        return self._sess

    def _on_lane(self, fn, *a, **kw):
        self.lane_calls.append(kw)
        return fn(*a)


def test_live_source_reads_shadow_symbols_read_only_through_the_runner_lane():
    stack = _Stack()
    src = make_shadow_live_source(stack, {"AUDUSD": "AUDUSD", "BADM": "BAD"})
    fr = src.m5_frame("AUDUSD", 650)
    assert len(fr) >= 600 and fr["ts"].iloc[-1] == pd.Timestamp(LAST_OPEN)
    q = src.latest_quote("AUDUSD")
    assert q is not None and q.valid
    assert {c[0] for c in stack.client.calls} == {"copy_rates_from_pos", "symbol_info_tick"}  # read-only surface only
    assert all(kw.get("retry_reads") is False for kw in stack.lane_calls)  # no read retries on the trading lane
    with pytest.raises(ShadowReadError):
        src.m5_frame("BADM", 650)  # a symbol the terminal cannot serve: contained ShadowReadError, never a fail-closed
    with pytest.raises(ShadowReadError):
        src.m5_frame("NOT_CONFIGURED", 10)
