# ruff: noqa: E501
"""Sealing / holdout guards: nothing after DEV_END, no fold-test / validation access (source scan + behaviour)."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from alpha.metalabel import dataset as ds

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [*sorted((ROOT / "src/alpha/metalabel").glob("*.py")), ROOT / "research/runners/v2_metalabel.py"]

# any of these identifiers would mean a fold TEST side, the sealed validation view or the holdout is reachable
FORBIDDEN = (
    r"\btest_mask\b", r"\bfold_test_masks\b", r"\btemporal_validation_gate_view\b", r"\bensure_full\b",
    r"\bplan\.validation\b", r"\.validation\b", r"\b_validation\b", r"\bplan\.oos\b", r"\bFORWARD_HOLDOUT_START\b",
    r"need_base\s*=\s*True", r"\bload_holdout\b",
)


def _code(path: Path) -> str:
    """Source without comments/docstrings noise is not needed: identifiers must not appear at all."""
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_fold_test_or_validation_access_in_sources(path):
    text = _code(path)
    hits = [pat for pat in FORBIDDEN if re.search(pat, text)]
    assert not hits, f"{path.name} references sealed data: {hits}"


def test_runner_uses_only_the_train_mask_and_lean_evaluations():
    text = _code(ROOT / "research/runners/v2_metalabel.py")
    assert "plan.train" in text and "need_base=False" in text
    assert "truncate_market" in text and "train_cut" in text  # arrays are cut after the last Train day
    assert "fold_report" not in text.replace("ctx.fold_rep", "")  # never rebuilds/reads fold test sides


def test_assert_rows_train_only_rejects_rows_outside_train():
    dates = (np.datetime64("2025-02-03") + np.arange(400) // 10).astype("datetime64[D]")
    train = np.zeros(400, dtype=bool)
    train[:200] = True
    ds.assert_rows_train_only(train, dates, np.array([0, 50, 199]))
    with pytest.raises(RuntimeError):
        ds.assert_rows_train_only(train, dates, np.array([0, 200]))


def test_assert_rows_train_only_rejects_forward_holdout_dates():
    dates = np.array(["2026-08-31", "2026-09-01"], dtype="datetime64[D]")
    with pytest.raises(RuntimeError):
        ds.assert_rows_train_only(np.ones(2, dtype=bool), dates, np.array([0]))


def test_train_cut_never_includes_a_later_day():
    dates = (np.datetime64("2025-02-03") + np.arange(500) // 10).astype("datetime64[D]")
    train = np.zeros(500, dtype=bool)
    train[:157] = True  # ends mid-day 15
    n = ds.train_cut(dates, train)
    assert n == 160 and dates[:n].max() == dates[train].max()  # full last train day kept, nothing later
