"""V2 multi-market frame builder: dev frame, FeatureStore and per-market sanity report.

Research only. For one market (``GER40|NAS100|SPX500|XAUUSD|EURUSD``) this builds

  (a) the DEV frame (bars after 2026-08-31 never enter; September 2026+ is the forward holdout),
  (b) the FeatureStore with the market's ``SessionCalendar`` and point size (GER40 keeps the V1
      default calendar so its arrays stay bit-identical to the V1 pipeline),
  (c) a small sanity report (bars/day, sessions, spread in PRICE units, ATR in points, share of
      bars in the entry window, first/last bar, gaps, provisional-calendar flag).

CLI: ``python research/runners/v2_market_frame.py [--out <report.json>]``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from alpha.common.frame import FrameParams, params_from_spec  # noqa: E402
from alpha.common.market_data import (  # noqa: E402
    DEV_END,
    assert_no_forward_holdout,
    dev_frame,
    gap_summary,
    load_market_dataset,
)
from alpha.common.protocol import Partition, SplitPlan  # noqa: E402
from alpha.fast.store import FeatureConfig, FeatureStore  # noqa: E402
from alpha.session import DEFAULT_CALENDAR, SessionCalendar  # noqa: E402
from markets.spec import MarketSpec, load_market_spec  # noqa: E402

MARKETS = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD")
DEFAULT_CACHE = REPO_ROOT / "data/feature_store/v2m"
DEFAULT_REPORT = REPO_ROOT / "research/reports/v2_markets/feature_build_report.json"


def dev_end_plan(first: str = "2025-01-01") -> SplitPlan:
    """Placeholder plan whose only job is the dev end (validation.end == 2026-08-31).

    The real train/validation split of a V2 market is decided by its research config; the OOS
    partition here is the forward holdout (2026-09-01 ..)."""
    return SplitPlan(
        train=Partition("train", first, "2026-06-30"),
        validation=Partition("validation", "2026-07-01", DEV_END),
        oos=Partition("oos", "2026-09-01", "2099-12-31"),
    )


def alpha_calendar(spec: MarketSpec) -> SessionCalendar:
    """`alpha.session.SessionCalendar` (the FeatureStore's calendar type) for a spec.

    GER40 (``verified_current_constants``) returns DEFAULT_CALENDAR after asserting that the spec
    matches it (so its features are bit-identical to V1). For the other markets the alpha buckets
    are the spec buckets clipped to [cash_open, entry_end); everything else is OVERNIGHT."""
    cal = spec.calendar
    if cal.status == "verified_current_constants":
        d = DEFAULT_CALENDAR
        if (cal.tz, cal.cash_open_min, cal.cash_close_min, cal.entry_start_min, cal.entry_end_min,
                cal.forced_flat_min) != (d.tz, d.cash_open_min, d.cash_close_min,
                                         d.entry_start_min, d.entry_end_min, d.flat_min):
            raise ValueError(f"{spec.canonical}: spec calendar differs from DEFAULT_CALENDAR")
        return DEFAULT_CALENDAR
    lo, hi = cal.cash_open_min, cal.entry_end_min
    buckets = tuple(
        (b.name, max(b.start_min, lo), min(b.end_min, hi))
        for b in cal.buckets
        if max(b.start_min, lo) < min(b.end_min, hi)
    )
    return SessionCalendar(
        name=spec.canonical, tz=cal.tz, cash_open_min=cal.cash_open_min,
        cash_close_min=cal.cash_close_min, entry_start_min=cal.entry_start_min,
        entry_end_min=cal.entry_end_min, flat_min=cal.forced_flat_min, buckets=buckets,
    )


def feature_config_for(spec: MarketSpec) -> FeatureConfig:
    return FeatureConfig(point_size=spec.point_size, session=alpha_calendar(spec))


def build_dev_frame(spec: MarketSpec, source: str = "v2", plan: SplitPlan | None = None,
                    data_root: str | Path | None = None) -> pd.DataFrame:
    ds = load_market_dataset(spec, "M5", source=source, data_root=data_root, max_month=DEV_END[:7])
    frame = dev_frame(ds.frame, plan or dev_end_plan())
    assert_no_forward_holdout(frame)
    return frame


def build_feature_store(dev: pd.DataFrame, spec: MarketSpec, cache_dir: str | Path | None = None):
    """FeatureStore for ``dev`` (refuses a frame with forward-holdout bars). ``cache_dir=None``
    builds in memory without writing a cache."""
    assert_no_forward_holdout(dev)
    cfg = feature_config_for(spec)
    if cache_dir is None:
        return FeatureStore.build(dev, cfg)
    return FeatureStore.load_or_build(dev, cfg, cache_dir)


def _q(a: np.ndarray, *qs: float) -> list[float]:
    return [round(float(x), 6) for x in np.quantile(a, qs)] if len(a) else []


def sanity_report(spec: MarketSpec, dev: pd.DataFrame, features=None, source: str = "v2") -> dict:
    p: FrameParams = params_from_spec(spec)
    cal = spec.calendar
    ts = pd.DatetimeIndex(dev["ts"])
    local = ts.tz_convert(cal.tz)
    minute = np.asarray(local.hour * 60 + local.minute)
    dates = local.normalize().tz_localize(None).to_numpy().astype("datetime64[D]")
    per_day = pd.Series(np.ones(len(dev)), index=dates).groupby(level=0).sum()
    in_entry = (minute >= cal.entry_start_min) & (minute < cal.entry_end_min)
    in_cash = (minute >= cal.cash_open_min) & (minute < cal.cash_close_min)
    cash_days = len(np.unique(dates[in_cash]))
    price_spread = dev["spread_pts"].to_numpy(float) * p.point
    entry_spread = price_spread[in_entry]
    over_cap = (
        float((entry_spread > spec.max_entry_spread_price).mean()) if len(entry_spread) else None
    )
    report = {
        "canonical": spec.canonical,
        "source": source,
        "calendar_status": cal.status,
        "calendar_provisional": cal.status == "provisional",
        "calendar_tz": cal.tz,
        "point_size": spec.point_size,
        "n_bars": len(dev),
        "first_bar_utc": ts[0].isoformat(),
        "last_bar_utc": ts[-1].isoformat(),
        "n_local_days": len(per_day),
        "n_cash_session_days": int(cash_days),
        "bars_per_day": {"min": int(per_day.min()), "median": float(per_day.median()),
                         "max": int(per_day.max())},
        "share_bars_in_entry_window": round(float(in_entry.mean()), 4),
        "share_bars_in_cash_session": round(float(in_cash.mean()), 4),
        "spread_price_units_all": {"p50_p95_p99_max": _q(price_spread, 0.5, 0.95, 0.99, 1.0)},
        "spread_price_units_entry_window": {
            "p50_p95_p99_max": _q(entry_spread, 0.5, 0.95, 0.99, 1.0)
        },
        "max_entry_spread_price_cap": spec.max_entry_spread_price,
        "share_entry_window_bars_over_cap": None if over_cap is None else round(over_cap, 4),
        "gaps": gap_summary(dev, cal.tz),
        "dev_end": DEV_END,
        "no_bar_after_dev_end": True,  # assert_no_forward_holdout ran in build_dev_frame
    }
    if features is not None:
        atr = np.asarray(features["m5_atr14"], dtype=float)
        atr_pts = atr[np.isfinite(atr)] / spec.point_size
        report["atr14_points"] = {"p50_p95": _q(atr_pts, 0.5, 0.95)}
        report["n_feature_arrays"] = len(features)
        report["session_calendar"] = {
            "buckets": [list(b) for b in alpha_calendar(spec).buckets],
            "flat_min": alpha_calendar(spec).flat_min,
        }
        report["local_minute_note"] = (
            "FeatureStore arrays 'berlin_minute'/'berlin_day_id' and the M15/H1/prior-day views "
            "stay "
            "Europe/Berlin for every market; session_* arrays use the spec calendar tz."
        )
    return report


def features_digest(features, n: int | None = None) -> dict[str, str]:
    """sha256 per feature array (first ``n`` rows) for bit-identity comparisons."""
    out = {}
    for name in sorted(features):
        a = np.ascontiguousarray(np.asarray(features[name]))
        out[name] = hashlib.sha256((a if n is None else a[:n]).tobytes()).hexdigest()
    return out


def run_market(canonical: str, source: str = "v2", cache_dir: str | Path | None = None) -> dict:
    t0 = time.perf_counter()
    spec = load_market_spec(canonical)
    dev = build_dev_frame(spec, source)
    features = build_feature_store(dev, spec, cache_dir)
    rep = sanity_report(spec, dev, features, source)
    rep["build_seconds"] = round(time.perf_counter() - t0, 1)
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--markets", nargs="*", default=list(MARKETS))
    ap.add_argument("--ger40-source", choices=("v2", "ar1"), default="v2")
    ap.add_argument("--cache-dir", default=None, help="omit: build in memory, no cache")
    ap.add_argument("--out", default=str(DEFAULT_REPORT))
    args = ap.parse_args(argv)
    reports = []
    for m in args.markets:
        src = args.ger40_source if m == "GER40" else "v2"
        reports.append(run_market(m, src, args.cache_dir))
        print(f"[v2-frame] {m} done", flush=True)
    doc = {
        "dev_end": DEV_END,
        "forward_holdout_start": "2026-09-01",
        "provisional_calendars": [r["canonical"] for r in reports if r["calendar_provisional"]],
        "markets": reports,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
