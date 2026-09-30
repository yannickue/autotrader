# ruff: noqa: E501
"""BarSource protocol + ReplayBarSource semantics (no MT5)."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from opp_helpers import LeakySource, now_of

from demo.opportunity.bar_source import (
    BarSource,
    Quote,
    ReplayBarSource,
    closed_bars_only,
    tick_activity_of,
    validate_frame,
)


def _src(frames, mspecs):
    return ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})


def test_replay_source_shows_only_closed_bars_and_quotes_the_last_close(frames, mspecs):
    src = _src(frames, mspecs)
    fr = frames["GER40"]
    k = 60000
    t = pd.Timestamp(fr["ts"].iloc[k])
    src.set_time((t + pd.Timedelta(seconds=299)).to_pydatetime())  # bar k still forming
    assert pd.Timestamp(src.m5_frame("GER40")["ts"].iloc[-1]) == pd.Timestamp(fr["ts"].iloc[k - 1])
    src.set_time(now_of(t))
    out = src.m5_frame("GER40", 100)
    assert len(out) == 100 and pd.Timestamp(out["ts"].iloc[-1]) == t
    q = src.latest_quote("GER40")
    row = fr.iloc[k]
    assert q is not None and q.valid
    assert q.bid == row["close"] and q.ask == pytest.approx(row["close"] + row["spread_pts"] * 0.01)
    assert q.ts_utc == now_of(t)
    out.loc[out.index[-1], "close"] = 1.0  # returned frames are copies
    assert src.m5_frame("GER40", 1)["close"].iloc[-1] == row["close"]
    assert isinstance(src, BarSource)


def test_quote_validity_and_optional_tick_activity(frames, mspecs):
    ts = datetime(2026, 6, 10, tzinfo=UTC)
    assert not Quote(ts_utc=ts, bid=100.0, ask=99.0).valid
    assert not Quote(ts_utc=ts, bid=0.0, ask=1.0).valid
    assert not Quote(ts_utc=ts, bid=float("nan"), ask=1.0).valid
    assert Quote(ts_utc=ts, bid=100.0, ask=100.0).valid

    class Minimal:
        def m5_frame(self, market, n=None):
            raise NotImplementedError

        def latest_quote(self, market):
            return None

    assert tick_activity_of(Minimal(), "GER40") is None


def test_validate_and_closed_only_reject_bad_frames(frames):
    fr = frames["GER40"].iloc[:50].copy()
    validate_frame(fr)
    with pytest.raises(ValueError, match="not strictly increasing"):
        validate_frame(pd.concat([fr, fr.iloc[:1]]))
    bad = fr.copy()
    bad["ts"] = bad["ts"] + pd.Timedelta(seconds=7)
    with pytest.raises(ValueError, match="M5 grid"):
        validate_frame(bad)
    nz = fr.copy()
    nz["ts"] = nz["ts"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="UTC"):
        validate_frame(nz)
    last = pd.Timestamp(fr["ts"].iloc[-1])
    assert len(closed_bars_only(fr, (last + pd.Timedelta(seconds=299)).to_pydatetime())) == 49
    assert len(closed_bars_only(fr, (last + pd.Timedelta(seconds=300)).to_pydatetime())) == 50


def test_leaky_source_really_leaks_and_mutates(frames, mspecs):
    """Guard for the perturbation test: the hostile source must actually show mutated future bars."""
    base = _src(frames, mspecs)
    t = pd.Timestamp(frames["GER40"]["ts"].iloc[60000])
    base.set_time(now_of(t))
    clean = base.m5_frame("GER40", 100)
    leaky = LeakySource(base, frames, mutate=False).m5_frame("GER40", 100)
    garbage = LeakySource(base, frames, mutate=True).m5_frame("GER40", 100)
    assert len(leaky) > len(clean) and len(garbage) == len(leaky)
    pd.testing.assert_frame_equal(leaky.iloc[: len(clean)].reset_index(drop=True), clean.reset_index(drop=True))
    fut_l, fut_g = leaky.iloc[len(clean):], garbage.iloc[len(clean):]
    assert not np.allclose(fut_l["close"].to_numpy(), fut_g["close"].to_numpy())
    assert (fut_g["spread_pts"] == 99999.0).all()
    pd.testing.assert_frame_equal(garbage.iloc[: len(clean)].reset_index(drop=True), clean.reset_index(drop=True))
