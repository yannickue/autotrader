# ruff: noqa: E501
"""SHADOW-ONLY market specs (Lane U): ``configs/markets_shadow/<CANONICAL>.toml``.

A shadow market is observed (quotes, bars, costs) and may be replayed in shadow mode, but it can NEVER be traded:

* it is NOT a ``markets.spec.MarketSpec`` (``MarketSpec.validate`` rejects any canonical outside ``CANONICALS`` /
  ``PHASE2_CANONICALS``), so no production loader, registry or enablement path accepts it;
* there is deliberately NO enablement switch for shadow markets (no ``enablement.toml`` here, none is read);
* the loader requires ``mode = "shadow_only"``, refuses any ``trading_enabled``/``enabled`` key set true, refuses
  canonicals or broker symbols that belong to the production/Phase-2 universe, and ``ShadowMarketSpec.tradable`` is
  the constant ``False``;
* ``demo_registry`` / ``Mt5DemoStack(extra_markets=...)`` only know the Phase-2 names and raise for anything else.

How a shadow-universe runner consumes them: ``load_shadow_specs()`` -> iterate specs -> for each, read bars/quotes
through the READ-ONLY MT5 path (``broker_symbol``), evaluate signals and log would-be decisions with the spec's
``reference_median_spread_price`` / ``max_entry_spread_price`` cost inputs. No ``ExecutionRequest`` is ever built.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from markets.spec import CANONICALS, PHASE2_CANONICALS

SHADOW_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "markets_shadow"
SHADOW_MODE = "shadow_only"
CLUSTERS = ("INDEX", "FX", "METALS", "ENERGY", "CRYPTO", "COMMODITY_SOFT", "STOCKS", "BONDS", "OTHER")

# Broker symbols of the ACTIVE discovery universe / production specs: never valid as shadow-only markets.
PRODUCTION_BROKER_SYMBOLS = frozenset(
    {"Ger40", "UsaTec", "Usa500", "GOLD", "EURUSD", "Brent", "BTCUSD"}
)
_FORBIDDEN_TRUE_KEYS = ("trading_enabled", "enabled", "live", "tradable")


class ShadowSpecError(ValueError):
    """A shadow spec is malformed, or tries to be tradable / collides with the production universe."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ShadowMarketSpec:
    canonical: str
    broker_symbol: str
    broker_path: str
    cluster: str
    description: str
    mode: str
    point_size: float
    digits: int
    contract_size: float
    tick_size: float
    volume_min: float
    volume_step: float
    volume_max: float
    currency_profit: str
    currency_margin: str
    calendar_status: str
    calendar_tz: str
    sessions_server_clock: dict[str, Any]
    server_utc_offset_s: int
    reference_median_spread_price: float
    reference_p95_spread_price: float
    spread_source: str
    max_entry_spread_price: float
    implied_leverage: float | None
    margin_min_lot_eur: float | None
    swap_mode: int | None
    swap_long: float | None
    swap_short: float | None
    snapshot_date: str
    data_quality_flags: tuple[str, ...]

    @property
    def tradable(self) -> bool:
        """Constant False: there is no code path that can flip a shadow market to tradable."""
        return False


