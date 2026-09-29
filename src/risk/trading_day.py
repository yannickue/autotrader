"""Explicit, configurable trading-day boundary and "realized PnL today" semantics.

Pure and deterministic: no clock reads, no I/O, no mutable state.

Definitions (docs/RISK_CONTRACT.md, "Daily PnL"):

- A *trading day* starts at `rollover_time` (a wall-clock time) in `timezone`
  (an IANA zone name) and lasts until the next such instant. The day start is
  always converted to UTC for comparison; every timestamp handed in or out is
  UTC-aware.
- *Realized PnL today* = the sum, over realized-PnL entries whose UTC timestamp
  lies in `[trading_day_start(now), now]`, of `realized_pnl - fee`. Fees and
  commissions are therefore charged on the day they were paid, and entries from
  earlier days never contribute. Unrealized PnL is NOT part of this figure; the
  daily-loss gate adds the current unrealized PnL separately, exactly as before.
- An entry whose day cannot be established -- unknown timestamp (`None`) or
  stamped after `now` (clock skew) -- contributes only its LOSS (`min(net, 0)`):
  a loss is never dropped, but such an entry can never contribute a profit that
  masks a real loss today. Production fills always carry timestamps.

Broker calibration is PENDING: ActivTrades' actual trading-day rollover (server
timezone and rollover time) is not yet known, so the default policy is UTC
midnight and is explicitly labelled `PENDING_BROKER_CALIBRATION`. Nothing here
invents broker rollover semantics; set `timezone`/`rollover_time` once the
broker behavior is measured and flip `calibration` to `CALIBRATED`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

ZERO = Decimal("0")
# A trading day is at most 25h (DST fall-back day in the configured zone).
MAX_TRADING_DAY = timedelta(hours=25)


class DayBoundaryCalibration(StrEnum):
    PENDING_BROKER_CALIBRATION = "PENDING_BROKER_CALIBRATION"
    CALIBRATED = "CALIBRATED"


@dataclass(frozen=True, slots=True, kw_only=True)
class RealizedPnlEntry:
    """One realized-PnL/fee contribution (typically one applied fill)."""

    timestamp: datetime | None
    realized_pnl: Decimal
    fee: Decimal = ZERO

    def __post_init__(self) -> None:
        # Validated at construction so a malformed entry (naive/non-UTC time,
        # NaN/Infinity amount) can never exist -- not via a fill, and not via
        # a corrupt checkpoint import.
        if self.timestamp is not None and (
            self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0)
        ):
            raise ValueError("entry timestamp must be UTC-aware")
        if not self.realized_pnl.is_finite() or not self.fee.is_finite():
            raise ValueError("entry realized_pnl and fee must be finite")


@dataclass(frozen=True, slots=True, kw_only=True)
class TradingDayPolicy:
    timezone: str = "UTC"
    rollover_time: time = time(0, 0)
    calibration: DayBoundaryCalibration = DayBoundaryCalibration.PENDING_BROKER_CALIBRATION

    def __post_init__(self) -> None:
        if self.rollover_time.tzinfo is not None:
            raise ValueError("rollover_time must be a naive wall-clock time in `timezone`")
        ZoneInfo(self.timezone)  # raises ZoneInfoNotFoundError for an unknown zone

    def trading_day_start(self, now: datetime) -> datetime:
        """UTC instant at which the trading day containing `now` began."""
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now must be a UTC-aware datetime")
        zone = ZoneInfo(self.timezone)
        local_now = now.astimezone(zone)
        start_local = datetime.combine(local_now.date(), self.rollover_time, tzinfo=zone)
        if start_local > local_now:
            start_local = datetime.combine(
                local_now.date() - timedelta(days=1), self.rollover_time, tzinfo=zone
            )
        return start_local.astimezone(UTC)

    def describe(self) -> str:
        return f"{self.timezone}@{self.rollover_time.isoformat()}:{self.calibration.value}"


def realized_pnl_today(
    entries: Iterable[RealizedPnlEntry], *, now: datetime, policy: TradingDayPolicy
) -> Decimal:
    """Net (after fees) realized PnL for the trading day containing `now`."""
    start = policy.trading_day_start(now)
    total = ZERO
    for entry in entries:
        net = entry.realized_pnl - entry.fee
        if entry.timestamp is None:
            total += min(net, ZERO)  # day unknown: count losses only (fail-closed)
            continue
        if entry.timestamp.tzinfo is None or entry.timestamp.utcoffset() != timedelta(0):
            raise ValueError("entry timestamps must be UTC-aware")
        if entry.timestamp < start:
            continue  # an earlier trading day
        if entry.timestamp > now:
            total += min(net, ZERO)  # future-dated: count losses only (fail-closed)
            continue
        total += net
    return total
