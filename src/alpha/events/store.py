"""EventSet builder and disk cache for the V2 temporal engine (W1).

``build_events(features, params)`` computes every PRECOMPUTED array of the registry
(``schema.all_array_names()``; bound-only events are evaluated by the temporal kernel) over a
``FeatureSet`` and returns an ``EventSet`` (dict name -> 1-D array of M5 length).  Nothing here
reads bars beyond the bar an array element is stamped at; origin (``evo`` prefix) arrays are
written for reporting and are never read by any logic in this package.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

import alpha.timeframe as _timeframe_module
from alpha.events import kernels_pattern as kp
from alpha.events import kernels_price as kpr
from alpha.events import kernels_state as ks
from alpha.events import kernels_structure as kst
from alpha.events import kernels_zone as kz
from alpha.events import schema
from alpha.events.htf import TfSeries, build_series, so_far_arrays
from alpha.fast.store import _run_start

_PKG_DIR = Path(__file__).resolve().parent

_ZONE_SLOT_M15_RANGE = "m15_range"  # M15 context range box (renamed from the misnomer vwap_band)


@dataclass(frozen=True)
class EventParams:
    """Knobs that change event values (part of the cache key)."""

    swing_order: int = 3
    adx_thr: float = 20.0
    momentum_window: int = 12
    momentum_impulse_atr: float = 1.0
    momentum_depth_atr: float = 1.5
    cluster_tol_atr: float = 0.5
    cluster_pad_atr: float = 0.15
    cluster_max_pivots: int = 8
    zone_ttl: int = 96
    line_ttl: int = 288
    pattern_tol_atr: float = 0.25
    pattern_max_span: int = 60
    pattern_expiry: int = 24


class EventSet(dict[str, np.ndarray]):
    """name -> 1-D array (M5 length).  Names/dtypes are exactly the registry's
    (``schema.all_array_names``); flags are uint8 without NaN; ``valid_{tf}`` masks warm-up.

    Cache hits are LAZY: only the names are known up front, an array is opened with
    ``mmap_mode='r'`` on first access.

    Zone kind ``m15_range`` (formerly ``vwap_band``, a misnomer) is the M15 context range box
    (``context_range_low/high``).
    """

    def __init__(
        self,
        arrays: Mapping[str, np.ndarray | None],
        metadata: Mapping[str, Any],
        lazy_dir: Path | None = None,
    ):
        super().__init__(arrays)
        self.metadata = dict(metadata)
        self._lazy_dir = lazy_dir

    def __getitem__(self, name: str) -> np.ndarray:
        value = super().__getitem__(name)
        if value is None:
            value = np.load(Path(self._lazy_dir) / f"{name}.npy", mmap_mode="r")  # type: ignore[arg-type]
            super().__setitem__(name, value)
        return value

    def __iter__(self):
        return super().__iter__()  # not the dict fast path: copies go through __getitem__

    def get(self, name, default=None):
        return self[name] if name in self else default  # noqa: SIM401

    def items(self):
        return ((k, self[k]) for k in self)

    def values(self):
        return (self[k] for k in self)

    def is_loaded(self, name: str) -> bool:
        return super().__getitem__(name) is not None


def _expected_dtype(name: str) -> str:
    return schema.PREFIX_DTYPE[name.split("_", 1)[0]]


# ------------------------------------------------------------------------------------------
# per-timeframe computation
# ------------------------------------------------------------------------------------------


class _Collector:
    """Maps compact per-timeframe outputs to M5 arrays under their registry names."""

    def __init__(self, series: TfSeries, valid_c: np.ndarray):
        self.s = series
        self.valid_c = valid_c
        self.out: dict[str, np.ndarray] = {}

    def pulse(self, event: str, variant: str, main, *companions) -> None:
        d = schema.get(event)
        tf = self.s.tf
        names = schema.array_names(event, tf, variant)
        if len(companions) != len(names) - 1:
            raise AssertionError(f"{event}: {len(companions)} companions for {names}")
        mask = np.asarray(main, dtype=bool) & self.valid_c
        self.out[names[0]] = self.s.pulse_to_m5(mask)
        for name, prefix, arr in zip(names[1:], d.companions, companions, strict=True):
            if prefix == "evo":
                m5 = self.s.origin_to_m5_index(np.asarray(arr, dtype=np.int64))
                self.out[name] = self.s.values_to_m5(m5, mask).astype(np.int32)
            else:
                self.out[name] = self.s.values_to_m5(np.asarray(arr, dtype=float), mask)

    def state(self, event: str, variant: str, arr) -> None:
        (name,) = schema.array_names(event, self.s.tf, variant)
        self.out[name] = self.s.ffill_to_m5(np.asarray(arr, dtype=np.int8))

    def level(self, event: str, variant: str, arr, zid=None) -> None:
        names = schema.array_names(event, self.s.tf, variant)
        self.out[names[0]] = self.s.ffill_to_m5(np.asarray(arr, dtype=float))
        if zid is not None:
            self.out[names[1]] = self.s.ffill_to_m5(np.asarray(zid, dtype=np.int32))


def _valid_m5(series: TfSeries, valid_c: np.ndarray) -> np.ndarray:
    vis = series.vis
    ok = vis >= 0
    out = np.zeros(len(vis), dtype=bool)
    out[ok] = valid_c[vis[ok]]
    return out


def _compute_tf(features, tf: str, p: EventParams) -> tuple[dict[str, np.ndarray], TfSeries]:
    S = build_series(features, tf)
    n = S.n
    o, h, low, c = S.o, S.h, S.l, S.c
    if tf == "M5":
        atr = np.asarray(features["m5_atr14"], dtype=float)
        adx = slope = None
    else:
        atr, adx, slope = ks.trend_inputs(h, low, c)
    valid_c = np.isfinite(atr)
    col = _Collector(S, valid_c)
    if tf == "M5":
        start = _run_start(features["berlin_day_id"], np.asarray(features["contig"], dtype=bool))
    else:
        start = np.zeros(n, dtype=np.int64)
    start = start.astype(np.int64)
    day = np.asarray(features["berlin_day_id"], dtype=np.int64)[S.m5_start] if n else start
    pdl = np.asarray(features["previous_day_low"], dtype=float)[S.m5_start] if n else atr
    pdh = np.asarray(features["previous_day_high"], dtype=float)[S.m5_start] if n else atr

    # ---- structure: pivots, swing levels, BOS / CHOCH ----------------------------------
    ph, ph_o, pl, pl_o = kst.pivot_confirmations(h, low, p.swing_order)
    hi_lvl, hi_org, lo_lvl, lo_org = kst.swing_levels(ph, ph_o, pl, pl_o)
    col.pulse("SWING_LOW_CONF", "", np.isfinite(pl), pl, pl_o)
    col.pulse("SWING_HIGH_CONF", "", np.isfinite(ph), ph, ph_o)
    col.level("SWING_LOW_LVL", "", lo_lvl)
    col.level("SWING_HIGH_LVL", "", hi_lvl)
    if tf == "D1":
        _trend(col, tf, adx, slope, p)
        return _finish(col, features, S, valid_c), S

    bu, bd, cu, cd, lu, ld, ou, od, _ = kst.bos_choch(c, ph, ph_o, pl, pl_o)
    col.pulse("BOS_UP", "", bu, lu, ou)
    col.pulse("BOS_DN", "", bd, ld, od)
    col.pulse("CHOCH_UP", "", cu, lu, ou)
    col.pulse("CHOCH_DN", "", cd, ld, od)

    # ---- price: sweeps ------------------------------------------------------------------
    nan_org = np.full(n, -1, np.int64)
    sess_lo = kpr.session_prior_extreme(low, day, True)
    sess_hi = kpr.session_prior_extreme(h, day, False)
    sw_lo_lvl, sw_lo_org = kpr.shift_known_before(lo_lvl, lo_org)
    sw_hi_lvl, sw_hi_org = kpr.shift_known_before(hi_lvl, hi_org)
    for src in schema.SWEEP_LOW_SRCS:
        if src in ("prior20", "prior48"):
            lvl, org = kpr.prior_extreme(low, int(src[5:]), start, True)
        elif src == "pdl":
            lvl, org = pdl, nan_org
        elif src == "session_low":
            lvl, org = sess_lo, nan_org
        else:
            lvl, org = sw_lo_lvl, sw_lo_org
        col.pulse("SWEEP_LOW", src, *_sweep_low(low, c, lvl, org))
    for src in schema.SWEEP_HIGH_SRCS:
        if src in ("prior20", "prior48"):
            lvl, org = kpr.prior_extreme(h, int(src[5:]), start, False)
        elif src == "pdh":
            lvl, org = pdh, nan_org
        elif src == "session_high":
            lvl, org = sess_hi, nan_org
        else:
            lvl, org = sw_hi_lvl, sw_hi_org
        pulse, evl, evx, orig = _sweep_high(h, c, lvl, org)
        col.pulse("SWEEP_HIGH", src, pulse, evl, evx, orig)

    # ---- price: momentum resumption -----------------------------------------------------
    pu, lu_m = kpr.momentum_resume_up(
        o, h, low, c, atr, start, p.momentum_window, p.momentum_impulse_atr, p.momentum_depth_atr
    )
    mo, mh, ml, mc = kpr.mirror_bars(o, h, low, c)
    pd_, ld_m = kpr.momentum_resume_up(
        mo, mh, ml, mc, atr, start, p.momentum_window, p.momentum_impulse_atr, p.momentum_depth_atr
    )
    col.pulse("MOMENTUM_RESUME_UP", "", pu, lu_m)
    col.pulse("MOMENTUM_RESUME_DN", "", pd_, -ld_m)

    # ---- zones ---------------------------------------------------------------------------
    _zones(col, features, S, c, atr, ph, pl, pdl, pdh, p)

    # ---- trendlines ----------------------------------------------------------------------
    lv, sl, side, zid, born = kz.trendline_lines(ph, ph_o, pl, pl_o, p.line_ttl)
    col.level("TRENDLINE_VALUE", "", lv, zid)
    for variant in schema.get("TRENDLINE_TOUCH").variants():
        tol = float(schema.get("TRENDLINE_TOUCH").parse_variant(variant)["tol"])  # type: ignore[arg-type]
        touch, brk, et, eb = kz.trendline_events(h, low, c, atr, lv, sl, side, born, tol)
        col.pulse("TRENDLINE_TOUCH", variant, touch, et)
        col.pulse("TRENDLINE_BREAK", variant, brk, eb)

    # ---- patterns ------------------------------------------------------------------------
    tol = p.pattern_tol_atr
    col.pulse(
        "PATTERN_COMPLETE",
        "double_bottom",
        *kp.double_bottom(h, low, c, atr, pl, pl_o, tol, p.pattern_max_span, p.pattern_expiry),
    )
    r = kp.double_bottom(-low, -h, -c, atr, -ph, ph_o, tol, p.pattern_max_span, p.pattern_expiry)
    col.pulse("PATTERN_COMPLETE", "double_top", r[0], -r[1], -r[2], r[3])
    col.pulse("PATTERN_COMPLETE", "inside_bar_break_up", *kp.inside_bar_break_up(h, low, c, start))
    r = kp.inside_bar_break_up(-low, -h, -c, start)
    col.pulse("PATTERN_COMPLETE", "inside_bar_break_dn", r[0], -r[1], -r[2], r[3])

    # ---- states --------------------------------------------------------------------------
    if tf in ("M15", "H1"):
        _trend(col, tf, adx, slope, p)
    return _finish(col, features, S, valid_c), S


def _sweep_low(low, c, lvl, org):
    return kpr.sweep_low(low, c, lvl, org)


def _sweep_high(h, c, lvl, org):
    pulse, evl, evx, orig = kpr.sweep_low(-h, -c, -lvl, org)
    return pulse, -evl, -evx, orig


def _trend(col: _Collector, tf: str, adx, slope, p: EventParams) -> None:
    up, dn = ks.trend_states(slope, adx, p.adx_thr)
    col.state("TREND_UP", "", up)
    col.state("TREND_DN", "", dn)


def _shift1(a: np.ndarray, fill) -> np.ndarray:
    out = np.full(len(a), fill, dtype=a.dtype)
    if len(a) > 1:
        out[1:] = a[:-1]
    return out


def _zones(col: _Collector, features, S: TfSeries, c, atr, ph, pl, pdl, pdh, p: EventParams):
    n = S.n
    rlo_f = np.asarray(features["context_range_low"], dtype=float)
    rhi_f = np.asarray(features["context_range_high"], dtype=float)
    for kind in schema.ZONE_KINDS:
        if kind == "swing_cluster":
            lo, hi, zid, born = kz.cluster_zones(
                ph, pl, atr, p.cluster_tol_atr, p.cluster_pad_atr, p.cluster_max_pivots, p.zone_ttl
            )
            kb_lo, kb_hi = _shift1(lo, np.nan), _shift1(hi, np.nan)
            kb_zid, kb_born = _shift1(zid, 0), _shift1(born, -1)
        elif kind == "prior_range":
            lo, hi = pdl, pdh
            zid, born = kz.level_zone_ids(lo, hi)
            kb_lo, kb_hi, kb_zid, kb_born = lo, hi, zid, born
        else:  # m15_range: M15 context range box
            lo, hi = rlo_f[S.m5_end], rhi_f[S.m5_end]
            zid, born = kz.level_zone_ids(lo, hi)
            before = S.m5_start - 1
            ok = before >= 0
            kb_lo = np.where(ok, rlo_f[np.maximum(before, 0)], np.nan)
            kb_hi = np.where(ok, rhi_f[np.maximum(before, 0)], np.nan)
            kb_zid, kb_born = kz.level_zone_ids(kb_lo, kb_hi)
        if n == 0:
            enter = leave = np.zeros(0, np.uint8)
            org = np.zeros(0, np.int64)
        else:
            enter, leave, org = kz.zone_events(c, kb_lo, kb_hi, kb_zid, kb_born)
        col.pulse("ZONE_ENTER", kind, enter, org)
        col.pulse("ZONE_EXIT", kind, leave, org)
        col.level("ZONE_LO", kind, lo, zid)
        col.level("ZONE_HI", kind, hi, zid)


def _finish(col: _Collector, features, S: TfSeries, valid_c) -> dict[str, np.ndarray]:
    tf = S.tf
    out = col.out
    out[schema.valid_array_name(tf)] = _valid_m5(S, valid_c)
    sf = so_far_arrays(features, S)
    names = schema.sf_array_names(tf)
    out[names[0]] = sf["progress"]
    out[names[1]] = sf["partial_h"]
    out[names[2]] = sf["partial_l"]
    return out


# ------------------------------------------------------------------------------------------
# public API
# ------------------------------------------------------------------------------------------


def build_events(features, params: EventParams | None = None) -> EventSet:
    """Compute every precomputed registry array over ``features`` (a FeatureSet)."""
    p = params or EventParams()
    started = time.perf_counter()
    arrays: dict[str, np.ndarray] = {}
    for tf in schema.TIMEFRAMES:
        part, _ = _compute_tf(features, tf, p)
        arrays.update(part)
    day = np.asarray(features["berlin_day_id"], dtype=np.int64)
    contig = np.asarray(features["contig"], dtype=bool)
    start = _run_start(day, contig)
    arrays[schema.SF_RUN_BARS] = (np.arange(len(day)) - start + 1).astype(np.float64)
    expected = set(schema.all_array_names())
    if set(arrays) != expected:
        raise AssertionError(
            f"array set mismatch: missing={sorted(expected - set(arrays))[:5]} "
            f"extra={sorted(set(arrays) - expected)[:5]}"
        )
    n = len(day)
    for name, arr in arrays.items():
        dt = _expected_dtype(name)
        if arr.dtype != np.dtype(dt):
            arrays[name] = arr.astype(dt)
        if arrays[name].shape != (n,):
            raise AssertionError(f"unaligned {name}: {arrays[name].shape}")
    elapsed = time.perf_counter() - started
    return EventSet(
        arrays,
        {
            "event_set_version": schema.EVENT_SET_VERSION,
            "registry_fingerprint": schema.registry_fingerprint(),
            "params": asdict(p),
            "timing_s": elapsed,
            "cache_hit": False,
        },
    )


_CONSUMED = (
    "ts_ns",
    "o",
    "h",
    "l",
    "c",
    "m5_atr14",
    "previous_day_low",
    "previous_day_high",
    "context_range_low",
    "context_range_high",
    "berlin_day_id",
    "berlin_minute",
    "contig",
)


def _source_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(_PKG_DIR.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    tf_path = Path(_timeframe_module.__file__ or "")
    digest.update(tf_path.name.encode())
    digest.update(tf_path.read_bytes())
    return digest.hexdigest()


def cache_key(features, params: EventParams | None = None) -> str:
    """FeatureStore cache key (when known) + content hash of the consumed feature arrays +
    EVENT_SET_VERSION + registry fingerprint + params + source hash of alpha/events/*."""
    p = params or EventParams()
    data = hashlib.sha256()
    for name in _CONSUMED:
        arr = np.ascontiguousarray(features[name])
        data.update(name.encode())
        data.update(str(arr.dtype).encode())
        data.update(arr.tobytes())
    meta = getattr(features, "metadata", {}) or {}
    payload = {
        "feature_cache_key": meta.get("cache_key"),
        "feature_data": data.hexdigest(),
        "event_set_version": schema.EVENT_SET_VERSION,
        "registry": schema.registry_fingerprint(),
        "params": asdict(p),
        "source": _source_hash(),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def load_or_build_events(
    features, params: EventParams | None, cache_dir: str | Path
) -> EventSet:
    """Disk cache: one ``.npy`` per array; hits are loaded lazily with ``mmap_mode='r'``."""
    started = time.perf_counter()
    p = params or EventParams()
    key = cache_key(features, p)
    target = Path(cache_dir) / key
    manifest_path = target / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("key") == key:
            metadata = dict(manifest["metadata"])
            metadata.update(cache_hit=True, cache_key=key, timing_s=time.perf_counter() - started)
            return EventSet(dict.fromkeys(manifest["arrays"]), metadata, lazy_dir=target)
    built = build_events(features, p)
    tmp = Path(cache_dir) / f"{key}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    for name, arr in built.items():
        np.save(tmp / f"{name}.npy", arr, allow_pickle=False)
    metadata = dict(built.metadata)
    metadata["cache_key"] = key
    manifest = {
        "key": key,
        "metadata": metadata,
        "arrays": {name: str(arr.dtype) for name, arr in built.items()},
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    if target.exists():
        shutil.rmtree(target)
    tmp.rename(target)
    built.metadata = metadata
    return built


__all__ = (
    "EventParams",
    "EventSet",
    "build_events",
    "cache_key",
    "load_or_build_events",
)
