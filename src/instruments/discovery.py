"""Canonical <-> broker symbol discovery and matching.

Standalone: takes an ALREADY-FETCHED list of raw broker symbol records (the
caller is responsible for fetching them from wherever, e.g. a future MT5
adapter) and a list of canonical instruments to find, and produces a
deterministic, explainable mapping. Performs no venue I/O itself.

Matching precedence (highest wins):
    1. Config override -- an explicit `{canonical: broker_symbol}` mapping
       always wins over automatic matching.
    2. Exact alias match -- the candidate's `broker_symbol` or `description`
       (case-insensitively) equals one of the canonical instrument's aliases.
    3. Substring alias match -- one of the aliases appears as a substring of
       the candidate's `broker_symbol` or `description` (case-insensitively).
    4. Fuzzy match -- token-overlap ratio between the candidate's text and
       the canonical instrument's aliases/name, using Python's stdlib
       `difflib.SequenceMatcher` for a deterministic similarity ratio.

Ranking within each tier is deterministic: ties are broken by
`broker_symbol` alphabetical order, so the same input always produces the
same ranked order.

Ambiguous match handling: when the top two ranked candidates are not
clearly separated (same match tier and similarity score within
`AMBIGUITY_MARGIN` of each other), or when there is no candidate at all, the
result is marked `UNVERIFIED`/`NOT_FOUND` rather than silently picking one.
"""

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

# How close two top candidates' scores must be (within the same match tier)
# to be considered ambiguous rather than a clear winner.
AMBIGUITY_MARGIN = 0.05

# Minimum fuzzy-match ratio to be considered a candidate at all.
FUZZY_MATCH_THRESHOLD = 0.6


class MatchTier(StrEnum):
    """How a candidate matched a canonical instrument's aliases."""

    OVERRIDE = "override"
    EXACT_ALIAS = "exact_alias"
    SUBSTRING_ALIAS = "substring_alias"
    FUZZY = "fuzzy"


class MatchStatus(StrEnum):
    """Overall confidence of a canonical instrument's resolved mapping."""

    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True, kw_only=True)
