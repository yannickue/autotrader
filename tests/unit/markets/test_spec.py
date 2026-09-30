"""MarketSpec: TOML loader/validator and exact GER40 parity with the current alpha constants."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

import pytest

from markets.spec import (
    CANONICALS,
    DEFAULT_CONFIG_DIR,
    MarketSpecError,
    load_all_specs,
    load_market_spec,
    spec_from_dict,
)


def _raw(canonical: str = "GER40") -> dict:
    return tomllib.loads((DEFAULT_CONFIG_DIR / f"{canonical}.toml").read_text(encoding="utf-8"))


def test_all_five_markets_load_and_validate():
    specs = load_all_specs()
    assert set(specs) == set(CANONICALS)
    for spec in specs.values():
        assert spec.research_only and not spec.trading_enabled
        assert 0 < spec.max_leverage <= 30
        assert spec.calendar.buckets[0].start_min == 0
        assert spec.calendar.buckets[-1].end_min == 1440


def test_ger40_reproduces_current_alpha_constants_exactly():
    from alpha.common import dataset, frame, sim

    spec = load_market_spec("GER40")
    cal = spec.calendar
    assert spec.point_size == dataset.POINT == 0.01
    assert cal.tz == spec.timezone == dataset.BERLIN == "Europe/Berlin"
    assert cal.entry_start_min == frame.ENTRY_START_MIN  # 09:00
    assert cal.entry_end_min == frame.ENTRY_END_MIN  # 20:00
    assert cal.forced_flat_min == frame.FLAT_MIN  # 21:30
    assert cal.bucket_tuples() == frame.SESSION_BUCKETS
    # sim compares against fr.spread (= recorded points * POINT), i.e. PRICE units.
    assert spec.max_entry_spread_price == sim.SimRules().max_entry_spread_pts == 8.0
    assert spec.max_entry_spread_recorded_points == pytest.approx(800.0)
    assert cal.status == "verified_current_constants"
    assert (cal.cash_open.hour, cal.cash_open.minute) == (9, 0)
    assert (cal.cash_close.hour, cal.cash_close.minute) == (17, 30)


def test_ger40_broker_symbol_matches_registry():
    from nautilus_mt5.symbols import GER40

    spec = load_market_spec("GER40")
    assert spec.broker_symbol == GER40.broker_symbol
    assert spec.broker_path.startswith(GER40.expected_path_prefix)


def test_other_markets_are_marked_provisional():
    for c in ("NAS100", "SPX500", "XAUUSD", "EURUSD"):
        assert load_market_spec(c).calendar.status == "provisional"


def test_missing_and_mismatched_files(tmp_path: Path):
    with pytest.raises(MarketSpecError, match="no market config"):
        load_market_spec("GER40", tmp_path)
    (tmp_path / "GER40.toml").write_text("not = [valid", encoding="utf-8")
    with pytest.raises(MarketSpecError, match="invalid TOML"):
        load_market_spec("GER40", tmp_path)
    text = (DEFAULT_CONFIG_DIR / "NAS100.toml").read_text(encoding="utf-8")
    (tmp_path / "GER40.toml").write_text(text, encoding="utf-8")
    with pytest.raises(MarketSpecError, match="!= file name"):
        load_market_spec("GER40", tmp_path)


def _mutate(fn) -> dict:
    raw = copy.deepcopy(_raw())
    fn(raw)
    return raw


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda r: r["risk"].update(max_leverage=31.0), "max_leverage"),
        (lambda r: r["risk"].update(max_leverage=0.0), "max_leverage"),
        (lambda r: r["market"].update(trading_enabled=True), "trading_enabled"),
        (lambda r: r["market"].update(research_only=False), "research_only"),
        (lambda r: r["market"].update(canonical="DAX"), "canonical"),
        (lambda r: r["market"].update(asset_class="crypto"), "asset_class"),
        (lambda r: r["market"].update(timezone="Mars/Base"), "timezone"),
        (lambda r: r["instrument"].update(point_size=0.0), "point_size"),
        (lambda r: r["instrument"].update(volume_min=10000.0), "volume_min > volume_max"),
        (lambda r: r["risk"].update(max_entry_spread_price=0.0), "max_entry_spread_price"),
        (lambda r: r["calendar"].update(entry_end="08:00"), "entry_start < entry_end"),
        (lambda r: r["calendar"].update(forced_flat="19:00"), "entry_start < entry_end"),
        (lambda r: r["calendar"].update(status="guess"), "calendar.status"),
        (lambda r: r["calendar"]["buckets"].pop(), "cover 00:00-24:00"),
        (lambda r: r["calendar"]["buckets"][1].update(start="09:30"), "not contiguous"),
        (lambda r: r["calendar"]["buckets"][1].update(name="PRE"), "duplicate bucket"),
        (lambda r: r["calendar"]["buckets"][0].update(end="25:00"), "bad HH:MM"),
        (lambda r: r["provenance"].update(snapshot_date=""), "provenance"),
        (lambda r: r["history"].update(M5=["2026-01-01", "2025-01-01"]), "history"),
    ],
)
def test_validator_rejects_bad_specs(mutation, message):
    with pytest.raises(MarketSpecError, match=message):
        spec_from_dict(_mutate(mutation))


def test_missing_key_is_reported():
    raw = copy.deepcopy(_raw())
    del raw["instrument"]["point_size"]
    with pytest.raises(MarketSpecError, match="point_size"):
        spec_from_dict(raw)
