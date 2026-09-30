# ruff: noqa: E501
"""Cumulative trial ledger: every examined cell of the raw scan is a trial.

The V1 cumulative counts are carried as a HEADER only (they are not added to and are not part of the
V2 raw-scan multiplicity); the raw scan's own counts are kept per market and family.
"""

from __future__ import annotations

from collections import defaultdict

V1_CUMULATIVE_TRIALS = 30310
V1_CUMULATIVE_UNIQUE = 29798


class TrialLedger:
    def __init__(self) -> None:
        self._total: dict[tuple[str, str], int] = defaultdict(int)
        self._unique: dict[tuple[str, str], int] = defaultdict(int)

    def add(self, market: str, family: str, n_total: int, n_unique: int | None = None) -> None:
        if n_total < 0 or (n_unique is not None and not 0 <= n_unique <= n_total):
            raise ValueError("bad trial counts")
        self._total[(market, family)] += int(n_total)
        self._unique[(market, family)] += int(n_total if n_unique is None else n_unique)

    def add_table(self, market: str, table, duplicate_cells: int = 0) -> None:
        """Count every row of a cell table (+ horizon-duplicate cells skipped as identical trades)."""
        for fam, n in table.groupby("family").size().items():
            self.add(market, str(fam), int(n), int(n))
        if duplicate_cells:
            self.add(market, "horizon_duplicates", int(duplicate_cells), 0)

    @property
    def total(self) -> int:
        return sum(self._total.values())

    @property
    def unique(self) -> int:
        return sum(self._unique.values())

    def per_market(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = defaultdict(lambda: {"trials": 0, "unique": 0})
        for (m, _f), v in self._total.items():
            out[m]["trials"] += v
        for (m, _f), v in self._unique.items():
            out[m]["unique"] += v
        return dict(out)

    def to_dict(self) -> dict:
        fam: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
        for (m, f), v in self._total.items():
            fam[m][f] = {"trials": v, "unique": self._unique[(m, f)]}
        return {
            "v1_cumulative_header_only": {
                "trials": V1_CUMULATIVE_TRIALS, "unique": V1_CUMULATIVE_UNIQUE,
            },
            "rawscan_trials": self.total,
            "rawscan_unique": self.unique,
            "per_market": self.per_market(),
            "per_market_family": dict(fam),
        }
