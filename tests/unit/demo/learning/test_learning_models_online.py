# ruff: noqa: E501
import numpy as np
import pytest
from learning_helpers import seed_store

from demo.learning.dataset import build_dataset
from demo.learning.features import FEATURE_NAMES, feature_vector
from demo.learning.models import BatchModel
from demo.learning.online import OnlineChallenger, OnlineLearningError
from demo.store import DemoStore


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    st = DemoStore(tmp_path_factory.mktemp("m") / "s.db")
    seed_store(st, 80, seed=7)
    return build_dataset(st)


def test_batch_models_train_and_are_reproducible(ds):
    a = BatchModel("lgbm_batch", seed=3).fit(ds)
    b = BatchModel("lgbm_batch", seed=3).fit(ds)
    x = feature_vector(dict(zip(FEATURE_NAMES, ds.X[5], strict=True)))
    pa, pb = a.predict_row(x), b.predict_row(x)
    assert pa == pb and set(pa) == {"target_before_stop", "net_positive", "expected_r"}
    assert all(v is not None for v in pa.values())
    lr = BatchModel("logreg_baseline", seed=3).fit(ds)
    assert set(lr.predict_row(x)) == {"target_before_stop", "net_positive"}
    qcol = FEATURE_NAMES.index("quality")
    hi, lo = int(np.argmax(ds.X[:, qcol])), int(np.argmin(ds.X[:, qcol]))
    assert (lr.predict_row(list(ds.X[hi]))["target_before_stop"]
            > lr.predict_row(list(ds.X[lo]))["target_before_stop"])


def test_single_class_or_tiny_data_gives_untrained_heads(ds):
    tiny = ds.subset(np.arange(4))
    m = BatchModel("lgbm_batch").fit(tiny)
    assert not m.trained
    assert set(m.predict_row(list(ds.X[0])).values()) == {None}
    one_class = ds.subset(np.flatnonzero(ds.y_target == 1.0)[:20])
    m2 = BatchModel("logreg_baseline").fit(one_class)
    assert m2.predict_row(list(ds.X[0]))["target_before_stop"] is None


def feats(q):
    f = {k: 0.0 for k in FEATURE_NAMES}
    f["quality"] = q
    return f


def test_river_requires_prior_prediction():
    on = OnlineChallenger(min_samples=5)
    with pytest.raises(OnlineLearningError, match="no stored prior prediction"):
        on.learn_one("opp-x", True, "2026-10-01T10:00:00+00:00")


def test_river_outcome_cannot_precede_prediction():
    on = OnlineChallenger(min_samples=5)
    on.predict_one("o1", feats(0.5), "2026-10-01T10:00:00+00:00")
    with pytest.raises(OnlineLearningError, match="before the prediction"):
        on.learn_one("o1", True, "2026-10-01T09:59:59+00:00")
    assert "o1" in on.pending and on.n_learned == 0  # nothing was learned


def test_river_double_learn_and_repredict_rejected():
    on = OnlineChallenger(min_samples=5)
    assert on.predict_one("o1", feats(0.5), "2026-10-01T10:00:00+00:00") is None  # untrained -> None
    on.learn_one("o1", False, "2026-10-01T11:00:00+00:00")
    with pytest.raises(OnlineLearningError, match="already learned"):
        on.learn_one("o1", False, "2026-10-01T11:00:00+00:00")
    with pytest.raises(OnlineLearningError, match="already learned"):
        on.predict_one("o1", feats(0.5), "2026-10-01T12:00:00+00:00")


def test_river_first_prediction_stands_and_learns_from_stored_features():
    on = OnlineChallenger(min_samples=5)
    on.predict_one("o1", feats(0.9), "2026-10-01T10:00:00+00:00")
    on.predict_one("o1", feats(0.1), "2026-10-01T10:00:01+00:00")  # idempotent, ignored
    assert on.pending["o1"].features["quality"] == 0.9


def test_river_learns_signal_streams_metrics_and_detects_drift():
    on = OnlineChallenger(min_samples=10, window=100)
    rng = np.random.default_rng(0)
    for i in range(400):
        q = float(rng.random())
        y = q > 0.5 if i < 250 else q < 0.5  # concept flips at i=250
        ts = f"2026-10-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00"
        on.predict_one(f"o{i}", feats(q), ts)
        on.learn_one(f"o{i}", y, ts)
    m = on.metrics()
    assert m["n_learned"] == 400 and m["n_pending"] == 0
    assert m["cumulative"]["n"] > 300 and 0 <= m["rolling"]["brier"] <= 1
    assert m["n_drift_events"] >= 1
    assert all(e["n_learned"] > 240 for e in on.drift_events)  # only after the flip


def test_river_early_learning_is_scored_only_when_prediction_was_made():
    on = OnlineChallenger(min_samples=5)
    for i in range(10):
        on.predict_one(f"o{i}", feats(i / 10), f"2026-10-01T{i:02d}:00:00+00:00")
        on.learn_one(f"o{i}", i % 2 == 0, f"2026-10-01T{i:02d}:30:00+00:00")
    assert on.metrics()["cumulative"]["n"] <= 5  # first 5 (untrained) predictions never scored


def test_register_prior_restores_pending_from_stored_prediction():
    on = OnlineChallenger(min_samples=5)
    on.register_prior("o9", feats(0.3), 0.42, "2026-10-01T10:00:00+00:00")
    on.learn_one("o9", True, "2026-10-01T11:00:00+00:00")
    assert on.n_learned == 1 and on.metrics()["cumulative"]["n"] == 1
