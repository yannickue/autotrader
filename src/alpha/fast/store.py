"""Persistent causal feature arrays aligned to M5 bar-open timestamps.

Stable array-name contract (every value is a one-dimensional NumPy array of M5 length):

``ts_ns:int64`` UTC timestamp; ``o/h/l/c/spread:float64`` M5 prices and spread in price
units; ``berlin_minute:int16``, ``berlin_day_id:int32``, ``phase_code:int8`` and
``contig:bool`` describe local time/session and M5 continuity.  Session arrays are
``previous_day_high/low/close``, ``session_open/high/low`` (float64, unknown is NaN).

Parity arrays are ``m5_atr14`` and, for ``m15`` and ``h1``, ``{tf}_o/h/l/c/range``.
Regime dimensions are ``regime_direction``, ``regime_trend_strength``,
``regime_volatility``, ``regime_vol_state`` (int16); context flags are
``context_trend_continuation/pullback/consolidation/compression/range_extreme/``
``breakout_setup/retest/failed_breakout/momentum_continuation/reversal_context`` (bool);
``context_range_low/high`` (float64, M15 range bounds from the context frame, NaN unknown).
Integer-to-string maps live in metadata and the cache manifest.

For each of ``m5``, ``m15`` and ``h1``, the curated arrays are ``{tf}_atr14``,
``{tf}_adx14``, ``{tf}_rsi14``, ``{tf}_ema_slope``, ``{tf}_sma_slope``,
``{tf}_bollinger_width``, ``{tf}_normalized_return``, ``{tf}_efficiency_ratio``,
``{tf}_range_position``, ``{tf}_volatility_percentile``, and candlestick outputs
``{tf}_cdlengulfing``, ``{tf}_cdlhammer``, ``{tf}_cdldoji``.  Higher-timeframe arrays
are forward-mapped only after a complete bar closes.

Price-action arrays are ``last_swing_high/low`` (float64; pivot exposed at its
confirmation bar), ``structure_state:int8`` (-2 LL, -1 LH, 0 unknown, 1 HL, 2 HH),
``bos:int8`` (-1 down, 0 none, 1 up), and ``compression_expansion_ratio:float64``.
All rolling calculations are trailing and causal; bar i is known at its close.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import talib

import alpha.context as context_module
import alpha.regime as regime_module
import alpha.timeframe as timeframe_module
from alpha.context import CONTEXT_LABELS, ContextConfig, classify_context
from alpha.regime import REGIME_DIMENSIONS, RegimeConfig, classify_regime
from alpha.timeframe import MtfView

FEATURE_SCHEMA_VERSION = 2
# Bump whenever the set/definition of price-action feature arrays changes (cache invalidation).
FEATURE_SET_VERSION = 2
NEW_FEATURE_NAMES: tuple[str, ...] = (
    "dist_pdh_atr",
    "dist_pdl_atr",
    "dist_pdc_atr",
    "dist_sess_open_atr",
    "dist_sess_high_atr",
    "dist_sess_low_atr",
    "dist_swing_high_atr",
    "dist_swing_low_atr",
    "brk_up_20",
    "brk_dn_20",
    "brk_up_48",
    "brk_dn_48",
    "from_high_24_atr",
    "from_low_24_atr",
    "range_ratio_12_48",
    "bar_range_atr",
    "bar_body_ratio",
    "bar_close_loc",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "bar_dir",
    "mom_3_atr",
    "mom_6_atr",
    "mom_12_atr",
    "sweep_hi_20",
    "sweep_lo_20",
    "sweep_pdh",
    "sweep_pdl",
    "gap_atr",
)
_PHASES = ("EUROPEAN_OPEN", "MORNING", "MIDDAY", "US_CASH_OPEN_OVERLAP", "LATE")
_TA_NAMES = (
    "atr14",
    "adx14",
    "rsi14",
    "ema_slope",
    "sma_slope",
    "bollinger_width",
    "normalized_return",
    "efficiency_ratio",
    "range_position",
    "volatility_percentile",
    "cdlengulfing",
    "cdlhammer",
    "cdldoji",
)


@dataclass(frozen=True)
class FeatureConfig:
    """Versioned knobs affecting feature values and therefore cache identity."""

    point_size: float = 1.0
    swing_order: int = 3
    slope_lookback: int = 3
    efficiency_window: int = 10
    range_window: int = 20
    volatility_window: int = 20
    volatility_history: int = 100
    compression_short: int = 5
    compression_long: int = 20
    timeframes: tuple[str, ...] = ("M5", "M15", "H1")
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    context: ContextConfig = field(default_factory=ContextConfig)


class FeatureSet(dict[str, np.ndarray]):
    """Dictionary of aligned arrays plus provenance and timing metadata."""

    def __init__(self, arrays: Mapping[str, np.ndarray], metadata: Mapping[str, Any]):
        super().__init__(arrays)
        self.metadata = dict(metadata)


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _config(config: FeatureConfig | Mapping[str, Any] | None) -> FeatureConfig:
    if config is None:
        return FeatureConfig()
    if isinstance(config, FeatureConfig):
        return config
    values = dict(config)
    if isinstance(values.get("regime"), Mapping):
        values["regime"] = RegimeConfig(**values["regime"])
    if isinstance(values.get("context"), Mapping):
        values["context"] = ContextConfig(**values["context"])
    if "timeframes" in values:
        values["timeframes"] = tuple(values["timeframes"])
    return FeatureConfig(**values)


def _hash_frame(frame: pd.DataFrame) -> str:
    spread_name = "spread" if "spread" in frame else "spread_pts"
    required = ["ts", "open", "high", "low", "close", spread_name]
    missing = set(required).difference(frame)
    if missing:
        raise ValueError(f"missing feature-store columns: {sorted(missing)}")
    hashed = pd.util.hash_pandas_object(frame.loc[:, required], index=False).to_numpy(np.uint64)
    return hashlib.sha256(hashed.tobytes()).hexdigest()


def _code_fingerprint() -> str:
    digest = hashlib.sha256()
    for module in (
        inspect.getmodule(_code_fingerprint),
        timeframe_module,
        regime_module,
        context_module,
    ):
        path = Path(inspect.getsourcefile(module) or "")
        digest.update(str(path.name).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _library_versions() -> dict[str, str]:
    """Feature values depend on these libraries (TA-Lib warm-up, pandas rolling, numpy)."""
    from importlib import metadata

    versions = {}
    for name in ("ta-lib", "numpy", "pandas"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "NOT_INSTALLED"
    return versions


def _key_components(frame: pd.DataFrame, config: FeatureConfig) -> dict[str, Any]:
    return {
        "dataset_hash": _hash_frame(frame),
        "schema_version": FEATURE_SCHEMA_VERSION,
        "feature_set_version": FEATURE_SET_VERSION,
        "new_feature_names": list(NEW_FEATURE_NAMES),
        "timeframes": list(config.timeframes),
        "parameters": _plain(config),
        "code_fingerprint": _code_fingerprint(),
        "library_versions": _library_versions(),
    }


def _cache_key(components: Mapping[str, Any]) -> str:
    payload = json.dumps(components, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    previous = np.r_[np.nan, close[:-1]]
    return np.nanmax(
        np.vstack((high - low, np.abs(high - previous), np.abs(low - previous))), axis=0
    )


def _rolling_percentile(values: np.ndarray, history: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    for i, value in enumerate(values):
        if not np.isfinite(value):
            continue
        start = max(0, i - history)
        past = values[start:i]
        past = past[np.isfinite(past)]
        if len(past):
            result[i] = (np.count_nonzero(past <= value)) / len(past)
    return result


def _lagged_slope(values: np.ndarray, scale: np.ndarray, lag: int) -> np.ndarray:
    """(x[i] - x[i-lag]) / scale[i], NaN for the first ``lag`` bars (also for short series)."""
    out = np.full(len(values), np.nan)
    if len(values) > lag:
        out[lag:] = (values[lag:] - values[:-lag]) / scale[lag:]
    return out


def _ta_arrays(bars: pd.DataFrame, config: FeatureConfig) -> dict[str, np.ndarray]:
    o = bars["open"].to_numpy(float)
    h = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    atr = talib.ATR(h, low, c, timeperiod=14)
    ema = talib.EMA(c, timeperiod=20)
    sma = talib.SMA(c, timeperiod=20)
    scale = np.where(atr > 0, atr, np.nan)
    lag = config.slope_lookback
    ema_slope = _lagged_slope(ema, scale, lag)
    sma_slope = _lagged_slope(sma, scale, lag)
    upper, middle, lower = talib.BBANDS(c, timeperiod=20)
    returns = np.r_[np.nan, np.diff(c) / np.where(c[:-1] != 0, c[:-1], np.nan)]
    normalized_return = np.r_[np.nan, np.diff(c)] / scale
    efficiency = np.full(len(c), np.nan)
    window = config.efficiency_window
    if len(c) > window:
        movement = np.abs(np.diff(c))
        denominator = pd.Series(movement).rolling(window).sum().to_numpy()
        numerator = np.abs(c[window:] - c[:-window])
        divisor = denominator[window - 1 :]
        efficiency[window:] = np.divide(
            numerator, divisor, out=np.full(len(numerator), np.nan), where=divisor > 0
        )
    roll_high = (
        pd.Series(h).rolling(config.range_window, min_periods=config.range_window).max().to_numpy()
    )
    roll_low = (
        pd.Series(low)
        .rolling(config.range_window, min_periods=config.range_window)
        .min()
        .to_numpy()
    )
    span = roll_high - roll_low
    realized = (
        pd.Series(returns)
        .rolling(config.volatility_window, min_periods=config.volatility_window)
        .std()
        .to_numpy()
    )
    return {
        "atr14": atr,
        "adx14": talib.ADX(h, low, c, timeperiod=14),
        "rsi14": talib.RSI(c, timeperiod=14),
        "ema_slope": ema_slope,
        "sma_slope": sma_slope,
        "bollinger_width": (upper - lower) / np.where(np.abs(middle) > 0, np.abs(middle), np.nan),
        "normalized_return": normalized_return,
        "efficiency_ratio": efficiency,
        "range_position": (c - roll_low) / np.where(span > 0, span, np.nan),
        "volatility_percentile": _rolling_percentile(realized, config.volatility_history),
        "cdlengulfing": talib.CDLENGULFING(o, h, low, c).astype(np.int16),
        "cdlhammer": talib.CDLHAMMER(o, h, low, c).astype(np.int16),
        "cdldoji": talib.CDLDOJI(o, h, low, c).astype(np.int16),
    }


def _map_higher(values: np.ndarray, alignment: np.ndarray) -> np.ndarray:
    dtype = values.dtype
    if np.issubdtype(dtype, np.integer):
        result = np.zeros(len(alignment), dtype=dtype)
    else:
        result = np.full(len(alignment), np.nan, dtype=float)
    known = alignment >= 0
    result[known] = values[alignment[known]]
    return result


def _levels(view: MtfView) -> tuple[dict[str, np.ndarray], dict[str, dict[str, int]]]:
    levels = view._levels
    names = (
        "previous_day_high",
        "previous_day_low",
        "previous_day_close",
        "session_open",
        "session_high",
        "session_low",
    )
    arrays = {
        name: np.asarray(
            [np.nan if getattr(row, name) is None else getattr(row, name) for row in levels],
            dtype=float,
        )
        for name in names
    }
    phase_map = {name: i for i, name in enumerate(_PHASES)}
    arrays["phase_code"] = np.asarray([phase_map[row.phase] for row in levels], dtype=np.int8)
    return arrays, {"phase": {str(code): name for name, code in phase_map.items()}}


def _encoded_labels(
    frame: pd.DataFrame, config: FeatureConfig
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    regime = classify_regime(frame, config.regime)
    context = classify_context(frame, config.context)
    arrays: dict[str, np.ndarray] = {}
    maps: dict[str, Any] = {"regime": {}}
    vocabularies = {
        "DIRECTION": ("UNDEFINED", "DOWN", "NEUTRAL", "UP"),
        "TREND_STRENGTH": ("UNDEFINED", "RANGE_LIKE", "WEAK", "TRENDING"),
        "VOLATILITY": ("UNDEFINED", "LOW", "NORMAL", "HIGH"),
        "VOL_STATE": ("UNDEFINED", "COMPRESSION", "EXPANSION"),
    }
    for dimension in REGIME_DIMENSIONS:
        values = regime[dimension].astype(str)
        encode = {label: code for code, label in enumerate(vocabularies[dimension])}
        arrays[f"regime_{dimension.lower()}"] = values.map(encode).to_numpy(np.int16)
        maps["regime"][dimension] = {str(code): label for label, code in encode.items()}
    for label in CONTEXT_LABELS:
        arrays[f"context_{label.lower()}"] = context[label].to_numpy(bool)
    # M15 range bounds exactly as the semantic mean-reversion strategy reads them (NaN = unknown)
    arrays["context_range_low"] = context["range_low"].to_numpy(float)
    arrays["context_range_high"] = context["range_high"].to_numpy(float)
    return arrays, maps


def _swings(high: np.ndarray, low: np.ndarray, order: int) -> dict[str, np.ndarray]:
    if order < 1:
        raise ValueError("swing_order must be positive")
    n = len(high)
    last_high = np.full(n, np.nan)
    last_low = np.full(n, np.nan)
    state = np.zeros(n, dtype=np.int8)
    bos = np.zeros(n, dtype=np.int8)
    known_high = known_low = np.nan
    for confirmation in range(2 * order, n):
        pivot = confirmation - order
        h_window = high[pivot - order : pivot + order + 1]
        l_window = low[pivot - order : pivot + order + 1]
        if (
            np.isfinite(high[pivot])
            and high[pivot] == np.nanmax(h_window)
            and np.count_nonzero(h_window == high[pivot]) == 1
        ):
            state[confirmation] = 2 if np.isnan(known_high) or high[pivot] > known_high else -1
            known_high = high[pivot]
        if (
            np.isfinite(low[pivot])
            and low[pivot] == np.nanmin(l_window)
            and np.count_nonzero(l_window == low[pivot]) == 1
        ):
            state[confirmation] = -2 if np.isnan(known_low) or low[pivot] < known_low else 1
            known_low = low[pivot]
        last_high[confirmation] = known_high
        last_low[confirmation] = known_low
        if confirmation:
            if np.isnan(last_high[confirmation]):
                last_high[confirmation] = last_high[confirmation - 1]
            if np.isnan(last_low[confirmation]):
                last_low[confirmation] = last_low[confirmation - 1]
        if np.isfinite(known_high) and high[confirmation] > known_high:
            bos[confirmation] = 1
        elif np.isfinite(known_low) and low[confirmation] < known_low:
            bos[confirmation] = -1
    return {
        "last_swing_high": last_high,
        "last_swing_low": last_low,
        "structure_state": state,
        "bos": bos,
    }


def _run_start(day_id: np.ndarray, contig: np.ndarray) -> np.ndarray:
    """Index of the first bar of the day-contiguous run containing each bar."""
    n = len(day_id)
    idx = np.arange(n)
    broken = ~contig
    if n:
        broken = broken.copy()
        broken[0] = True
        broken[1:] |= day_id[1:] != day_id[:-1]
    return np.maximum.accumulate(np.where(broken, idx, 0))


def _roll(values: np.ndarray, window: int, fn: str) -> np.ndarray:
    roller = pd.Series(values).rolling(window, min_periods=window)
    return (roller.max() if fn == "max" else roller.min()).to_numpy()


def _price_action_arrays(a: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Normalized price-action features; bar i uses only bars <= i (NaN when window leaves the
    day-contiguous run, so nothing crosses the overnight gap, a DST change or a data hole)."""
    o, h, low, c = a["o"], a["h"], a["l"], a["c"]
    n = len(c)
    idx = np.arange(n)
    start = _run_start(a["berlin_day_id"], a["contig"])
    atr = a["m5_atr14"]
    scale = np.where(atr > 0, atr, np.nan)
    out: dict[str, np.ndarray] = {}
    for name, level in (
        ("dist_pdh_atr", "previous_day_high"),
        ("dist_pdl_atr", "previous_day_low"),
        ("dist_pdc_atr", "previous_day_close"),
        ("dist_sess_open_atr", "session_open"),
        ("dist_sess_high_atr", "session_high"),
        ("dist_sess_low_atr", "session_low"),
        ("dist_swing_high_atr", "last_swing_high"),
        ("dist_swing_low_atr", "last_swing_low"),
    ):
        out[name] = (c - a[level]) / scale

    def valid(back: int) -> np.ndarray:
        return idx - back >= start

    def prior(window: int, fn: str) -> np.ndarray:
        src = h if fn == "max" else low
        shifted = np.r_[np.nan, _roll(src, window, fn)[:-1]] if n else src.copy()
        return np.where(valid(window), shifted, np.nan)

    for window in (20, 48):
        out[f"brk_up_{window}"] = (c - prior(window, "max")) / scale
        out[f"brk_dn_{window}"] = (prior(window, "min") - c) / scale
    hi24 = np.where(valid(23), _roll(h, 24, "max"), np.nan)
    lo24 = np.where(valid(23), _roll(low, 24, "min"), np.nan)
    out["from_high_24_atr"] = (hi24 - c) / scale
    out["from_low_24_atr"] = (c - lo24) / scale
    span12 = _roll(h, 12, "max") - _roll(low, 12, "min")
    span48 = _roll(h, 48, "max") - _roll(low, 48, "min")
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = span12 / np.where(span48 > 0, span48, np.nan)
    out["range_ratio_12_48"] = np.where(valid(47), ratio, np.nan)

    rng = h - low
    flat = rng <= 0
    safe = np.where(flat, np.nan, rng)
    with np.errstate(invalid="ignore", divide="ignore"):
        out["bar_range_atr"] = rng / scale
        out["bar_body_ratio"] = np.where(flat, 0.0, np.abs(c - o) / safe)
        out["bar_close_loc"] = np.where(flat, 0.5, (c - low) / safe)
        out["upper_wick_ratio"] = np.where(flat, 0.0, (h - np.maximum(o, c)) / safe)
        out["lower_wick_ratio"] = np.where(flat, 0.0, (np.minimum(o, c) - low) / safe)
    out["bar_dir"] = np.sign(c - o).astype(float)
    for k in (3, 6, 12):
        lagged = np.full(n, np.nan)
        if n > k:
            lagged[k:] = c[:-k]
        out[f"mom_{k}_atr"] = np.where(valid(k), (c - lagged) / scale, np.nan)

    pmax = prior(20, "max")
    pmin = prior(20, "min")
    ok20 = np.isfinite(pmax) & np.isfinite(pmin)
    out["sweep_hi_20"] = np.where(ok20, ((h > pmax) & (c < pmax)).astype(float), np.nan)
    out["sweep_lo_20"] = np.where(ok20, ((low < pmin) & (c > pmin)).astype(float), np.nan)
    pdh, pdl = a["previous_day_high"], a["previous_day_low"]
    out["sweep_pdh"] = np.where(np.isfinite(pdh), ((h > pdh) & (c < pdh)).astype(float), np.nan)
    out["sweep_pdl"] = np.where(np.isfinite(pdl), ((low < pdl) & (c > pdl)).astype(float), np.nan)
    out["gap_atr"] = (a["session_open"] - a["previous_day_close"]) / scale
    return {name: out[name].astype(np.float64) for name in NEW_FEATURE_NAMES}


