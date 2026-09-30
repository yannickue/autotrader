# ruff: noqa: E501
"""Lane U2: engine ``ShadowScan`` (OUT_OF_WINDOW_SHADOW relaxed-window pass + SHADOW_UNIVERSE code) and sequence plumbing.

The family generators are replaced by a deterministic stub (``stub_generate``) so the tests exercise exactly the new
engine logic (relaxed live-only calendar, real-window filter, reason override, no-intent guarantee, causality)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from alpha.fast.sim import CandidateArrays
from demo.opportunity import engine as eng
from demo.opportunity.bar_source import M5_SECONDS, ReplayBarSource
from demo.opportunity.engine import OpportunityEngine, ShadowScan, relaxed_market_spec
from demo.opportunity.production_spec import load_production_spec
from demo.sequence_metrics import SequenceTracker
from markets.spec import load_market_spec

MSPEC = load_market_spec("GER40")
PROD = load_production_spec()


def synth_frame(last_open: datetime, n: int = 800, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.DatetimeIndex([last_open - timedelta(seconds=M5_SECONDS * k) for k in range(n - 1, -1, -1)], tz="UTC").as_unit("ns")
    close = 15000 + np.cumsum(rng.normal(0, 3.0, n))
    return pd.DataFrame({
        "ts": ts, "open": close - 0.5, "high": close + 4.0, "low": close - 4.0, "close": close,
        "tick_volume": np.full(n, 100.0), "spread_pts": np.full(n, 10.0),
    })


def engine_at(last_open: datetime, *, seq: SequenceTracker | None = None, n: int = 800) -> tuple[OpportunityEngine, datetime]:
    fr = synth_frame(last_open, n)
    src = ReplayBarSource({"GER40": fr}, {"GER40": MSPEC.point_size})
    now = last_open + timedelta(seconds=M5_SECONDS)
    src.set_time(now)
    e = OpportunityEngine(
        src, production=PROD, market_specs={"GER40": MSPEC}, commit="t", min_history_bars=600, sequence_tracker=seq,
    )
    return e, now


@pytest.fixture
def stub_generate(monkeypatch):
    """EVERY frozen spec signals LONG at the deciding bar (stop 10 below the close, fixed 1.5R target)."""

    def gen(data, spec, thr):
        i = len(data) - 2
        stop = float(data.c[i]) - 10.0
        return CandidateArrays(
            np.array([i], dtype=np.int64), np.array([1], dtype=np.int8), np.array([stop]), np.array([np.nan]),
            np.array([1.5]), np.array([0], dtype=np.int8),
        )

    monkeypatch.setattr(eng, "generate_candidates", gen)
    return gen


def _empty() -> CandidateArrays:
    z = np.zeros(0)
    return CandidateArrays(z.astype(np.int64), z.astype(np.int8), z, z, z, z.astype(np.int8))


# 2026-06-10 is a Wednesday; GER40 entry window 09:00-20:00 Berlin (CEST = UTC+2)
OUT_OPEN = datetime(2026, 6, 10, 22, 0, tzinfo=UTC)  # bar open 22:00Z, entry bar 22:05Z = 00:05 Berlin: window closed
IN_OPEN = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)  # entry bar 08:05Z = 10:05 Berlin: window open


def test_out_of_window_pass_records_rejected_code_and_never_an_intent(stub_generate):
    e, now = engine_at(OUT_OPEN)
    pairs = e.on_m5_close("GER40", now, shadow=ShadowScan(code="OUT_OF_WINDOW_SHADOW", origin="OUT_OF_WINDOW_SHADOW", relax_window=True))
    assert len(pairs) == len(PROD.specs_for("GER40"))
    snap, dec = pairs[0]
    assert dec.accepted is False and dec.reasons[0] == "OUT_OF_WINDOW_SHADOW"
    assert snap.signal["origin"] == "OUT_OF_WINDOW_SHADOW"
    w = snap.signal["window_relaxed"]
    assert w["real_entry_start_min"] == 540 and w["real_entry_end_min"] == 1200  # the REAL window is recorded, not widened
    assert e.last_intents == [] and e.intents_for(pairs) == []  # measurement only: no intent, ever


def test_bar_inside_the_real_window_is_left_to_the_normal_path(stub_generate):
    e, now = engine_at(IN_OPEN)
    pairs = e.on_m5_close("GER40", now, shadow=ShadowScan(code="OUT_OF_WINDOW_SHADOW", origin="OUT_OF_WINDOW_SHADOW", relax_window=True))
    assert pairs == [] and e.shadow_skipped_in_window >= 1


def test_shadow_pass_does_not_mutate_frozen_specs_or_market_spec(stub_generate):
    e, now = engine_at(OUT_OPEN)
    before_hashes = [fs.spec_hash for fs in PROD.specs_for("GER40")]
    cal_before = MSPEC.calendar
    e.on_m5_close("GER40", now, shadow=ShadowScan(code="OUT_OF_WINDOW_SHADOW", origin="OUT_OF_WINDOW_SHADOW", relax_window=True))
    assert [fs.spec_hash for fs in PROD.specs_for("GER40")] == before_hashes
    assert MSPEC.calendar == cal_before and (MSPEC.calendar.entry_start_min, MSPEC.calendar.entry_end_min) == (540, 1200)
    rel = relaxed_market_spec(MSPEC)
    assert (rel.calendar.entry_start_min, rel.calendar.entry_end_min, rel.calendar.forced_flat_min) == (0, 1440, 1440)
    assert rel is not MSPEC and rel.trading_enabled is False


def test_shadow_scan_is_idempotent_per_bar_and_independent_of_the_normal_path(stub_generate):
    e, now = engine_at(OUT_OPEN)
    sh = ShadowScan(code="OUT_OF_WINDOW_SHADOW", origin="OUT_OF_WINDOW_SHADOW", relax_window=True)
    first = e.on_m5_close("GER40", now, shadow=sh)
    assert first and e.on_m5_close("GER40", now, shadow=sh) == []  # same bar again: nothing
    # the normal path for the SAME bar still runs (its own idempotence namespace)
    assert isinstance(e.on_m5_close("GER40", now), list)


def test_admit_callback_caps_and_counts(stub_generate):
    e, now = engine_at(OUT_OPEN)
    seen: list[str] = []

    def admit(c):
        seen.append(c.family)
        return False

    assert e.on_m5_close("GER40", now, shadow=ShadowScan(code="OUT_OF_WINDOW_SHADOW", origin="OUT_OF_WINDOW_SHADOW", relax_window=True, admit=admit)) == []
    assert seen  # the callback was consulted; a False answer records nothing


def test_shadow_scan_is_causal_future_bars_do_not_change_the_output(stub_generate):
    e1, now = engine_at(OUT_OPEN)
    p1 = e1.on_m5_close("GER40", now, shadow=ShadowScan(code="OUT_OF_WINDOW_SHADOW", origin="OUT_OF_WINDOW_SHADOW", relax_window=True))
    # same history + 50 garbage bars AFTER the deciding bar (ReplayBarSource only exposes closed bars <= now)
    fr = pd.concat([
        synth_frame(OUT_OPEN, 800),
        synth_frame(OUT_OPEN + timedelta(seconds=M5_SECONDS * 50), 50, seed=99).assign(close=1.0, open=1.0, high=1.0, low=1.0),
    ]).drop_duplicates("ts").reset_index(drop=True)
    src = ReplayBarSource({"GER40": fr}, {"GER40": MSPEC.point_size})
    src.set_time(now)
    e2 = OpportunityEngine(src, production=PROD, market_specs={"GER40": MSPEC}, commit="t", min_history_bars=600)
    p2 = e2.on_m5_close("GER40", now, shadow=ShadowScan(code="OUT_OF_WINDOW_SHADOW", origin="OUT_OF_WINDOW_SHADOW", relax_window=True))
    assert [s.opportunity_id for s, _ in p1] == [s.opportunity_id for s, _ in p2]
    assert p1[0][0].geometry == p2[0][0].geometry


def test_default_path_is_unchanged_without_shadow_and_without_tracker(stub_generate):
    e, now = engine_at(IN_OPEN)
    pairs = e.on_m5_close("GER40", now)
    assert pairs and "sequence" not in pairs[0][0].signal and "origin" not in pairs[0][0].signal


def test_sequence_metrics_are_persisted_in_signal_when_a_tracker_is_attached(stub_generate):
    e, now = engine_at(IN_OPEN, seq=SequenceTracker())
    snap, _dec = e.on_m5_close("GER40", now)[0]
    seq = snap.signal["sequence"]
    assert seq["schema"] == "signal-sequence-1" and seq["same_zone_reengagement"] is False and seq["whipsaw_sequence"] == "LONG"