class BrokerSymbolCandidate:
    """One raw broker symbol record fetched by the caller (e.g. an MT5 adapter)."""

    broker_symbol: str
    description: str = ""

    def __post_init__(self) -> None:
        if not self.broker_symbol.strip():
            raise ValueError("broker_symbol must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class CanonicalInstrument:
    """A canonical instrument to find, with its known name aliases.

    `aliases` is configuration data, not matching logic -- it can be
    corrected later without touching the matching algorithm.
    """

    canonical_symbol: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.canonical_symbol.strip():
            raise ValueError("canonical_symbol must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class RankedCandidate:
    """One candidate's match result against a single canonical instrument."""

    broker_symbol: str
    tier: MatchTier
    score: float


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolMatch:
    """The resolved mapping outcome for one canonical instrument."""

    canonical_symbol: str
    status: MatchStatus
    broker_symbol: str | None
    candidates: tuple[RankedCandidate, ...]

    def __post_init__(self) -> None:
        if self.status is MatchStatus.MATCHED and self.broker_symbol is None:
            raise ValueError("a MATCHED result must carry a broker_symbol")
        if self.status is not MatchStatus.MATCHED and self.broker_symbol is not None:
            raise ValueError("only a MATCHED result may carry a broker_symbol")


def _normalize(text: str) -> str:
    return text.strip().casefold()


def _rank_candidates(
    canonical: CanonicalInstrument,
    candidates: tuple[BrokerSymbolCandidate, ...],
) -> tuple[RankedCandidate, ...]:
    aliases = [_normalize(a) for a in canonical.aliases if a.strip()]
    canonical_name = _normalize(canonical.canonical_symbol)
    ranked: list[RankedCandidate] = []

    for candidate in candidates:
        symbol_norm = _normalize(candidate.broker_symbol)
        description_norm = _normalize(candidate.description)
        haystacks = (symbol_norm, description_norm)

        tier: MatchTier | None = None
        score = 0.0

        if any(h == a for h in haystacks for a in aliases) or any(
            h == canonical_name for h in haystacks
        ):
            tier = MatchTier.EXACT_ALIAS
            score = 1.0
        elif any(a and a in h for h in haystacks for a in aliases) or (
            canonical_name and canonical_name in symbol_norm
        ):
            tier = MatchTier.SUBSTRING_ALIAS
            # Longer alias relative to haystack length => stronger substring match.
            best = 0.0
            for h in haystacks:
                for a in (*aliases, canonical_name):
                    if a and a in h:
                        best = max(best, len(a) / max(len(h), 1))
            tier = MatchTier.SUBSTRING_ALIAS
            score = best
        else:
            best_ratio = 0.0
            for h in haystacks:
                if not h:
                    continue
                for a in (*aliases, canonical_name):
                    if not a:
                        continue
                    ratio = SequenceMatcher(None, h, a).ratio()
                    best_ratio = max(best_ratio, ratio)
            if best_ratio >= FUZZY_MATCH_THRESHOLD:
                tier = MatchTier.FUZZY
                score = best_ratio

        if tier is not None:
            ranked.append(
                RankedCandidate(broker_symbol=candidate.broker_symbol, tier=tier, score=score)
            )

    tier_order = {
        MatchTier.OVERRIDE: 0,
        MatchTier.EXACT_ALIAS: 1,
        MatchTier.SUBSTRING_ALIAS: 2,
        MatchTier.FUZZY: 3,
    }
    ranked.sort(key=lambda r: (tier_order[r.tier], -r.score, r.broker_symbol))
    return tuple(ranked)


def _resolve_one(
    canonical: CanonicalInstrument,
    candidates: tuple[BrokerSymbolCandidate, ...],
    override: str | None,
) -> SymbolMatch:
    if override is not None:
        override_candidate = RankedCandidate(
            broker_symbol=override, tier=MatchTier.OVERRIDE, score=1.0
        )
        return SymbolMatch(
            canonical_symbol=canonical.canonical_symbol,
            status=MatchStatus.MATCHED,
            broker_symbol=override,
            candidates=(override_candidate,),
        )

    ranked = _rank_candidates(canonical, candidates)

    if not ranked:
        return SymbolMatch(
            canonical_symbol=canonical.canonical_symbol,
            status=MatchStatus.NOT_FOUND,
            broker_symbol=None,
            candidates=(),
        )

    top = ranked[0]
    if len(ranked) > 1:
        runner_up = ranked[1]
        if runner_up.tier == top.tier and (top.score - runner_up.score) <= AMBIGUITY_MARGIN:
            return SymbolMatch(
                canonical_symbol=canonical.canonical_symbol,
                status=MatchStatus.AMBIGUOUS,
                broker_symbol=None,
                candidates=ranked,
            )

    return SymbolMatch(
        canonical_symbol=canonical.canonical_symbol,
        status=MatchStatus.MATCHED,
        broker_symbol=top.broker_symbol,
        candidates=ranked,
    )


def match_symbols(
    candidates: list[BrokerSymbolCandidate],
    canonical_instruments: list[CanonicalInstrument],
    overrides: dict[str, str] | None = None,
) -> dict[str, SymbolMatch]:
    """Resolve each canonical instrument to a broker symbol, deterministically.

    `overrides` (`{canonical_symbol: broker_symbol}`) always wins over
    automatic alias/fuzzy matching for that canonical instrument.
    """
    overrides = overrides or {}
    candidates_tuple = tuple(candidates)

    results: dict[str, SymbolMatch] = {}
    for canonical in canonical_instruments:
        override = overrides.get(canonical.canonical_symbol)
        results[canonical.canonical_symbol] = _resolve_one(
            canonical, candidates_tuple, override
        )
    return results
