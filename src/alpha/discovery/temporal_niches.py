# ruff: noqa: E501
"""Quality-diversity archive (MAP-Elites style) for the V2 temporal search (design section 5).

Niche key = (role-path signature, direction, tf-set bucket, trade-frequency bucket).  The archive keeps
``per_niche`` elites (default ONE) per niche ranked by TRAIN-only fitness (ties: canonical hash) and
detects behavioural twins: two canonically different genomes whose TRAIN decision stream hashes
equal are ONE behaviour, only the better one may hold an archive slot.  Selection pressure toward
novel niches is a novelty bonus that decays with how often a niche has been visited.

No Validation quantity is reachable here: entries carry a Train fitness and Train-derived numbers only.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

from alpha.discovery.temporal_genome import TemporalGenome, genome_tfs

FREQ_EDGES: tuple[float, ...] = (0.05, 0.2, 0.6)  # trades / Train day -> buckets 0..3

_EVENT_ROLE = {
    "ZONE_ENTER": "ZONE", "ZONE_EXIT": "ZEXIT", "SWEEP_LOW": "SWEEP", "RECLAIM_UP": "RECLAIM",
    "BOS_UP": "BOS", "CHOCH_UP": "CHOCH", "RETEST_HOLD_UP": "RETEST", "TOUCH": "TOUCH",
    "BREAK_UP": "BREAK", "BREAK_DN": "BREAKDN", "SWING_LOW_CONF": "SWING", "SWING_HIGH_CONF": "SWING",
    "PATTERN_COMPLETE": "PATTERN", "TRENDLINE_TOUCH": "TLTOUCH", "TRENDLINE_BREAK": "TLBREAK",
    "MOMENTUM_RESUME_UP": "MOMENTUM",
}


def clause_role(kind: str, name: str, op: str = "IS") -> str:
    if kind == "feature":
        return "FEAT"
    if kind == "state":
        return "PULLBACK" if op == "HOLD" else "TREND"
    return _EVENT_ROLE.get(name, name)


def role_path(g: TemporalGenome) -> str:
    """e.g. ``TREND+ZONE>SWEEP>RECLAIM>BOS>RETEST`` (anchor tokens sorted; direction not included)."""
    anchor = "+".join(sorted({clause_role(c.kind, c.name, c.op) for c in g.anchor}))
    steps = [clause_role(s.event.kind, s.event.name, s.event.op) for s in g.steps]
    return ">".join([anchor, *steps])


def freq_bucket(trades_per_day: float | None) -> int:
    if trades_per_day is None or not math.isfinite(trades_per_day):
        return 0
    return sum(trades_per_day >= e for e in FREQ_EDGES)


@dataclass(frozen=True)
class NicheKey:
    path: str
    direction: str
    tfs: str
    freq: int

    def label(self) -> str:
        return f"{self.path}|{self.direction}|{self.tfs}|f{self.freq}"


def niche_key(g: TemporalGenome, trades_per_day: float | None) -> NicheKey:
    return NicheKey(role_path(g), g.direction, "+".join(genome_tfs(g)), freq_bucket(trades_per_day))


@dataclass(frozen=True)
class Elite:
    key: NicheKey
    chash: str
    fitness: float
    twin_hash: str
    payload: Any = None  # caller data (e.g. the Candidate)

    def rank(self) -> tuple[float, str]:
        return (-self.fitness, self.chash)  # smaller is better


class NicheArchive:
    """One (or ``per_niche``) elite(s) per niche; twin-aware; counts niche visits for novelty."""

    def __init__(self, per_niche: int = 1) -> None:
        assert per_niche >= 1
        self.per_niche = per_niche
        self.cells: dict[NicheKey, list[Elite]] = {}
        self.twin_owner: dict[str, str] = {}  # twin_hash -> chash of the retained elite
        self.visits: Counter[NicheKey] = Counter()
        self.twins_rejected = 0
        self.twins_replaced = 0

    # -- bookkeeping ------------------------------------------------------------------------
    def visit(self, key: NicheKey) -> None:
        self.visits[key] += 1

    def occupancy(self, key: NicheKey) -> int:
        return len(self.cells.get(key, ()))

    def novelty(self, key: NicheKey, weight: float = 1.0) -> float:
        """Bonus in [0, weight]: 1 for a never-visited niche, decaying with visits."""
        return weight / math.sqrt(1 + self.visits.get(key, 0))

    @property
    def n_niches(self) -> int:
        return len(self.cells)

    def elites(self) -> list[Elite]:
        return sorted((e for cell in self.cells.values() for e in cell), key=Elite.rank)

    def find(self, chash: str) -> Elite | None:
        for cell in self.cells.values():
            for e in cell:
                if e.chash == chash:
                    return e
        return None

    def _remove(self, e: Elite) -> None:
        cell = self.cells[e.key]
        cell.remove(e)
        if not cell:
            del self.cells[e.key]
        if e.twin_hash and self.twin_owner.get(e.twin_hash) == e.chash:
            del self.twin_owner[e.twin_hash]

    # -- insertion --------------------------------------------------------------------------
    def insert(self, e: Elite) -> str:
        """'new_niche' | 'added' | 'improved' | 'kept' | 'rejected' | 'twin'."""
        if self.find(e.chash) is not None:
            return "kept"
        old: Elite | None = None
        if e.twin_hash:
            owner = self.twin_owner.get(e.twin_hash)
            if owner is not None and owner != e.chash:
                old = self.find(owner)
                if old is not None and old.rank() <= e.rank():
                    self.twins_rejected += 1
                    return "twin"
        eff = [x for x in self.cells.get(e.key, ()) if x is not old]
        if len(eff) >= self.per_niche and e.rank() >= eff[-1].rank():
            return "rejected"
        if old is not None:
            self._remove(old)
            self.twins_replaced += 1
        cell = self.cells.setdefault(e.key, [])
        status = "new_niche" if not cell else "added"
        if len(cell) >= self.per_niche:
            self._remove(cell[-1])
            cell = self.cells.setdefault(e.key, [])
            status = "improved"
        cell.append(e)
        cell.sort(key=Elite.rank)
        if e.twin_hash:
            self.twin_owner[e.twin_hash] = e.chash
        return status

    def stats(self) -> dict[str, Any]:
        el = self.elites()
        return {"niches": self.n_niches, "elites": len(el),
                "best": el[0].fitness if el else None, "twins_rejected": self.twins_rejected,
                "twins_replaced": self.twins_replaced, "visited_niches": len(self.visits)}


__all__ = ("FREQ_EDGES", "Elite", "NicheArchive", "NicheKey", "clause_role", "freq_bucket",
           "niche_key", "role_path")
