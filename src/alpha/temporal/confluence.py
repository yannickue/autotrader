# ruff: noqa: E501
"""Confluence data contract (design section 6): dataclasses only, no strategy logic.

INVARIANT: a confluence decision at bar ``u`` may only read records with ``decision_idx <= u``.
Consumers must obtain records through ``visible_at(table, u)``.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from alpha.fast.sim import CandidateArrays
from alpha.temporal.reference import InstanceTrail, MarketFrame
from alpha.temporal.spec import MAX_REG, MAX_STATES, StateMachineStrategySpec


def make_signal_id(spec_hash: str, decision_idx: int) -> str:
    return hashlib.sha256(f"{spec_hash}:{int(decision_idx)}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    strategy_id: str
    spec_hash: str
    family: str
    lineage_signature: str
    feature_set_signature: str
    decision_idx: int
    ts_close_ns: int  # -1 when the market carried no timestamps
    direction: int
    entry_zone_lo: float
    entry_zone_hi: float
    stop: float
    target: float  # NaN for fixed_r
    target_r: float
    anchor_idx: int
    step_idx: tuple[int, ...]  # enter bar per state (anchor, T1..Tn)
    registers: tuple[float, ...]
    event_ids: tuple[int, ...]


def records_from_reference(
    candidates: CandidateArrays,
    trails: Sequence[InstanceTrail],
    spec: StateMachineStrategySpec,
    market: MarketFrame,
    *,
    family: str = "",
    lineage_signature: str = "",
    feature_set_signature: str = "",
) -> tuple[SignalRecord, ...]:
    if len(trails) != len(candidates.decision_idx):
        raise ValueError("one trail per candidate required")
    spec_hash = spec.spec_hash()
    out = []
    for i, tr in enumerate(trails):
        d = int(candidates.decision_idx[i])
        if tr.step_idx[-1] != d:
            raise ValueError("trail does not end at the decision bar")
        out.append(SignalRecord(
            signal_id=make_signal_id(spec_hash, d), strategy_id=spec.strategy_id,
            spec_hash=spec_hash, family=family, lineage_signature=lineage_signature,
            feature_set_signature=feature_set_signature, decision_idx=d,
            ts_close_ns=-1 if market.ts_close_ns is None else int(market.ts_close_ns[d]),
            direction=int(candidates.direction[i]), entry_zone_lo=tr.entry_zone_lo,
            entry_zone_hi=tr.entry_zone_hi, stop=float(candidates.stop[i]),
            target=float(candidates.target[i]), target_r=float(candidates.target_r[i]),
            anchor_idx=tr.anchor_idx, step_idx=tuple(tr.step_idx),
            registers=tuple(tr.registers), event_ids=tuple(tr.event_ids),
        ))
    return tuple(out)


@dataclass(frozen=True)
class SignalTable:
    """Columnar container: dict of equal-length numpy columns."""

    columns: dict[str, np.ndarray]

    def __len__(self) -> int:
        return len(self.columns["decision_idx"])

    @classmethod
    def from_records(cls, records: Sequence[SignalRecord]) -> SignalTable:
        n = len(records)
        step = np.full((n, MAX_STATES), -1, dtype=np.int64)
        regs = np.full((n, MAX_REG), math.nan, dtype=np.float64)
        ev_ids = np.empty(n, dtype=object)
        for i, r in enumerate(records):
            step[i, : len(r.step_idx)] = r.step_idx
            regs[i, : len(r.registers)] = r.registers
            ev_ids[i] = r.event_ids
        def col(f, dt):
            return np.asarray([getattr(r, f) for r in records], dtype=dt)
        return cls({
            "signal_id": col("signal_id", object), "strategy_id": col("strategy_id", object),
            "spec_hash": col("spec_hash", object), "family": col("family", object),
            "lineage_signature": col("lineage_signature", object),
            "feature_set_signature": col("feature_set_signature", object),
            "decision_idx": col("decision_idx", np.int64), "ts_close_ns": col("ts_close_ns", np.int64),
            "direction": col("direction", np.int8), "entry_zone_lo": col("entry_zone_lo", np.float64),
            "entry_zone_hi": col("entry_zone_hi", np.float64), "stop": col("stop", np.float64),
            "target": col("target", np.float64), "target_r": col("target_r", np.float64),
            "anchor_idx": col("anchor_idx", np.int64), "step_idx": step, "registers": regs,
            "event_ids": ev_ids,
        })


def visible_at(table: SignalTable, u: int) -> SignalTable:
    """Records a confluence decision at bar ``u`` may read: decision_idx <= u only."""
    mask = table.columns["decision_idx"] <= u
    return SignalTable({k: v[mask] for k, v in table.columns.items()})
