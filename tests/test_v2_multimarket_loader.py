"""V2 multi-market loader integrity, forward-holdout guard, contiguity."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha.common.dataset import load_research_dataset
from alpha.common.market_data import (
    DEV_END,
    ForwardHoldoutError,
    MarketDataError,
    assert_no_forward_holdout,
    contiguity_flags,
    dev_frame,
    gap_summary,
    load_dev_market_frame,
    load_market_dataset,
    load_market_frame,
)
from alpha.common.protocol import Partition, SplitPlan
from markets.spec import CANONICALS, load_market_spec
from research.runners.v2_market_frame import build_dev_frame, build_feature_store, dev_end_plan
from tests._v2mm_helpers import make_frame

DATA = Path(__file__).resolve().parents[1] / "data"
HAVE_V2 = (DATA / "markets" / "manifest_NAS100.json").is_file()
HAVE_AR1 = (DATA / "ar1_ger40" / "download_manifest.json").is_file()
SCHEMA = ["ts", "open", "high", "low", "close", "tick_volume", "spread_pts"]


# ------------------------------------------------------------------ forward-holdout guard
def test_frame_with_2026_09_01_bars_is_refused():
    df = make_frame("2026-08-28", "2026-09-03")
    assert (pd.DatetimeIndex(df["ts"]) >= pd.Timestamp("2026-09-01", tz="UTC")).any()
    with pytest.raises(ForwardHoldoutError):
        assert_no_forward_holdout(df)


def test_build_feature_store_refuses_forward_holdout_frame():
    df = make_frame("2026-08-28", "2026-09-03")
    with pytest.raises(ForwardHoldoutError):
        build_feature_store(df, load_market_spec("NAS100"))


def test_dev_frame_cuts_and_no_bar_after_dev_end():
    df = make_frame("2026-08-20", "2026-09-10")
    out = dev_frame(df, dev_end_plan())
    assert len(out) < len(df)
    berlin = pd.DatetimeIndex(out["ts"]).tz_convert("Europe/Berlin").normalize().tz_localize(None)
    assert berlin.max() <= pd.Timestamp(DEV_END)
    assert_no_forward_holdout(out)
    # the cut is exactly the Berlin date, not a UTC date: 2026-08-31 23:55 UTC is Berlin 09-01
    edge = df[(df.ts >= "2026-08-31 21:50") & (df.ts <= "2026-08-31 22:10")]
    assert len(edge) > 0
    assert out.ts.max() < pd.Timestamp("2026-08-31 22:00", tz="UTC")


def test_dev_frame_refuses_plan_reaching_into_holdout():
    df = make_frame("2026-08-20", "2026-09-10")
    bad = SplitPlan(train=Partition("train", "2025-01-01", "2026-06-30"),
                    validation=Partition("validation", "2026-07-01", "2026-09-15"),
                    oos=Partition("oos", "2026-09-16", "2026-12-31"))
    with pytest.raises(ForwardHoldoutError):
        dev_frame(df, bad)


def test_dev_frame_leaves_earlier_frame_unchanged():
    df = make_frame("2026-08-01", "2026-08-20")
    pd.testing.assert_frame_equal(dev_frame(df), df)


# ------------------------------------------------------------------ contiguity
def test_contiguity_flags_and_gap_summary():
    df = make_frame("2025-06-02", "2025-06-06")
    flags = contiguity_flags(df)
    assert flags.dtype == bool and not flags[-1]
    df2 = df.drop(index=[100, 101, 102, 103]).reset_index(drop=True)
    f2 = contiguity_flags(df2)
    assert (~f2[:-1]).sum() == (~flags[:-1]).sum() + 1
    g = gap_summary(df2, "Europe/Berlin")
    base = gap_summary(df, "Europe/Berlin")["n_intraday_gaps_gt3bars"]
    assert g["n_intraday_gaps_gt3bars"] == base + 1


# ------------------------------------------------------------------ loader on real data
@pytest.mark.skipif(not HAVE_V2, reason="data/markets not available")
@pytest.mark.parametrize("canonical", list(CANONICALS))
def test_v2_loader_schema_and_integrity(canonical):
    spec = load_market_spec(canonical)
    df = load_market_frame(spec, "M5", first="2025-06-02", last="2025-06-20", source="v2")
    assert list(df.columns) == SCHEMA
    assert str(df.ts.dt.tz) == "UTC"
    assert df.ts.is_monotonic_increasing and not df.ts.duplicated().any()
    assert df.ts.min() >= pd.Timestamp("2025-06-02", tz="UTC")
    assert df.ts.max() <= pd.Timestamp("2025-06-20", tz="UTC")
    assert (df.spread_pts >= 0).all() and (df.high >= df.low).all()
    assert len(df) > 500
    assert (pd.DatetimeIndex(df["ts"]).as_unit("s").asi8 % 300 == 0).all()


@pytest.mark.skipif(not HAVE_V2, reason="data/markets not available")
def test_v2_dev_loader_never_opens_holdout_months():
    spec = load_market_spec("NAS100")
    ds = load_market_dataset(spec, "M5", source="v2", max_month="2026-08")
    assert ds.frame.ts.max() < pd.Timestamp("2026-09-01", tz="UTC")
    assert all(m.month <= "2026-08" for m in ds.months)
    full = load_market_frame(spec, "M5", first="2026-08-25", source="v2")
    assert full.ts.max() >= pd.Timestamp("2026-09-01", tz="UTC")  # full loader does contain it
    with pytest.raises(ForwardHoldoutError):
        assert_no_forward_holdout(full)
    dev = load_dev_market_frame(spec, dev_end_plan())
    assert_no_forward_holdout(dev)


@pytest.mark.skipif(not HAVE_V2, reason="data/markets not available")
def test_v2_loader_rejects_manifest_symbol_mismatch(tmp_path):
    import dataclasses
    import json

    spec = load_market_spec("NAS100")
    (tmp_path / "markets").mkdir()
    src = json.loads((DATA / "markets" / "manifest_NAS100.json").read_text(encoding="utf-8"))
    (tmp_path / "markets" / "manifest_NAS100.json").write_text(json.dumps(src), encoding="utf-8")
    other = dataclasses.replace(spec, broker_symbol="Wrong")
    with pytest.raises(MarketDataError):
        load_market_dataset(other, "M5", source="v2", data_root=tmp_path)


def test_loader_source_validation():
    spec = load_market_spec("NAS100")
    with pytest.raises(MarketDataError):
        load_market_dataset(spec, "M5", source="bogus")
    with pytest.raises(MarketDataError):
        load_market_dataset(spec, "M5", source="ar1")  # ar1 = frozen GER40 set only
    with pytest.raises(MarketDataError):
        load_market_dataset(load_market_spec("GER40"), "M7", source="v2")


@pytest.mark.skipif(not (HAVE_V2 and HAVE_AR1), reason="data not available")
def test_ger40_sources_are_explicit_and_distinct():
    spec = load_market_spec("GER40")
    ar1 = load_market_frame(spec, "M5", source="ar1", max_month="2025-03")
    v2 = load_market_frame(spec, "M5", source="v2", max_month="2025-03")
    assert ar1.ts.min() == pd.Timestamp("2025-02-04 00:15", tz="UTC")
    assert v2.ts.min() == pd.Timestamp("2025-02-10 17:55", tz="UTC")
    # the overlap is the same broker data
    both = ar1.merge(v2, on="ts", suffixes=("_a", "_b"))
    assert len(both) > 1000
    for k in ("open", "high", "low", "close", "spread_pts"):
        assert np.array_equal(both[k + "_a"], both[k + "_b"]), k


@pytest.mark.skipif(not HAVE_AR1, reason="data/ar1_ger40 not available")
def test_ar1_source_equals_v1_loader_bit_identical():
    spec = load_market_spec("GER40")
    v1 = load_research_dataset(DATA / "ar1_ger40").frame
    new = load_market_frame(spec, "M5", source="ar1")
    pd.testing.assert_frame_equal(v1, new)
    dev = build_dev_frame(spec, "ar1")
    assert dev.ts.min() == v1.ts.min()
    assert len(dev) == len(v1)  # the frozen ar1 set already ends 2026-08-31