def _need(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise ShadowSpecError(f"missing [{where}].{key}")
    return d[key]


def shadow_spec_from_dict(raw: dict[str, Any]) -> ShadowMarketSpec:
    market = _need(raw, "market", "root")
    instrument = _need(raw, "instrument", "root")
    cost = _need(raw, "cost", "root")
    risk = _need(raw, "risk", "root")
    cal = _need(raw, "calendar", "root")
    prov = _need(raw, "provenance", "root")
    for section in (raw, market, raw.get("shadow", {})):
        for key in _FORBIDDEN_TRUE_KEYS:
            if section.get(key) not in (None, False):
                raise ShadowSpecError(f"shadow spec must not set {key}={section.get(key)!r}: shadow markets are never tradable")
    if market.get("mode") != SHADOW_MODE:
        raise ShadowSpecError(f"[market].mode must be {SHADOW_MODE!r}, got {market.get('mode')!r}")
    canonical = str(_need(market, "canonical", "market"))
    symbol = str(_need(market, "broker_symbol", "market"))
    if canonical in CANONICALS or canonical in PHASE2_CANONICALS or symbol in PRODUCTION_BROKER_SYMBOLS:
        raise ShadowSpecError(f"{canonical}/{symbol} belongs to the production/Phase-2 universe; not a shadow-only market")
    cluster = str(_need(market, "cluster", "market"))
    if cluster not in CLUSTERS:
        raise ShadowSpecError(f"cluster {cluster!r} not in {CLUSTERS}")
    spec = ShadowMarketSpec(
        canonical=canonical,
        broker_symbol=symbol,
        broker_path=str(_need(market, "broker_path", "market")),
        cluster=cluster,
        description=str(market.get("description", "")),
        mode=SHADOW_MODE,
        point_size=float(_need(instrument, "point_size", "instrument")),
        digits=int(_need(instrument, "digits", "instrument")),
        contract_size=float(_need(instrument, "contract_size", "instrument")),
        tick_size=float(_need(instrument, "tick_size", "instrument")),
        volume_min=float(_need(instrument, "volume_min", "instrument")),
        volume_step=float(_need(instrument, "volume_step", "instrument")),
        volume_max=float(_need(instrument, "volume_max", "instrument")),
        currency_profit=str(_need(instrument, "currency_profit", "instrument")),
        currency_margin=str(_need(instrument, "currency_margin", "instrument")),
        calendar_status=str(_need(cal, "status", "calendar")),
        calendar_tz=str(_need(cal, "tz", "calendar")),
        sessions_server_clock=dict(cal.get("sessions_server_clock", {})),
        server_utc_offset_s=int(cal.get("server_utc_offset_s", 7200)),
        reference_median_spread_price=float(_need(cost, "reference_median_spread_price", "cost")),
        reference_p95_spread_price=float(_need(cost, "reference_p95_spread_price", "cost")),
        spread_source=str(_need(cost, "spread_source", "cost")),
        max_entry_spread_price=float(_need(risk, "max_entry_spread_price", "risk")),
        implied_leverage=None if "implied_leverage" not in risk else float(risk["implied_leverage"]),
        margin_min_lot_eur=None if "margin_min_lot_eur" not in risk else float(risk["margin_min_lot_eur"]),
        swap_mode=None if "swap_mode" not in cost else int(cost["swap_mode"]),
        swap_long=None if "swap_long_points" not in cost else float(cost["swap_long_points"]),
        swap_short=None if "swap_short_points" not in cost else float(cost["swap_short_points"]),
        snapshot_date=str(_need(prov, "snapshot_date", "provenance")),
        data_quality_flags=tuple(raw.get("data_quality", {}).get("flags", [])),
    )
    if spec.calendar_status != "provisional":
        raise ShadowSpecError("shadow calendars are always 'provisional'")
    for name in ("point_size", "contract_size", "tick_size", "volume_min", "volume_step", "reference_median_spread_price"):
        if not getattr(spec, name) > 0:
            raise ShadowSpecError(f"{canonical}: {name} must be > 0")
    if spec.volume_min > spec.volume_max:
        raise ShadowSpecError(f"{canonical}: volume_min > volume_max")
    return spec


def load_shadow_spec(canonical: str, config_dir: Path | str | None = None) -> ShadowMarketSpec:
    base = Path(config_dir) if config_dir is not None else SHADOW_CONFIG_DIR
    path = base / f"{canonical}.toml"
    if not path.is_file():
        raise ShadowSpecError(f"no shadow market config {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ShadowSpecError(f"{path}: invalid TOML: {exc}") from exc
    spec = shadow_spec_from_dict(raw)
    if spec.canonical != canonical:
        raise ShadowSpecError(f"{path}: canonical {spec.canonical!r} != file name {canonical!r}")
    return spec


def load_shadow_specs(config_dir: Path | str | None = None) -> dict[str, ShadowMarketSpec]:
    base = Path(config_dir) if config_dir is not None else SHADOW_CONFIG_DIR
    return {p.stem: load_shadow_spec(p.stem, base) for p in sorted(base.glob("*.toml"))}
