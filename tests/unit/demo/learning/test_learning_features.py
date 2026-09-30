# ruff: noqa: E501
import dataclasses
from datetime import timedelta

import numpy as np
import pytest
from demo_factories import T0, make_decision, make_label, make_outcome, make_snapshot
from learning_helpers import seed_store

from demo.learning.dataset import build_dataset
from demo.learning.features import (
    ALLOWED_SNAPSHOT_FIELDS,
    FEATURE_NAMES,
    FEATURE_VERSION,
    FORBIDDEN_TOKENS,
    LeakageGuardError,
    assert_no_forbidden_tokens,
    extract_features,
    snapshot_view,
)
from demo.store import DemoStore


def test_whitelist_and_feature_names_are_clean():
    assert_no_forbidden_tokens(ALLOWED_SNAPSHOT_FIELDS)
    assert_no_forbidden_tokens(FEATURE_NAMES)
    assert FEATURE_VERSION.startswith("demo-features-")


def test_forbidden_token_detected():
    for bad in ("net_r_x", "mfe_r", "hypothetical_r", "outcome_flag", "decision_accepted"):
        with pytest.raises(LeakageGuardError):
            assert_no_forbidden_tokens([bad])
    assert "mfe" in FORBIDDEN_TOKENS


def test_feature_values_and_order():
    f = extract_features(make_snapshot(risk=1.5, spread=0.1, target_r=2.0))
    assert tuple(f) == FEATURE_NAMES
    assert f["risk_atr"] == pytest.approx(1.5 / 2.0)
    assert f["rr_plan"] == pytest.approx(2.0)
    assert f["spread_risk"] == pytest.approx(0.1 / 1.5)
    assert f["family_ORB"] == 1.0 and f["family_GAP"] == 0.0 and f["family_other"] == 0.0
    assert f["session_other"] == 1.0  # "open" is not a known bucket
    assert f["direction"] == 1.0 and f["confluence"] == 2.0 and f["quality"] == 0.5
    assert np.isnan(f["realized_vol_atr"])


def test_quality_components_dict():
    s = make_snapshot(signal={"family": "GAP", "confluence": 1, "quality": {"a": 0.2, "b": 0.6, "c": "x"}})
    f = extract_features(s)
    assert f["quality"] == pytest.approx(0.4) and f["quality_min"] == 0.2 and f["quality_max"] == 0.6


def test_rejects_anything_but_snapshot():
    snap = make_snapshot()
    for bad in (make_decision(snap), make_outcome(), make_label(snap), snap.to_dict(), None):
        with pytest.raises(LeakageGuardError):
            extract_features(bad)


def test_unknown_outcome_like_keys_never_enter_features():
    base = make_snapshot()
    poisoned = dataclasses.replace(
        base,
        signal={**base.signal, "net_r": 9.9, "outcome": "TARGET", "label": 1, "mfe_r": 3.0},
        structure={**base.structure, "hypothetical_r": 4.0, "sweep": True},
        context={**base.context, "H1": {"t": 1, "trend": 1, "pnl": 100}},
    )
    clean = dataclasses.replace(base, structure={**base.structure, "sweep": True},
                                context={**base.context, "H1": {"t": 1, "trend": 1}})
    assert extract_features(poisoned) == extract_features(clean)
    assert set(snapshot_view(poisoned)) == set(ALLOWED_SNAPSHOT_FIELDS)


def test_features_independent_of_outcome_labels(tmp_path):
    a = DemoStore(tmp_path / "a.db")
    b = DemoStore(tmp_path / "b.db")
    seed_store(a, 12, seed=1, learnable=True)
    seed_store(b, 12, seed=1, learnable=False)  # same snapshots, different outcomes
    da, db = build_dataset(a), build_dataset(b)
    assert da.opportunity_ids == db.opportunity_ids
    assert np.array_equal(da.X, db.X, equal_nan=True)
    assert not np.array_equal(da.y_target, db.y_target)
    assert da.feature_names == FEATURE_NAMES


def test_dataset_counterfactual_switch_indicator_and_weights(tmp_path):
    st = DemoStore(tmp_path / "s.db")
    seed_store(st, 10, n_rejected=6, seed=2)
    real = build_dataset(st)
    both = build_dataset(st, include_counterfactual=True, cf_weight=0.25)
    assert len(real) == 10 and not real.is_cf.any()
    assert len(both) == 16 and both.is_cf.sum() == 6
    assert set(both.weight[both.is_cf]) == {0.25} and set(both.weight[~both.is_cf]) == {1.0}
    assert np.all(np.diff(both.ts) >= 0)  # chronological
    assert np.all(both.label_ts >= both.ts)
    assert set(np.unique(both.y_target)) <= {0.0, 1.0}
    assert both.subset(np.array([0, 1])).X.shape == (2, len(FEATURE_NAMES))
    assert both.window()["n"] == 16


def test_dataset_as_of_drops_future_labels(tmp_path):
    st = DemoStore(tmp_path / "s.db")
    seed_store(st, 10, seed=3)
    full = build_dataset(st)
    cut = build_dataset(st, as_of_utc=(T0 + timedelta(hours=6 * 4 + 2)).isoformat())
    assert 0 < len(cut) < len(full)
    assert cut.label_ts.max() <= full.label_ts[len(cut) - 1] + 1
