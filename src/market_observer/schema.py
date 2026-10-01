# ruff: noqa: E501
"""Shared contract of the Market Structure Observer (OBSERVATION ONLY / SHADOW ONLY / NOT ALPHA VALIDATED).

Nothing in this package may change an entry, exit, stop, size, risk or execution decision. It only describes the market state AT DECISION TIME
and, separately and retrospectively, what happened afterwards.

Hard rules encoded here (and tested in tests/unit/market_observer/test_schema.py):

1. DECISION_FEATURES vs POST_EVENT_LABELS are different types with DISJOINT column names (``f_<group>__<name>`` vs ``y_<name>``).
   A label can never be passed where a feature is expected.
2. Every decision feature is causal: any value whose name ends in ``_ts_ns`` (a timestamp) must be <= the decision timestamp.
3. Feature computations are pure functions of ``(ObserverBars, i)`` with ``i`` = index of the last CLOSED bar. PREFIX INVARIANCE:
   ``compute(bars, i) == compute(bars.prefix(i + 1), i)`` for every group and every i.
4. Unknown / not causally determinable => ``None`` (never back-filled).
5. Any change of a definition bumps the version of its group (and OBSERVER_VERSION): no silent semantic drift, no mixing of versions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import numpy as np

OBSERVER_VERSION = "market-structure-observer-v1"
SCHEMA_VERSION = "mso-schema-1"
# one version per feature group; bump on ANY semantic change of that group
GROUP_VERSIONS: dict[str, str] = {
    "levels": "mso-levels-1",
    "swings": "mso-swings-1",
    "acceptance": "mso-acceptance-1",
    "participation": "mso-participation-1",
    "balance": "mso-balance-1",
    "fib": "mso-fib-1",  # experimental (Lane E), placebo-tested before any use
}
STATUS = "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED"

JsonScalar = float | int | str | bool | None


# ---------------------------------------------------------------------------------------------- input contract
@dataclass(frozen=True)
class SessionSpec:
    """Market-local session facts the adapter supplies (never guessed inside the observer)."""

    tz: str  # IANA zone of the market calendar
    cash_open_min: int | None  # local minute-of-day of the session open (None = no defensible open, e.g. 24h crypto)
    cash_close_min: int | None


@dataclass(frozen=True)
class ObserverBars:
    """Closed-bar arrays of ONE market (bar OPEN timestamps, ascending). All arrays share one length.

    ``atr`` and ``segment_id`` must already be causal (value at index j uses bars <= j only). ``segment_id`` increases at every break of the
    data (broker pause, weekend, missing bars): structures must not be built across a segment boundary.
    """

    market: str
    ts_ns: np.ndarray  # int64, UTC ns of the bar open
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741 (open/high/low/close convention of the repo)
    c: np.ndarray
    tick_volume: np.ndarray  # raw MT5 tick count per bar (NOT exchange volume); NaN if unavailable
    spread: np.ndarray  # price units
    atr: np.ndarray  # ATR (14) in price units, causal; NaN during warm-up
    segment_id: np.ndarray  # int64, +1 at each contiguity break
    local_minute: np.ndarray  # int64 market-local minute-of-day of the bar open
    local_day: np.ndarray  # int64 market-local trading date ordinal
    tick_size: float
    session: SessionSpec
    bar_seconds: int = 300

    def __len__(self) -> int:
        return len(self.ts_ns)

    def prefix(self, n: int) -> ObserverBars:
        """The first ``n`` bars: the ONLY object a prefix-invariance test may compare against."""
        sl = slice(0, n)
        return ObserverBars(
            self.market, self.ts_ns[sl], self.o[sl], self.h[sl], self.l[sl], self.c[sl], self.tick_volume[sl], self.spread[sl],
            self.atr[sl], self.segment_id[sl], self.local_minute[sl], self.local_day[sl], self.tick_size, self.session, self.bar_seconds,
        )

    def validate(self) -> None:
        n = len(self)
        for name in ("o", "h", "l", "c", "tick_volume", "spread", "atr", "segment_id", "local_minute", "local_day"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"ObserverBars.{name}: length {len(getattr(self, name))} != {n}")
        if n > 1 and bool(np.any(np.diff(self.ts_ns) <= 0)):
            raise ValueError("ObserverBars.ts_ns must be strictly ascending")

    def decision_ts_ns(self, i: int) -> int:
        """Decision time of bar ``i``: its CLOSE (open + one bar). Nothing later may influence a feature at ``i``."""
        return int(self.ts_ns[i]) + self.bar_seconds * 1_000_000_000


# ---------------------------------------------------------------------------------------------- enums
class LevelSource(StrEnum):
    SWING_M5 = "SWING_M5"
    SWING_M15 = "SWING_M15"
    PREV_DAY_HIGH = "PREV_DAY_HIGH"
    PREV_DAY_LOW = "PREV_DAY_LOW"
    PREV_DAY_CLOSE = "PREV_DAY_CLOSE"
    SESSION_HIGH = "SESSION_HIGH"
    SESSION_LOW = "SESSION_LOW"
    OVERNIGHT_HIGH = "OVERNIGHT_HIGH"
    OVERNIGHT_LOW = "OVERNIGHT_LOW"
    ORB_HIGH = "ORB_HIGH"
    ORB_LOW = "ORB_LOW"
    STRUCT_RANGE_HIGH = "STRUCT_RANGE_HIGH"
    STRUCT_RANGE_LOW = "STRUCT_RANGE_LOW"
    ROUND_MAJOR = "ROUND_MAJOR"
    ROUND_MINOR = "ROUND_MINOR"
    VWAP_PROXY = "VWAP_PROXY"


class LevelRole(StrEnum):
    UNCLASSIFIED = "UNCLASSIFIED"
    SUPPORT = "SUPPORT"
    RESISTANCE = "RESISTANCE"
    BROKEN_UP = "BROKEN_UP"
    BROKEN_DOWN = "BROKEN_DOWN"
    ACCEPTED_ABOVE = "ACCEPTED_ABOVE"
    ACCEPTED_BELOW = "ACCEPTED_BELOW"
    RECLAIMED_FROM_ABOVE = "RECLAIMED_FROM_ABOVE"
    RECLAIMED_FROM_BELOW = "RECLAIMED_FROM_BELOW"
    FLIPPED_TO_SUPPORT = "FLIPPED_TO_SUPPORT"
    FLIPPED_TO_RESISTANCE = "FLIPPED_TO_RESISTANCE"


class SwingLabel(StrEnum):
    HH = "HH"
    LH = "LH"
    HL = "HL"
    LL = "LL"
    EQ = "EQ"  # equal within tolerance (tolerance is a documented constant of the swings group)


class SwingSequence(StrEnum):
    UP_SEQUENCE = "UP_SEQUENCE"
    DOWN_SEQUENCE = "DOWN_SEQUENCE"
    MIXED_TRANSITION = "MIXED_TRANSITION"
    RANGE_OR_UNDEFINED = "RANGE_OR_UNDEFINED"


# ---------------------------------------------------------------------------------------------- shared small contracts
@dataclass(frozen=True)
class LevelRef:
    """A level handed from the level group to the acceptance group (explicit interface; no hidden coupling)."""

    level_id: str
    price: float
    zone_low: float
    zone_high: float
    sources: tuple[str, ...]  # LevelSource values in the cluster
    created_at_ts_ns: int
    confirmed_at_ts_ns: int


@dataclass(frozen=True)
class FeatureResult:
    """Output of one feature group at one decision bar (names WITHOUT the f_<group>__ prefix)."""

    group: str
    version: str
    values: Mapping[str, JsonScalar]


class FeatureGroup(Protocol):
    """Pure, prefix-invariant feature computation."""

    name: str

    def compute(self, bars: ObserverBars, i: int, **inputs: Any) -> FeatureResult: ...


# ---------------------------------------------------------------------------------------------- column naming
FEATURE_PREFIX = "f_"
LABEL_PREFIX = "y_"
TS_SUFFIX = "_ts_ns"


def feature_key(group: str, name: str) -> str:
    if group not in GROUP_VERSIONS:
        raise ValueError(f"unknown feature group {group!r}")
    if "__" in name:
        raise ValueError("feature names must not contain '__'")
    return f"{FEATURE_PREFIX}{group}__{name}"


def label_key(name: str) -> str:
    return f"{LABEL_PREFIX}{name}"


# ---------------------------------------------------------------------------------------------- decision features / labels
class CausalityError(ValueError):
    pass


@dataclass(frozen=True)
class DecisionFeatures:
    """Everything known AT the decision bar (``decision_ts_ns``). Immutable; flat ``f_<group>__<name>`` columns."""

    decision_ts_ns: int
    columns: Mapping[str, JsonScalar]
    versions: Mapping[str, str]

    def __post_init__(self) -> None:
        for k, v in self.columns.items():
            if not k.startswith(FEATURE_PREFIX) or "__" not in k:
                raise ValueError(f"not a feature column: {k!r}")
            if k.startswith(LABEL_PREFIX):
                raise ValueError(f"a label column cannot be a decision feature: {k!r}")
            if k.endswith(TS_SUFFIX) and v is not None and int(v) > self.decision_ts_ns:  # type: ignore[arg-type]
                raise CausalityError(f"{k}={v} is later than the decision time {self.decision_ts_ns}")

    @staticmethod
    def from_results(decision_ts_ns: int, results: list[FeatureResult]) -> DecisionFeatures:
        cols: dict[str, JsonScalar] = {}
        vers: dict[str, str] = {}
        for r in results:
            vers[r.group] = r.version
            for name, value in r.values.items():
                key = feature_key(r.group, name)
                if key in cols:
                    raise ValueError(f"duplicate feature column {key}")
                cols[key] = value
        return DecisionFeatures(int(decision_ts_ns), cols, vers)


@dataclass(frozen=True)
class PostEventLabels:
    """RETROSPECTIVE outcome of an event (first-passage and excursion labels). NEVER an input to a decision feature."""

    horizon_end_ts_ns: int
    columns: Mapping[str, JsonScalar]  # y_<name>

    def __post_init__(self) -> None:
        for k in self.columns:
            if not k.startswith(LABEL_PREFIX):
                raise ValueError(f"not a label column: {k!r}")
            if k.startswith(FEATURE_PREFIX):
                raise ValueError(f"a feature column cannot be a label: {k!r}")


# first-passage label family (R multiples of the event's own initial risk; adverse side is part of the name)
FIRST_PASSAGE_LABELS: tuple[tuple[float, float], ...] = ((0.25, 0.25), (0.50, 0.50), (0.75, 0.50), (1.00, 0.50))


def first_passage_label_name(fav_r: float, adv_r: float) -> str:
    return f"fav{round(fav_r * 100):03d}_before_adv{round(adv_r * 100):03d}"


EXCURSION_LABELS: tuple[str, ...] = (
    "mfe_r", "mae_r", "time_to_mfe_s", "time_to_mae_s", "mfe_before_mae", "post_stop_favorable_excursion_r",
    "structural_target_reached", "reclaim_or_followthrough",
)


def all_label_columns() -> tuple[str, ...]:
    cols = [label_key(first_passage_label_name(a, b)) for a, b in FIRST_PASSAGE_LABELS]
    cols += [label_key(x) for x in EXCURSION_LABELS]
    return tuple(cols)


# ---------------------------------------------------------------------------------------------- the stored record
@dataclass(frozen=True)
class ObserverRecord:
    """One observed event/opportunity (or matched control). ``labels`` stay None in the live path (filled retrospectively)."""

    event_id: str
    market: str
    family: str | None
    variant: str | None
    direction: int | None  # +1 / -1 / None for non-directional controls
    is_control: bool
    control_of: str | None  # event_id the control matches
    features: DecisionFeatures
    labels: PostEventLabels | None = None
    observer_version: str = OBSERVER_VERSION
    schema_version: str = SCHEMA_VERSION
    status: str = STATUS
    meta: Mapping[str, JsonScalar] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "event_id": self.event_id, "market": self.market, "family": self.family, "variant": self.variant, "direction": self.direction,
            "is_control": self.is_control, "control_of": self.control_of, "decision_ts_ns": self.features.decision_ts_ns,
            "observer_version": self.observer_version, "schema_version": self.schema_version, "status": self.status,
        }
        row.update({f"v_{g}": v for g, v in sorted(self.features.versions.items())})
        row.update(self.features.columns)
        if self.labels is not None:
            row["horizon_end_ts_ns"] = self.labels.horizon_end_ts_ns
            row.update(self.labels.columns)
        row.update({f"m_{k}": v for k, v in self.meta.items()})
        return row


def assert_disjoint(feature_cols: set[str], label_cols: set[str]) -> None:
    both = feature_cols & label_cols
    if both:
        raise ValueError(f"feature and label columns overlap: {sorted(both)[:5]}")
    if any(not c.startswith(FEATURE_PREFIX) for c in feature_cols) or any(not c.startswith(LABEL_PREFIX) for c in label_cols):
        raise ValueError("column prefix rule violated")
