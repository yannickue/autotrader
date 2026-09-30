"""Training-set construction: features from snapshots, labels from outcomes / counterfactuals.

Labels are read ONLY after they exist in the store (closed outcomes, post-horizon counterfactual
labels). Features never come from the label side (see `features.py`). Real trades and
counterfactual-labelled rejected opportunities are kept apart by the boolean column `is_cf`;
counterfactual rows are excluded unless `include_counterfactual=True` and are down-weighted by
`cf_weight`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from demo.contracts import CounterfactualLabel, OutcomeRecord
from demo.learning.features import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    extract_features,
    feature_vector,
    group_keys,
)
from demo.store import DemoStore, parse_utc

SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class Dataset:
    feature_names: tuple[str, ...]
    feature_version: str
    X: np.ndarray  # (n, f) float64, NaN = unknown
    y_target: np.ndarray  # target before stop; NaN = unknown label
    y_pos: np.ndarray  # net (or hypothetical) R > 0
    r: np.ndarray
    mfe: np.ndarray
    mae: np.ndarray
    is_cf: np.ndarray  # bool: counterfactual (never traded) row
    weight: np.ndarray
    ts: np.ndarray  # signal time, epoch seconds
    label_ts: np.ndarray  # when the label became available, epoch seconds (>= ts)
    opportunity_ids: tuple[str, ...]
    market: tuple[str, ...]
    session: tuple[str, ...]
    family: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.opportunity_ids)

    @property
    def day(self) -> np.ndarray:
        return np.floor(self.ts / SECONDS_PER_DAY).astype(np.int64)

    @property
    def label_day(self) -> np.ndarray:
        return np.floor(self.label_ts / SECONDS_PER_DAY).astype(np.int64)

    def subset(self, idx: np.ndarray) -> Dataset:
        idx = np.asarray(idx, dtype=np.int64)
        return Dataset(
            feature_names=self.feature_names,
            feature_version=self.feature_version,
            X=self.X[idx], y_target=self.y_target[idx], y_pos=self.y_pos[idx], r=self.r[idx],
            mfe=self.mfe[idx], mae=self.mae[idx], is_cf=self.is_cf[idx], weight=self.weight[idx],
            ts=self.ts[idx], label_ts=self.label_ts[idx],
            opportunity_ids=tuple(self.opportunity_ids[i] for i in idx),
            market=tuple(self.market[i] for i in idx),
            session=tuple(self.session[i] for i in idx),
            family=tuple(self.family[i] for i in idx),
        )

    def window(self) -> dict[str, Any]:
        if not len(self):
            return {"start_utc": None, "end_utc": None, "n": 0}
        from datetime import UTC, datetime

        def f(x: float) -> str:
            return datetime.fromtimestamp(x, tz=UTC).isoformat()

        return {"start_utc": f(float(self.ts.min())), "end_utc": f(float(self.label_ts.max())),
                "n": len(self)}


def _epoch(iso: str) -> float:
    return parse_utc(iso).timestamp()


def target_hit(outcome: OutcomeRecord) -> bool:
    return outcome.exit_reason == "TARGET"


def _empty() -> Dataset:
    z = np.zeros(0)
    return Dataset(FEATURE_NAMES, FEATURE_VERSION, np.zeros((0, len(FEATURE_NAMES))), z, z, z, z, z,
                   z.astype(bool), z, z, z, (), (), (), ())


def build_dataset(
    store: DemoStore,
    *,
    phase: str | None = None,
    include_counterfactual: bool = False,
    cf_weight: float = 0.5,
    as_of_utc: str | None = None,
) -> Dataset:
    """Chronologically sorted dataset. `as_of_utc` drops rows whose label became available later."""
    rows: list[tuple] = []
    for _intent_id, opp_id, out in store.list_outcomes(phase):
        snap = store.get_snapshot(opp_id)
        if snap is None:
            continue
        lts = _epoch(out.closed_utc) if out.closed_utc else _epoch(snap.created_utc)
        rows.append((snap, float(target_hit(out)), float(out.net_r > 0), out.net_r, out.mfe_r,
                     out.mae_r, False, 1.0, lts))
    if include_counterfactual:
        for lab in store.list_counterfactuals(phase):
            snap = store.get_snapshot(lab.opportunity_id)
            if snap is None:
                continue
            rows.append(_cf_row(snap, lab, cf_weight))
    if as_of_utc is not None:
        cut = _epoch(as_of_utc)
        rows = [r for r in rows if r[8] <= cut]
    if not rows:
        return _empty()
    rows.sort(key=lambda r: (_epoch(r[0].signal_ts_utc), r[0].opportunity_id))
    X = np.array([feature_vector(extract_features(r[0])) for r in rows], dtype=np.float64)
    gk = [group_keys(r[0]) for r in rows]
    label_ts = np.array([max(r[8], _epoch(r[0].signal_ts_utc)) for r in rows], dtype=np.float64)
    return Dataset(
        feature_names=FEATURE_NAMES,
        feature_version=FEATURE_VERSION,
        X=X,
        y_target=np.array([r[1] for r in rows], dtype=np.float64),
        y_pos=np.array([r[2] for r in rows], dtype=np.float64),
        r=np.array([r[3] for r in rows], dtype=np.float64),
        mfe=np.array([r[4] for r in rows], dtype=np.float64),
        mae=np.array([r[5] for r in rows], dtype=np.float64),
        is_cf=np.array([r[6] for r in rows], dtype=bool),
        weight=np.array([r[7] for r in rows], dtype=np.float64),
        ts=np.array([_epoch(r[0].signal_ts_utc) for r in rows], dtype=np.float64),
        label_ts=label_ts,
        opportunity_ids=tuple(r[0].opportunity_id for r in rows),
        market=tuple(g["market"] for g in gk),
        session=tuple(g["session"] for g in gk),
        family=tuple(g["family"] for g in gk),
    )


def _cf_row(snap: Any, lab: CounterfactualLabel, cf_weight: float) -> tuple:
    y_t = float("nan") if lab.target_before_stop is None else float(lab.target_before_stop)
    lts = max(_epoch(lab.horizon_end_utc), _epoch(lab.labelled_utc))
    return (snap, y_t, float(lab.hypothetical_r > 0), lab.hypothetical_r, lab.hypothetical_mfe_r,
            lab.hypothetical_mae_r, True, cf_weight, lts)


def head_arrays(ds: Dataset, head: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(row_idx, X, y, w) of the rows with a known label for `head` in
    {target_before_stop, net_positive, expected_r}."""
    y = {"target_before_stop": ds.y_target, "net_positive": ds.y_pos, "expected_r": ds.r}[head]
    ok = np.flatnonzero(np.isfinite(y))
    return ok, ds.X[ok], y[ok], ds.weight[ok]
