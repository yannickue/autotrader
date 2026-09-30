"""V2 multi-market bar loader, dev-frame cut (forward-holdout guard), cross-market alignment.

Research only. Reads the monthly Parquet files produced by ``scripts/v2_download_market.py``
(``data/markets/manifest_<CANONICAL>.json``, ``entries[]`` with ``tf`` and a data-root relative
``path``) or, for GER40, the frozen V1 set ``data/ar1_ger40`` (``download_manifest.json``), and
normalises them to the SAME frame schema the V1 pipeline consumes (``ts`` UTC bar-OPEN,
``open, high, low, close, tick_volume, spread_pts``; BID OHLC, spread in recorded points).

Two GER40 sources exist and are selected EXPLICITLY (``source='ar1'|'v2'``): the older frozen
``ar1`` series starts 2025-02-04, the re-download ``v2`` starts 2025-02-10 (terminal serves at most
100000 bars per timeframe). They are never merged.

Forward holdout: September 2026 and later is the future clean forward holdout. Nothing after
``DEV_END`` (2026-08-31, Berlin date, as in ``research.runners.ar2_fast.dev_frame``) may enter a
development frame; ``dev_frame`` cuts and ``assert_no_forward_holdout`` refuses.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from alpha.common.dataset import BERLIN, ResearchDataset, load_research_dataset
from markets.spec import MarketSpec

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_ROOT = REPO_ROOT / "data"
V2_ROOT_NAME = "markets"
AR1_ROOT_NAME = "ar1_ger40"
DEV_END = "2026-08-31"  # inclusive Berlin date: last development day
FORWARD_HOLDOUT_START = "2026-09-01"
TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "H4": 14400, "D1": 86400}
EXPECTED_SERVER_ZONE = "Europe/Berlin"  # timestamps stored are already true UTC


class ForwardHoldoutError(RuntimeError):
    """A frame contains bars from the reserved forward-holdout period."""


class MarketDataError(RuntimeError):
    """Manifest/spec mismatch or integrity problem in a market dataset."""


# ------------------------------------------------------------------ loading
def _utc(value: str | pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None:
        return None
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _month_bounds(entry: dict) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    if "first" in entry and "last" in entry:
        return pd.Timestamp(entry["first"]), pd.Timestamp(entry["last"])
    return None


def load_market_dataset(
    spec: MarketSpec,
    timeframe: str = "M5",
    *,
    source: str = "v2",
    data_root: str | Path | None = None,
    first: str | pd.Timestamp | None = None,
    last: str | pd.Timestamp | None = None,
    max_month: str | None = None,
) -> ResearchDataset:
    """Admitted months of one market as a ``ResearchDataset`` (frame + per-month provenance).

    ``first``/``last`` (UTC, bar-OPEN, inclusive) prune months by manifest bounds AND trim rows.
    ``max_month`` ('YYYY-MM') skips later months without reading them (used by the dev loader so
    forward-holdout months are never even opened).
    """
    if timeframe not in TIMEFRAME_SECONDS:
        raise MarketDataError(f"unsupported timeframe {timeframe!r}")
    root = Path(data_root) if data_root is not None else DEFAULT_DATA_ROOT
    lo, hi = _utc(first), _utc(last)

    def keep(entry: dict) -> bool:
        if "month" in entry and max_month is not None and entry["month"] > max_month:
            return False
        b = _month_bounds(entry)
        if b is not None:
            if lo is not None and b[1] < lo:
                return False
            if hi is not None and b[0] > hi:
                return False
        return True

    if source == "ar1":
        if spec.canonical != "GER40" or timeframe != "M5":
            raise MarketDataError("source='ar1' is the frozen GER40 M5 set only")
        base = root / AR1_ROOT_NAME

        def resolve_ar1(entry: dict) -> Path:
            # V1 manifest stores absolute paths into the repo that produced it; prefer the copy
            # under this data root (content hash is verified on read either way).
            local = base / "ACTIVTRADES_MT5_CFD" / "GER40" / "bars_5m" / Path(
                str(entry["path"]).replace("\\", "/")
            ).name
            return local if local.is_file() else Path(entry["path"])

        ds = load_research_dataset(
            base,
            entry_filter=lambda e: e.get("month", "") <= (max_month or "9999-99"),
            resolve_path=resolve_ar1,
        )
    elif source == "v2":
        base = root / V2_ROOT_NAME
        manifest_path = base / f"manifest_{spec.canonical}.json"
        if not manifest_path.is_file():
            raise MarketDataError(f"no manifest {manifest_path}")
        head = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (head.get("canonical"), head.get("broker_symbol")) != (
            spec.canonical, spec.broker_symbol
        ):
            raise MarketDataError(
                f"manifest symbol {head.get('canonical')}/{head.get('broker_symbol')} != spec "
                f"{spec.canonical}/{spec.broker_symbol}"
            )
        zone = head.get("timezone_policy", {}).get("zone")
        if zone != EXPECTED_SERVER_ZONE or EXPECTED_SERVER_ZONE not in spec.server_tz_policy:
            raise MarketDataError(f"unexpected server timezone policy {zone!r}")
        ds = load_research_dataset(
            base,
            manifest_name=f"manifest_{spec.canonical}.json",
            entries_key="entries",
            entry_filter=lambda e: e.get("tf") == timeframe and keep(e),
            resolve_path=lambda e: base / str(e["path"]).replace("\\", "/"),
            reviewed_gaps={},
            any_gap=True,  # recorded gaps stay listed per month; contiguity flags handle them
            source=f"ActivTrades MT5 DEMO, {spec.broker_symbol} (canonical {spec.canonical}), "
            f"{timeframe} BID OHLC + spread points (v2 download)",
        )
    else:
        raise MarketDataError(f"source must be 'ar1' or 'v2', got {source!r}")

    frame = ds.frame
    if lo is not None or hi is not None:
        m = np.ones(len(frame), dtype=bool)
        if lo is not None:
            m &= (frame["ts"] >= lo).to_numpy()
        if hi is not None:
            m &= (frame["ts"] <= hi).to_numpy()
        frame = frame.loc[m].reset_index(drop=True)
        ds = ResearchDataset(
            frame=frame, months=ds.months, excluded=ds.excluded, source=ds.source,
            broker_account_kind=ds.broker_account_kind, gap_review=ds.gap_review,
        )
    _integrity(frame, timeframe)
    return ds


def load_market_frame(
    spec: MarketSpec,
    timeframe: str = "M5",
    first: str | pd.Timestamp | None = None,
    last: str | pd.Timestamp | None = None,
    *,
    source: str = "v2",
    data_root: str | Path | None = None,
    max_month: str | None = None,
) -> pd.DataFrame:
    """Frame in the V1 schema (see module doc). May contain post-``DEV_END`` bars: use
    ``dev_frame`` before any feature/strategy/evaluator construction."""
    return load_market_dataset(
        spec, timeframe, source=source, data_root=data_root, first=first, last=last,
        max_month=max_month,
    ).frame


def load_dev_market_frame(
    spec: MarketSpec,
    plan=None,
    timeframe: str = "M5",
    *,
    source: str = "v2",
    data_root: str | Path | None = None,
) -> pd.DataFrame:
    """Development frame: forward-holdout months are never opened, then ``dev_frame`` cuts."""
    frame = load_market_frame(
        spec, timeframe, source=source, data_root=data_root, max_month=DEV_END[:7]
    )
    return dev_frame(frame, plan)


def _integrity(frame: pd.DataFrame, timeframe: str) -> None:
    if frame.empty:
        raise MarketDataError("empty frame")
    ts = pd.DatetimeIndex(frame["ts"])
    if ts.tz is None or str(ts.tz) != "UTC":
        raise MarketDataError("ts must be tz-aware UTC")
    if not ts.is_monotonic_increasing or ts.has_duplicates:
        raise MarketDataError("timestamps not strictly increasing")
    step = TIMEFRAME_SECONDS[timeframe]
    if (ts.as_unit("s").asi8 % step != 0).any():
        raise MarketDataError(f"bars off the {timeframe} grid")
    for col in ("open", "high", "low", "close", "tick_volume", "spread_pts"):
        if col not in frame or frame[col].isna().any():
            raise MarketDataError(f"column {col} missing or has NaN")
    o, h, lo, c = (frame[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    if not ((h >= lo) & (h >= np.maximum(o, c)) & (lo <= np.minimum(o, c))).all():
        raise MarketDataError("inconsistent OHLC")


# ------------------------------------------------------------------ contiguity
def contiguity_flags(frame: pd.DataFrame, timeframe: str = "M5") -> np.ndarray:
    """bool[n]: bar i+1 exists and starts one bar after bar i (same rule as Frame.contig_next)."""
    secs = pd.DatetimeIndex(frame["ts"]).as_unit("s").asi8
    out = np.zeros(len(frame), dtype=bool)
    out[:-1] = (secs[1:] - secs[:-1]) == TIMEFRAME_SECONDS[timeframe]
    return out


def gap_summary(frame: pd.DataFrame, tz: str, timeframe: str = "M5") -> dict:
    """Gaps between consecutive bars: total, intraday (same local date) > 3 bars, the largest."""
    ts = pd.DatetimeIndex(frame["ts"])
    step = TIMEFRAME_SECONDS[timeframe]
    d = np.diff(ts.as_unit("s").asi8)
    gap = np.flatnonzero(d != step)
    local_date = ts.tz_convert(tz).normalize().tz_localize(None).to_numpy()
    intraday = [int(i) for i in gap if local_date[i] == local_date[i + 1] and d[i] > 3 * step]
    largest = int(d.max()) if len(d) else 0
    return {
        "n_gaps": len(gap),
        "n_intraday_gaps_gt3bars": len(intraday),
        "largest_gap_hours": round(largest / 3600, 2),
        "intraday_gap_starts": [ts[i].isoformat() for i in intraday[:5]],
    }


# ------------------------------------------------------------------ forward holdout guard
def _berlin_dates(frame: pd.DataFrame) -> np.ndarray:
    ts = pd.DatetimeIndex(frame["ts"])
    return ts.tz_convert(BERLIN).normalize().tz_localize(None).to_numpy()


def assert_no_forward_holdout(frame: pd.DataFrame) -> None:
    """Refuse any frame with a bar on a Berlin date after ``DEV_END``."""
    if len(frame) and (_berlin_dates(frame) > np.datetime64(DEV_END)).any():
        last = pd.DatetimeIndex(frame["ts"]).max()
        raise ForwardHoldoutError(
            f"frame contains forward-holdout bars (last bar {last}); development ends {DEV_END}"
        )


def dev_frame(frame: pd.DataFrame, plan=None, *, end: str | None = None) -> pd.DataFrame:
    """Physically remove every bar after the development end (Berlin date, like
    ``ar2_fast.dev_frame``), then assert. ``plan.validation.end`` (if given) may not exceed
    ``DEV_END``; a plan reaching into the forward holdout is refused."""
    cut = end or (plan.validation.end if plan is not None else DEV_END)
    if pd.Timestamp(cut) > pd.Timestamp(DEV_END):
        raise ForwardHoldoutError(f"dev end {cut} is after {DEV_END} (forward holdout)")
    keep = _berlin_dates(frame) <= np.datetime64(cut)
    out = frame.loc[keep].reset_index(drop=True)
    assert_no_forward_holdout(out)
    return out


# ------------------------------------------------------------------ cross-market alignment
@dataclass(frozen=True)
class Alignment:
    """Per reference bar: the other market's latest COMPLETED bar at the reference bar's CLOSE."""

    idx: np.ndarray  # int64, index into that market's frame; -1 where invalid
    valid: np.ndarray  # bool
    lag_seconds: np.ndarray  # int64, ref bar-open minus matched bar-open (0 = same bar); -1 invalid

    def take(self, values: np.ndarray) -> np.ndarray:
        """`values[idx]` as float with NaN where invalid."""
        v = np.asarray(values, dtype=float)
        out = np.full(len(self.idx), np.nan)
        out[self.valid] = v[self.idx[self.valid]]
        return out


def align_markets(
    frames: Mapping[str, pd.DataFrame],
    ref: str = "GER40",
    *,
    timeframe: str = "M5",
    max_lag_bars: int = 1,
) -> dict[str, Alignment]:
    """Causal alignment of every market to the reference M5 grid.

    A reference bar i with open t_i closes at t_i + step. Market m's bar j is COMPLETED by then iff
    t_j + step <= t_i + step, i.e. t_j <= t_i; the match is the latest such bar
    (``searchsorted(side='right') - 1``), so no bar that closes after the reference close is ever
    used. ``valid`` is False before the market's first bar and when the match is older than
    ``max_lag_bars`` bars (the market has no fresh bar: closed session, holiday, gap). Prefix
    stable: the result for reference bars <= T does not change when later data is appended.
    """
    if ref not in frames:
        raise KeyError(ref)
    step = TIMEFRAME_SECONDS[timeframe]
    ref_t = pd.DatetimeIndex(frames[ref]["ts"]).as_unit("s").asi8
    out: dict[str, Alignment] = {}
    for name, fr in frames.items():
        t = pd.DatetimeIndex(fr["ts"]).as_unit("s").asi8
        if not (np.diff(t) > 0).all():
            raise MarketDataError(f"{name}: timestamps not strictly increasing")
        j = np.searchsorted(t, ref_t, side="right") - 1
        have = j >= 0
        lag = np.where(have, ref_t - t[np.maximum(j, 0)], -1)
        valid = have & (lag <= max_lag_bars * step)
        out[name] = Alignment(
            idx=np.where(valid, j, -1).astype(np.int64),
            valid=valid,
            lag_seconds=np.where(valid, lag, -1).astype(np.int64),
        )
    return out


__all__ = (
    "AR1_ROOT_NAME", "DEV_END", "FORWARD_HOLDOUT_START", "Alignment", "ForwardHoldoutError",
    "MarketDataError", "align_markets", "assert_no_forward_holdout", "contiguity_flags",
    "dev_frame", "gap_summary", "load_dev_market_frame", "load_market_dataset",
    "load_market_frame",
)
