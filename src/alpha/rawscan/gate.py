# ruff: noqa: E501
"""Sealed persistence gate: the ONLY place the later fold TEST sides are read.

``persistence_gate`` takes the pre-registered top-K Train cells (frozen, hashed before the call),
the full DEV frame (still <= 2026-08-31, asserted) and the folds, rebuilds each cell with the SAME
definition and evaluates it on every fold's TEST days.  It may be called once per ``run_key``; a
second call raises.  Its output is a survival diagnostic, never a selection input.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from alpha.common.market_data import assert_no_forward_holdout
from alpha.rawscan.engine import (
    CostParams,
    WindowSpec,
    build_cell_space,
    build_pairs,
    moments,
    net_matrix,
)
from alpha.rawscan.grid import build_day_grid
from alpha.rawscan.stats import p_one_sided, t_from_moments

NOTE = "survival stage, not for selection"
_USED: set[str] = set()


class GateAlreadyUsed(RuntimeError):
    """The persistence gate was already evaluated for this run."""


def _reset_for_tests() -> None:
    _USED.clear()


def _cell_id(meta: pd.DataFrame) -> pd.Series:
    return meta["family"].astype(str) + "|" + meta["sigma"] + "|" + meta["hhmm"] + "|" + meta["hlabel"]


def persistence_gate(
    cells: pd.DataFrame, full_frame: pd.DataFrame, folds: list, *, tz: str, point_size: float,
    win: WindowSpec, costs: dict[str, CostParams], run_key: str, out_path: str | Path | None = None,
    prereg_sha256: str | None = None,
) -> dict:
    if run_key in _USED:
        raise GateAlreadyUsed(f"persistence gate already used for {run_key!r}")
    _USED.add(run_key)
    assert_no_forward_holdout(full_frame)
    grid = build_day_grid(full_frame, tz, point_size)
    cs = build_cell_space(grid, win, build_pairs(grid, win, costs))
    ids = _cell_id(cs.meta)
    pos = {c: i for i, c in enumerate(ids)}
    sel = np.array([pos[c] for c in cells["cell_id"] if c in pos], dtype=int)
    found = [c for c in cells["cell_id"] if c in pos]
    X = net_matrix(cs, "BASE", cols=sel)
    dates = grid.dates
    fold_res: list[list[dict]] = [[] for _ in found]
    pooled_mask = np.zeros(len(dates), dtype=bool)
    for f in folds:
        m = (dates >= np.datetime64(f.test_start)) & (dates <= np.datetime64(f.test_end))
        pooled_mask |= m
        n, s1, s2 = moments(np.where(m[:, None], X, np.nan))
        mean, t = t_from_moments(n, s1, s2, 5)
        for i in range(len(found)):
            fold_res[i].append({"fold": f.index, "n": int(n[i]),
                                "mean_net": None if np.isnan(mean[i]) else float(mean[i]),
                                "t": None if np.isnan(t[i]) else float(t[i])})
    n, s1, s2 = moments(np.where(pooled_mask[:, None], X, np.nan))
    mean, t = t_from_moments(n, s1, s2, 10)
    p = p_one_sided(t, n)
    out_cells = []
    for i, cid in enumerate(found):
        train = cells[cells["cell_id"] == cid].iloc[0]
        pos_folds = sum(1 for r in fold_res[i] if r["mean_net"] is not None and r["mean_net"] > 0)
        survives = bool(np.isfinite(t[i]) and t[i] >= 2.0 and mean[i] > 0
                        and pos_folds * 2 > len(folds))
        out_cells.append({
            "cell_id": cid, "train_t": float(train["t"]), "train_mean_net": float(train["mean_net"]),
            "train_q_bh": None if pd.isna(train["q_bh"]) else float(train["q_bh"]),
            "test_pooled_n": int(n[i]),
            "test_pooled_mean_net": None if np.isnan(mean[i]) else float(mean[i]),
            "test_pooled_t": None if np.isnan(t[i]) else float(t[i]),
            "test_pooled_p_one_sided": None if np.isnan(p[i]) else float(p[i]),
            "positive_test_folds": pos_folds, "n_folds": len(folds), "folds": fold_res[i],
            "survives": survives,
        })
    doc = {
        "note": NOTE, "run_key": run_key, "prereg_sha256": prereg_sha256, "k": len(cells),
        "found": len(found), "n_survive": sum(c["survives"] for c in out_cells),
        "criterion": "pooled test t >= 2.0, pooled test mean net > 0, positive in a majority of folds",
        "cells": out_cells,
    }
    if out_path is not None:
        p_out = Path(out_path)
        p_out.parent.mkdir(parents=True, exist_ok=True)
        p_out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return doc
