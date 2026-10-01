# ruff: noqa: E501
"""Contract tests of the observer schema: feature/label separation, causality guard, prefix helper, naming, versioning."""

from __future__ import annotations

import numpy as np
import pytest

from market_observer import schema as S


def _bars(n: int = 12) -> S.ObserverBars:
    ts = (np.arange(n, dtype=np.int64) * 300 + 1_700_000_000) * 1_000_000_000
    c = np.linspace(100.0, 101.0, n)
    return S.ObserverBars(
        "T", ts, c - 0.1, c + 0.2, c - 0.2, c, np.full(n, 50.0), np.full(n, 0.02), np.full(n, 0.5), np.zeros(n, dtype=np.int64),
        (np.arange(n, dtype=np.int64) * 5) % 1440, np.zeros(n, dtype=np.int64), 0.01, S.SessionSpec("UTC", 480, 1020),
    )


def test_bars_validate_and_prefix_is_a_pure_truncation():
    b = _bars()
    b.validate()
    p = b.prefix(5)
    assert len(p) == 5 and p.market == "T" and np.array_equal(p.c, b.c[:5]) and p.session == b.session
    p.validate()
    assert b.decision_ts_ns(4) == int(b.ts_ns[4]) + 300 * 1_000_000_000


def test_bars_reject_length_mismatch_and_non_ascending_time():
    b = _bars()
    bad = S.ObserverBars(b.market, b.ts_ns, b.o, b.h, b.l, b.c[:-1], b.tick_volume, b.spread, b.atr, b.segment_id, b.local_minute,
                         b.local_day, b.tick_size, b.session)
    with pytest.raises(ValueError):
        bad.validate()
    ts = b.ts_ns.copy()
    ts[3] = ts[2]
    with pytest.raises(ValueError):
        S.ObserverBars(b.market, ts, b.o, b.h, b.l, b.c, b.tick_volume, b.spread, b.atr, b.segment_id, b.local_minute, b.local_day,
                       b.tick_size, b.session).validate()


def test_feature_and_label_columns_are_disjoint_by_construction():
    feats = {S.feature_key("levels", "touch_count"), S.feature_key("balance", "range_width_atr")}
    labels = set(S.all_label_columns())
    S.assert_disjoint(feats, labels)
    assert all(c.startswith("f_") for c in feats) and all(c.startswith("y_") for c in labels)
    with pytest.raises(ValueError):
        S.assert_disjoint({"y_mfe_r"}, labels)
    with pytest.raises(ValueError):
        S.assert_disjoint(feats, {"f_levels__touch_count"})


def test_a_label_can_never_be_a_decision_feature():
    with pytest.raises(ValueError):
        S.DecisionFeatures(1, {"y_mfe_r": 1.0}, {})
    with pytest.raises(ValueError):
        S.PostEventLabels(1, {"f_levels__touch_count": 1})


def test_future_timestamp_feature_raises_causality_error():
    ok = S.DecisionFeatures(1000, {"f_levels__last_touch_ts_ns": 1000, "f_levels__x": 3.0}, {})
    assert ok.columns["f_levels__last_touch_ts_ns"] == 1000
    with pytest.raises(S.CausalityError):
        S.DecisionFeatures(1000, {"f_levels__last_touch_ts_ns": 1001}, {})
    S.DecisionFeatures(1000, {"f_levels__last_touch_ts_ns": None}, {})  # unknown stays None, never back-filled


def test_from_results_builds_prefixed_columns_and_versions_and_rejects_duplicates():
    r1 = S.FeatureResult("levels", S.GROUP_VERSIONS["levels"], {"touch_count": 2, "age_bars": 10})
    r2 = S.FeatureResult("balance", S.GROUP_VERSIONS["balance"], {"range_width_atr": 1.5})
    f = S.DecisionFeatures.from_results(5, [r1, r2])
    assert f.columns == {"f_levels__touch_count": 2, "f_levels__age_bars": 10, "f_balance__range_width_atr": 1.5}
    assert f.versions == {"levels": "mso-levels-1", "balance": "mso-balance-1"}
    with pytest.raises(ValueError):
        S.DecisionFeatures.from_results(5, [r1, r1])
    with pytest.raises(ValueError):
        S.feature_key("nope", "x")
    with pytest.raises(ValueError):
        S.feature_key("levels", "bad__name")


def test_first_passage_label_names_and_serialisation_row():
    assert S.first_passage_label_name(0.5, 0.5) == "fav050_before_adv050"
    assert S.first_passage_label_name(1.0, 0.5) == "fav100_before_adv050"
    feats = S.DecisionFeatures.from_results(7, [S.FeatureResult("swings", S.GROUP_VERSIONS["swings"], {"sequence_length": 3})])
    lab = S.PostEventLabels(99, {"y_mfe_r": 0.8, "y_fav050_before_adv050": 1})
    rec = S.ObserverRecord("e1", "T", "ROUND", "reject", -1, False, None, feats, lab)
    row = rec.to_row()
    assert row["observer_version"] == "market-structure-observer-v1" and row["status"].startswith("OBSERVATION_ONLY")
    assert row["f_swings__sequence_length"] == 3 and row["y_mfe_r"] == 0.8 and row["v_swings"] == "mso-swings-1"
    live = S.ObserverRecord("e1", "T", "ROUND", "reject", -1, False, None, feats).to_row()
    assert "horizon_end_ts_ns" not in live and not any(k.startswith("y_") for k in live)  # live path carries NO labels


def test_versions_are_declared_for_every_group_and_the_observer():
    assert S.OBSERVER_VERSION == "market-structure-observer-v1"
    assert set(S.GROUP_VERSIONS) == {"levels", "swings", "acceptance", "participation", "balance", "fib"}
    assert all(v.startswith("mso-") and v[-1].isdigit() for v in S.GROUP_VERSIONS.values())
