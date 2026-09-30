# ruff: noqa: E501
"""FAMILY 6 - LEADLAG: cross-market lead-lag (leader move -> follower same-direction entry).

HYPOTHESIS.  Highly correlated index CFDs (NAS100 / SPX500 / GER40) do not update in perfect lock-step: after a
strong ``n_bars`` move of the LEADER (beyond the TRAIN-ONLY quantile ``q`` of ``|return| / ATR_leader``) the
FOLLOWER continues in the same direction for a short while (``sign=+1``; ``sign=-1`` is the fade control).  With
``catchup`` the trade is taken only while the follower has NOT yet moved (its own ``n_bars`` return in ATR units
is below half the leader's), i.e. only when there is something left to catch up.  Time stop: clock exit
(``exit_clock``, see ``spec.py``).

FAILURE MODES.  M5 bars are coarse: any real index lead-lag is seconds to a minute, i.e. already inside the
follower's own bar; what remains at M5 is mostly contemporaneous correlation (no edge, negative after spread);
the leader is only usable while its CASH session is active (short overlaps, e.g. GER40 17:30 close vs NY open);
the follower's spread on fast-market bars.

CAUSALITY.  ``alpha.common.market_data.align_markets``: for each follower bar ``i`` (closing at ``t_i + 5min``)
the leader's latest bar with open ``<= t_i`` (COMPLETED by that close) and no older than ``max_lag_bars`` is used;
its n-bar return only reads leader bars ``<= j``.  Prefix stable; only markets whose cash sessions overlap
produce active bars.  The decision at ``i`` is filled at ``i + 1`` by the simulator.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np

from alpha.families.common import (
    Thr,
    cooldown,
    empty_candidates,
    entry_mask,
    finalize,
    rising,
    train_quantile,
)
from alpha.families.data import LEADER_NS, FamilyData
from alpha.families.spec import FamilySpec, check, thin_grid
from alpha.fast.sim import CandidateArrays

COOLDOWN = 6
LEADERS = ("NAS100", "SPX500", "GER40")
# follower -> leaders whose CASH session can overlap the follower's entry window
PAIRS: dict[str, tuple[str, ...]] = {
    "GER40": ("NAS100", "SPX500"),
    "NAS100": ("SPX500", "GER40"),
    "SPX500": ("NAS100", "GER40"),
}


@dataclass(frozen=True)
class LEADLAGSpec(FamilySpec):
    FAMILY = "LEADLAG"
    leader: str = "NAS100"
    n_bars: int = 3
    q: float = 0.95
    sign: int = 1
    catchup: bool = True
    stop_atr: float = 1.5
    target_r: float = 1.5
    exit_clock: str = "flat"
    tod: str = "all"
    side: str = "both"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        check(self.leader in LEADERS, "leader")
        check(self.n_bars in LEADER_NS and 0.5 <= self.q < 1.0 and self.sign in (-1, 1), "n_bars/q/sign")
        check(self.stop_atr > 0 and self.target_r > 0, "stop/target")
        check(self.exit_clock in ("flat", "close") and self.tod in ("all", "am", "pm") and self.side in ("both", "long", "short"), "exit/tod/side")

    @property
    def complexity(self) -> int:
        return 4 + int(self.catchup) + int(self.sign < 0)


def grid(leaders: tuple[str, ...] = ("NAS100", "SPX500"), max_n: int | None = 400) -> list[FamilySpec]:
    out = [LEADLAGSpec(ld, n, q, 1, cu, s, r, ex) for ld, n, q, cu, s, r, ex in product(
        leaders, LEADER_NS, (0.9, 0.95, 0.98), (True, False), (1.0, 1.5), (1.0, 2.0), ("flat", "close"))]
    return thin_grid(out, max_n)


def _ret(data: FamilyData, spec: LEADLAGSpec) -> tuple[np.ndarray, np.ndarray] | None:
    lf = data.cross.get(spec.leader)
    if lf is None or spec.n_bars not in lf.ret:
        return None
    return lf.ret[spec.n_bars], lf.active


def fit(train: FamilyData, spec: LEADLAGSpec) -> Thr:
    got = _ret(train, spec)
    if got is None:
        return Thr((float("nan"),))
    r, active = got
    ok = entry_mask(train, spec.effective_window(train.cal))
    return Thr((train_quantile(np.abs(r)[active & ok & np.isfinite(r)], spec.q),))


def generate(data: FamilyData, spec: LEADLAGSpec, thr: Thr) -> CandidateArrays:
    got = _ret(data, spec)
    if got is None or not thr.ok or len(data) == 0:
        return empty_candidates()
    r, active = got
    n = len(data)
    ok = entry_mask(data, spec.effective_window(data.cal), spec.tod)
    with np.errstate(invalid="ignore", divide="ignore"):
        cond = active & np.isfinite(r) & (np.abs(r) >= thr.values[0]) & ok
        if spec.catchup:
            k = spec.n_bars
            own = np.full(n, np.nan)
            if n > k:
                own[k:] = (data.c[k:] - data.c[:-k]) / data.atr[k:]
            own = np.where(data.run_start <= np.arange(n) - k, own, np.nan)
            cond &= np.isfinite(own) & (np.sign(r) * own < 0.5 * np.abs(r))
        cond = rising(cond, data)
    idx = cooldown(np.flatnonzero(cond), COOLDOWN)
    if len(idx) == 0:
        return empty_candidates()
    dirn = (np.sign(r[idx]) * spec.sign).astype(np.int8)
    stop = data.c[idx] - dirn * spec.stop_atr * data.atr[idx]
    return finalize(data, idx, dirn, stop, None, spec.target_r, spec.side)


__all__ = ("COOLDOWN", "LEADERS", "PAIRS", "LEADLAGSpec", "fit", "generate", "grid")
