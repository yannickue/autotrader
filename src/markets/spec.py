"""MarketSpec: the single source of truth for per-market constants (research, V2).

Everything the alpha code currently hard-codes for GER40 (point size, Berlin session buckets,
entry window / forced-flat minute, spread cap) is expressed here per market and loaded from
`configs/markets/<CANONICAL>.toml`. No trading is enabled through a spec: `trading_enabled` must
be false (validated) -- these are research definitions only. Broker symbols are OBSERVED values
(from the MT5 discovery snapshot), never guessed.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CANONICALS = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD")
# Phase-2 markets (Lane M) are deliberately NOT part of CANONICALS: every research/opportunity
# module iterates CANONICALS and needs downloaded history + production strategy lists for each
# entry. Phase-2 specs live in `configs/markets_phase2/` and are loaded via `markets.phase2`.
PHASE2_CANONICALS = ("BRENT", "BTCUSD")
ASSET_CLASSES = ("index_cfd", "metal_cfd", "fx_cfd", "energy_cfd", "crypto_cfd")
MAX_LEVERAGE_CAP = 30  # hard permitted ceiling; an upper bound, NEVER a target
CALENDAR_STATUSES = ("verified_current_constants", "provisional")
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "markets"
PHASE2_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "markets_phase2"


class MarketSpecError(ValueError):
    """A market spec file is missing, malformed or internally inconsistent."""


def _parse_hhmm(value: str, *, allow_24: bool = False) -> int:
    """'HH:MM' -> minutes since midnight. '24:00' only when `allow_24` (bucket end)."""
    try:
        hh, mm = value.split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError) as exc:
        raise MarketSpecError(f"bad HH:MM value {value!r}") from exc
    if allow_24 and (h, m) == (24, 0):
        return 24 * 60
    if not (0 <= h < 24 and 0 <= m < 60):
        raise MarketSpecError(f"bad HH:MM value {value!r}")
    return h * 60 + m


def _to_time(minutes: int) -> time:
    return time(minutes // 60, minutes % 60)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionBucket:
    name: str
    start_min: int  # local minute-of-day, inclusive
    end_min: int  # local minute-of-day, exclusive (1440 = end of day)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionCalendar:
    tz: str  # IANA zone in which all minutes below are expressed
    cash_open_min: int
    cash_close_min: int
    entry_start_min: int  # first allowed entry-bar OPEN (inclusive)
    entry_end_min: int  # entry-bar OPEN must be strictly before this
    forced_flat_min: int  # everything flat at/after this minute (no overnight)
    buckets: tuple[SessionBucket, ...]
    weekend_policy: str
    holiday_policy: str
    dst_notes: str
    status: str  # verified_current_constants | provisional

    @property
    def cash_open(self) -> time:
        return _to_time(self.cash_open_min)

    @property
    def cash_close(self) -> time:
        return _to_time(self.cash_close_min)

    @property
    def entry_start(self) -> time:
        return _to_time(self.entry_start_min)

    @property
    def entry_end(self) -> time:
        return _to_time(self.entry_end_min)

    @property
    def forced_flat(self) -> time:
        return _to_time(self.forced_flat_min)

    def bucket_tuples(self) -> tuple[tuple[str, int, int], ...]:
        """Same shape as `alpha.common.frame.SESSION_BUCKETS`: (name, lo_min, hi_min)."""
        return tuple((b.name, b.start_min, b.end_min) for b in self.buckets)


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketSpec:
    canonical: str
    broker_symbol: str  # observed, exact, case-sensitive
    broker_path: str  # observed MT5 symbol path (category guard)
    asset_class: str
    point_size: float
    digits: int
    contract_size: float
    tick_size: float
    volume_min: float
    volume_step: float
    volume_max: float
    currency_profit: str
    currency_margin: str
    timezone: str  # market (exchange/reference) timezone
    calendar: SessionCalendar
    max_entry_spread_price: float  # PRICE units (= recorded points * point_size), like sim.SimRules
    spread_model_notes: str
    max_leverage: float  # permitted ceiling (<= 30); NOT a sizing target
    margin_notes: str
    history: dict[str, tuple[str, str]]  # timeframe -> (first_utc, last_utc) OBSERVED
    data_quality_flags: tuple[str, ...]
    snapshot_date: str  # date of the MT5 symbol_info snapshot the values come from
    server_tz_policy: str
    research_only: bool = True
    trading_enabled: bool = False

    @property
    def max_entry_spread_recorded_points(self) -> float:
        """The same cap in broker points (comparable with the parquet `spread_pts` column)."""
        return self.max_entry_spread_price / self.point_size

    def validate(self) -> None:
        errs: list[str] = []
        known = CANONICALS + PHASE2_CANONICALS
        if self.canonical not in known:
            errs.append(f"canonical {self.canonical!r} not in {known}")
        if self.asset_class not in ASSET_CLASSES:
            errs.append(f"asset_class {self.asset_class!r} not in {ASSET_CLASSES}")
        if not self.broker_symbol.strip():
            errs.append("broker_symbol empty")
        for name in ("point_size", "contract_size", "tick_size", "volume_min", "volume_step"):
            if not getattr(self, name) > 0:
                errs.append(f"{name} must be > 0")
        if not (self.volume_min <= self.volume_max):
            errs.append("volume_min > volume_max")
        if self.volume_step > self.volume_max:
            errs.append("volume_step > volume_max")
        if self.digits < 0:
            errs.append("digits < 0")
        if self.tick_size < self.point_size - 1e-15:
            errs.append("tick_size < point_size")
        if not (0 < self.max_leverage <= MAX_LEVERAGE_CAP):
            errs.append(f"max_leverage must be in (0, {MAX_LEVERAGE_CAP}]")
        if self.max_entry_spread_price <= 0:
            errs.append("max_entry_spread_price must be > 0")
        for tz in (self.timezone, self.calendar.tz):
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError):
                errs.append(f"unknown timezone {tz!r}")
        if self.trading_enabled:
            errs.append("trading_enabled must be false (V2 specs are research-only)")
        if not self.research_only:
            errs.append("research_only must be true")
        if not self.snapshot_date or not self.server_tz_policy:
            errs.append("provenance (snapshot_date, server_tz_policy) required")
        for tf, span in self.history.items():
            if len(span) != 2 or not span[0] <= span[1]:
                errs.append(f"history[{tf}] must be (first<=last)")
        errs.extend(_validate_calendar(self.calendar))
        if errs:
            raise MarketSpecError(f"{self.canonical}: " + "; ".join(errs))


def _validate_calendar(cal: SessionCalendar) -> list[str]:
    errs = []
    if cal.status not in CALENDAR_STATUSES:
        errs.append(f"calendar.status {cal.status!r} not in {CALENDAR_STATUSES}")
    if not (0 <= cal.cash_open_min < cal.cash_close_min <= 1440):
        errs.append("cash_open must be before cash_close")
    if not (0 <= cal.entry_start_min < cal.entry_end_min <= cal.forced_flat_min <= 1440):
        errs.append("need entry_start < entry_end <= forced_flat")
    if not cal.buckets:
        errs.append("no session buckets")
    else:
        pos = 0
        for b in cal.buckets:
            if b.start_min != pos or b.end_min <= b.start_min:
                errs.append(f"bucket {b.name!r} not contiguous at minute {pos}")
                break
            pos = b.end_min
        if pos != 1440:
            errs.append("buckets must cover 00:00-24:00 exactly")
        if len({b.name for b in cal.buckets}) != len(cal.buckets):
            errs.append("duplicate bucket names")
    return errs


def _need(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise MarketSpecError(f"{where}: missing key {key!r}")
    return d[key]


def spec_from_dict(raw: dict[str, Any]) -> MarketSpec:
    m = _need(raw, "market", "root")
    c = _need(raw, "calendar", "root")
    inst = _need(raw, "instrument", "root")
    risk = _need(raw, "risk", "root")
    prov = _need(raw, "provenance", "root")
    hist = raw.get("history", {})
    buckets = tuple(
        SessionBucket(
            name=_need(b, "name", "calendar.buckets"),
            start_min=_parse_hhmm(_need(b, "start", "calendar.buckets")),
            end_min=_parse_hhmm(_need(b, "end", "calendar.buckets"), allow_24=True),
        )
        for b in c.get("buckets", [])
    )
    cal = SessionCalendar(
        tz=_need(c, "tz", "calendar"),
        cash_open_min=_parse_hhmm(_need(c, "cash_open", "calendar")),
        cash_close_min=_parse_hhmm(_need(c, "cash_close", "calendar")),
        entry_start_min=_parse_hhmm(_need(c, "entry_start", "calendar")),
        entry_end_min=_parse_hhmm(_need(c, "entry_end", "calendar")),
        forced_flat_min=_parse_hhmm(_need(c, "forced_flat", "calendar")),
        buckets=buckets,
        weekend_policy=_need(c, "weekend_policy", "calendar"),
        holiday_policy=_need(c, "holiday_policy", "calendar"),
        dst_notes=_need(c, "dst_notes", "calendar"),
        status=_need(c, "status", "calendar"),
    )
    spec = MarketSpec(
        canonical=_need(m, "canonical", "market"),
        broker_symbol=_need(m, "broker_symbol", "market"),
        broker_path=_need(m, "broker_path", "market"),
        asset_class=_need(m, "asset_class", "market"),
        timezone=_need(m, "timezone", "market"),
        research_only=bool(m.get("research_only", True)),
        trading_enabled=bool(m.get("trading_enabled", False)),
        point_size=float(_need(inst, "point_size", "instrument")),
        digits=int(_need(inst, "digits", "instrument")),
        contract_size=float(_need(inst, "contract_size", "instrument")),
        tick_size=float(_need(inst, "tick_size", "instrument")),
        volume_min=float(_need(inst, "volume_min", "instrument")),
        volume_step=float(_need(inst, "volume_step", "instrument")),
        volume_max=float(_need(inst, "volume_max", "instrument")),
        currency_profit=_need(inst, "currency_profit", "instrument"),
        currency_margin=_need(inst, "currency_margin", "instrument"),
        calendar=cal,
        max_entry_spread_price=float(_need(risk, "max_entry_spread_price", "risk")),
        spread_model_notes=_need(risk, "spread_model_notes", "risk"),
        max_leverage=float(_need(risk, "max_leverage", "risk")),
        margin_notes=_need(risk, "margin_notes", "risk"),
        history={k: (str(v[0]), str(v[1])) for k, v in hist.items()},
        data_quality_flags=tuple(raw.get("data_quality", {}).get("flags", [])),
        snapshot_date=_need(prov, "snapshot_date", "provenance"),
        server_tz_policy=_need(prov, "server_tz_policy", "provenance"),
    )
    spec.validate()
    return spec


def load_market_spec(canonical: str, config_dir: Path | str | None = None) -> MarketSpec:
    # Phase-2 names resolve to their own config root by default (Lane M2); the five research
    # markets and every explicit ``config_dir`` are untouched (load_all_specs globs five only).
    if config_dir is not None:
        base = Path(config_dir)
    else:
        base = PHASE2_CONFIG_DIR if canonical in PHASE2_CANONICALS else DEFAULT_CONFIG_DIR
    path = base / f"{canonical}.toml"
    if not path.is_file():
        raise MarketSpecError(f"no market config {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise MarketSpecError(f"{path}: invalid TOML: {exc}") from exc
    spec = spec_from_dict(raw)
    if spec.canonical != canonical:
        raise MarketSpecError(f"{path}: canonical {spec.canonical!r} != file name {canonical!r}")
    return spec


def load_all_specs(config_dir: Path | str | None = None) -> dict[str, MarketSpec]:
    base = Path(config_dir) if config_dir is not None else DEFAULT_CONFIG_DIR
    return {p.stem: load_market_spec(p.stem, base) for p in sorted(base.glob("*.toml"))}
