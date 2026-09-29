"""Minimal provider boundary between alpha generation and downstream selection/risk."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from alpha.common.protocol import stable_hash
from alpha.common.sim import ExitSpec
from alpha.context import CONTEXT_LABELS
from alpha.fast.registry import FastFamily, discover
from alpha.fast.sim import CandidateArrays
from alpha.fast.spec import StrategySpec, evaluate_spec
from alpha.fast.store import FeatureSet
from alpha.regime import REGIME_DIMENSIONS
from alpha.signals import ENTRY_INTENT, SignalCandidate

AlphaBatch = tuple[str, str, str, CandidateArrays]


@runtime_checkable
class AlphaProvider(Protocol):
    name: str

    def candidates(self, features: FeatureSet) -> Iterable[AlphaBatch]: ...


def _plain_params(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "__dict__"):
        return vars(value)
    return value


@dataclass(frozen=True)
class KernelFamilyProvider:
    families: Mapping[str, FastFamily] | None = None
    versions: Mapping[str, str] | None = None
    name: str = "kernel_families"

    def candidates(self, features: FeatureSet) -> Iterable[AlphaBatch]:
        families = discover() if self.families is None else self.families
        versions = {} if self.versions is None else self.versions
        for strategy_id, family in sorted(families.items()):
            version = versions.get(strategy_id, "fast-1")
            for params in family.variants:
                yield (
                    strategy_id,
                    version,
                    stable_hash(_plain_params(params)),
                    family.generate(features, params),
                )


@dataclass(frozen=True)
class SpecProvider:
    specs: Sequence[StrategySpec]
    name: str = "declarative_specs"

    def candidates(self, features: FeatureSet) -> Iterable[AlphaBatch]:
        for spec in self.specs:
            yield spec.strategy_id, spec.version, spec.spec_hash(), evaluate_spec(features, spec)


def to_signal_candidates(
    features: FeatureSet,
    arrays: CandidateArrays,
    strategy_id: str,
    version: str,
    param_fingerprint: str,
    exit_spec: ExitSpec,
    instrument: str = "GER40",
) -> list[SignalCandidate]:
    """Materialize survivor candidates; kernel-only setup metadata is intentionally unavailable."""
    maps = features.metadata["maps"]
    regime_maps = maps["regime"]
    phase_map = maps["phase"]
    result: list[SignalCandidate] = []
    for position, direction, stop, target in zip(
        arrays.decision_idx, arrays.direction, arrays.stop, arrays.target, strict=True
    ):
        index = int(position)
        signal_price = float(features["c"][index])
        materialized_target = (
            float(target)
            if np.isfinite(target)
            else signal_price
            + int(direction) * float(exit_spec.r) * abs(signal_price - float(stop))
            if exit_spec.kind == "fixed_r"
            else None
        )
        regime = {
            dimension: regime_maps[dimension][
                str(int(features[f"regime_{dimension.lower()}"][index]))
            ]
            for dimension in REGIME_DIMENSIONS
        }
        context = {
            label: bool(features[f"context_{label.lower()}"][index]) for label in CONTEXT_LABELS
        }
        result.append(
            SignalCandidate(
                strategy_id=strategy_id,
                strategy_version=version,
                instrument=instrument,
                direction=int(direction),
                signal_ts=pd.Timestamp(int(features["ts_ns"][index]) + 300_000_000_000, tz="UTC"),
                signal_price=signal_price,
                entry_intent=ENTRY_INTENT,
                stop=float(stop),
                target=materialized_target,
                exit_spec=exit_spec,
                h1_regime=regime,
                m15_context=context,
                session_phase=phase_map[str(int(features["phase_code"][index]))],
                setup_metadata={},
                param_fingerprint=param_fingerprint,
            )
        )
    return result


__all__ = (
    "AlphaProvider",
    "KernelFamilyProvider",
    "SpecProvider",
    "to_signal_candidates",
)
