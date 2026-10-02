# ruff: noqa: E501
"""Artifact adapters of the workbench stages (OFFLINE ONLY): FeatureSet -> compact market arrays, (de)serialisation.

Everything that PRODUCES or SERIALISES a stage artifact lives here, so its import-closure hash
(``dag.ADAPTER_CODE``) can be part of the SIGNALS / SIMULATION / METRICS keys: a change of adapter behaviour can never
produce a stale upstream cache HIT.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from alpha.fast.sim import CandidateArrays, MarketArrays, TradeArrays

from .experiment import berlin_dates


def npz_bytes(arrays: dict[str, np.ndarray]) -> bytes:
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    return buf.getvalue()


def load_npz(path: Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=False) as stored:
        return {name: stored[name] for name in stored.files}


def json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, indent=1, default=str, sort_keys=True).encode("utf-8")


def market_from_features(features: Any) -> dict[str, np.ndarray]:
    """Compact market arrays (GER40/Berlin basis, as ``simulate_fast`` defaults) from a FeatureSet."""
    contig = np.asarray(features["contig"], dtype=bool)
    contig_next = np.zeros(len(contig), dtype=bool)
    contig_next[:-1] = contig[1:]
    ts_ns = np.asarray(features["ts_ns"], dtype=np.int64)
    date_days = berlin_dates(pd.DatetimeIndex(ts_ns, tz="UTC")).astype(np.int64)
    return {
        "o": np.asarray(features["o"], dtype=np.float64),
        "h": np.asarray(features["h"], dtype=np.float64),
        "l": np.asarray(features["l"], dtype=np.float64),
        "c": np.asarray(features["c"], dtype=np.float64),
        "spread": np.asarray(features["spread"], dtype=np.float64),
        "minute": np.asarray(features["berlin_minute"], dtype=np.int64),
        "day": np.asarray(features["berlin_day_id"], dtype=np.int64),
        "contig_next": contig_next,
        "ts_ns": ts_ns,
        "date_days": date_days,
        "atr": np.asarray(features["m5_atr14"], dtype=np.float64),
    }


def market_arrays(arrays: dict[str, np.ndarray]) -> MarketArrays:
    return MarketArrays(
        arrays["o"], arrays["h"], arrays["l"], arrays["c"], arrays["spread"],
        arrays["minute"], arrays["day"], arrays["contig_next"],
    )  # fmt: skip


def candidates_from(arrays: dict[str, np.ndarray]) -> CandidateArrays:
    return CandidateArrays(
        arrays["decision_idx"], arrays["direction"], arrays["stop"], arrays["target"],
        arrays["target_r"], arrays["exit_kind"],
    )  # fmt: skip


_CAND_FIELDS = ("decision_idx", "direction", "stop", "target", "target_r", "exit_kind")


def _trade_fields() -> tuple[str, ...]:
    return tuple(TradeArrays.__dataclass_fields__)


def trades_from(arrays: dict[str, np.ndarray]) -> TradeArrays:
    return TradeArrays(**{name: arrays[name] for name in _trade_fields()})
