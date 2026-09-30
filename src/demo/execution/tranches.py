# ruff: noqa: E501
"""Tranche ledger: intents -> tranche records -> broker net position (pure data model).

BROKER vs INTERNAL. The RETAIL_NETTING broker represents exactly ONE net position per symbol; that
is a property of the broker's position representation and nothing else. Internally the unit of
account is the TRANCHE: one record per intent_id (entry quantity / price / stop / target / risk /
cluster / family). Several tranches may contribute to the same net position provided the
portfolio, cluster, leverage, margin and safety limits hold on the COMBINED exposure. The ledger
aggregates tranches into net positions and computes the risk numbers the sizer and the concentration
logging need.

v1 EXECUTION LIMITATION (not a risk rule): MT5 keeps ONE position-level SL/TP for the whole net
position, so two tranches with different structural stops cannot each be broker-protected. v1 opens
at most one tranche per symbol. ``classify_addon`` implements the precise, conservative test that
decides whether a NEW same-direction intent could share the existing net position's protective stop
(``SHARED_STOP_POSSIBLE``, permitted in principle but not yet implemented) or would need independent
stops (``INDEPENDENT_STOPS_NEEDED``); an opposite-side intent is ``OPPOSITE_SIDE`` (reduction /
reversal is out of scope in v1). Every stop decision is conservative: an existing stop is NEVER
widened, and tightening beyond the stated tolerance counts as "independent stops needed".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal(0)
UNKNOWN_FAMILY = "UNKNOWN"


@dataclass(frozen=True, slots=True, kw_only=True)
class Tranche:
    """One intent's contribution to a net position."""

    intent_id: str
    market: str
    cluster: str
    family: str
    direction: int  # +1 long / -1 short
    quantity: Decimal  # lots
    entry_price: Decimal
    stop: Decimal  # the tranche's STRUCTURAL stop (never modified)
    target: Decimal | None
    risk_money: Decimal  # account currency, quantity x contract x |entry - stop| x fx
    opened_utc: str | None = None


@dataclass(frozen=True, slots=True)
class NetPosition:
    """The broker-side net position of one symbol, with its contributing tranches."""

    market: str
    tranches: tuple[Tranche, ...]

    @property
    def direction(self) -> int:
        return 0 if not self.tranches else self.tranches[0].direction

    @property
    def net_quantity(self) -> Decimal:
        return sum((t.quantity * t.direction for t in self.tranches), ZERO)

    @property
    def average_entry(self) -> Decimal | None:
        volume = sum((t.quantity for t in self.tranches), ZERO)
        if volume == 0:
            return None
        return sum((t.quantity * t.entry_price for t in self.tranches), ZERO) / volume

    @property
    def risk_money(self) -> Decimal:
        return sum((t.risk_money for t in self.tranches), ZERO)

    def single_stop_protecting_all(self) -> Decimal | None:
        """The ONE stop that keeps every tranche's invalidation protected: the tightest structural
        stop (long: highest, short: lowest). Tighter than a tranche's own stop => that tranche is
        stopped out early, which is exactly why v1 does not add to a net position."""
        if not self.tranches:
            return None
        stops = [t.stop for t in self.tranches]
        return max(stops) if self.direction == 1 else min(stops)


class AddonClass(StrEnum):
    SHARED_STOP_POSSIBLE = "SHARED_STOP_POSSIBLE"
    INDEPENDENT_STOPS_NEEDED = "INDEPENDENT_STOPS_NEEDED"
    OPPOSITE_SIDE = "OPPOSITE_SIDE"


@dataclass(frozen=True, slots=True, kw_only=True)
class AddonAssessment:
    classification: AddonClass
    why: str
    tightening_fraction: Decimal | None = None  # (shared - new) / new stop distance, long-signed


DEFAULT_MAX_SHARED_STOP_TIGHTENING = Decimal("0.10")


