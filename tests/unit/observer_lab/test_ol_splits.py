# ruff: noqa: E501
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alpha.common.market_data import DEV_END, FORWARD_HOLDOUT_START, ForwardHoldoutError
from coverage_analysis.observer_lab import splits as SP

H = 3600 * 1_000_000_000


def ts(s: str) -> int:
    return pd.Timestamp(s, tz="Europe/Berlin").value


@pytest.fixture(scope="module")
def days():
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", "2026-09-15")]


def test_plan_respects_repo_freeze_constants(days):
    from demo.opportunity.production_spec import DEFAULT_FIT_END

    plan = SP.build_plan(days)
    assert plan.core_fit_end == DEFAULT_FIT_END == "2026-06-30"
    assert plan.oos_start == "2026-07-01" and plan.oos_end == DEV_END == "2026-08-31"
    assert plan.forward_start == FORWARD_HOLDOUT_START == "2026-09-01"
    assert plan.train_end < plan.validation_start <= plan.validation_end == plan.core_fit_end
    # validation = the LAST ~25% of the distinct trading days up to the core fit end; chronological, no gaps
    fit_days = [d for d in days if d <= plan.core_fit_end]
    assert len([d for d in fit_days if d >= plan.validation_start]) == pytest.approx(0.25 * len(fit_days), abs=1)
    assert plan.train_start == days[0]


def test_assign_partitions_purge_and_embargo(days):
    plan = SP.build_plan(days)
    dec = np.array([
        ts("2026-03-02 10:00"), ts("2026-06-30 23:30"), ts("2026-07-01 02:00"), ts("2026-07-01 09:00"), ts("2026-09-01 01:00"),
        ts("2026-09-02 10:00"),
    ])
    hor = dec + 4 * H
    out = SP.assign_partitions(dec, hor, plan, embargo_s=4 * 3600)
    assert list(out) == ["TRAIN", "PURGED", "EMBARGO", "OOS", "EMBARGO", "FORWARD"]
    # default embargo = the longest label horizon of the data
    assert list(SP.assign_partitions(dec, hor, plan)) == list(out)
    # validation events well inside their partition stay VALIDATION
    v = SP.assign_partitions(np.array([ts("2026-06-20 10:00")]), np.array([ts("2026-06-20 12:00")]), plan, embargo_s=4 * 3600)
    assert list(v) == ["VALIDATION"]


def test_guards_refuse_forward_period_for_fitting():
    SP.guard_dev_only(np.array([ts("2026-08-31 23:00")]))
    with pytest.raises(ForwardHoldoutError):
        SP.guard_dev_only(np.array([ts("2026-08-31 10:00"), ts("2026-09-01 00:30")]))
    SP.assert_partition_use(["TRAIN"], "fit")
    SP.assert_partition_use(["TRAIN", "VALIDATION"], "select")
    SP.assert_partition_use(["OOS"], "final_oos")
    SP.assert_partition_use(["FORWARD"], "observe_forward")
    for parts, purpose in ((["TRAIN", "FORWARD"], "fit"), (["FORWARD"], "select"), (["OOS"], "fit"), (["VALIDATION"], "fit"), (["TRAIN"], "final_oos")):
        with pytest.raises((ForwardHoldoutError, ValueError)):
            SP.assert_partition_use(parts, purpose)
    with pytest.raises(ForwardHoldoutError):
        SP.assert_partition_use(["FORWARD"], "fit")
    assert SP.FORWARD_NEVER_USED_FOR_FITTING is True


@pytest.mark.parametrize("mode", ["rolling", "expanding"])
def test_walk_forward_windows_counts_ordering_and_embargo(mode):
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=100)]
    wins = SP.walk_forward_windows(days, train_days=40, test_days=10, embargo_days=2, mode=mode)
    assert len(wins) == 5  # last start s=40: 40 + 40 + 2 + 10 <= 100
    for k, w in enumerate(wins):
        assert w.index == k
        assert w.train_end < w.test_start and w.train_start <= w.train_end
        gap = days.index(w.test_start) - days.index(w.train_end) - 1
        assert gap == 2  # embargo in trading days
        assert days.index(w.test_end) - days.index(w.test_start) + 1 == 10
        if k:
            assert w.test_start > wins[k - 1].test_end and wins[k].test_start == days[days.index(wins[k - 1].test_start) + 10]
            if mode == "rolling":
                assert days.index(w.train_end) - days.index(w.train_start) + 1 == 40
            else:
                assert w.train_start == days[0] and days.index(w.train_end) > days.index(wins[k - 1].train_end)
    with pytest.raises(ValueError):
        SP.walk_forward_windows(days, train_days=40, test_days=10, embargo_days=2, mode="shuffled")
    assert SP.walk_forward_windows(days[:40], train_days=40, test_days=10, embargo_days=2, mode=mode) == []


def test_walk_forward_refuses_forward_period_days():
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-07-01", "2026-09-10")]
    with pytest.raises(ForwardHoldoutError):
        SP.walk_forward_windows(days, train_days=20, test_days=5, embargo_days=1)


def test_window_masks_purge_events_whose_horizon_reaches_the_test_window():
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=100)]
    w = SP.walk_forward_windows(days, train_days=40, test_days=10, embargo_days=0, mode="rolling")[0]
    last_train = w.train_end
    dec = np.array([ts(f"{days[0]} 10:00"), ts(f"{last_train} 10:00"), ts(f"{last_train} 20:00"), ts(f"{w.test_start} 10:00"), ts(f"{days[99]} 10:00")])
    hor = dec + 4 * H
    hor[2] = ts(f"{w.test_start} 02:00")  # decided on the last train day, horizon reaches into the test day
    tr, te = SP.window_masks(dec, hor, w)
    assert list(tr) == [True, True, False, False, False]  # event 2 purged; later days in neither
    assert list(te) == [False, False, False, True, False]
    assert not (tr & te).any()
    # nothing in train is later than anything in test (ordering), and the masks never shuffle (boolean masks over given order)
    assert dec[tr].max() < dec[te].min()
