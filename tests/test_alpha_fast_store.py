from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pandas as pd

from alpha.fast.store import FEATURE_SCHEMA_VERSION, FeatureConfig, FeatureStore


def _bars(start: str = "2024-03-28 00:00", periods: int = 900) -> pd.DataFrame:
    ts = pd.date_range(start, periods=periods, freq="5min", tz="UTC")
    x = np.arange(periods, dtype=float)
    close = 100.0 + 0.02 * x + np.sin(x / 9.0)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": close - 0.1,
            "high": close + 0.7 + (x % 7) / 100,
            "low": close - 0.6 - (x % 5) / 100,
            "close": close,
            "spread_pts": np.full(periods, 1.5),
        }
    )


def _same(left: np.ndarray, right: np.ndarray) -> bool:
    return left.dtype == right.dtype and np.array_equal(left, right, equal_nan=True)


def test_contract_is_aligned_dict_like_and_timed() -> None:
    frame = _bars(periods=400)
    features = FeatureStore.build(frame, FeatureConfig())
    assert features.metadata["schema_version"] == FEATURE_SCHEMA_VERSION
    assert features.metadata["timing_s"] >= 0
    assert all(value.ndim == 1 and len(value) == len(frame) for value in features.values())
    required = {
        "ts_ns",
        "o",
        "h",
        "l",
        "c",
        "spread",
        "berlin_minute",
        "berlin_day_id",
        "phase_code",
        "contig",
        "m5_atr14",
        "m15_o",
        "m15_range",
        "h1_c",
        "h1_range",
        "previous_day_high",
        "previous_day_low",
        "previous_day_close",
        "session_open",
        "session_high",
        "session_low",
        "last_swing_high",
        "last_swing_low",
        "structure_state",
        "bos",
        "m5_cdlengulfing",
        "m15_atr14",
        "h1_adx14",
    }
    assert required <= set(features)


def test_all_features_are_truncation_invariant() -> None:
    frame = _bars(periods=650)
    cut = 500
    full = FeatureStore.build(frame, FeatureConfig())
    changed = frame.iloc[:cut].copy()
    short = FeatureStore.build(changed, FeatureConfig())
    for name in full:
        assert _same(full[name][:cut], short[name]), name


def test_higher_timeframe_values_change_only_on_completed_boundaries() -> None:
    features = FeatureStore.build(_bars(periods=400), FeatureConfig())
    for prefix, stride in (("m15_", 3), ("h1_", 12)):
        for name, values in features.items():
            if name.startswith(prefix):
                changes = (
                    np.flatnonzero(
                        ~(
                            (values[1:] == values[:-1])
                            | (np.isnan(values[1:]) & np.isnan(values[:-1]))
                        )
                    )
                    + 1
                )
                assert np.all((changes + 1) % stride == 0), name


def test_swing_is_exposed_at_confirmation_not_pivot() -> None:
    frame = _bars(periods=30)
    frame.loc[:, ["open", "high", "low", "close"]] = 100.0
    frame.loc[5, "high"] = 120.0
    features = FeatureStore.build(frame, FeatureConfig(swing_order=2))
    assert np.isnan(features["last_swing_high"][6])
    assert features["last_swing_high"][7] == 120.0


def test_session_and_previous_day_levels_are_causal_across_dst() -> None:
    frame = _bars(start="2024-03-30 00:00", periods=900)
    features = FeatureStore.build(frame, FeatureConfig())
    day = features["berlin_day_id"]
    for day_id in np.unique(day):
        pos = np.flatnonzero(day == day_id)
        assert np.allclose(
            features["session_high"][pos], np.maximum.accumulate(frame.high.to_numpy()[pos])
        )
        assert np.allclose(
            features["session_low"][pos], np.minimum.accumulate(frame.low.to_numpy()[pos])
        )
        if day_id:
            previous = np.flatnonzero(day == day_id - 1)
            assert np.all(
                features["previous_day_high"][pos] == frame.high.to_numpy()[previous].max()
            )
            assert np.all(features["previous_day_low"][pos] == frame.low.to_numpy()[previous].min())
    autumn = FeatureStore.build(_bars(start="2024-10-26 00:00", periods=600), FeatureConfig())
    assert len(np.unique(autumn["berlin_day_id"])) == 3


def test_cache_hit_round_trip_and_all_key_components_invalidate(tmp_path, monkeypatch) -> None:
    frame = _bars(periods=400)
    config = FeatureConfig()
    first = FeatureStore.load_or_build(frame, config, tmp_path)
    manifest = json.loads(next(tmp_path.glob("*/manifest.json")).read_text())
    assert {
        "dataset_hash",
        "schema_version",
        "timeframes",
        "parameters",
        "code_fingerprint",
    } <= set(manifest["key_components"])

    def forbidden(*args, **kwargs):
        raise AssertionError("cache hit recomputed features")

    monkeypatch.setattr(FeatureStore, "build", forbidden)
    second = FeatureStore.load_or_build(frame, config, tmp_path)
    assert second.metadata["cache_hit"] is True
    assert all(_same(first[name], second[name]) for name in first)
    monkeypatch.undo()

    changed = frame.copy()
    changed.loc[10, "close"] += 1
    assert FeatureStore.load_or_build(changed, config, tmp_path).metadata["cache_hit"] is False
    assert (
        FeatureStore.load_or_build(frame, replace(config, swing_order=4), tmp_path).metadata[
            "cache_hit"
        ]
        is False
    )
    monkeypatch.setattr("alpha.fast.store.FEATURE_SCHEMA_VERSION", FEATURE_SCHEMA_VERSION + 1)
    assert FeatureStore.load_or_build(frame, config, tmp_path).metadata["cache_hit"] is False
    assert len(list(tmp_path.glob("*/manifest.json"))) == 4