def classify_addon(
    *,
    existing_direction: int,
    existing_stop: Decimal | None,
    existing_target: Decimal | None,
    new_direction: int,
    new_stop: Decimal,
    new_target: Decimal | None,
    executable_price: Decimal,
    tick_size: Decimal,
    max_tightening_fraction: Decimal = DEFAULT_MAX_SHARED_STOP_TIGHTENING,
) -> AddonAssessment:
    """Could ``new`` share the EXISTING net position's broker stop (and target)?

    Conservative definition (long shown; short mirrors with signs flipped). Let ``S_e`` be the
    existing broker stop, ``S_n`` the new structural stop, ``d_n = executable - S_n``:

    1. ``S_e`` must exist (the position is protected) and sit on the protective side of the market;
    2. NOT LOOSER:  ``S_e >= S_n`` (within half a tick): the shared stop protects the new tranche's
       invalidation at least as tightly as the new setup needs; a looser stop would let the new
       tranche lose more than its structural risk;
    3. NOT MATERIALLY TIGHTER: ``(S_e - S_n) <= max_tightening_fraction x d_n``: a much tighter
       shared stop would stop the new tranche out long before its thesis is invalidated, i.e. it
       would be a different trade;
    4. TARGETS IDENTICAL (within half a tick, both absent counts): a single position-level TP cannot
       serve two different exits.

    Anything else needs independent stops. The existing stop is never widened.
    """
    if new_direction != existing_direction:
        return AddonAssessment(
            classification=AddonClass.OPPOSITE_SIDE,
            why="opposite-side intent would reduce/reverse the net position",
        )
    half = tick_size / 2
    sign = Decimal(existing_direction)
    if existing_stop is None or existing_stop <= 0:
        return AddonAssessment(
            classification=AddonClass.INDEPENDENT_STOPS_NEEDED,
            why="existing net position has no confirmed broker stop",
        )
    if (executable_price - existing_stop) * sign <= 0:
        return AddonAssessment(
            classification=AddonClass.INDEPENDENT_STOPS_NEEDED,
            why="existing stop is not on the protective side of the market",
        )
    new_distance = (executable_price - new_stop) * sign
    if new_distance <= 0:
        return AddonAssessment(
            classification=AddonClass.INDEPENDENT_STOPS_NEEDED,
            why="new structural stop is not on the protective side of the market",
        )
    tightening = (existing_stop - new_stop) * sign  # > 0: shared stop is tighter than needed
    fraction = tightening / new_distance
    if tightening < -half:
        return AddonAssessment(
            classification=AddonClass.INDEPENDENT_STOPS_NEEDED,
            why="shared stop is LOOSER than the new structural stop (never widen / never accept more risk)",
            tightening_fraction=fraction,
        )
    if tightening > max_tightening_fraction * new_distance + half:
        return AddonAssessment(
            classification=AddonClass.INDEPENDENT_STOPS_NEEDED,
            why="shared stop is materially tighter than the new structural stop: different trade",
            tightening_fraction=fraction,
        )
    if (existing_target is None) != (new_target is None) or (
        existing_target is not None
        and new_target is not None
        and abs(existing_target - new_target) > half
    ):
        return AddonAssessment(
            classification=AddonClass.INDEPENDENT_STOPS_NEEDED,
            why="exits differ: one position-level TP cannot serve two different targets",
            tightening_fraction=fraction,
        )
    return AddonAssessment(
        classification=AddonClass.SHARED_STOP_POSSIBLE,
        why="existing broker stop protects the new invalidation at least as tightly (within tolerance)",
        tightening_fraction=fraction,
    )


@dataclass(slots=True)
class TrancheLedger:
    """In-memory ledger keyed by intent_id; aggregation helpers for risk math and logging."""

    _tranches: dict[str, Tranche] = field(default_factory=dict)

    @classmethod
    def of(cls, tranches: Iterable[Tranche]) -> TrancheLedger:
        ledger = cls()
        for tranche in tranches:
            ledger.add(tranche)
        return ledger

    def add(self, tranche: Tranche) -> None:
        if tranche.intent_id in self._tranches:
            raise ValueError(f"tranche {tranche.intent_id} already in the ledger")
        self._tranches[tranche.intent_id] = tranche

    def close(self, intent_id: str) -> Tranche | None:
        return self._tranches.pop(intent_id, None)

    def tranches(self) -> tuple[Tranche, ...]:
        return tuple(self._tranches.values())

    def net_position(self, market: str) -> NetPosition | None:
        mine = tuple(t for t in self._tranches.values() if t.market == market)
        return NetPosition(market=market, tranches=mine) if mine else None

    def net_positions(self) -> dict[str, NetPosition]:
        markets = dict.fromkeys(t.market for t in self._tranches.values())
        return {m: p for m in markets if (p := self.net_position(m)) is not None}

    def total_risk(self) -> Decimal:
        return sum((t.risk_money for t in self._tranches.values()), ZERO)

    def _by(self, key: str) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for t in self._tranches.values():
            out[getattr(t, key)] += t.risk_money
        return dict(out)

    def risk_by_market(self) -> dict[str, Decimal]:
        return self._by("market")

    def risk_by_cluster(self) -> dict[str, Decimal]:
        return self._by("cluster")

    def risk_by_family(self) -> dict[str, Decimal]:
        return self._by("family")

    def shares(self) -> Mapping[str, Mapping[str, Decimal]]:
        """Share of the aggregate open stop-risk by market / cluster / family (0 when flat)."""
        total = self.total_risk()
        result: dict[str, dict[str, Decimal]] = {}
        for name, book in (
            ("market", self.risk_by_market()),
            ("cluster", self.risk_by_cluster()),
            ("family", self.risk_by_family()),
        ):
            result[name] = {k: (v / total if total > 0 else ZERO) for k, v in book.items()}
        return result
