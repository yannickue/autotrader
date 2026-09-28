"""Data quality checks for ingested bar/tick record batches.

Standalone: no dependency on `src/adapters`, `src/pipeline`, `src/risk`, or
`src/execution`, and no venue I/O.

This module only DETECTS and REPORTS violations -- it never silently
"fixes", drops, or reorders a questionable record. Callers decide what to do
with a `QualityReport` (e.g. quarantine the batch, alert an operator, or
still ingest with `quality_flags` attached).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from data.parquet_store import BarRecord, TickRecord


class ViolationType(StrEnum):
    """Kinds of data quality problems this module can detect."""

    NON_MONOTONIC_TIMESTAMP = "NON_MONOTONIC_TIMESTAMP"
    DUPLICATE_RECORD = "DUPLICATE_RECORD"
    GAP_DETECTED = "GAP_DETECTED"
    STALE_DATA = "STALE_DATA"
    ZERO_OR_NEGATIVE_PRICE = "ZERO_OR_NEGATIVE_PRICE"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    CROSSED_QUOTE = "CROSSED_QUOTE"
    ABSURD_SPREAD = "ABSURD_SPREAD"
    MALFORMED_VOLUME = "MALFORMED_VOLUME"


@dataclass(frozen=True, slots=True, kw_only=True)
class Violation:
    """One detected problem, with enough detail to act on."""

    violation_type: ViolationType
    index: int | None
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class QualityReport:
    """All violations found in a checked batch."""

    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def is_clean(self) -> bool:
        return len(self.violations) == 0

    def of_type(self, violation_type: ViolationType) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.violation_type is violation_type)


@dataclass(frozen=True, slots=True, kw_only=True)
class QualityConfig:
    """Thresholds controlling gap/stale/spread detection."""

    max_gap: timedelta = timedelta(minutes=5)
    max_age: timedelta = timedelta(minutes=5)
    max_spread_ratio: Decimal = Decimal("0.05")  # 5% of mid price


def _is_finite(value: Decimal | None) -> bool:
    return value is not None and value.is_finite()


def _check_prices(
    index: int, prices: dict[str, Decimal | None], violations: list[Violation]
) -> None:
    for name, value in prices.items():
        if value is None:
            continue
        if not value.is_finite():
            violations.append(
                Violation(
                    violation_type=ViolationType.NON_FINITE_VALUE,
                    index=index,
                    detail=f"{name} is not finite: {value}",
                )
            )
        elif value <= 0:
            violations.append(
                Violation(
                    violation_type=ViolationType.ZERO_OR_NEGATIVE_PRICE,
                    index=index,
                    detail=f"{name} must be positive, got {value}",
                )
            )


def _check_volume(index: int, volume: Decimal | None, violations: list[Violation]) -> None:
    if volume is None:
        violations.append(
            Violation(
                violation_type=ViolationType.MALFORMED_VOLUME,
                index=index,
                detail="volume is missing",
            )
        )
        return
    if not volume.is_finite():
        violations.append(
            Violation(
                violation_type=ViolationType.MALFORMED_VOLUME,
                index=index,
                detail=f"volume is not finite: {volume}",
            )
        )
    elif volume < 0:
        violations.append(
            Violation(
                violation_type=ViolationType.MALFORMED_VOLUME,
                index=index,
                detail=f"volume must be non-negative, got {volume}",
            )
        )


def _check_timestamps(
    records: list, violations: list[Violation], config: QualityConfig, now: datetime | None
) -> None:
    seen: dict[datetime, int] = {}
    prev_ts: datetime | None = None
    prev_index: int | None = None

    for index, record in enumerate(records):
        ts = record.timestamp

        if ts in seen:
            violations.append(
                Violation(
                    violation_type=ViolationType.DUPLICATE_RECORD,
                    index=index,
                    detail=f"duplicate timestamp {ts.isoformat()} (first seen at index {seen[ts]})",
                )
            )
        else:
            seen[ts] = index

        if prev_ts is not None:
            if ts < prev_ts:
                violations.append(
                    Violation(
                        violation_type=ViolationType.NON_MONOTONIC_TIMESTAMP,
                        index=index,
                        detail=(
                            f"timestamp {ts.isoformat()} is before previous "
                            f"{prev_ts.isoformat()} (index {prev_index})"
                        ),
                    )
                )
            elif ts - prev_ts > config.max_gap:
                violations.append(
                    Violation(
                        violation_type=ViolationType.GAP_DETECTED,
                        index=index,
                        detail=(
                            f"gap of {ts - prev_ts} between index {prev_index} "
                            f"({prev_ts.isoformat()}) and index {index} ({ts.isoformat()}), "
                            f"exceeds max_gap={config.max_gap}"
                        ),
                    )
                )

        prev_ts = ts
        prev_index = index

    if now is not None and records:
        latest = max(r.timestamp for r in records)
        age = now - latest
        if age > config.max_age:
            violations.append(
                Violation(
                    violation_type=ViolationType.STALE_DATA,
                    index=None,
                    detail=(
                        f"latest timestamp {latest.isoformat()} is {age} old, "
                        f"exceeds max_age={config.max_age} (now={now.isoformat()})"
                    ),
                )
            )


def check_ticks(
    records: list[TickRecord],
    *,
    config: QualityConfig | None = None,
    now: datetime | None = None,
) -> QualityReport:
    """Validate a batch of ticks. Detection only -- never mutates `records`."""
    config = config or QualityConfig()
    violations: list[Violation] = []

    _check_timestamps(records, violations, config, now)

    for index, record in enumerate(records):
        _check_prices(
            index,
            {"bid": record.bid, "ask": record.ask, "last": record.last},
            violations,
        )
        _check_volume(index, record.volume, violations)

        if _is_finite(record.bid) and _is_finite(record.ask):
            if record.bid > record.ask:
                violations.append(
                    Violation(
                        violation_type=ViolationType.CROSSED_QUOTE,
                        index=index,
                        detail=f"bid {record.bid} > ask {record.ask}",
                    )
                )
            else:
                mid = (record.bid + record.ask) / 2
                if mid > 0:
                    spread_ratio = (record.ask - record.bid) / mid
                    if spread_ratio > config.max_spread_ratio:
                        violations.append(
                            Violation(
                                violation_type=ViolationType.ABSURD_SPREAD,
                                index=index,
                                detail=(
                                    f"spread ratio {spread_ratio} exceeds "
                                    f"max_spread_ratio={config.max_spread_ratio} "
                                    f"(bid={record.bid}, ask={record.ask})"
                                ),
                            )
                        )

    return QualityReport(violations=tuple(violations))


def check_bars(
    records: list[BarRecord],
    *,
    config: QualityConfig | None = None,
    now: datetime | None = None,
) -> QualityReport:
    """Validate a batch of bars. Detection only -- never mutates `records`."""
    config = config or QualityConfig()
    violations: list[Violation] = []

    _check_timestamps(records, violations, config, now)

    for index, record in enumerate(records):
        _check_prices(
            index,
            {
                "open": record.open,
                "high": record.high,
                "low": record.low,
                "close": record.close,
            },
            violations,
        )
        _check_volume(index, record.volume, violations)

        if all(_is_finite(v) for v in (record.open, record.high, record.low, record.close)):
            if record.high < record.low:
                violations.append(
                    Violation(
                        violation_type=ViolationType.CROSSED_QUOTE,
                        index=index,
                        detail=f"high {record.high} < low {record.low}",
                    )
                )
            elif not (record.low <= record.open <= record.high):
                violations.append(
                    Violation(
                        violation_type=ViolationType.CROSSED_QUOTE,
                        index=index,
                        detail=(
                            f"open {record.open} outside [low={record.low}, high={record.high}]"
                        ),
                    )
                )
            elif not (record.low <= record.close <= record.high):
                violations.append(
                    Violation(
                        violation_type=ViolationType.CROSSED_QUOTE,
                        index=index,
                        detail=(
                            f"close {record.close} outside [low={record.low}, high={record.high}]"
                        ),
                    )
                )

    return QualityReport(violations=tuple(violations))
