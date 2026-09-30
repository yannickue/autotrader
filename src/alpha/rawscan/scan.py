# ruff: noqa: E501
"""Train-side scan orchestration: grid -> pairs -> cells -> BH -> nulls -> pre-registration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from alpha.common.market_data import assert_no_forward_holdout
from alpha.discovery.folds import berlin_dates_from_ts_ns
from alpha.rawscan.engine import (
    CellSpace,
    CostParams,
    WindowSpec,
    build_cell_space,
    build_pairs,
    cell_table,
)
from alpha.rawscan.grid import DayGrid, build_day_grid
from alpha.rawscan.nulls import (
    day_bootstrap_null,
    day_shift_null,
    day_signflip_null,
    null_summary,
)
from alpha.rawscan.stats import bh_qvalues


class DataDisciplineError(RuntimeError):
    """The scan was handed bars outside the Train side it is allowed to see."""


@dataclass
class ScanResult:
    grid: DayGrid
    cs: CellSpace
    table: pd.DataFrame
    duplicate_cells: int
    null_shift: dict | None = None
    null_boot: dict | None = None
    null_shift_raw: dict | None = None
    null_boot_raw: dict | None = None
    null_flip: dict | None = None
    null_flip_raw: dict | None = None


def assert_train_only(frame: pd.DataFrame, train_end: str | None) -> None:
    assert_no_forward_holdout(frame)
    if train_end is not None and len(frame):
        d = berlin_dates_from_ts_ns(pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8)
        if d.max() > np.datetime64(train_end):
            raise DataDisciplineError(
                f"scan frame reaches {d.max()} but the Train side ends {train_end}"
            )


def scan_train(
    train_frame: pd.DataFrame, *, tz: str, point_size: float, win: WindowSpec,
    costs: dict[str, CostParams], train_end: str | None = None, n_rep: int = 200, seed: int = 0,
    min_n: int = 40, min_n_dow: int = 20, run_nulls: bool = True,
) -> ScanResult:
    assert_train_only(train_frame, train_end)
    grid = build_day_grid(train_frame, tz, point_size)
    pairs = build_pairs(grid, win, costs)
    cs = build_cell_space(grid, win, pairs)
    table = cell_table(cs, None, min_n=min_n, min_n_dow=min_n_dow)
    table["q_bh"] = bh_qvalues(table["p1"].to_numpy())
    res = ScanResult(grid, cs, table, pairs.n_duplicate_horizons * 12)
    if run_nulls:
        add_nulls(res, n_rep=n_rep, seed=seed, min_n=min_n, min_n_dow=min_n_dow)
    return res


def _obs(table: pd.DataFrame) -> tuple[float, float, int, int]:
    t = table["t"].to_numpy(float)
    t = t[np.isfinite(t)]
    if not len(t):
        return float("nan"), float("nan"), 0, 0
    return float(t.max()), float(np.abs(t).max()), int((t > 2).sum()), int((t > 3).sum())


def add_nulls(res: ScanResult, *, n_rep: int, seed: int, min_n: int, min_n_dow: int) -> None:
    lab = res.table[res.table["labelled"]]
    res.null_shift_raw = day_shift_null(
        res.cs, n_rep=n_rep, seed=seed, min_n=min_n, min_n_dow=min_n_dow
    )
    res.null_boot_raw = day_bootstrap_null(
        res.cs, n_rep=n_rep, seed=seed + 1, min_n=min_n, min_n_dow=min_n_dow
    )
    res.null_flip_raw = day_signflip_null(
        res.cs, n_rep=n_rep, seed=seed + 2, min_n=min_n, min_n_dow=min_n_dow
    )
    ad = res.table[res.table["dow"] < 0]
    ad_best = float(np.nanmax(ad["t"])) if len(ad) else float("nan")
    res.null_shift = null_summary(res.null_shift_raw, *_obs(lab))
    res.null_boot = null_summary(res.null_boot_raw, *_obs(res.table), ad_best)
    res.null_flip = null_summary(res.null_flip_raw, *_obs(res.table), ad_best)


def preregister(table: pd.DataFrame, k: int = 20, *, min_q_rows: int = 1) -> pd.DataFrame:
    """Top-``k`` cells by net t (positive net edge; the mirrored direction is its own cell, so this
    equals ranking by |t| among edge-direction cells) on the Train side, frozen before the gate."""
    t = table[np.isfinite(table["t"])]
    cols = ["cell_id", "family", "sigma", "dir", "slot", "hhmm", "h", "hlabel", "dow", "n",
            "mean_net", "t", "q_bh"]
    return t.sort_values(["t", "cell_id"], ascending=[False, True]).head(k)[cols].reset_index(drop=True)


def prereg_hash(cells: pd.DataFrame) -> str:
    payload = json.dumps(cells[["cell_id", "t"]].round(6).to_dict("records"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
