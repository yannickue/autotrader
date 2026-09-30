"""Persistence gate: sealed, single-use, survival-stage output; spread-spike diagnostic sanity."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from alpha.discovery.folds import berlin_dates_from_ts_ns, make_folds
from alpha.rawscan import (
    GateAlreadyUsed,
    persistence_gate,
    preregister,
    scan_train,
    spread_spike_diagnostic,
)
from alpha.rawscan import gate as gate_mod
from alpha.rawscan.scan import prereg_hash
from tests.test_v2_rawscan_core import COSTS, POINT, TZ, WIN, make_frame


@pytest.fixture(scope="module")
def setup():
    frame = make_frame(400, seed=3, plant=0.16)
    dates = berlin_dates_from_ts_ns(pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8)
    folds = make_folds(dates, 2, 5, 12, initial_train_frac=0.4, min_test_days=20)
    train = frame.loc[folds[0].train_mask].reset_index(drop=True)
    res = scan_train(train, tz=TZ, point_size=POINT, win=WIN, costs=COSTS, run_nulls=False,
                     train_end=folds[0].train_end)
    return frame, folds, res


def test_gate_is_single_use_and_marked_survival_stage(setup, tmp_path):
    frame, folds, res = setup
    gate_mod._reset_for_tests()
    cells = preregister(res.table, 20)
    assert len(cells) == 20 and cells["t"].is_monotonic_decreasing
    out = tmp_path / "persistence_gate.json"
    doc = persistence_gate(cells, frame, folds, tz=TZ, point_size=POINT, win=WIN, costs=COSTS,
                           run_key="syn", out_path=out, prereg_sha256=prereg_hash(cells))
    assert doc["note"] == "survival stage, not for selection"
    assert json.loads(out.read_text())["note"] == doc["note"]
    assert doc["found"] == 20 and len(doc["cells"][0]["folds"]) == len(folds)
    # the planted cell (or a horizon neighbour) keeps its edge on the untouched test days
    assert any(c["survives"] for c in doc["cells"])
    with pytest.raises(GateAlreadyUsed):
        persistence_gate(cells, frame, folds, tz=TZ, point_size=POINT, win=WIN, costs=COSTS,
                         run_key="syn")


def test_scan_never_sees_test_days(setup):
    frame, folds, res = setup
    d = berlin_dates_from_ts_ns(pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8)
    assert res.grid.dates.max() <= np.datetime64(folds[0].train_end)
    assert res.grid.n_days < len(np.unique(d))


def test_spike_diagnostic_independent_series_has_ratio_near_one():
    fr = make_frame(150, seed=4)
    rng = np.random.default_rng(0)
    fr["spread_pts"] = rng.integers(1, 30, len(fr)).astype(float)
    r = spread_spike_diagnostic(fr, TZ, window=500)
    assert r["n_spike_bars"] > 100
    assert 0.6 < r["observed_over_expected_by_slot"] < 1.5
    assert abs(r["z_slot_stratified"]) < 4.0
