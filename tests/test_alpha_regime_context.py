from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from alpha.context import CONTEXT_LABELS, ContextConfig, classify_context
from alpha.regime import RegimeConfig, classify_regime
from alpha.timeframe import MtfView


def _bars(closes: np.ndarray, start: str = "2026-02-02 08:00") -> pd.DataFrame:
    ts = pd.date_range(start, periods=len(closes), freq="5min", tz="Europe/Berlin")
    opens = np.r_[closes[0], closes[:-1]]
    width = np.maximum(0.2, np.abs(closes - opens) * 0.25)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": opens,
            "high": np.maximum(opens, closes) + width,
            "low": np.minimum(opens, closes) - width,
            "close": closes,
            "spread_pts": np.full(len(closes), 1.5),
        }
    )


def _m5_trend(count: int = 900, step: float = 0.08) -> pd.DataFrame:
    return _bars(10_000.0 + np.arange(count) * step)


def test_config_fingerprints_are_stable_and_cover_every_parameter() -> None:
    for config in (RegimeConfig(), ContextConfig()):
        assert config.fingerprint() == config.fingerprint()
        for field_name in config.__dataclass_fields__:
            value = getattr(config, field_name)
            changed = value + 1 if isinstance(value, int) else value + 0.01
            assert replace(config, **{field_name: changed}).fingerprint() != config.fingerprint()


def test_regime_is_m5_aligned_and_uses_only_completed_h1_bars() -> None:
    frame = _m5_trend(25)
    result = classify_regime(frame, RegimeConfig(volatility_history=2))
    view = MtfView(frame)

    assert result.index.equals(view.m5.index)
    assert pd.isna(result.iloc[10]["higher_bar_ts"])
    assert result.iloc[11]["higher_bar_ts"] == view.h1.index[0]
    assert result.iloc[22]["higher_bar_ts"] == view.h1.index[0]
    assert result.iloc[23]["higher_bar_ts"] == view.h1.index[1]


def test_regime_warmup_is_undefined_then_synthetic_trend_is_up_and_trending() -> None:
    config = RegimeConfig(volatility_history=8)
    result = classify_regime(_m5_trend(), config)

    assert (
        (
            result.loc[
                ~result["defined"], ["DIRECTION", "TREND_STRENGTH", "VOLATILITY", "VOL_STATE"]
            ]
            == "UNDEFINED"
        )
        .all()
        .all()
    )
    last = result[result["defined"]].iloc[-1]
    assert last["DIRECTION"] == "UP"
    assert last["TREND_STRENGTH"] == "TRENDING"


def test_regime_detects_range_like_and_volatility_shift() -> None:
    hours = 90
    quiet = np.sin(np.arange(hours * 12) / 12.0 * np.pi) * 0.4
    volatile = np.sin(np.arange(20 * 12) * np.pi / 6.0) * 8.0
    closes = 10_000.0 + np.r_[quiet, volatile]
    result = classify_regime(_bars(closes), RegimeConfig(volatility_history=20))

    defined = result[result["defined"]]
    assert "RANGE_LIKE" in set(defined["TREND_STRENGTH"])
    assert "HIGH" in set(defined.iloc[-12:]["VOLATILITY"])
    assert "EXPANSION" in set(defined.iloc[-12:]["VOL_STATE"])


def test_regime_percentiles_are_past_only() -> None:
    config = RegimeConfig(volatility_history=8)
    frame = _m5_trend(700)
    result = classify_regime(frame, config)
    row = result[result["defined"]].iloc[0]
    completed = result.drop_duplicates("higher_bar_ts").dropna(subset=["higher_bar_ts"])
    current = completed.index.get_loc(row.name)
    prior = completed.iloc[current - config.volatility_history : current]["realized_vol"]

    assert row["volatility_past_low"] == pytest.approx(
        prior.quantile(config.volatility_low_quantile)
    )
    assert row["volatility_past_high"] == pytest.approx(
        prior.quantile(config.volatility_high_quantile)
    )


@pytest.mark.parametrize("classifier", [classify_regime, classify_context])
def test_engines_are_truncation_invariant_and_stateless(classifier) -> None:
    frame = _m5_trend(700)
    cutoff = 521
    full = classifier(frame)
    prefix = classifier(frame.iloc[: cutoff + 1].copy())
    altered = frame.copy()
    altered.loc[cutoff + 1 :, ["open", "high", "low", "close"]] += 1_000_000.0
    changed = classifier(altered)

    pd.testing.assert_frame_equal(full.iloc[: cutoff + 1], prefix)
    pd.testing.assert_frame_equal(full.iloc[: cutoff + 1], changed.iloc[: cutoff + 1])
    pd.testing.assert_frame_equal(full, classifier(frame.copy()))


def test_context_is_m5_aligned_uses_completed_m15_and_has_undefined_warmup() -> None:
    config = ContextConfig(range_window=6, breakout_lookback=4)
    frame = _m5_trend(250)
    result = classify_context(frame, config)
    view = MtfView(frame)

    assert result.index.equals(view.m5.index)
    assert pd.isna(result.iloc[1]["higher_bar_ts"])
    assert result.iloc[2]["higher_bar_ts"] == view.m15.index[0]
    assert all(labels == ("UNDEFINED",) for labels in result.loc[~result["defined"], "labels"])
    assert not result.loc[~result["defined"], list(CONTEXT_LABELS)].to_numpy(bool).any()


def test_context_synthetic_series_emit_trend_pullback_range_and_compression_labels() -> None:
    trend = 10_000.0 + np.arange(450) * 0.12
    trend[-18:] -= np.linspace(0.0, 3.0, 18)
    ranged = (
        10_100.0
        + np.r_[
            np.sin(np.arange(300) * np.pi / 9.0) * 3.0,
            np.sin(np.arange(150) * np.pi / 9.0) * 0.25,
        ]
    )

    trend_result = classify_context(_bars(trend))
    range_result = classify_context(_bars(ranged))

    trend_labels = {label for labels in trend_result["labels"] for label in labels}
    range_labels = {label for labels in range_result["labels"] for label in labels}
    assert "TREND_CONTINUATION" in trend_labels
    assert "PULLBACK" in trend_labels
    assert {"CONSOLIDATION", "COMPRESSION", "RANGE_EXTREME"} <= range_labels


def test_context_breakout_retest_failed_breakout_momentum_and_reversal_are_reachable() -> None:
    config = ContextConfig(
        range_window=6,
        breakout_lookback=4,
        reference_window=4,
        breakout_buffer_atr=0.75,
    )
    base = np.tile(np.array([100.0, 100.3, 99.8]), 70)
    tail = np.array([100.0, 100.2, 100.1, 103.0, 100.6, 100.0, 97.0, 99.7, 103.5])
    closes = np.repeat(np.r_[base, tail], 3)
    result = classify_context(_bars(closes), config)
    labels = {label for row in result["labels"] for label in row}

    assert {
        "BREAKOUT_SETUP",
        "RETEST",
        "FAILED_BREAKOUT",
        "MOMENTUM_CONTINUATION",
        "REVERSAL_CONTEXT",
    } <= labels