class FeatureStore:
    """Build and persist an immutable set of causal research features."""

    @staticmethod
    def build(
        df: pd.DataFrame, config: FeatureConfig | Mapping[str, Any] | None = None
    ) -> FeatureSet:
        started = time.perf_counter()
        cfg = _config(config)
        print(f"[features] building {len(df):,} M5 bars")
        view = MtfView(df)
        m5 = view.m5
        n = len(m5)
        local = m5.index.tz_convert("Europe/Berlin")
        local_dates = local.normalize().tz_localize(None).to_numpy().astype("datetime64[D]")
        _, day_id = np.unique(local_dates, return_inverse=True)
        o, h, low, c = (m5[name].to_numpy(float) for name in ("open", "high", "low", "close"))
        arrays: dict[str, np.ndarray] = {
            "ts_ns": m5.index.as_unit("ns").asi8.copy(),
            "o": o,
            "h": h,
            "l": low,
            "c": c,
            "spread": m5["spread_pts"].to_numpy(float) * cfg.point_size,
            "berlin_minute": (local.hour * 60 + local.minute).to_numpy(np.int16),
            "berlin_day_id": day_id.astype(np.int32),
            "contig": np.r_[True, np.diff(m5.index.as_unit("s").asi8) == 300],
        }
        level_arrays, maps = _levels(view)
        arrays.update(level_arrays)
        arrays.update({f"m5_{name}": value for name, value in _ta_arrays(m5, cfg).items()})
        arrays["m5_atr14"] = (
            pd.Series(_true_range(h, low, c)).rolling(14, min_periods=14).mean().to_numpy()
        )
        for prefix, bars, alignment in (
            ("m15", view.m15, view.m15_alignment),
            ("h1", view.h1, view.h1_alignment),
        ):
            for short, source in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")):
                arrays[f"{prefix}_{short}"] = _map_higher(bars[source].to_numpy(float), alignment)
            arrays[f"{prefix}_range"] = arrays[f"{prefix}_h"] - arrays[f"{prefix}_l"]
            completed = np.flatnonzero(bars["complete"].to_numpy(bool))
            complete_features = _ta_arrays(bars.iloc[completed], cfg) if len(completed) else {}
            for name, compact in complete_features.items():
                expanded = np.full(len(bars), np.nan)
                if np.issubdtype(compact.dtype, np.integer):
                    expanded = np.zeros(len(bars), dtype=compact.dtype)
                expanded[completed] = compact
                arrays[f"{prefix}_{name}"] = _map_higher(expanded, alignment)
        label_arrays, label_maps = _encoded_labels(df, cfg)
        arrays.update(label_arrays)
        maps.update(label_maps)
        arrays.update(_swings(h, low, cfg.swing_order))
        tr = _true_range(h, low, c)
        short = (
            pd.Series(tr)
            .rolling(cfg.compression_short, min_periods=cfg.compression_short)
            .mean()
            .to_numpy()
        )
        long = (
            pd.Series(tr)
            .rolling(cfg.compression_long, min_periods=cfg.compression_long)
            .mean()
            .to_numpy()
        )
        arrays["compression_expansion_ratio"] = np.divide(
            short, long, out=np.full(n, np.nan), where=long > 0
        )
        arrays.update(_price_action_arrays(arrays))
        for name, value in arrays.items():
            if value.ndim != 1 or len(value) != n:
                raise AssertionError(f"unaligned feature {name}: {value.shape}")
        elapsed = time.perf_counter() - started
        print(f"[features] built {len(arrays)} arrays: {elapsed:.3f} s")
        return FeatureSet(
            arrays,
            {
                "schema_version": FEATURE_SCHEMA_VERSION,
                "feature_set_version": FEATURE_SET_VERSION,
                "timing_s": elapsed,
                "cache_hit": False,
                "maps": maps,
            },
        )

    @staticmethod
    def load_or_build(
        df: pd.DataFrame,
        config: FeatureConfig | Mapping[str, Any] | None,
        cache_dir: str | Path,
    ) -> FeatureSet:
        started = time.perf_counter()
        cfg = _config(config)
        components = _key_components(df, cfg)
        key = _cache_key(components)
        target = Path(cache_dir) / key
        manifest_path = target / "manifest.json"
        arrays_path = target / "features.npz"
        if manifest_path.is_file() and arrays_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("key") == key and manifest.get("key_components") == components:
                with np.load(arrays_path, allow_pickle=False) as stored:
                    arrays = {name: stored[name] for name in stored.files}
                elapsed = time.perf_counter() - started
                metadata = dict(manifest["metadata"])
                metadata.update({"timing_s": elapsed, "cache_hit": True, "cache_key": key})
                print(f"[features] loaded cache: {elapsed:.3f} s")
                return FeatureSet(arrays, metadata)
        built = FeatureStore.build(df, cfg)
        target.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(arrays_path, **built)
        metadata = dict(built.metadata)
        metadata["cache_key"] = key
        manifest = {
            "key": key,
            "key_components": components,
            "metadata": metadata,
            "arrays": {name: str(value.dtype) for name, value in built.items()},
        }
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")
        built.metadata = metadata
        print(f"[features] cached {key}: {time.perf_counter() - started:.3f} s")
        return built


__all__ = (
    "FEATURE_SCHEMA_VERSION",
    "FEATURE_SET_VERSION",
    "NEW_FEATURE_NAMES",
    "FeatureConfig",
    "FeatureSet",
    "FeatureStore",
)
