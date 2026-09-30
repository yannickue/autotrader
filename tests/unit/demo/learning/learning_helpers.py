# ruff: noqa: E501
"""Synthetic store seeding for the learning tests (small, deterministic, memory-light)."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import numpy as np
from demo_factories import (
    T0,
    iso,
    make_decision,
    make_exec,
    make_intent,
    make_label,
    make_outcome,
    make_risk,
    make_snapshot,
)

from demo.store import DemoStore

MARKETS = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD")
FAMILIES = ("orb", "gap", "eod")


def snap_i(i: int, seed: int = 0, step_hours: int = 6):
    rng = np.random.default_rng(seed * 100003 + i)
    q = float(rng.random())
    snap = make_snapshot(
        i=0,
        market=MARKETS[i % len(MARKETS)],
        direction=1 if i % 2 == 0 else -1,
        signal_ts=T0 + timedelta(hours=step_hours * i),
        risk=float(1.0 + rng.random()),
        spread=float(0.05 + 0.1 * rng.random()),
        signal={"family": FAMILIES[i % 3], "strategy_id": "s", "confluence": int(rng.integers(1, 5)),
                "quality": q},
    )
    return snap, q


def seed_store(store: DemoStore, n: int, *, seed: int = 0, n_rejected: int = 0,
               shadow_names: tuple[str, ...] = (), shadow_p: float | None = None,
               learnable: bool = True):
    """n accepted+closed trades (label = quality>0.5 when learnable) and n_rejected labelled rejects.
    Returns the list of (opportunity_id, quality, target_hit)."""
    out = []
    rng = np.random.default_rng(seed)
    for i in range(n + n_rejected):
        snap, q = snap_i(i, seed)
        hit = (q > 0.5) if learnable else bool(rng.random() < 0.5)
        store.record_snapshot(snap)
        for name in shadow_names:
            store.record_shadow_prediction(snap.opportunity_id, name,
                                           {"p_target_before_stop": shadow_p}, created_utc=snap.created_utc)
        accepted = i < n
        store.record_decision(make_decision(snap, accepted))
        if accepted:
            it = make_intent(snap)
            store.record_intent(it)
            store.record_risk(it.intent_id, make_risk())
            for st in ("RISK_APPROVED", "SENT"):
                store.transition(it.intent_id, st)
            store.record_execution(it.intent_id, make_exec())
            for st in ("FILLED", "PROTECTED", "CLOSED"):
                store.transition(it.intent_id, st)
            net = 2.0 if hit else -1.0
            oc = dataclasses.replace(
                make_outcome(net, closed=T0 + timedelta(hours=step_hours(i) + 1)),
                exit_reason="TARGET" if hit else "STOP",
            )
            store.record_outcome(it.intent_id, oc)
        else:
            lab = dataclasses.replace(
                make_label(snap, r=2.0 if hit else -1.0),
                target_before_stop=hit,
                horizon_end_utc=iso(T0 + timedelta(hours=step_hours(i) + 1)),
                labelled_utc=iso(T0 + timedelta(hours=step_hours(i) + 2)),
            )
            store.record_counterfactual(lab)
        out.append((snap.opportunity_id, q, hit))
    return out


def step_hours(i: int, step: int = 6) -> int:
    return step * i
