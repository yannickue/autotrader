"""Tests for `instruments.discovery.match_symbols`."""

from instruments.discovery import (
    BrokerSymbolCandidate,
    CanonicalInstrument,
    MatchStatus,
    match_symbols,
)

DAX = CanonicalInstrument(
    canonical_symbol="DAX",
    aliases=("GER40", "DAX40", "DE40", "GERMANY 40"),
)
NASDAQ = CanonicalInstrument(
    canonical_symbol="NASDAQ100",
    aliases=("NAS100", "USTEC", "US100"),
)


def test_exact_alias_match() -> None:
    candidates = [
        BrokerSymbolCandidate(broker_symbol="GER40.cash", description="Germany 40 Cash CFD"),
        BrokerSymbolCandidate(broker_symbol="US30.cash", description="Dow Jones 30 CFD"),
    ]
    result = match_symbols(candidates, [DAX])
    match = result["DAX"]
    assert match.status is MatchStatus.MATCHED
    assert match.broker_symbol == "GER40.cash"


def test_config_override_wins_over_alias_match() -> None:
    candidates = [
        BrokerSymbolCandidate(broker_symbol="GER40.cash", description="Germany 40 Cash CFD"),
    ]
    result = match_symbols(candidates, [DAX], overrides={"DAX": "DE40.forced.fixture"})
    match = result["DAX"]
    assert match.status is MatchStatus.MATCHED
    assert match.broker_symbol == "DE40.forced.fixture"


def test_ambiguous_case_flagged_not_silently_resolved() -> None:
    candidates = [
        BrokerSymbolCandidate(broker_symbol="GER40.a", description="Germany 40 variant A"),
        BrokerSymbolCandidate(broker_symbol="GER40.b", description="Germany 40 variant B"),
    ]
    result = match_symbols(candidates, [DAX])
    match = result["DAX"]
    assert match.status is MatchStatus.AMBIGUOUS
    assert match.broker_symbol is None
    assert len(match.candidates) == 2


def test_no_match_case() -> None:
    candidates = [
        BrokerSymbolCandidate(broker_symbol="XAUUSD", description="Gold vs US Dollar"),
    ]
    result = match_symbols(candidates, [DAX])
    match = result["DAX"]
    assert match.status is MatchStatus.NOT_FOUND
    assert match.broker_symbol is None
    assert match.candidates == ()


def test_deterministic_ranking_same_input_same_order() -> None:
    candidates = [
        BrokerSymbolCandidate(broker_symbol="GER40.raw", description="Germany 40 raw feed"),
        BrokerSymbolCandidate(broker_symbol="GER40.cash", description="Germany 40 cash CFD"),
    ]
    first = match_symbols(candidates, [DAX])["DAX"]
    second = match_symbols(list(candidates), [DAX])["DAX"]
    assert [c.broker_symbol for c in first.candidates] == [
        c.broker_symbol for c in second.candidates
    ]


def test_substring_alias_match_ranks_above_fuzzy() -> None:
    candidates = [
        BrokerSymbolCandidate(broker_symbol="GER40.cash", description="Germany 40 Cash CFD"),
        BrokerSymbolCandidate(broker_symbol="GERX.other", description="Unrelated symbol"),
    ]
    result = match_symbols(candidates, [DAX])
    match = result["DAX"]
    assert match.status is MatchStatus.MATCHED
    assert match.broker_symbol == "GER40.cash"


def test_multiple_canonical_instruments_resolved_independently() -> None:
    candidates = [
        BrokerSymbolCandidate(broker_symbol="GER40.cash", description="Germany 40 Cash CFD"),
        BrokerSymbolCandidate(broker_symbol="NAS100.cash", description="US Tech 100 Cash CFD"),
    ]
    result = match_symbols(candidates, [DAX, NASDAQ])
    assert result["DAX"].broker_symbol == "GER40.cash"
    assert result["NASDAQ100"].broker_symbol == "NAS100.cash"


def test_override_for_unknown_broker_symbol_still_matches() -> None:
    """An override is trusted even with no matching candidate in the list."""
    result = match_symbols([], [DAX], overrides={"DAX": "DE40.manual.fixture"})
    match = result["DAX"]
    assert match.status is MatchStatus.MATCHED
    assert match.broker_symbol == "DE40.manual.fixture"
