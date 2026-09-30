"""Session-conditioned (time-of-day normalised) features, Train-only tables (research only).

Problem: features such as ``bar_range_atr`` or ``mom_3_atr`` have a strongly time-of-day
dependent distribution, so a global Train quantile threshold is mostly a "is it the European
open" filter.  Design (simplest causal one): a per-bar array ``{name}_sq`` in [0, 1] = the
Train-quantile RANK of the value inside its session bucket (``SessionCalendar.buckets``).  A
catalog clause on ``{name}_sq`` with quantile q then reads "in the top (1-q) of what is normal
for THIS time of day"; the existing quantile-threshold / mirror machinery is reused unchanged
(the rule is a plain ``{name}_sq > threshold`` mask, no OR-of-windows rule needed).

Leakage-safe by construction:

* ``fit_tables`` reads ONLY ``features[name][train_mask]`` (bucket assignment depends on the
  local clock alone, never on values).  Embargo / Validation / OOS bars are never read.
* The table (sorted Train values per bucket) is frozen; ``apply`` is a per-bar pure function of
  the bar's own value + bucket, so Validation/OOS bars are normalised with the frozen Train table
  and the array is causal (bar i depends on bar i only) and truncation invariant.
* The table fingerprint is folded into the FeatureSet ``cache_key`` (evaluator result caches
  therefore never mix different Train partitions / calendars).

Rank definition: mid-rank ``(#train < x + #train <= x) / (2 n_b)`` for ordinary features.  For
breakout distances (``brk_*``; > 0 = fresh breakout) values are clipped at 0 and the strict rank
``#train' < x'`` is used, so every non-breakout bar gets rank exactly 0 and only true breakouts
can pass a high-quantile clause (the raw-domain floor 0.0 of the catalog is preserved).
Buckets with fewer than ``MIN_BUCKET_N`` finite Train values fall back to the pooled table.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from alpha.fast.store import FeatureSet
from alpha.session import (
    DEFAULT_CALENDAR,
    SQ_BASE_FEATURES,
    SessionCalendar,
    local_clock,
)

MIN_BUCKET_N = 50
ZERO_FLOOR_PREFIX = "brk_"


def is_zero_floor(name: str) -> bool:
    return name.startswith(ZERO_FLOOR_PREFIX)


def bucket_codes(features: Mapping[str, np.ndarray], calendar: SessionCalendar) -> np.ndarray:
    """Session bucket per bar from the local clock only (value independent)."""
    if calendar.tz == "Europe/Berlin":
        minute = np.asarray(features["berlin_minute"])
    else:
        minute, _ = local_clock(np.asarray(features["ts_ns"]), calendar)
    return calendar.bucket_codes(minute)


@dataclass(frozen=True)
class SessionQuantileTable:
    """Frozen Train-only per-(feature, bucket) sorted value tables."""

    calendar: SessionCalendar
    tables: dict[str, tuple[np.ndarray, ...]]  # feature -> per-bucket sorted finite Train values

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(repr(self.calendar).encode())
        for name in sorted(self.tables):
            digest.update(name.encode())
            for values in self.tables[name]:
                digest.update(np.ascontiguousarray(values, dtype=np.float64).tobytes())
                digest.update(b"|")
        return digest.hexdigest()

    def rank(self, name: str, values: np.ndarray, codes: np.ndarray) -> np.ndarray:
        """Session-quantile rank in [0, 1] of ``values`` (NaN stays NaN); pure per-bar."""
        values = np.asarray(values, dtype=float)
        out = np.full(len(values), np.nan)
        zero_floor = is_zero_floor(name)
        finite = np.isfinite(values)
        for code, table in enumerate(self.tables[name]):
            sel = finite & (codes == code)
            if not sel.any():
                continue
            x = np.maximum(values[sel], 0.0) if zero_floor else values[sel]
            if zero_floor:
                out[sel] = np.searchsorted(table, x, side="left") / len(table)
            else:
                lo = np.searchsorted(table, x, side="left")
                hi = np.searchsorted(table, x, side="right")
                out[sel] = (lo + hi) / (2.0 * len(table))
        return out


def fit_tables(
    features: Mapping[str, np.ndarray],
    train_mask: np.ndarray,
    calendar: SessionCalendar = DEFAULT_CALENDAR,
    names: Iterable[str] = SQ_BASE_FEATURES,
) -> SessionQuantileTable:
    """Build the frozen tables from TRAIN bars only (``features[name][train_mask]``)."""
    mask = np.asarray(train_mask, dtype=bool)
    codes = bucket_codes(features, calendar)[mask]
    n_buckets = len(calendar.bucket_names)
    tables: dict[str, tuple[np.ndarray, ...]] = {}
    for name in names:
        train = np.asarray(features[name])[mask].astype(float)
        keep = np.isfinite(train)
        train, tcodes = train[keep], codes[keep]
        if is_zero_floor(name):
            train = np.maximum(train, 0.0)
        pooled = np.sort(train)
        per_bucket = []
        for code in range(n_buckets):
            values = np.sort(train[tcodes == code])
            per_bucket.append(values if len(values) >= MIN_BUCKET_N else pooled)
        if not len(pooled):
            raise ValueError(f"no finite TRAIN values for {name}")
        tables[name] = tuple(per_bucket)
    return SessionQuantileTable(calendar, tables)


def add_session_quantile_features(
    features: FeatureSet,
    train_mask: np.ndarray,
    calendar: SessionCalendar = DEFAULT_CALENDAR,
    names: Iterable[str] = SQ_BASE_FEATURES,
) -> FeatureSet:
    """Return a copy of ``features`` with ``{name}_sq`` arrays added (opt-in).

    Call once per run with the TRAIN mask of the split plan (the same mask the
    ``ThresholdResolver`` uses).  The original arrays are shared, not modified.
    """
    names = tuple(n for n in names if n in features)
    table = fit_tables(features, train_mask, calendar, names)
    codes = bucket_codes(features, calendar)
    arrays: dict[str, np.ndarray] = dict(features)
    for name in names:
        arrays[f"{name}_sq"] = table.rank(name, features[name], codes)
    metadata: dict[str, Any] = dict(getattr(features, "metadata", {}))
    fingerprint = table.fingerprint()
    base_key = metadata.get("cache_key")
    metadata["base_cache_key"] = base_key
    metadata["cache_key"] = hashlib.sha256(f"{base_key}|sq|{fingerprint}".encode()).hexdigest()
    metadata["session_norm"] = {
        "fingerprint": fingerprint,
        "calendar": calendar.name,
        "features": list(names),
        "buckets": list(calendar.bucket_names),
    }
    return FeatureSet(arrays, metadata)


__all__ = (
    "MIN_BUCKET_N",
    "SessionQuantileTable",
    "add_session_quantile_features",
    "bucket_codes",
    "fit_tables",
)
