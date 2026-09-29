"""Historical-data validation for C3 (bars and ticks).

Builds on `data.quality` (duplicates, monotonicity, OHLC, non-positive and
non-finite prices, crossed quotes) and adds what a broker CFD history needs:
off-grid bars, gap classification (expected session gap vs suspicious intraday
gap), spread anomalies, and tick jumps. Detection only -- nothing is repaired,
dropped, or synthesized. Missing prices are never filled.

Gap policy (practical, not a trading-calendar): a gap whose two ends fall on
different SERVER-clock days, starts at/after `session_close_from` and ends at/
before `session_open_until` (or spans only Saturday/Sunday) is an EXPECTED
session gap. Everything else above `max_intraday_gap_bars` bars is SUSPICIOUS
(holidays therefore surface as suspicious -- reviewed, not hidden).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from data.parquet_store import BarRecord, TickRecord
from data.quality import (
    QualityConfig,
    QualityReport,
    Violation,
    ViolationType,
    check_bars,
    check_ticks,
)


class HistoricalIssue(StrEnum):
    OFF_GRID_BAR = "OFF_GRID_BAR"
    SUSPICIOUS_GAP = "SUSPICIOUS_GAP"
    SPREAD_ANOMALY = "SPREAD_ANOMALY"
    NEGATIVE_SPREAD = "NEGATIVE_SPREAD"
    TICK_JUMP = "TICK_JUMP"
    EMPTY_DATASET = "EMPTY_DATASET"
    MIXED_SYMBOLS = "MIXED_SYMBOLS"


class ValidationStatus(StrEnum):
    PASSED = "PASSED"
    PASSED_WITH_WARNINGS = "PASSED_WITH_WARNINGS"
    FAILED = "FAILED"


# Hard failures: dataset must not be written/used.
_HARD_BASE = {
    ViolationType.NON_MONOTONIC_TIMESTAMP,
    ViolationType.DUPLICATE_RECORD,
    ViolationType.ZERO_OR_NEGATIVE_PRICE,
    ViolationType.NON_FINITE_VALUE,
    ViolationType.CROSSED_QUOTE,
    ViolationType.MALFORMED_VOLUME,
}
_HARD_EXTRA = {
    HistoricalIssue.OFF_GRID_BAR,
    HistoricalIssue.NEGATIVE_SPREAD,
    HistoricalIssue.EMPTY_DATASET,
    HistoricalIssue.MIXED_SYMBOLS,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalQualityConfig:
    max_intraday_gap_bars: int = 5
    server_zone: str = "Europe/Berlin"
    session_close_from: time = time(21, 0)
    session_open_until: time = time(3, 0)
    spread_outlier_factor: Decimal = Decimal("10")
    tick_jump_ratio: Decimal = Decimal("0.01")  # 1% mid move between consecutive ticks
    max_tick_spread_ratio: Decimal = Decimal("0.01")


@dataclass(frozen=True, slots=True, kw_only=True)
class GapInfo:
    start: datetime
    end: datetime
    expected: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalQualityReport:
    base: QualityReport
    issues: tuple[Violation, ...] = ()
    expected_gaps: int = 0
    suspicious_gaps: tuple[GapInfo, ...] = ()
    row_count: int = 0
    status: ValidationStatus = ValidationStatus.PASSED
    hard_failures: int = 0
    warnings: int = 0
    summary: dict[str, int] = field(default_factory=dict)


def _classify(base: QualityReport, issues: list[Violation]) -> tuple[ValidationStatus, int, int]:
    hard = sum(1 for v in base.violations if v.violation_type in _HARD_BASE)
    hard += sum(1 for v in issues if v.violation_type in _HARD_EXTRA)  # type: ignore[comparison-overlap]
    total = len(base.violations) + len(issues)
    warnings = total - hard
    if hard:
        return ValidationStatus.FAILED, hard, warnings
    if warnings:
        return ValidationStatus.PASSED_WITH_WARNINGS, 0, warnings
    return ValidationStatus.PASSED, 0, 0


def _issue(kind: HistoricalIssue, index: int | None, detail: str) -> Violation:
    return Violation(violation_type=kind, index=index, detail=detail)  # type: ignore[arg-type]


def _is_expected_gap(prev: datetime, cur: datetime, cfg: HistoricalQualityConfig) -> bool:
    zone = ZoneInfo(cfg.server_zone)
    a = prev.astimezone(zone)
    b = cur.astimezone(zone)
    if a.date() == b.date():
        return False
    if a.timetz().replace(tzinfo=None) < cfg.session_close_from:
        return False
    if b.timetz().replace(tzinfo=None) > cfg.session_open_until:
        return False
    # Days strictly between must be only Saturday/Sunday (weekday() 5/6).
    day = a.date() + timedelta(days=1)
    while day < b.date():
        if day.weekday() < 5:
            return False
        day += timedelta(days=1)
    return True


def validate_bars(
    bars: list[BarRecord],
    *,
    timeframe_seconds: int,
    config: HistoricalQualityConfig | None = None,
) -> HistoricalQualityReport:
    cfg = config or HistoricalQualityConfig()
    issues: list[Violation] = []
    if not bars:
        base = QualityReport()
        issues.append(_issue(HistoricalIssue.EMPTY_DATASET, None, "no bars"))
        status, hard, warn = _classify(base, issues)
        return HistoricalQualityReport(
            base=base, issues=tuple(issues), status=status, hard_failures=hard, warnings=warn
        )

    # Base checks with gap detection disabled (handled below with session awareness).
    base = check_bars(bars, config=QualityConfig(max_gap=timedelta(days=3650)))

    if len({(b.canonical_symbol, b.broker_symbol, b.timeframe) for b in bars}) != 1:
        issues.append(_issue(HistoricalIssue.MIXED_SYMBOLS, None, "multiple symbol/timeframe keys"))

    tf = timedelta(seconds=timeframe_seconds)
    expected_gaps = 0
    suspicious: list[GapInfo] = []
    for i, bar in enumerate(bars):
        if int(bar.timestamp.timestamp()) % timeframe_seconds != 0:
            issues.append(
                _issue(
                    HistoricalIssue.OFF_GRID_BAR, i, f"{bar.timestamp.isoformat()} off {tf} grid"
                )
            )
        if i == 0:
            continue
        delta = bar.timestamp - bars[i - 1].timestamp
        if delta <= tf:
            continue
        if (
            delta <= tf * cfg.max_intraday_gap_bars
            and bar.timestamp.astimezone(ZoneInfo(cfg.server_zone)).date()
            == bars[i - 1].timestamp.astimezone(ZoneInfo(cfg.server_zone)).date()
        ):
            continue
        if _is_expected_gap(bars[i - 1].timestamp, bar.timestamp, cfg):
            expected_gaps += 1
            continue
        gap = GapInfo(start=bars[i - 1].timestamp, end=bar.timestamp, expected=False)
        suspicious.append(gap)
        issues.append(
            _issue(
                HistoricalIssue.SUSPICIOUS_GAP,
                i,
                f"gap {delta} {gap.start.isoformat()}..{gap.end.isoformat()}",
            )
        )

    spreads = [b.spread_points for b in bars if b.spread_points is not None]
    for i, bar in enumerate(bars):
        if bar.spread_points is not None and bar.spread_points < 0:
            issues.append(_issue(HistoricalIssue.NEGATIVE_SPREAD, i, f"spread {bar.spread_points}"))
    positive = [s for s in spreads if s > 0]
    if positive:
        median = statistics.median(positive)
        limit = float(cfg.spread_outlier_factor) * median
        for i, bar in enumerate(bars):
            if bar.spread_points is not None and bar.spread_points > limit:
                issues.append(
                    _issue(
                        HistoricalIssue.SPREAD_ANOMALY,
                        i,
                        f"spread {bar.spread_points} > {limit:.0f} (10x median {median})",
                    )
                )

    status, hard, warn = _classify(base, issues)
    return HistoricalQualityReport(
        base=base,
        issues=tuple(issues),
        expected_gaps=expected_gaps,
        suspicious_gaps=tuple(suspicious),
        row_count=len(bars),
        status=status,
        hard_failures=hard,
        warnings=warn,
        summary=_summarize(base, issues),
    )


def validate_ticks(
    ticks: list[TickRecord],
    *,
    config: HistoricalQualityConfig | None = None,
) -> HistoricalQualityReport:
    cfg = config or HistoricalQualityConfig()
    issues: list[Violation] = []
    if not ticks:
        base = QualityReport()
        issues.append(_issue(HistoricalIssue.EMPTY_DATASET, None, "no ticks"))
        status, hard, warn = _classify(base, issues)
        return HistoricalQualityReport(
            base=base, issues=tuple(issues), status=status, hard_failures=hard, warnings=warn
        )
    # Same-millisecond ticks with different prices are normal; only exact-timestamp
    # duplicates of the FULL record count as duplicates, so dedupe check is
    # done on (timestamp, bid, ask) below instead of check_ticks' timestamp-only key.
    base = check_ticks(
        ticks,
        config=QualityConfig(
            max_gap=timedelta(days=3650), max_spread_ratio=cfg.max_tick_spread_ratio
        ),
    )
    base = QualityReport(
        violations=tuple(
            v for v in base.violations if v.violation_type is not ViolationType.DUPLICATE_RECORD
        )
    )
    seen: set[tuple] = set()
    extra: list[Violation] = []
    for i, t in enumerate(ticks):
        key = (t.timestamp, t.bid, t.ask)
        if key in seen:
            extra.append(
                Violation(
                    violation_type=ViolationType.DUPLICATE_RECORD,
                    index=i,
                    detail=f"duplicate tick {t.timestamp.isoformat()}",
                )
            )
        seen.add(key)
    base = QualityReport(violations=base.violations + tuple(extra))

    prev_mid: Decimal | None = None
    for i, t in enumerate(ticks):
        if not (t.bid.is_finite() and t.ask.is_finite()):
            prev_mid = None
            continue
        mid = (t.bid + t.ask) / 2
        if prev_mid is not None and prev_mid > 0:
            move = abs(mid - prev_mid) / prev_mid
            if move > cfg.tick_jump_ratio:
                issues.append(_issue(HistoricalIssue.TICK_JUMP, i, f"mid move {move:.4%}"))
        prev_mid = mid

    status, hard, warn = _classify(base, issues)
    return HistoricalQualityReport(
        base=base,
        issues=tuple(issues),
        row_count=len(ticks),
        status=status,
        hard_failures=hard,
        warnings=warn,
        summary=_summarize(base, issues),
    )


def _summarize(base: QualityReport, issues: list[Violation]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in (*base.violations, *issues):
        key = str(v.violation_type)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


__all__ = [
    "GapInfo",
    "HistoricalIssue",
    "HistoricalQualityConfig",
    "HistoricalQualityReport",
    "ValidationStatus",
    "validate_bars",
    "validate_ticks",
]
