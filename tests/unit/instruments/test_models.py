"""Tests for `instruments.models.InstrumentSpec`."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from instruments.models import (
    ExecutionMode,
    FillingMode,
    InstrumentSpec,
    MarginCalculationMode,
    TradeMode,
)

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def _base_kwargs(**overrides: object) -> dict[object, object]:
    kwargs: dict[object, object] = dict(
        canonical_symbol="DAX",
        broker_symbol="GER40.cash.fixture",
        description="DAX 40 CFD (fixture)",
        digits=1,
        point=Decimal("0.1"),
        trade_tick_size=Decimal("0.1"),
        trade_tick_value=Decimal("1.0"),
        trade_contract_size=Decimal("1"),
        volume_min=Decimal("0.1"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.1"),
        currency_base="EUR",
        currency_profit="EUR",
        currency_margin="EUR",
        source="ACTIVTRADES_MT5",
        retrieved_at=NOW,
    )
    kwargs.update(overrides)
    return kwargs


def test_minimal_required_fields_construct() -> None:
    spec = InstrumentSpec(**_base_kwargs())
    assert spec.canonical_symbol == "DAX"
    assert spec.margin_initial is None
    assert spec.trade_mode is None


def test_all_optional_fields_accept_none() -> None:
    spec = InstrumentSpec(
        **_base_kwargs(
            trade_tick_value_profit=None,
            trade_tick_value_loss=None,
            volume_limit=None,
            margin_initial=None,
            margin_maintenance=None,
            margin_hedged=None,
            margin_calculation_mode=None,
            trade_mode=None,
            execution_mode=None,
            stop_level=None,
            freeze_level=None,
            session_note=None,
        )
    )
    assert spec.margin_initial is None
    assert spec.margin_calculation_mode is None
    assert spec.session_note is None


def test_optional_fields_accept_real_values() -> None:
    spec = InstrumentSpec(
        **_base_kwargs(
            trade_tick_value_profit=Decimal("1.0"),
            trade_tick_value_loss=Decimal("1.0"),
            volume_limit=Decimal("50"),
            margin_initial=Decimal("1000"),
            margin_maintenance=Decimal("500"),
            margin_hedged=Decimal("500"),
            margin_calculation_mode=MarginCalculationMode.CFDINDEX,
            trade_mode=TradeMode.FULL,
            execution_mode=ExecutionMode.MARKET,
            filling_modes=(FillingMode.FOK, FillingMode.IOC),
            stop_level=Decimal("5"),
            freeze_level=Decimal("5"),
            session_note="Cash index session, weekdays only (fixture)",
        )
    )
    assert spec.margin_calculation_mode is MarginCalculationMode.CFDINDEX
    assert spec.filling_modes == (FillingMode.FOK, FillingMode.IOC)


@pytest.mark.parametrize(
    "field_name",
    ["canonical_symbol", "broker_symbol", "currency_base", "currency_profit", "currency_margin"],
)
def test_required_string_fields_reject_empty(field_name: str) -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        InstrumentSpec(**_base_kwargs(**{field_name: ""}))


@pytest.mark.parametrize(
    "field_name",
    ["point", "trade_tick_size", "trade_tick_value", "trade_contract_size"],
)
def test_required_price_fields_reject_non_positive(field_name: str) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        InstrumentSpec(**_base_kwargs(**{field_name: Decimal("0")}))


@pytest.mark.parametrize("field_name", ["volume_min", "volume_max", "volume_step"])
def test_required_volume_fields_reject_non_positive(field_name: str) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        InstrumentSpec(**_base_kwargs(**{field_name: Decimal("0")}))


def test_volume_min_cannot_exceed_volume_max() -> None:
    with pytest.raises(ValueError, match="volume_min cannot exceed volume_max"):
        InstrumentSpec(**_base_kwargs(volume_min=Decimal("10"), volume_max=Decimal("1")))


def test_negative_digits_rejected() -> None:
    with pytest.raises(ValueError, match="digits must be non-negative"):
        InstrumentSpec(**_base_kwargs(digits=-1))


def test_non_finite_tick_value_rejected() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        InstrumentSpec(**_base_kwargs(trade_tick_value=Decimal("NaN")))


def test_naive_retrieved_at_rejected() -> None:
    with pytest.raises(ValueError, match="retrieved_at must be UTC"):
        InstrumentSpec(**_base_kwargs(retrieved_at=datetime(2026, 9, 29)))


def test_negative_margin_rejected() -> None:
    with pytest.raises(ValueError, match="must be finite and non-negative"):
        InstrumentSpec(**_base_kwargs(margin_initial=Decimal("-1")))


def test_frozen_and_immutable() -> None:
    spec = InstrumentSpec(**_base_kwargs())
    with pytest.raises((AttributeError, TypeError)):
        spec.canonical_symbol = "OTHER"  # type: ignore[misc]
