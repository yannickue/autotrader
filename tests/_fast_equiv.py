"""Equivalence gate helper: fast generators must equal the golden reference candidates EXACTLY.

Reference: research/reference/ar2_ref12k/golden.pkl (candidate lists of the 22 semantic strategy
variants on dev_frame(...).iloc[46000:58000]). Requires the research dataset (data/ar1_ger40).
"""

from __future__ import annotations

import json
import pickle
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "ar1_ger40"
GOLDEN = ROOT / "research" / "reference" / "ar2_ref12k" / "golden.pkl"

requires_dataset = pytest.mark.skipif(
    not DATA.exists() or not GOLDEN.exists(),
    reason="research dataset / golden reference not present",
)


@lru_cache(maxsize=1)
def golden_setup():
    """(dataframe slice, Frame, FeatureSet, golden dict). Built once per test session."""
    sys.path.insert(0, str(ROOT / "research" / "runners"))
    import ar2_compare

    from alpha.common.dataset import load_research_dataset
    from alpha.common.frame import Frame
    from alpha.common.protocol import Partition, SplitPlan
    from alpha.fast.store import FeatureStore

    cfg = json.loads((ROOT / "research/configs/ar2_phase2.json").read_text(encoding="utf-8"))
    plan = SplitPlan(**{k: Partition(k, *v) for k, v in cfg["splits"].items()})
    dataset = load_research_dataset(DATA)
    df = ar2_compare.dev_frame(dataset.frame, plan).iloc[46000:58000].reset_index(drop=True)
    features = FeatureStore.build(df)
    with GOLDEN.open("rb") as handle:
        golden = pickle.load(handle)
    return df, Frame.from_dataframe(df), features, golden


def assert_generator_matches_golden(strategy_id: str, variants, generate) -> int:
    """Compare ``generate(features, params)`` with the reference candidates variant by variant.

    Returns the number of candidates compared. Floats must be BIT-identical (no tolerance)."""
    from alpha.fast.sim import CandidateArrays

    _, frame, features, golden = golden_setup()
    total = 0
    for index, params in enumerate(variants):
        reference = golden[f"{strategy_id}#{index}"]
        got = generate(features, params)
        want = CandidateArrays.from_signal_candidates(frame, reference)
        for name in ("decision_idx", "direction", "stop", "target", "target_r", "exit_kind"):
            np.testing.assert_array_equal(
                getattr(got, name), getattr(want, name), err_msg=f"{strategy_id}#{index}.{name}"
            )
        total += len(reference)
    return total
