"""Dependency-light preparation contracts for optional research engines."""

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from research.immutability import freeze


@dataclass(frozen=True, slots=True, kw_only=True)
class OptimizationPlan:
    seed: int
    search_space: Mapping[str, tuple[int | float, int | float]]
    objective: str

    def __post_init__(self) -> None:
        if not self.objective.startswith("oos_"):
            raise ValueError("optimization objective must be out-of-sample")
        if any(lower >= upper for lower, upper in self.search_space.values()):
            raise ValueError("each search-space lower bound must be below its upper bound")
        object.__setattr__(self, "search_space", freeze(self.search_space))

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "search_space": {
                name: [bounds[0], bounds[1]]
                for name, bounds in sorted(self.search_space.items())
            },
            "seed": self.seed,
        }


def prepare_vectorbt_inputs(rows: tuple[dict[str, Any], ...]) -> dict[str, tuple[Any, ...]]:
    """Transpose uniform row data without importing optional VectorBT/Pandas dependencies."""

    if not rows:
        return {}
    columns = tuple(rows[0])
    if any(tuple(row) != columns for row in rows):
        raise ValueError("all rows must have identical ordered columns")
    return {column: tuple(row[column] for row in rows) for column in columns}
