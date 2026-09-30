# ruff: noqa: E501
"""Batch replay == streaming engine (same frozen specs, same policy, same position oracle).

The batch path is the research-style whole-array generation; the stream path is the live engine with
bars arriving one closed bar at a time. Equality of (signal time, strategy, direction, accepted,
reasons) is the end-to-end causality proof against the research generators.
"""

from __future__ import annotations

import pytest

from demo.opportunity.replay import replay_opportunities

WINDOWS = [
    ("GER40", "2026-06-10T06:55", "2026-06-10T10:00"),
    ("NAS100", "2026-06-10T13:25", "2026-06-10T16:00"),
    ("SPX500", "2026-06-10T13:25", "2026-06-10T16:00"),
    ("XAUUSD", "2026-06-10T06:55", "2026-06-10T10:00"),
    ("EURUSD", "2026-06-10T06:55", "2026-06-10T10:00"),
]


def _key(rows):
    return sorted((r.signal_ts_utc, r.strategy_id, r.direction, r.accepted, r.reasons) for r in rows)


@pytest.mark.parametrize(("market", "start", "end"), WINDOWS)
def test_batch_equals_stream(frames, mspecs, prod, market, start, end):
    kw = dict(production=prod, frames=frames, market_specs=mspecs)
    batch = replay_opportunities(market, start, end, mode="batch", **kw)
    stream = replay_opportunities(market, start, end, mode="stream", **kw)
    assert _key(batch.rows) == _key(stream.rows)
    assert len(stream.pairs) == len(stream.rows)
    assert len(batch.rows) > 0, "window must contain signals to be meaningful"


def test_replay_refuses_the_forward_holdout(frames, mspecs, prod):
    with pytest.raises(ValueError, match="forward holdout"):
        replay_opportunities("GER40", "2026-08-01", "2026-09-01", production=prod)
    with pytest.raises(ValueError, match="forward holdout"):
        replay_opportunities("GER40", "2026-08-30", "2026-09-30", production=prod)


def test_batch_statistics_shape(frames, mspecs, prod):
    res = replay_opportunities("GER40", "2026-06-08", "2026-06-12", production=prod,
                               frames=frames, market_specs=mspecs)
    s = res.summary()
    assert s["active_days"] == 5
    assert s["opportunities"] == len(res.rows) > 0
    assert s["accepted"] <= s["opportunities"]
    assert sum(v for k, v in s["reason_histogram"].items()) >= s["opportunities"]
    assert s["opportunities_per_day"] == round(s["opportunities"] / 5, 2)


def test_no_position_gating_in_replay_or_engine(frames, mspecs, prod):
    for mode in ("batch", "stream"):
        res = replay_opportunities("GER40", "2026-06-10T06:55", "2026-06-10T10:00", production=prod,
                                   frames=frames, market_specs=mspecs, mode=mode)
        s = res.summary()
        assert "ONE_POSITION_PER_INSTRUMENT" not in s["reason_histogram"]
        assert "valid_before_position_gate" not in s
        # every non-duplicate opportunity is either accepted or has a technical reject reason
        assert all(r.accepted or r.reasons for r in res.rows)
