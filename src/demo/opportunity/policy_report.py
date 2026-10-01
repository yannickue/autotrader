# ruff: noqa: E501
"""Read-only diagnostic of the live operating policy (Lane R): per-market EFFECTIVE flat deadline and DERIVED entry runway.

``scripts/demo_trader.py --print-operating-policy`` prints ``operating_policy_report()``.  No broker, no store, no MT5: it only
combines the versioned policy with the checked-in market calendars.  Every value is computed through ``zoneinfo`` for the sample
days (a summer and a winter weekday, plus the Fridays that matter for the weekly broker breaks)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from demo.opportunity.clock import forced_flat_utc, live_spec, local_of, market_flat_utc
from demo.opportunity.operating_policy import OperatingPolicy, derive_entry_runway
from markets.spec import load_market_spec

REPORT_MARKETS = ("NAS100", "SPX500", "BTCUSD", "GER40", "BRENT", "XAUUSD", "EURUSD")
SAMPLE_DAYS = {  # label -> (Berlin date, comment)
    "summer_wed": date(2026, 7, 15),
    "winter_wed": date(2026, 12, 16),
    "summer_fri": date(2026, 7, 17),
    "winter_fri": date(2026, 12, 18),
}


def _hhmm(utc: datetime | None, tz: ZoneInfo) -> str | None:
    return None if utc is None else utc.astimezone(tz).strftime("%H:%M")


def market_row(policy: OperatingPolicy, canonical: str, day: date) -> dict[str, Any]:
    """One market on one Berlin day: which instant binds the flat deadline, the derived runway and the entry end."""
    berlin = policy.zone
    spec = load_market_spec(canonical)
    sig = datetime.combine(day, time(10, 0), tzinfo=berlin).astimezone(UTC)  # 10:00 Berlin of that day (an entry-bar instant)
    cal_flat = market_flat_utc(spec, sig, spec.calendar.forced_flat_min)
    flat_start = policy.flatten_start_utc(day)
    close = policy.broker_close_utc(canonical, policy._close_ref(sig))
    cand = {"market_calendar_flat": cal_flat, "global_flatten_start": flat_start}
    if close is not None:
        cand["broker_close_minus_buffer"] = close - timedelta(minutes=policy.broker_close_buffer_min)
    binding = min(cand, key=lambda k: cand[k])
    flat = forced_flat_utc(spec, sig, spec.calendar.forced_flat_min, policy)
    rw = derive_entry_runway(policy, canonical, flat)
    latest_safe = flat - timedelta(minutes=rw.minutes)
    overlay = live_spec(spec, policy, sig)
    entry_end: str | None = None
    if overlay is not None:
        loc = local_of(spec, sig)
        end_local = datetime.combine(loc.date(), time(0, 0), tzinfo=ZoneInfo(spec.calendar.tz)) + timedelta(
            minutes=overlay.calendar.entry_end_min)
        entry_end = _hhmm(end_local.astimezone(UTC), berlin)
    return {
        "market": canonical,
        "asset_class": spec.asset_class,
        "calendar_status": spec.calendar.status,
        "effective_flat_deadline_berlin": _hhmm(flat, berlin),
        "effective_flat_deadline_utc": _hhmm(flat, UTC),
        "binding_constraint": binding,
        "candidates_berlin": {k: _hhmm(v, berlin) for k, v in sorted(cand.items())},
        "broker_close_berlin": _hhmm(close, berlin),
        "runway": rw.as_dict(),
        "latest_safe_entry_berlin": _hhmm(latest_safe, berlin),
        "live_entry_end_berlin": entry_end,  # also bounded by the research entry end for markets without entry_end_live
    }


def operating_policy_report(policy: OperatingPolicy, markets: tuple[str, ...] = REPORT_MARKETS) -> dict[str, Any]:
    rp = policy.runway
    return {
        "policy": policy.describe(),
        "global_flat_deadline_berlin": f"{policy.deadline_min // 60:02d}:{policy.deadline_min % 60:02d}",
        "flatten_start_berlin": f"{policy.flatten_start_min // 60:02d}:{policy.flatten_start_min % 60:02d}",
        "broker_close_buffer_min": policy.broker_close_buffer_min,
        "flatten_retry_backoff_s": list(policy.retry_backoff_s),
        "runway_params": None if rp is None else {
            "bar_minutes": rp.bar_minutes, "evaluation_budget_s": rp.evaluation_budget_s, "flatten_wait_s": rp.flatten_wait_s,
            "close_grace_s": rp.close_grace_s, "liquidity_allowance_s": dict(rp.liquidity_allowance_s),
            "default_liquidity_allowance_s": rp.default_liquidity_allowance_s, "round_to_bar": rp.round_to_bar,
        },
        "reference_runway_min": policy.min_entry_runway_min,
        "days": {label: day.isoformat() for label, day in SAMPLE_DAYS.items()},
        "markets": {
            m: {label: market_row(policy, m, day) for label, day in SAMPLE_DAYS.items()} for m in markets
        },
        "note": "latest_safe_entry = effective_flat - derived runway; Brent stops early because its (provisional) calendar flat "
                "18:55 Berlin binds, not the global deadline; see docs/DEMO_TRADER.md",
    }
