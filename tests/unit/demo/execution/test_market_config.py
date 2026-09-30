from decimal import Decimal

from demo.execution.market_config import load_demo_market_specs
from nautilus_mt5.symbols import default_registry, demo_registry


def test_demo_registry_is_explicit_and_does_not_enable_default_live_path():
    assert [m.canonical for m in default_registry().all()] == ["GER40"]
    mappings = demo_registry().all()
    assert [(m.canonical, m.broker_symbol) for m in mappings] == [
        ("GER40", "Ger40"),
        ("NAS100", "UsaTec"),
        ("SPX500", "Usa500"),
        ("XAUUSD", "GOLD"),
        ("EURUSD", "EURUSD"),
    ]
    assert not any(m.research_only for m in mappings)


def test_demo_market_specs_are_derived_from_checked_in_market_configs():
    specs = load_demo_market_specs()
    ger40 = specs["GER40"]
    assert ger40.broker_symbol == "Ger40"
    assert ger40.volume_min == Decimal("0.25")
    assert ger40.volume_step == Decimal("0.25")
    assert ger40.volume_max == Decimal("250.0")
    assert ger40.contract_size == Decimal("1.0")
    assert ger40.tick_size == Decimal("0.01")
    assert ger40.max_spread == Decimal("8.0")
    assert ger40.max_leverage == Decimal("20.0")


def test_each_demo_market_spec_comes_from_its_own_config_and_has_no_unit_confused_risk_limits():
    specs = load_demo_market_specs()
    assert set(specs) == {"GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD"}
    for market, spec in specs.items():
        assert spec.canonical == market
        assert spec.volume_min > 0 and spec.volume_step > 0 and spec.max_spread > 0
        assert 0 < spec.max_leverage <= 30
        # the old risk_limits() used max_spread (PRICE units) as bps; risk limits are built only in
        # demo.execution.risk_policy with the proper bps conversion
        assert not hasattr(spec, "risk_limits")
