"""V2 research-only symbol data: observed names registered without touching the live registry."""

from nautilus_mt5.symbols import (
    GER40,
    RESEARCH_ONLY_MAPPINGS,
    default_registry,
    research_registry,
)


def test_default_registry_is_unchanged_ger40_only():
    reg = default_registry()
    assert [m.canonical for m in reg.all()] == ["GER40"]
    assert not GER40.research_only


def test_research_registry_adds_observed_symbols_as_research_only():
    reg = research_registry()
    assert {m.canonical for m in reg.all()} == {"GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD"}
    assert all(m.research_only for m in RESEARCH_ONLY_MAPPINGS)
    assert reg.by_broker_symbol("UsaTec").canonical == "NAS100"
    assert reg.by_broker_symbol("Usa500").canonical == "SPX500"
    assert reg.by_broker_symbol("GOLD").canonical == "XAUUSD"
    assert reg.by_broker_symbol("EURUSD").canonical == "EURUSD"
    assert reg.by_broker_symbol("Nas100") is None  # never guessed


def test_specs_agree_with_registry():
    from markets.spec import load_all_specs

    reg = research_registry()
    for canonical, spec in load_all_specs().items():
        mapping = next(m for m in reg.all() if m.canonical == canonical)
        assert spec.broker_symbol == mapping.broker_symbol
        assert spec.broker_path.startswith(mapping.expected_path_prefix)
