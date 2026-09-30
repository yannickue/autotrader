# ruff: noqa: E501
"""Lane U2 (C): same-zone / direction-flip / whipsaw instrumentation on synthetic sequences (measure only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from demo.sequence_metrics import SequenceTracker, regime_of, summarize

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def obs(tr, i, minutes, direction, stop, close=100.0, atr=1.0, market="GER40", family="ORB"):
    return tr.observe(
        market=market, oid=f"o{i}", signal_ts=T0 + timedelta(minutes=minutes), direction=direction, family=family,
        stop=stop, close=close, atr=atr, regime="H1:up/D1:up",
    )


def test_first_signal_has_no_history():
    s = obs(SequenceTracker(), 0, 0, -1, 101.0)
    assert s["same_zone_reengagement"] is False and s["same_zone_count"] == 0 and s["direction_flip"] is False
    assert s["time_between_signals_s"] is None and s["whipsaw_sequence"] == "SHORT" and s["whipsaw"] is False
    assert s["regime"] == "H1:up/D1:up" and s["family"] == "ORB"


def test_whipsaw_sequence_short_short_long_short_around_one_zone():
    tr = SequenceTracker()
    out = [
        obs(tr, 0, 0, -1, 101.0, close=100.0),
        obs(tr, 1, 10, -1, 101.1, close=99.8),
        obs(tr, 2, 25, +1, 100.9, close=100.2),
        obs(tr, 3, 40, -1, 101.0, close=99.9),
    ]
    assert out[1]["repeated_level_attempt"] is True and out[1]["direction_flip"] is False
    assert out[2]["direction_flip"] is True and out[2]["whipsaw_flips"] == 1 and out[2]["whipsaw"] is False
    last = out[3]
    assert last["whipsaw_sequence"] == "SHORT,SHORT,LONG,SHORT"
    assert last["whipsaw_flips"] == 2 and last["whipsaw"] is True
    assert last["same_zone_reengagement"] is True and last["same_zone_count"] == 3
    assert last["direction_flip"] is True and last["prev_direction"] == "LONG"
    assert last["time_between_signals_s"] == pytest.approx(15 * 60)
    assert last["price_distance_between_signals"] == pytest.approx(0.3)
    assert last["price_distance_between_signals_atr"] == pytest.approx(0.3)


def test_different_zone_is_not_a_reengagement_but_still_a_flip():
    tr = SequenceTracker()
    obs(tr, 0, 0, -1, 101.0)
    s = obs(tr, 1, 5, +1, 95.0)  # stop 6 ATR away
    assert s["same_zone_reengagement"] is False and s["repeated_level_attempt"] is False
    assert s["direction_flip"] is True and s["whipsaw_sequence"] == "LONG"


def test_lookback_and_markets_are_independent():
    tr = SequenceTracker(lookback_s=3600)
    obs(tr, 0, 0, -1, 101.0)
    assert obs(tr, 1, 120, -1, 101.0)["same_zone_reengagement"] is False  # older than the lookback
    assert obs(tr, 2, 121, -1, 101.0, market="NAS100")["same_zone_reengagement"] is False
    assert obs(tr, 3, 121, -1, 101.0, market="NAS100")["same_zone_reengagement"] is True


def test_observe_is_idempotent_per_opportunity_id_and_causal():
    tr = SequenceTracker()
    a = obs(tr, 0, 0, -1, 101.0)
    assert obs(tr, 0, 0, -1, 101.0) == a  # an engine retry of the same bar must not double count
    b = obs(tr, 1, 5, -1, 101.0)
    assert b["same_zone_count"] == 1  # only ONE prior signal despite the repeated observe of o0
    assert obs(tr, 1, 5, -1, 101.0) == b


def test_non_finite_inputs_do_not_raise_and_do_not_claim_a_zone():
    tr = SequenceTracker()
    obs(tr, 0, 0, -1, 101.0)
    s = obs(tr, 1, 5, -1, float("nan"), atr=float("nan"))
    assert s["same_zone_reengagement"] is False and s["price_distance_between_signals_atr"] is None


def test_regime_of():
    assert regime_of({"H1": {"trend": "up"}, "D1": {"trend": "down"}}) == "H1:up/D1:down"
    assert regime_of({}) is None and regime_of(None) is None


def test_summarize_slices_counterfactual_outcomes_and_is_measure_only():
    tr = SequenceTracker()
    rows, cf = [], {}
    for i, (m, d, stop) in enumerate([(0, -1, 101.0), (10, -1, 101.1), (25, 1, 100.9), (40, -1, 101.0)]):
        rows.append({"opportunity_id": f"o{i}", "sequence": obs(tr, i, m, d, stop)})
        cf[f"o{i}"] = {"r": -1.0 if i == 3 else 1.0, "mfe_r": 0.5, "mae_r": -1.0}
    rows.append({"opportunity_id": "legacy", "sequence": None})  # pre-U2 snapshot: ignored, not an error
    out = summarize(rows, cf)
    assert out["measure_only"] is True and "no cooldown" in out["note"] and out["signals_with_sequence"] == 4
    assert out["whipsaw"]["n"] == 1 and out["whipsaw"]["mean_r"] == -1.0
    assert out["direction_flip"]["n"] == 2 and out["same_zone_reengagement"]["n"] == 3
    assert out["top_whipsaw_sequences"] == {"SHORT,SHORT,LONG,SHORT": 1}
    assert summarize([], {})["signals_with_sequence"] == 0
