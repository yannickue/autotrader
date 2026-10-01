# ruff: noqa: E501
"""Family spec base, market calendar and the effective simulation window (research only, causal).

A ``FamilySpec`` is a small frozen, hashable, JSON-serialisable parameter set of ONE intraday edge family.
``canonical_hash`` is over the CANONICAL parameters (parameters that a given mode ignores are normalised to a
fixed value), so two specs that describe the same behaviour share a hash and count once in the trial ledger.

Time stops.  ``simulate_fast`` has no per-trade holding limit; its only clock exit is ``SimWindow.flat_min``.
A spec that wants an earlier "time stop" therefore states a CLOCK time (``exit_clock``): the evaluation window
becomes ``SimWindow(entry_start, min(entry_end, exit), exit)`` and the generator restricts its entry bars to that
same effective window (``effective_window``), so generator and simulator always agree on what is enterable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from typing import Any, ClassVar

from alpha.fast.sim import SimWindow

TOD_CHOICES = ("all", "am", "pm")
EXIT_CLOCKS = ("flat", "close")


@dataclass(frozen=True)
class MarketCalendar:
    """Local-time session definition (all minutes are minutes of the LOCAL calendar day of ``tz``)."""

    tz: str = "Europe/Berlin"
    cash_open_min: int = 9 * 60
    cash_close_min: int = 17 * 60 + 30
    entry_start_min: int = 9 * 60
    entry_end_min: int = 20 * 60
    flat_min: int = 21 * 60 + 30
    # Lane Z (M3): the frozen, RESEARCH entry end.  Only the live overlay sets it (demo.opportunity.engine.live_family_calendar)
    # when it widened/cut ``entry_end_min`` for entry GATING; families whose semantics are defined by the window end (EOD:
    # T = min(cash_close, entry_end)) read ``semantic_entry_end_min`` so a live extension never moves their fitted window.
    research_entry_end_min: int | None = None

    def __post_init__(self) -> None:
        if not (0 <= self.cash_open_min < self.cash_close_min <= 1440):
            raise ValueError("cash session must satisfy 0 <= open < close <= 1440")
        if not (0 <= self.entry_start_min < self.entry_end_min <= self.flat_min <= 1440):
            raise ValueError("need 0 <= entry_start < entry_end <= flat <= 1440")

    @property
    def semantic_entry_end_min(self) -> int:
        return self.entry_end_min if self.research_entry_end_min is None else self.research_entry_end_min

    @classmethod
    def from_market_spec(cls, spec: Any) -> MarketCalendar:
        c = spec.calendar
        return cls(c.tz, c.cash_open_min, c.cash_close_min, c.entry_start_min, c.entry_end_min, c.forced_flat_min)

    def window(self) -> SimWindow:
        return SimWindow(self.entry_start_min, self.entry_end_min, self.flat_min)

    def session_calendar(self):  # -> alpha.session.SessionCalendar
        from alpha.session import SessionCalendar

        return SessionCalendar(
            name="family", tz=self.tz, cash_open_min=self.cash_open_min, cash_close_min=self.cash_close_min,
            entry_start_min=self.entry_start_min, entry_end_min=self.entry_end_min, flat_min=self.flat_min, buckets=(),
        )


@dataclass(frozen=True)
class EffectiveWindow:
    """Entry window [start, end) and clock exit of one spec on one market (local minutes)."""

    entry_start_min: int
    entry_end_min: int
    exit_min: int

    def sim_window(self) -> SimWindow:
        return SimWindow(self.entry_start_min, self.entry_end_min, self.exit_min)


def tod_bounds(entry_start: int, entry_end: int, tod: str) -> tuple[int, int]:
    """Time-of-day sub-window of the (effective) entry window: all / first half / second half (5-minute grid)."""
    if tod not in TOD_CHOICES:
        raise ValueError(f"tod must be one of {TOD_CHOICES}")
    mid = entry_start + ((entry_end - entry_start) // 2) // 5 * 5
    if tod == "am":
        return entry_start, mid
    if tod == "pm":
        return mid, entry_end
    return entry_start, entry_end


@dataclass(frozen=True)
class FamilySpec:
    """Base class; subclasses are frozen dataclasses with ``FAMILY`` and the parameters of the family."""

    FAMILY: ClassVar[str] = ""

    # ---- serialisation / identity -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"family": self.FAMILY, **asdict(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, allow_nan=False)

    def canonical(self) -> FamilySpec:
        """Same behaviour with every ignored parameter normalised (default: identity)."""
        return self

    def canonical_hash(self) -> str:
        payload = self.canonical().to_dict()
        return hashlib.sha256(("family-spec-v1:" + json.dumps(payload, sort_keys=True, allow_nan=False)).encode()).hexdigest()[:16]

    def validate(self) -> None:
        """Raise ``ValueError`` for an invalid parameter set (also called from ``__post_init__``)."""

    @property
    def complexity(self) -> int:
        """Clause-count style complexity (V1 fitness penalises 0.02 per unit): overridden per family."""
        return 3

    # ---- simulation window ---------------------------------------------------------------------
    def exit_min(self, cal: MarketCalendar) -> int:
        """Clock exit (local minute): default the forced flat; ``exit_clock == 'close'`` = min(cash close, flat)."""
        if getattr(self, "exit_clock", "flat") == "close":
            return min(cal.cash_close_min, cal.flat_min)
        return cal.flat_min

    def effective_window(self, cal: MarketCalendar) -> EffectiveWindow:
        ex = self.exit_min(cal)
        end = min(cal.entry_end_min, ex)
        if end <= cal.entry_start_min:
            raise ValueError("exit clock is not after the entry start")
        return EffectiveWindow(cal.entry_start_min, end, ex)

    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in fields(self))


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def thin_grid(specs: list[FamilySpec], max_n: int | None) -> list[FamilySpec]:
    """Deduplicate by canonical hash, order by hash (a deterministic pseudo-random order) and keep ``max_n``."""
    seen: dict[str, FamilySpec] = {}
    for s in specs:
        s = s.canonical()
        seen.setdefault(s.canonical_hash(), s)
    ordered = [seen[h] for h in sorted(seen)]
    return ordered if max_n is None else ordered[:max_n]


__all__ = (
    "EXIT_CLOCKS", "TOD_CHOICES", "EffectiveWindow", "FamilySpec", "MarketCalendar", "check", "thin_grid", "tod_bounds",
)
