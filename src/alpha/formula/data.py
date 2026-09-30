# ruff: noqa: E501
"""FormulaData: the causal input arrays a formula may read (NO labels live here).

``FormulaData`` holds OHLC, ATR, ``run_start`` (day-contiguous run start), the Berlin minute and the
named terminal arrays (bar helpers + already-causal FeatureStore columns).  There is intentionally no
field for forward returns: labels are built in ``alpha.formula.fitness`` and never enter this object, so
a factor cannot read them (test: ``test_formula_alpha_gp.py::test_label_never_reaches_factor_inputs``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np

from alpha.formula import ops


@dataclass(frozen=True)
class FormulaData:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    atr: np.ndarray
    run_start: np.ndarray
    minute: np.ndarray  # local minute of day of each bar OPEN (bucket key only)
    arrays: Mapping[str, np.ndarray] = field(default_factory=dict)  # terminal name -> array

    def __post_init__(self) -> None:
        n = len(self.c)
        for name in ("o", "h", "l", "atr", "run_start", "minute"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} length != {n}")
        for k, v in self.arrays.items():
            if len(v) != n:
                raise ValueError(f"terminal {k} length != {n}")

    def __len__(self) -> int:
        return len(self.c)

    @property
    def terminals(self) -> tuple[str, ...]:
        return tuple(sorted(self.arrays))

    def column(self, name: str) -> np.ndarray:
        try:
            return self.arrays[name]
        except KeyError:
            raise KeyError(f"unknown terminal {name!r}") from None

    def prefix(self, n: int) -> FormulaData:
        """Bars ``[0, n)`` only (truncation): later bars are not reachable from the result."""
        return FormulaData(
            self.o[:n], self.h[:n], self.l[:n], self.c[:n], self.atr[:n], self.run_start[:n],
            self.minute[:n], {k: v[:n] for k, v in self.arrays.items()},
        )


def build_formula_data(
    o, h, lo, c, atr14, run_start, minute, features: Mapping[str, np.ndarray] | None = None,
    *, extra: Mapping[str, np.ndarray] | None = None, feature_names: tuple[str, ...] | None = None,
) -> FormulaData:
    """Assemble terminals: bar helpers + the ``ops.FRAME_FEATURE_TERMINALS`` present in ``features``.

    ``extra`` adds named arrays (synthetic planted signals in tests).  Names must be causal arrays.
    """
    rs = np.ascontiguousarray(run_start, dtype=np.int64)
    arrays = dict(ops.bar_terminals(o, h, lo, c, atr14, rs))
    if features is not None:
        for name in feature_names if feature_names is not None else ops.FRAME_FEATURE_TERMINALS:
            if name in features:
                arrays[name] = np.ascontiguousarray(features[name], dtype=np.float64)
    if extra:
        for name, arr in extra.items():
            arrays[name] = np.ascontiguousarray(arr, dtype=np.float64)
    return FormulaData(
        np.ascontiguousarray(o, dtype=np.float64), np.ascontiguousarray(h, dtype=np.float64),
        np.ascontiguousarray(lo, dtype=np.float64), np.ascontiguousarray(c, dtype=np.float64),
        np.ascontiguousarray(atr14, dtype=np.float64), rs, np.asarray(minute), arrays,
    )


def from_market_frame(frame, features: Mapping[str, np.ndarray] | None = None, **kw) -> FormulaData:
    """Adapter from a ``alpha.temporal.reference.MarketFrame`` (+ optional FeatureStore mapping)."""
    src = features if features is not None else frame.arrays
    return build_formula_data(frame.o, frame.h, frame.l, frame.c, frame.atr, frame.run_start,
                              frame.berlin_minute, src, **kw)
