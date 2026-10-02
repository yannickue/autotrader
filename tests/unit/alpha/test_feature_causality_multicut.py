"""Independent causality check of EVERY FeatureStore feature (research workbench leakage guard).

For several truncation points (day boundary, mid-session, mid-hour, last bars) on frames that span the spring and the autumn
DST change, the features of the truncated frame must equal the prefix of the features of the full frame: a feature at bar t
that depends on data after t would change when the future bars are removed. Complements the single-cut test in
tests/test_alpha_fast_store.py.
"""

from __future__ import annotations

import numpy as np
import pytest

from alpha.fast.store import FeatureConfig, FeatureStore
from research_workbench.experiment import synthetic_bars


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    a, b = np.asarray(a), np.asarray(b)
    return np.array_equal(a, b, equal_nan=True) if a.dtype.kind == "f" else np.array_equal(a, b)


@pytest.mark.parametrize(("seed", "start"), [(11, "2024-03-27"), (29, "2024-10-23")])
def test_every_feature_is_causal_across_multiple_cuts_and_dst(seed: int, start: str) -> None:
    frame = synthetic_bars(seed=seed, days=6, start=start)
    n = len(frame)
    full = FeatureStore.build(frame, FeatureConfig())
    cuts = sorted(
        {168 * 2, 168 * 2 + 37, 168 * 3 - 1, 168 * 3 + 1, 168 * 4 + 84, n - 5, int(n * 0.37)}
    )
    assert len(full) > 100  # the whole feature set is covered
    offenders: dict[str, list[int]] = {}
    for cut in cuts:
        short = FeatureStore.build(frame.iloc[:cut].copy(), FeatureConfig())
        for name in full:
            if len(short[name]) != cut or not _same(full[name][:cut], short[name]):
                offenders.setdefault(name, []).append(cut)
    assert not offenders, f"features that depend on future bars: {offenders}"
