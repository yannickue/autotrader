# ruff: noqa: E501
"""Shadow-only market specs (Lane U): loader, hard mode marker, and proof they can never reach the live order path."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from markets.phase2 import PHASE2_MARKETS, flag_enabled_markets, load_enablement, load_phase2_spec
from markets.shadow import (
    PRODUCTION_BROKER_SYMBOLS,
    SHADOW_CONFIG_DIR,
    ShadowMarketSpec,
    ShadowSpecError,
    load_shadow_spec,
    load_shadow_specs,
    shadow_spec_from_dict,
)
from markets.spec import (
    CANONICALS,
    DEFAULT_CONFIG_DIR,
    PHASE2_CANONICALS,
    PHASE2_CONFIG_DIR,
    MarketSpecError,
    load_market_spec,
    spec_from_dict,
)
from nautilus_mt5.symbols import demo_registry

GOOD = """
[market]
canonical = "GBPUSD"
broker_symbol = "GBPUSD"
broker_path = 'Forex\\Majors\\GBPUSD'
cluster = "FX"
description = "GBP vs USD"
mode = "shadow_only"
[instrument]
point_size = 0.00001
digits = 5
contract_size = 100000.0
tick_size = 0.00001
volume_min = 0.01
volume_step = 0.01
volume_max = 50.0
currency_profit = "USD"
currency_margin = "GBP"
[calendar]
status = "provisional"
tz = "UTC"
server_utc_offset_s = 7200
[cost]
reference_median_spread_price = 0.00012
reference_p95_spread_price = 0.0002
spread_source = "observed"
[risk]
max_entry_spread_price = 0.0003
[provenance]
snapshot_date = "2026-09-30"
"""


def _raw(**patch) -> dict:
    raw = tomllib.loads(GOOD)
    for dotted, value in patch.items():
        section, key = dotted.split("__")
        if value is None:
            raw[section].pop(key, None)
        else:
            raw[section][key] = value
    return raw


def test_good_spec_loads_and_is_never_tradable():
    spec = shadow_spec_from_dict(_raw())
    assert isinstance(spec, ShadowMarketSpec)
    assert spec.mode == "shadow_only"
    assert spec.tradable is False


def test_mode_marker_is_mandatory_and_exact():
    for bad in (None, "demo", "live", "SHADOW_ONLY"):
        with pytest.raises(ShadowSpecError, match="mode"):
            shadow_spec_from_dict(_raw(market__mode=bad))


@pytest.mark.parametrize("key", ["trading_enabled", "enabled", "live", "tradable"])
def test_any_enablement_style_key_set_true_is_rejected(key):
    with pytest.raises(ShadowSpecError, match="never tradable"):
        shadow_spec_from_dict(_raw(**{f"market__{key}": True}))
    raw = _raw()
    raw[key] = True
    with pytest.raises(ShadowSpecError, match="never tradable"):
        shadow_spec_from_dict(raw)


def test_dataclass_has_no_way_to_flip_tradable():
    spec = shadow_spec_from_dict(_raw())
    with pytest.raises((AttributeError, TypeError)):
        spec.tradable = True  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        spec.mode = "demo"  # type: ignore[misc]


@pytest.mark.parametrize("canonical", [*CANONICALS, *PHASE2_CANONICALS])
def test_production_canonicals_are_not_valid_shadow_markets(canonical):
    with pytest.raises(ShadowSpecError, match="production"):
        shadow_spec_from_dict(_raw(market__canonical=canonical))


@pytest.mark.parametrize("symbol", sorted(PRODUCTION_BROKER_SYMBOLS))
def test_production_broker_symbols_are_not_valid_shadow_markets(symbol):
    with pytest.raises(ShadowSpecError, match="production"):
        shadow_spec_from_dict(_raw(market__broker_symbol=symbol))


def test_calendar_must_stay_provisional_and_cluster_known():
    with pytest.raises(ShadowSpecError, match="provisional"):
        shadow_spec_from_dict(_raw(calendar__status="verified_current_constants"))
    with pytest.raises(ShadowSpecError, match="cluster"):
        shadow_spec_from_dict(_raw(market__cluster="WHATEVER"))


def test_filename_must_match_canonical(tmp_path: Path):
    (tmp_path / "OTHER.toml").write_text(GOOD, encoding="utf-8")
    with pytest.raises(ShadowSpecError, match="file name"):
        load_shadow_spec("OTHER", tmp_path)


# ---- they can never enter the live order path ---------------------------------------------------------------


def test_shadow_toml_is_rejected_by_the_production_market_spec_parser():
    with pytest.raises(MarketSpecError):
        spec_from_dict(tomllib.loads(GOOD))


@pytest.mark.parametrize("name", ["GBPUSD", "UK100", "SAP_GE"])
def test_production_loaders_do_not_know_shadow_names(name):
    with pytest.raises(MarketSpecError):
        load_phase2_spec(name)
    with pytest.raises(MarketSpecError):
        load_market_spec(name)
    with pytest.raises(KeyError):
        demo_registry(extra_markets=(name,))
    assert name not in PHASE2_MARKETS


def test_enablement_has_no_entry_for_shadow_markets(tmp_path: Path):
    (tmp_path / "enablement.toml").write_text("[GBPUSD]\nenabled = true\n[BRENT]\nenabled = false\n", encoding="utf-8")
    flags = load_enablement(tmp_path)
    assert set(flags) == set(PHASE2_MARKETS)  # a shadow entry in the file is simply ignored
    assert "GBPUSD" not in flag_enabled_markets(tmp_path)
    assert not (SHADOW_CONFIG_DIR / "enablement.toml").exists()


def test_shadow_configs_live_in_their_own_root_only():
    assert SHADOW_CONFIG_DIR not in (DEFAULT_CONFIG_DIR, PHASE2_CONFIG_DIR)
    shadow_names = {p.stem for p in SHADOW_CONFIG_DIR.glob("*.toml")}
    for root in (DEFAULT_CONFIG_DIR, PHASE2_CONFIG_DIR):
        assert shadow_names.isdisjoint({p.stem for p in root.glob("*.toml")})


def test_mt5_demo_stack_refuses_a_shadow_market_in_live_mode_and_in_shadow_mode(tmp_path: Path):
    from tests.unit.demo.execution.stack_harness import build_broker, make_stack

    for dry_run in (False, True):
        with pytest.raises((KeyError, MarketSpecError, ValueError)):
            make_stack(build_broker(), tmp_path / str(dry_run), dry_run=dry_run, extra_markets=("GBPUSD",))


# ---- the generated configs ---------------------------------------------------------------------------------


def test_all_checked_in_shadow_configs_load_and_are_shadow_only():
    specs = load_shadow_specs()
    for canonical, spec in specs.items():
        assert spec.canonical == canonical
        assert spec.mode == "shadow_only" and spec.tradable is False
        assert spec.canonical not in CANONICALS and spec.canonical not in PHASE2_CANONICALS
        assert spec.broker_symbol not in PRODUCTION_BROKER_SYMBOLS
        assert spec.calendar_status == "provisional"
        assert spec.reference_median_spread_price > 0
        raw = tomllib.loads((SHADOW_CONFIG_DIR / f"{canonical}.toml").read_text(encoding="utf-8"))
        assert raw["market"]["mode"] == "shadow_only"
        assert "shadow_only" in raw["data_quality"]["flags"]
