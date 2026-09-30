# ruff: noqa: E501
"""Lane P: the live OpportunityEngine with / without the operating policy (real dev bars, no MT5).

Default (no policy) is untouched; with the policy the snapshot carries its version+hash, the intent's forced flat is
the policy's effective flat, and the flatten phase / runway produce no entry."""

from __future__ import annotations

from datetime import UTC, datetime

from opp_helpers import bar_times, now_of

from demo.opportunity.bar_source import ReplayBarSource
from demo.opportunity.clock import forced_flat_utc
from demo.opportunity.engine import OpportunityEngine
from demo.opportunity.operating_policy import load_operating_policy, policy_from_dict


def _engine(source, prod, mspecs, **kw):
    return OpportunityEngine(source, production=prod, market_specs=mspecs, commit="test", **kw)


def _run(eng, base, frames, market, start, end):
    out = []
    for t in bar_times(frames[market], start, end):
        now = now_of(t)
        base.set_time(now)
        out += eng.on_m5_close(market, now)
    return out


def test_policy_does_not_change_what_a_normal_window_finds_only_versions_and_deadline(frames, mspecs, prod):
    market, pol = "GER40", load_operating_policy()
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    plain = _run(_engine(base, prod, mspecs), base, frames, market, "2026-06-10T06:55", "2026-06-10T11:30")
    live = _run(_engine(base, prod, mspecs, operating=pol), base, frames, market, "2026-06-10T06:55", "2026-06-10T11:30")
    assert plain, "choose a window with opportunities"
    assert [(s.opportunity_id, d.accepted, d.reasons) for s, d in plain] == [(s.opportunity_id, d.accepted, d.reasons) for s, d in live]
    for (sp, _), (sl, _) in zip(plain, live, strict=True):
        assert "operating_policy" not in sp.versions  # default: snapshot versions bit-identical to before
        assert sl.versions["operating_policy"] == f"{pol.version}:{pol.policy_hash}"
        assert {k: v for k, v in sl.versions.items() if k != "operating_policy"} == sp.versions
        assert sl.signal["forced_flat_utc"] == sp.signal["forced_flat_utc"]  # GER40 10:00 Berlin entry: 21:30 flat stays


def test_intent_forced_flat_is_the_effective_flat_of_the_policy(frames, mspecs, prod):
    market = "GER40"
    raw = {"policy": {"version": "t", "global_flat_deadline": "22:00", "flatten_start": "21:55",
                      "broker_close_buffer_min": 5, "min_entry_runway_min": 10, "operating_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                      "flatten_retry_backoff_s": [5]},
           "markets": {"GER40": {"session": {"status": "provisional", "source": "synthetic", "pauses": [
               {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "20:30", "end": "00:00"}]}}}}
    early = policy_from_dict(raw)
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    eng = _engine(base, prod, mspecs, operating=early)
    pairs = _run(eng, base, frames, market, "2026-06-10T06:55", "2026-06-10T11:30")
    accepted = [s for s, d in pairs if d.accepted]
    assert accepted
    flat = datetime.fromisoformat(accepted[0].signal["forced_flat_utc"])
    assert flat == datetime(2026, 6, 10, 18, 25, tzinfo=UTC)  # broker close 20:30 Berlin (18:30 UTC) - 5 min
    assert flat == forced_flat_utc(mspecs[market], datetime(2026, 6, 10, 9, 0, tzinfo=UTC), mspecs[market].calendar.forced_flat_min, early)
    # the TradeIntent agrees with the snapshot (single choke point)
    intents = eng.intents_for([(s, d) for s, d in pairs if d.accepted])
    assert intents and {datetime.fromisoformat(i.forced_flat_utc) for i in intents} == {flat}


def test_flatten_phase_and_runway_yield_no_entry(frames, mspecs, prod):
    market = "GER40"
    # flatten phase from 10:30 Berlin (08:30 UTC on 2026-06-10, CEST) for this test only
    raw = {"policy": {"version": "t", "global_flat_deadline": "11:00", "flatten_start": "10:30",
                      "broker_close_buffer_min": 5, "min_entry_runway_min": 10, "operating_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                      "flatten_retry_backoff_s": [5]}}
    early = policy_from_dict(raw)
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    eng = _engine(base, prod, mspecs, operating=early)
    plain_eng = _engine(base, prod, mspecs)
    late_live = _run(eng, base, frames, market, "2026-06-10T08:15", "2026-06-10T11:30")
    late_plain = _run(plain_eng, base, frames, market, "2026-06-10T08:15", "2026-06-10T11:30")
    assert late_plain, "without the policy this window has opportunities"
    assert all(now_of_ < datetime(2026, 6, 10, 8, 20, tzinfo=UTC) for now_of_ in [datetime.fromisoformat(s.signal_ts_utc) for s, _ in late_live])  # only before the runway cut (10:20 Berlin)
    assert eng.health[market] == "outside_live_entry_window"
