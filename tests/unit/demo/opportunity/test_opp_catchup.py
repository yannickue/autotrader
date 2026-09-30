# ruff: noqa: E501
"""Catch-up evaluation of the REAL engine on real dev bars: same opportunities as the live path,
causal (truncated at the bar close), synthetic quote, never tradable."""

from __future__ import annotations

from datetime import timedelta

from opp_helpers import bar_times, now_of

from demo.opportunity.bar_source import ReplayBarSource
from demo.opportunity.engine import (
    ALREADY_MOVED,
    CATCHUP_MISSED,
    EXPIRED_ENTRY,
    CatchupInfo,
    InMemorySeenStore,
    OpportunityEngine,
)


def _engine(source, prod, mspecs, **kw):
    return OpportunityEngine(source, production=prod, market_specs=mspecs, commit="test", **kw)


def test_catchup_finds_the_same_opportunities_but_never_accepts(frames, mspecs, prod):
    market = "GER40"
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    live_eng = _engine(base, prod, mspecs)
    cu_eng = _engine(base, prod, mspecs)
    live, cu = {}, {}
    accepted_live = 0
    end_t = None
    for t in bar_times(frames[market], "2026-06-10T06:55", "2026-06-10T11:30"):
        now = now_of(t)
        base.set_time(now)
        for s, d in live_eng.on_m5_close(market, now):
            live[s.opportunity_id] = (s, d)
            accepted_live += d.accepted
        end_t = now
    # later the same cursor is far in the future: replay every bar as CATCH-UP (bars truncated at own close)
    base.set_time(end_t + timedelta(hours=2))
    live_now = end_t + timedelta(hours=2)
    for t in bar_times(frames[market], "2026-06-10T06:55", "2026-06-10T11:30"):
        now = now_of(t)
        for s, d in cu_eng.on_m5_close(market, now, catchup=CatchupInfo(live_now, base.latest_quote(market))):
            cu[s.opportunity_id] = (s, d)
            assert d.decided_utc == live_now.isoformat()
            assert s.signal["origin"] == "CATCHUP"
            assert s.created_utc == now.isoformat()  # causal time of the evaluation, not the processing time
            assert not d.accepted  # NEVER tradable
    assert set(cu) == set(live) and live, "catch-up must find exactly the live opportunities"
    assert accepted_live > 0, "choose a window that has at least one engine-accepted opportunity"
    for oid, (s, d) in cu.items():
        _ls, ld = live[oid]
        if ld.accepted:
            assert d.reasons[:2] == (CATCHUP_MISSED, EXPIRED_ENTRY) and set(d.reasons) <= {
                CATCHUP_MISSED, EXPIRED_ENTRY, ALREADY_MOVED}
            assert s.signal["catchup"]["engine_verdict"] == "ACCEPTED"
        else:
            assert CATCHUP_MISSED not in d.reasons  # an engine reject keeps its own gate codes
    assert cu_eng.last_intents == [] and cu_eng.intents_for(list(cu.values())) == []


def test_catchup_dedupes_against_already_processed_bars(frames, mspecs, prod):
    market = "GER40"
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    seen = InMemorySeenStore()
    eng1 = _engine(base, prod, mspecs, seen_store=seen)
    eng2 = _engine(base, prod, mspecs, seen_store=seen)  # e.g. after a restart sharing the persistent seen-set
    first, second = [], []
    for t in bar_times(frames[market], "2026-06-10T06:55", "2026-06-10T11:30"):
        now = now_of(t)
        base.set_time(now)
        first += eng1.on_m5_close(market, now)
    base.set_time(now_of(t) + timedelta(hours=1))
    for t in bar_times(frames[market], "2026-06-10T06:55", "2026-06-10T11:30"):
        second += eng2.on_m5_close(market, now_of(t), catchup=CatchupInfo(now_of(t) + timedelta(hours=1)))
    assert first and second == []
    assert eng2.suppressed_duplicates >= len(first)
