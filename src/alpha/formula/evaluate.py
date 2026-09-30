# ruff: noqa: E501
"""Train-only light screen of a factor's candidates through the V1 fast simulator.

Search-side module: it receives a ``prepare.TrainView`` (Train bars only) and simulates only that view's
candidates with ``simulate_fast``; metrics come from ``screen_partition_trades`` (the reducer inside
``light_screen``).  Later partitions are structurally absent.  The trial ledger is the V1-style
``TemporalTrialLedger`` (generalised through callables) so cumulative counts carry over between runs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from alpha.common.sim import COST_SCENARIOS, DEFAULT_RULES, DEFAULT_SIZING, SimRules, SizingSpec
from alpha.discovery.temporal_evaluate import TemporalTrialLedger
from alpha.fast.screen import PartitionScreen, screen_partition_trades
from alpha.fast.sim import simulate_fast
from alpha.formula.fitness import FactorScore
from alpha.formula.prepare import TrainView
from alpha.formula.signal import SignalSpec, build_candidates, fit_thresholds
from alpha.formula.tree import canonical_hash, validate

BASE_COST = "BASE"
ADVERSE_COST = "COMBINED_ADVERSE"


def make_ledger() -> TemporalTrialLedger:
    return TemporalTrialLedger(validate_fn=validate, hash_fn=canonical_hash)


def load_ledger(path: Path | str) -> TemporalTrialLedger:
    p = Path(path)
    led = make_ledger()
    if p.exists():
        loaded = TemporalTrialLedger.from_json(p.read_text(encoding="utf-8"))
        loaded.validate_fn, loaded.hash_fn = validate, canonical_hash
        return loaded
    return led


def save_ledger(led: TemporalTrialLedger, path: Path | str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(led.to_json(), encoding="utf-8")


@dataclass(frozen=True)
class TrainScreen:
    n_candidates: int
    candidates_per_day: float
    base: PartitionScreen
    adverse: PartitionScreen

    def to_dict(self) -> dict:
        return {"n_candidates": self.n_candidates, "candidates_per_day": round(self.candidates_per_day, 4),
                "base": asdict(self.base), "adverse": asdict(self.adverse)}


def train_screen(
    view: TrainView, factor: np.ndarray, score: FactorScore, spec: SignalSpec, *,
    sizing: SizingSpec = DEFAULT_SIZING, rules: SimRules = DEFAULT_RULES,
) -> TrainScreen | None:
    """Thresholds from the Train view, candidates on the Train view, simulate once per cost scenario."""
    if not score.valid or spec.sign != score.sign:
        raise ValueError("SignalSpec.sign must be the Train-fitted sign of a valid factor")
    g = spec.sign * factor
    thr = fit_thresholds(g, view.data.minute, spec.q, spec.session)
    cands = build_candidates(factor, view.data, spec, thr)
    n = len(cands.decision_idx)
    if n == 0:
        return None
    sides = {}
    for name in (BASE_COST, ADVERSE_COST):
        trades = simulate_fast(view.market, cands, COST_SCENARIOS[name], sizing, rules)
        sides[name] = screen_partition_trades(trades, np.ones(len(trades), dtype=bool), view.n_days,
                                              contract_size=sizing.contract_size)
    return TrainScreen(n, n / max(view.n_days, 1), sides[BASE_COST], sides[ADVERSE_COST])


__all__ = ("ADVERSE_COST", "BASE_COST", "TrainScreen", "load_ledger", "make_ledger", "save_ledger", "train_screen")
