"""Causality of the OpportunityEngine on REAL dev bars (no MT5).

* truncation invariance: an engine that is handed bars beyond ``now`` (a hostile source) produces
  byte-identical snapshots/decisions to the clean source that has no future bars at all;
* perturbation: garbage-mutating every bar after ``now`` (in the market and in its LEADLAG leaders)
  changes nothing;
* window-size invariance: a shorter rolling window gives the same signals/decisions;
* idempotence and dedupe.
"""

from __future__ import annotations

import pandas as pd
import pytest
from opp_helpers import LeakySource, bar_times, now_of

from demo.opportunity.bar_source import ReplayBarSource
from demo.opportunity.engine import InMemorySeenStore, OpportunityEngine
from demo.opportunity.policy import DUPLICATE_OPPORTUNITY

CASES = [
    ("GER40", "2026-06-10T06:55", "2026-06-10T11:30"),  # Berlin cash open + morning, CEST
    ("NAS100", "2026-06-10T13:25", "2026-06-10T16:00"),  # NY cash open (EDT) with GER40/SPX leaders
    ("XAUUSD", "2026-06-10T06:55", "2026-06-10T10:00"),
]


def _engine(source, prod, mspecs, **kw):
    return OpportunityEngine(source, production=prod, market_specs=mspecs, commit="test", **kw)


def _rec(pairs):
    return [(s.to_json(), d.to_json()) for s, d in pairs]


@pytest.mark.parametrize(("market", "start", "end"), CASES)
def test_truncation_and_perturbation_invariance(frames, mspecs, prod, market, start, end):
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    clean = _engine(base, prod, mspecs)
    leaky = _engine(LeakySource(base, frames, mutate=False), prod, mspecs)
    garbage = _engine(LeakySource(base, frames, mutate=True), prod, mspecs)
    n_signals = 0
    for t in bar_times(frames[market], start, end):
        now = now_of(t)
        base.set_time(now)
        ref = _rec(clean.on_m5_close(market, now))
        assert _rec(leaky.on_m5_close(market, now)) == ref
        assert _rec(garbage.on_m5_close(market, now)) == ref
        n_signals += len(ref)
    assert n_signals > 0, "test window produced no signals: choose a busier window"


def test_short_window_gives_same_signals(frames, mspecs, prod):
    market = "GER40"
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    long_w = _engine(base, prod, mspecs, window_bars=6000)
    short_w = _engine(base, prod, mspecs, window_bars=1500, min_history_bars=600)
    n = 0
    for t in bar_times(frames[market], "2026-06-10T06:55", "2026-06-10T10:00"):
        now = now_of(t)
        base.set_time(now)
        a = long_w.on_m5_close(market, now)
        b = short_w.on_m5_close(market, now)

        def core(pairs):
            return [
                (s.opportunity_id, d.to_json(), s.geometry.to_json(), s.signal["confluence"])
                for s, d in pairs
            ]

        assert core(a) == core(b)
        n += len(a)
    assert n > 0


def test_only_closed_bars_are_used(frames, mspecs, prod):
    market = "GER40"
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    ts = pd.DatetimeIndex(frames[market]["ts"])
    k = int((ts >= pd.Timestamp("2026-06-10T08:00", tz="UTC")).argmax())
    eng = _engine(LeakySource(base, frames, mutate=False), prod, mspecs)
    # 1 second before the close of bar k: bar k is still forming and must not be used
    base.set_time((ts[k] + pd.Timedelta(seconds=299)).to_pydatetime())
    eng.on_m5_close(market, (ts[k] + pd.Timedelta(seconds=299)).to_pydatetime())
    assert eng._last_bar[market] == ts[k - 1]


def test_idempotent_per_bar_and_dedupe_across_engines(frames, mspecs, prod):
    market = "GER40"
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    seen = InMemorySeenStore()
    e1 = _engine(base, prod, mspecs, seen_store=seen)
    e2 = _engine(base, prod, mspecs, seen_store=seen)
    e3 = _engine(base, prod, mspecs, seen_store=seen, emit_duplicates=True)
    hit = None
    for t in bar_times(frames[market], "2026-06-10T06:55", "2026-06-10T09:00"):
        now = now_of(t)
        base.set_time(now)
        out = e1.on_m5_close(market, now)
        if out:
            hit = (now, out)
            break
    assert hit is not None
    now, out = hit
    assert e1.on_m5_close(market, now) == []  # same bar again: nothing
    assert e2.on_m5_close(market, now) == []  # fresh engine, shared seen store: suppressed
    assert e2.suppressed_duplicates == len(out)
    dup = e3.on_m5_close(market, now)
    assert [s.opportunity_id for s, _ in dup] == [s.opportunity_id for s, _ in out]
    assert all(DUPLICATE_OPPORTUNITY in d.reasons and not d.accepted for _, d in dup)


def test_insufficient_history_is_silent(frames, mspecs, prod):
    market = "GER40"
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    eng = _engine(base, prod, mspecs)
    first = pd.Timestamp(frames[market]["ts"].iloc[100])
    base.set_time(now_of(first))
    assert eng.on_m5_close(market, now_of(first)) == []
    assert eng.health[market] == "insufficient_history"
