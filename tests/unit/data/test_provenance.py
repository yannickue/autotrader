"""Tests for `data.provenance.Provenance`.

The critical correctness rule under test: a CFD broker's tick count is
activity/frequency data, never real exchange volume, because a CFD has no
central order book to report real traded volume from.
"""

import pytest

from data.provenance import Provenance, SemanticType, Source


def test_activtrades_tick_activity_is_valid() -> None:
    provenance = Provenance(
        source=Source.ACTIVTRADES_MT5_CFD,
        semantic_type=SemanticType.BROKER_TICK_ACTIVITY,
    )
    assert provenance.semantic_type is SemanticType.BROKER_TICK_ACTIVITY


def test_binance_real_exchange_volume_is_valid() -> None:
    provenance = Provenance(
        source=Source.BINANCE_USDM,
        semantic_type=SemanticType.REAL_EXCHANGE_VOLUME,
    )
    assert provenance.semantic_type is SemanticType.REAL_EXCHANGE_VOLUME


def test_activtrades_cannot_claim_real_exchange_volume() -> None:
    """A CFD broker's tick count must never be mistaken for real exchange volume.

    Constructing this combination is a bug in the caller (e.g. treating tick
    count as traded volume) -- it must be rejected at construction, not
    silently accepted and consumed downstream as if it were genuine
    order-book volume.
    """
    with pytest.raises(ValueError, match="activity/frequency data"):
        Provenance(
            source=Source.ACTIVTRADES_MT5_CFD,
            semantic_type=SemanticType.REAL_EXCHANGE_VOLUME,
        )


def test_all_source_and_semantic_type_members_present() -> None:
    assert {s.value for s in Source} == {
        "ACTIVTRADES_MT5_CFD",
        "BINANCE_USDM",
        "EXTERNAL_REFERENCE",
        "CME_REFERENCE",
        "EUREX_REFERENCE",
    }
    assert {s.value for s in SemanticType} == {
        "BROKER_BID_ASK",
        "BROKER_TICK",
        "BROKER_TICK_ACTIVITY",
        "OHLC_BAR",
        "REAL_EXCHANGE_VOLUME",
        "ORDER_BOOK",
        "OPEN_INTEREST",
        "FUNDING",
        "REFERENCE_PRICE",
    }
