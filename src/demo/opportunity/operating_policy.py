# ruff: noqa: E501
"""LIVE operating policy (Lane P): global flatten deadline, per-market broker sessions, entry runway.

Live-only and versioned (``configs/live_operating_policy.toml``). It never touches research SimWindows or the
frozen family windows: it is an OVERLAY applied by the live engine (entry window), ``clock.forced_flat_utc``
(the single choke point of the forced-flat instant), the execution stack (flatten sweep + entry gate) and the
runner (broker-session-aware feed semantics, ``--daily`` shutdown). Its ``version`` / ``policy_hash`` is
persisted with every opportunity snapshot next to ``strategy_hash``.

All wall-clock arithmetic goes through ``zoneinfo`` (Europe/Berlin is DST-aware); nothing is a fixed UTC offset.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[3] / "configs" / "live_operating_policy.toml"
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# market session states (heartbeat ``market_state`` uses the KNOWN_SESSION_PAUSE name verbatim)
SESSION_OPEN = "OPEN"
SESSION_PAUSE = "KNOWN_SESSION_PAUSE"
SESSION_WEEKEND = "WEEKEND_CLOSED"

# flatten_state (heartbeat)
FLATTEN_IDLE = "IDLE"
FLATTEN_WINDOW = "WINDOW"
FLATTEN_OVERDUE = "OVERDUE"
FLATTEN_CONFIRMED = "FLAT_CONFIRMED"

# entry refusal reasons (stable codes; see ``demo.execution.gates``)
ENTRY_FLATTEN_WINDOW = "flatten_window_active"
ENTRY_RUNWAY = "entry_runway_too_short"
ENTRY_OUTSIDE_DAY = "outside_operating_day"


class OperatingPolicyError(ValueError):
    pass


def _hhmm(value: str) -> int:
    try:
        hh, mm = str(value).split(":")
        h, m = int(hh), int(mm)
    except ValueError as exc:
        raise OperatingPolicyError(f"bad HH:MM {value!r}") from exc
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise OperatingPolicyError(f"bad HH:MM {value!r}")
    return h * 60 + m


def _at(day: date, minute: int, tz: ZoneInfo) -> datetime:
    """UTC instant of local wall-clock ``minute`` on ``day`` in ``tz`` (DST-correct)."""
    naive = datetime.combine(day, time(0, 0)) + timedelta(minutes=minute)
    return naive.replace(tzinfo=tz).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Pause:
    """A known broker pause: starts at ``start`` (``tz``) on a local weekday in ``days``, ends at the first
    ``end`` (``end_tz``) wall-clock instant after the start."""

    days: frozenset[int]
    start_min: int
    tz: str
    end_min: int
    end_tz: str

    def window_from(self, start_day: date) -> tuple[datetime, datetime] | None:
        if start_day.weekday() not in self.days:
            return None
        start = _at(start_day, self.start_min, ZoneInfo(self.tz))
        end_zone = ZoneInfo(self.end_tz)
        first = start.astimezone(end_zone).date()
        for offset in range(0, 4):
            end = _at(first + timedelta(days=offset), self.end_min, end_zone)
            if end > start:
                return start, end
        return None


@dataclass(frozen=True, slots=True)
class MarketOperating:
    entry_end_live_min: int | None = None  # market-local minute that replaces the research entry_end (live only)
    pauses: tuple[Pause, ...] = ()
    session_status: str = "unknown"  # observed | provisional | unknown
    session_source: str = ""

    @property
    def has_session(self) -> bool:
        return bool(self.pauses)


@dataclass(frozen=True, slots=True)
class OperatingPolicy:
    version: str
    tz: str
    deadline_min: int
    flatten_start_min: int
    broker_close_buffer_min: int
    min_entry_runway_min: int
    operating_days: frozenset[int]
    retry_backoff_s: tuple[float, ...]
    markets: Mapping[str, MarketOperating] = field(default_factory=dict)
    policy_hash: str = ""

    def __post_init__(self) -> None:
        if not (0 <= self.flatten_start_min < self.deadline_min <= 1440):
            raise OperatingPolicyError("need flatten_start < global_flat_deadline")
        if self.broker_close_buffer_min < 0 or self.min_entry_runway_min < 0:
            raise OperatingPolicyError("buffer / runway must be >= 0")
        if not self.retry_backoff_s or any(s <= 0 for s in self.retry_backoff_s):
            raise OperatingPolicyError("flatten_retry_backoff_s must be non-empty and positive")

    # ------------------------------------------------------------------------------- Berlin day
    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def local(self, utc: datetime) -> datetime:
        return utc.astimezone(self.zone)

    def day_of(self, utc: datetime) -> date:
        return self.local(utc).date()

    def flatten_start_utc(self, day: date) -> datetime:
        return _at(day, self.flatten_start_min, self.zone)

    def deadline_utc(self, day: date) -> datetime:
        return _at(day, self.deadline_min, self.zone)

    def flatten_active(self, now: datetime) -> bool:
        """True from ``flatten_start`` (Berlin) to the end of that Berlin calendar day."""
        loc = self.local(now)
        return loc.hour * 60 + loc.minute >= self.flatten_start_min

    def deadline_passed(self, now: datetime) -> bool:
        loc = self.local(now)
        return loc.hour * 60 + loc.minute >= self.deadline_min

    def is_operating_day(self, now: datetime) -> bool:
        return self.local(now).weekday() in self.operating_days

    # ------------------------------------------------------------------------ broker sessions
    def market(self, canonical: str) -> MarketOperating:
        return self.markets.get(canonical, MarketOperating())

    def _pause_windows(self, canonical: str, around: datetime) -> list[tuple[datetime, datetime]]:
        windows: list[tuple[datetime, datetime]] = []
        for pause in self.market(canonical).pauses:
            anchor = around.astimezone(ZoneInfo(pause.tz)).date()
            for delta in (-2, -1, 0, 1, 2):
                w = pause.window_from(anchor + timedelta(days=delta))
                if w is not None:
                    windows.append(w)
        return sorted(windows)

    def in_pause(self, canonical: str, at: datetime) -> bool:
        return any(s <= at < e for s, e in self._pause_windows(canonical, at))

    def broker_close_utc(self, canonical: str, at: datetime) -> datetime | None:
        """Start of the next known broker pause strictly after ``at`` (None: no session schedule)."""
        starts = [s for s, _ in self._pause_windows(canonical, at) if s > at]
        return min(starts) if starts else None

    def _close_ref(self, at: datetime) -> datetime:
        """Reference instant for 'the broker close of this Berlin day': ``at`` itself before Berlin noon, noon
        afterwards, so the answer does not jump to tomorrow's pause once today's pause has started."""
        loc = self.local(at)
        if loc.hour < 12:
            return at
        return _at(loc.date(), 12 * 60, self.zone)

    def session_state(self, canonical: str, now: datetime) -> str | None:
        """OPEN | KNOWN_SESSION_PAUSE | WEEKEND_CLOSED, or None when the market has no configured broker session
        (the caller then falls back to its cash-session rule)."""
        if not self.market(canonical).has_session:
            return None
        if self.local(now).weekday() >= 5:
            return SESSION_WEEKEND
        return SESSION_PAUSE if self.in_pause(canonical, now) else SESSION_OPEN

    # -------------------------------------------------------------------------- the deadline
    def effective_flat_utc(self, canonical: str, market_flat_utc: datetime, at: datetime) -> datetime:
        """Earliest of the market-local flat instant, the Berlin flatten start of ``at``'s Berlin day and
        (broker session close - buffer). Never later than the global deadline (flatten_start < deadline)."""
        cands = [market_flat_utc, self.flatten_start_utc(self.day_of(at))]
        close = self.broker_close_utc(canonical, self._close_ref(at))
        if close is not None:
            cands.append(close - timedelta(minutes=self.broker_close_buffer_min))
        return min(cands)

    def sweep_start_utc(self, canonical: str, at: datetime) -> datetime:
        """Instant from which a position of ``canonical`` must be swept flat (no registry row needed)."""
        cands = [self.flatten_start_utc(self.day_of(at))]
        close = self.broker_close_utc(canonical, self._close_ref(at))
        if close is not None and self.day_of(close) == self.day_of(at):
            cands.append(close - timedelta(minutes=self.broker_close_buffer_min))
        return min(cands)

    def entry_refusal(self, canonical: str, signal_utc: datetime, flat_utc: datetime) -> str | None:
        """Why a NEW entry at ``signal_utc`` (intent forced flat ``flat_utc``) is refused; None = allowed."""
        if self.flatten_active(signal_utc):
            return ENTRY_FLATTEN_WINDOW
        if signal_utc >= flat_utc - timedelta(minutes=self.min_entry_runway_min):
            return ENTRY_RUNWAY
        return None

    def backoff_s(self, failures: int) -> float:
        return self.retry_backoff_s[min(max(failures, 1), len(self.retry_backoff_s)) - 1]

    def describe(self) -> dict[str, Any]:
        return {"version": self.version, "hash": self.policy_hash, "tz": self.tz}


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def policy_from_dict(raw: dict[str, Any]) -> OperatingPolicy:
    p = raw.get("policy")
    if not isinstance(p, dict):
        raise OperatingPolicyError("missing [policy]")
    markets: dict[str, MarketOperating] = {}
    for name, m in (raw.get("markets") or {}).items():
        sess = m.get("session") or {}
        pauses = []
        for pz in sess.get("pauses", []):
            tz = pz.get("tz", "Europe/Berlin")
            days = frozenset(_DAYS.index(d) for d in pz["days"])
            pauses.append(Pause(days, _hhmm(pz["start"]), tz, _hhmm(pz["end"]), pz.get("end_tz", tz)))
        end_live = m.get("entry_end_live")
        markets[name] = MarketOperating(
            entry_end_live_min=None if end_live is None else _hhmm(end_live),
            pauses=tuple(pauses),
            session_status=str(sess.get("status", "unknown")),
            session_source=str(sess.get("source", "")),
        )
    return OperatingPolicy(
        version=str(p["version"]),
        tz=str(p.get("tz", "Europe/Berlin")),
        deadline_min=_hhmm(p["global_flat_deadline"]),
        flatten_start_min=_hhmm(p["flatten_start"]),
        broker_close_buffer_min=int(p["broker_close_buffer_min"]),
        min_entry_runway_min=int(p["min_entry_runway_min"]),
        operating_days=frozenset(_DAYS.index(d) for d in p["operating_days"]),
        retry_backoff_s=tuple(float(s) for s in p["flatten_retry_backoff_s"]),
        markets=markets,
        policy_hash=hashlib.sha256(_canon(raw).encode()).hexdigest()[:16],
    )


def load_operating_policy(path: Path | str | None = None) -> OperatingPolicy:
    target = Path(path) if path is not None else DEFAULT_POLICY_PATH
    if not target.is_file():
        raise OperatingPolicyError(f"no live operating policy {target}")
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise OperatingPolicyError(f"{target}: invalid TOML: {exc}") from exc
    return policy_from_dict(raw)
