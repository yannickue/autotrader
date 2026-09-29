"""Trading-day boundary + "realized PnL today" semantics (risk.trading_day).

Old behavior (pre-fix): `AccountRiskState.realized_pnl_today` was the ALL-TIME
cumulative realized PnL minus all-time fees, so a loss from weeks ago kept
tripping (or a profit kept masking) the daily-loss gate.
New behavior: realized PnL - fees of fills inside the current trading day only.
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfoNotFoundError

import pytest

from risk.trading_day import (
    DayBoundaryCalibration,
    RealizedPnlEntry,
    TradingDayPolicy,
    realized_pnl_today,
)


def _e(ts: datetime | None, pnl: str, fee: str = "0") -> RealizedPnlEntry:
    return RealizedPnlEntry(timestamp=ts, realized_pnl=Decimal(pnl), fee=Decimal(fee))


def test_default_policy_is_utc_midnight_and_marked_uncalibrated() -> None:
    policy = TradingDayPolicy()
    assert policy.calibration is DayBoundaryCalibration.PENDING_BROKER_CALIBRATION
    now = datetime(2026, 3, 10, 15, 30, tzinfo=UTC)
    assert policy.trading_day_start(now) == datetime(2026, 3, 10, 0, 0, tzinfo=UTC)


def test_same_trading_day_entries_are_summed_net_of_fees() -> None:
    now = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
    entries = [
        _e(datetime(2026, 3, 10, 1, 0, tzinfo=UTC), "-40", "1"),
        _e(datetime(2026, 3, 10, 9, 0, tzinfo=UTC), "10", "2"),
    ]
    assert realized_pnl_today(entries, now=now, policy=TradingDayPolicy()) == Decimal("-33")


def test_utc_date_boundary_excludes_previous_day_and_includes_midnight_instant() -> None:
    now = datetime(2026, 3, 10, 0, 30, tzinfo=UTC)
    entries = [
        _e(datetime(2026, 3, 9, 23, 59, 59, tzinfo=UTC), "-500", "5"),  # yesterday
        _e(datetime(2026, 3, 10, 0, 0, 0, tzinfo=UTC), "-7", "1"),  # exactly at boundary
    ]
    assert realized_pnl_today(entries, now=now, policy=TradingDayPolicy()) == Decimal("-8")


def test_configured_day_boundary_uses_zone_and_rollover_time() -> None:
    # Rollover at 17:00 America/New_York. 2026-03-10 is EDT (UTC-4) -> 21:00 UTC.
    policy = TradingDayPolicy(timezone="America/New_York", rollover_time=time(17, 0))
    before = datetime(2026, 3, 10, 20, 0, tzinfo=UTC)  # 16:00 local: day began Mar 9 17:00
    after = datetime(2026, 3, 10, 22, 0, tzinfo=UTC)  # 18:00 local: day began Mar 10 17:00
    assert policy.trading_day_start(before) == datetime(2026, 3, 9, 21, 0, tzinfo=UTC)
    assert policy.trading_day_start(after) == datetime(2026, 3, 10, 21, 0, tzinfo=UTC)

    entries = [
        _e(datetime(2026, 3, 10, 20, 30, tzinfo=UTC), "-50"),  # day ending 21:00Z
        _e(datetime(2026, 3, 10, 21, 30, tzinfo=UTC), "-5"),  # new day
    ]
    assert realized_pnl_today(entries, now=after, policy=policy) == Decimal("-5")
    assert realized_pnl_today(entries[:1], now=before, policy=policy) == Decimal("-50")


def test_dst_change_day_start_follows_local_wall_clock() -> None:
    policy = TradingDayPolicy(timezone="America/New_York", rollover_time=time(17, 0))
    # After US spring-forward (2026-03-08) 17:00 local is 21:00 UTC; before it was 22:00 UTC.
    assert policy.trading_day_start(datetime(2026, 3, 7, 23, 0, tzinfo=UTC)) == datetime(
        2026, 3, 7, 22, 0, tzinfo=UTC
    )
    assert policy.trading_day_start(datetime(2026, 3, 9, 22, 0, tzinfo=UTC)) == datetime(
        2026, 3, 9, 21, 0, tzinfo=UTC
    )


def test_multiple_historical_days_only_current_day_counts() -> None:
    now = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
    entries = [
        _e(datetime(2026, 3, 9, 10, 0, tzinfo=UTC), "-1000", "10"),
        _e(datetime(2026, 3, 10, 10, 0, tzinfo=UTC), "-1000", "10"),
        _e(datetime(2026, 3, 11, 10, 0, tzinfo=UTC), "500", "10"),
        _e(datetime(2026, 3, 12, 3, 0, tzinfo=UTC), "-20", "2"),
    ]
    assert realized_pnl_today(entries, now=now, policy=TradingDayPolicy()) == Decimal("-22")


def test_unknown_timestamp_entries_are_counted_fail_closed() -> None:
    now = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
    result = realized_pnl_today([_e(None, "-9", "1")], now=now, policy=TradingDayPolicy())
    assert result == Decimal("-10")


def test_unknown_or_future_profits_can_never_mask_a_real_loss_today() -> None:
    now = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
    today_loss = _e(datetime(2026, 3, 12, 3, 0, tzinfo=UTC), "-100")
    for masking in (_e(None, "500"), _e(now + timedelta(hours=1), "500", "1")):
        assert realized_pnl_today(
            [today_loss, masking], now=now, policy=TradingDayPolicy()
        ) == Decimal("-100")


def test_entry_stamped_after_now_is_not_dropped() -> None:
    now = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
    entries = [_e(now + timedelta(seconds=3), "-4")]  # clock skew must not hide a loss
    assert realized_pnl_today(entries, now=now, policy=TradingDayPolicy()) == Decimal("-4")


def test_rejects_naive_or_non_utc_inputs_and_unknown_zone() -> None:
    policy = TradingDayPolicy()
    with pytest.raises(ValueError):
        policy.trading_day_start(datetime(2026, 3, 12, 12, 0))
    with pytest.raises(ValueError):
        realized_pnl_today(
            [_e(datetime(2026, 3, 12, 1, 0), "1")],
            now=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
            policy=policy,
        )
    with pytest.raises(ZoneInfoNotFoundError):
        TradingDayPolicy(timezone="Not/AZone")
