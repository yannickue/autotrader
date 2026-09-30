# ruff: noqa: E501
"""Shared fixtures for the event tests (real dev data slices, comparison helpers)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from alpha.fast.store import FeatureStore

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def dev_frame() -> pd.DataFrame:
    """Full AD1 dev frame (OOS bars physically removed), as research/runners/ad1_discovery.py."""
    from alpha.common.dataset import load_research_dataset
    from research.runners import ar2_fast

    cfg = json.loads((ROOT / "research/configs/ad1_discovery.json").read_text(encoding="utf-8"))
    plan = ar2_fast._plan(cfg)
    ds = load_research_dataset(ROOT / cfg["dataset_root"])
    return ar2_fast.dev_frame(ds.frame, plan)


def slice_frame(start: str, end: str) -> pd.DataFrame:
    """Bars whose Berlin date is in [start, end]."""
    frame = dev_frame()
    local = pd.DatetimeIndex(frame["ts"]).tz_convert("Europe/Berlin").normalize().tz_localize(None)
    mask = (local >= pd.Timestamp(start)) & (local <= pd.Timestamp(end))
    return frame.loc[mask].reset_index(drop=True)


def build_features(frame: pd.DataFrame):
    with redirect_stdout(io.StringIO()):
        return FeatureStore.build(frame.reset_index(drop=True))


def same_array(a: np.ndarray, b: np.ndarray) -> bool:
    """Bit-identical (NaN-aware) comparison including dtype."""
    if a.dtype != b.dtype or a.shape != b.shape:
        return False
    if a.dtype.kind == "f":
        return bool(np.array_equal(a, b, equal_nan=True))
    return bool(np.array_equal(a, b))


def diff_names(left, right, upto: int) -> list[str]:
    """Names whose first ``upto`` elements differ between two EventSets."""
    return [n for n in left if not same_array(np.asarray(left[n][:upto]), np.asarray(right[n][:upto]))]
