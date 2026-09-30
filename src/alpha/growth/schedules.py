"""Risk-fraction schedules (fraction of CURRENT equity risked per trade). Vectorised over paths."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from alpha.growth.analytics import kelly_fraction


class Schedule:
    name: str = "schedule"

    def init(self, n_paths: int, r_hat: np.ndarray) -> dict:
        return {}

    def fraction(self, eq: np.ndarray, peak: np.ndarray, st: dict) -> np.ndarray:
        raise NotImplementedError

    def update(self, st: dict, r: np.ndarray, executed: np.ndarray, eq: np.ndarray,
               peak: np.ndarray) -> None:
        return None


@dataclass
class FixedFraction(Schedule):
    f: float
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = f"fixed_{self.f * 100:g}%"

    def fraction(self, eq, peak, st):
        return np.full_like(eq, self.f)


@dataclass
class FractionalKelly(Schedule):
    """frac x Kelly f* of the ESTIMATED stream (never the hidden true mean), shrunk by shrink_se."""

    frac: float = 0.5
    shrink_se: float = 0.0
    se: float = 0.0
    cap: float = 0.10
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = f"kelly_{self.frac:g}x"

    def init(self, n_paths, r_hat):
        f = kelly_fraction(r_hat - self.shrink_se * self.se)
        return {"f": min(self.frac * f, self.cap)}

    def fraction(self, eq, peak, st):
        return np.full_like(eq, st["f"])


@dataclass
class DrawdownThrottle(Schedule):
    """Halve risk after a drawdown >= ``dd`` from peak; restore at a new equity high."""

    base: float = 0.02
    dd: float = 0.15
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = f"dd_throttle_{self.base * 100:g}%"

    def init(self, n_paths, r_hat):
        return {"thr": np.zeros(n_paths, dtype=bool)}

    def fraction(self, eq, peak, st):
        return np.where(st["thr"], 0.5 * self.base, self.base)

    def update(self, st, r, executed, eq, peak):
        dd = 1.0 - eq / np.maximum(peak, 1e-12)
        thr = np.where(eq >= peak, False, st["thr"] | (dd >= self.dd))
        st["thr"] = np.where(executed, thr, st["thr"])


@dataclass
class RampAfterWins(Schedule):
    """base + inc per consecutive win, capped; back to base after a loss."""

    base: float = 0.01
    inc: float = 0.005
    cap: float = 0.05
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = f"ramp_{self.base * 100:g}-{self.cap * 100:g}%"

    def init(self, n_paths, r_hat):
        return {"wins": np.zeros(n_paths)}

    def fraction(self, eq, peak, st):
        return np.minimum(self.base + self.inc * st["wins"], self.cap)

    def update(self, st, r, executed, eq, peak):
        w = np.where(r > 0, st["wins"] + 1.0, 0.0)
        st["wins"] = np.where(executed, w, st["wins"])
