from datetime import UTC, datetime
from decimal import Decimal

from adapters.activtrades_mt5.models import (
    MT5DealEntry,
    MT5DealType,
    MT5OrderKind,
    MT5OrderState,
    MT5PositionSide,
    account_info_to_account_state,
    deal_to_deal_record,
    make_margin_estimate,
    make_profit_estimate,
    order_check_result_from_mt5,
    order_to_order_record,
    position_to_position_record,
    symbol_info_raw_from_mt5,
    symbol_info_to_instrument_spec,
    terminal_info_to_terminal_state,
    tick_to_symbol_tick,
    version_tuple_to_version_info,
)
from instruments.models import ExecutionMode, FillingMode, MarginCalculationMode, TradeMode
from tests.unit.adapters.activtrades_mt5.conftest import (
    make_account_info,
    make_deal,
    make_order,
    make_order_check_result,
    make_position,
    make_symbol_info,
    make_terminal_info,
    make_tick,
)

RETRIEVED_AT = datetime(2026, 9, 29, tzinfo=UTC)


def test_account_info_to_account_state_demo() -> None:
    state = account_info_to_account_state(make_account_info())

    assert state.login == 12345678
    assert state.balance == Decimal("10000.0")
    assert state.equity == Decimal("10050.0")
    assert state.margin_level == Decimal("2010.0")
    assert state.currency == "EUR"
    assert state.leverage == 30
    assert state.trade_allowed is True
    assert state.is_demo is True


def test_account_info_to_account_state_real_is_not_demo() -> None:
    state = account_info_to_account_state(make_account_info(trade_mode=2))
    assert state.is_demo is False


def test_account_info_to_account_state_contest_is_not_demo() -> None:
    state = account_info_to_account_state(make_account_info(trade_mode=1))
    assert state.is_demo is False


def test_terminal_info_to_terminal_state() -> None:
    state = terminal_info_to_terminal_state(make_terminal_info())
    assert state.connected is True
    assert state.company == "ActivTrades"
    assert state.build == 4500


def test_version_tuple_to_version_info() -> None:
    info = version_tuple_to_version_info((500, 4500, "29 Sep 2026"))
    assert info.terminal_version == 500
    assert info.build == 4500
    assert info.release_date == "29 Sep 2026"


def test_tick_to_symbol_tick() -> None:
    tick = tick_to_symbol_tick(make_tick(), symbol="GER40.cash")
    assert tick.bid == Decimal("18000.5")
    assert tick.ask == Decimal("18001.0")
    assert tick.time == datetime.fromtimestamp(1_700_000_000, tz=UTC)


class TestSymbolInfoToInstrumentSpec:
    def test_basic_mapping(self) -> None:
        raw = symbol_info_raw_from_mt5(make_symbol_info())
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )

        assert spec.canonical_symbol == "DAX40"
        assert spec.broker_symbol == "GER40.cash"
        assert spec.description == "DAX 40 Index CFD"
        assert spec.digits == 2
        assert spec.point == Decimal("0.01")
        assert spec.trade_contract_size == Decimal("1.0")
        assert spec.currency_base == "EUR"
        assert spec.margin_calculation_mode is MarginCalculationMode.CFDINDEX
        assert spec.trade_mode is TradeMode.FULL
        assert spec.execution_mode is ExecutionMode.MARKET
        assert spec.is_tradable is True
        assert spec.source == "ACTIVTRADES_MT5_CFD"
        assert spec.retrieved_at == RETRIEVED_AT
        assert spec.quality_flags == ()

    def test_stop_and_freeze_level_converted_from_points_to_price(self) -> None:
        raw = symbol_info_raw_from_mt5(
            make_symbol_info(trade_stops_level=50, trade_freeze_level=20, point=0.01)
        )
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )
        assert spec.stop_level == Decimal("0.5")
        assert spec.freeze_level == Decimal("0.2")

    def test_zero_stop_level_is_none_not_zero(self) -> None:
        raw = symbol_info_raw_from_mt5(make_symbol_info(trade_stops_level=0))
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )
        assert spec.stop_level is None

    def test_unmapped_calc_mode_is_none_and_flagged(self) -> None:
        raw = symbol_info_raw_from_mt5(make_symbol_info(trade_calc_mode=32))
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )
        assert spec.margin_calculation_mode is None
        assert any(f.startswith("UNMAPPED_CALC_MODE:32") for f in spec.quality_flags)

    def test_unmapped_trade_mode_is_none_flagged_and_not_tradable(self) -> None:
        raw = symbol_info_raw_from_mt5(make_symbol_info(trade_mode=99))
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )
        assert spec.trade_mode is None
        assert spec.is_tradable is False
        assert any(f.startswith("UNMAPPED_TRADE_MODE:99") for f in spec.quality_flags)

    def test_disabled_trade_mode_is_not_tradable(self) -> None:
        raw = symbol_info_raw_from_mt5(make_symbol_info(trade_mode=0))
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )
        assert spec.trade_mode is TradeMode.DISABLED
        assert spec.is_tradable is False

    def test_filling_modes_decodes_fok_and_ioc_bits(self) -> None:
        raw = symbol_info_raw_from_mt5(make_symbol_info(filling_mode=0b11))
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )
        assert set(spec.filling_modes) == {FillingMode.FOK, FillingMode.IOC}

    def test_filling_modes_unverified_bit_flagged_not_fabricated(self) -> None:
        raw = symbol_info_raw_from_mt5(make_symbol_info(filling_mode=0b100))
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )
        assert spec.filling_modes == ()
        assert any(f.startswith("UNVERIFIED_FILLING_BITS:4") for f in spec.quality_flags)

    def test_zero_margin_fields_are_none_not_fabricated_zero(self) -> None:
        raw = symbol_info_raw_from_mt5(
            make_symbol_info(margin_initial=0.0, margin_maintenance=0.0, margin_hedged=0.0)
        )
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )
        assert spec.margin_initial is None
        assert spec.margin_maintenance is None
        assert spec.margin_hedged is None

    def test_positive_margin_fields_are_preserved(self) -> None:
        raw = symbol_info_raw_from_mt5(make_symbol_info(margin_initial=100.0))
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )
        assert spec.margin_initial == Decimal("100.0")

    def test_instrument_spec_validates_required_fields_still(self) -> None:
        # Regression guard: this conversion must produce a spec that still
        # passes InstrumentSpec.__post_init__'s own required-field checks.
        raw = symbol_info_raw_from_mt5(make_symbol_info())
        spec = symbol_info_to_instrument_spec(
            raw, canonical_symbol="DAX40", retrieved_at=RETRIEVED_AT
        )
        assert spec.point > 0
        assert spec.volume_min <= spec.volume_max


def test_position_to_position_record() -> None:
    record = position_to_position_record(make_position())
    assert record.side is MT5PositionSide.BUY
    assert record.volume == Decimal("1.0")
    assert record.stop_loss == Decimal("17950.0")
    assert record.take_profit == Decimal("18100.0")
    assert record.opened_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert record.quality_flags == ()


def test_position_zero_sl_tp_are_none() -> None:
    record = position_to_position_record(make_position(sl=0.0, tp=0.0))
    assert record.stop_loss is None
    assert record.take_profit is None


def test_position_unmapped_side_flagged() -> None:
    record = position_to_position_record(make_position(type=99))
    assert record.side is None
    assert any(f.startswith("UNMAPPED_POSITION_TYPE:99") for f in record.quality_flags)


def test_order_to_order_record() -> None:
    record = order_to_order_record(make_order())
    assert record.kind is MT5OrderKind.BUY_LIMIT
    assert record.state is MT5OrderState.PLACED
    assert record.volume_initial == Decimal("1.0")
    assert record.expires_at is None


def test_order_unmapped_kind_and_state_flagged() -> None:
    record = order_to_order_record(make_order(type=77, state=88))
    assert record.kind is None
    assert record.state is None
    assert any("UNMAPPED_ORDER_TYPE:77" in f for f in record.quality_flags)
    assert any("UNMAPPED_ORDER_STATE:88" in f for f in record.quality_flags)


def test_deal_to_deal_record() -> None:
    record = deal_to_deal_record(make_deal())
    assert record.kind is MT5DealType.BUY
    assert record.entry is MT5DealEntry.IN
    assert record.commission == Decimal("-0.5")


def test_deal_unmapped_type_flagged() -> None:
    record = deal_to_deal_record(make_deal(type=255, entry=255))
    assert record.kind is None
    assert record.entry is None


def test_margin_estimate_available() -> None:
    est = make_margin_estimate(
        symbol="GER40.cash", volume=Decimal("1"), price=Decimal("18000"), raw_margin=600.0
    )
    assert est.available is True
    assert est.margin == Decimal("600.0")


def test_margin_estimate_unavailable_when_none() -> None:
    est = make_margin_estimate(
        symbol="GER40.cash", volume=Decimal("1"), price=Decimal("18000"), raw_margin=None
    )
    assert est.available is False
    assert est.margin is None


def test_profit_estimate_available() -> None:
    est = make_profit_estimate(
        symbol="GER40.cash",
        volume=Decimal("1"),
        price_open=Decimal("18000"),
        price_close=Decimal("18010"),
        raw_profit=10.0,
    )
    assert est.available is True
    assert est.profit == Decimal("10.0")


def test_order_check_result_success_on_zero_retcode() -> None:
    """The REAL, primary success convention order_check() actually uses --
    verified against a live ActivTrades demo account, 2026-09-29 (a
    genuinely valid, fundable 0.25-lot Ger40 request returned exactly
    retcode=0, comment='Done'). A prior version of this codebase incorrectly
    treated only 10008/10009 as success, which misclassified every real
    valid check as a failure -- this is the regression test for that bug.
    """
    result = order_check_result_from_mt5(make_order_check_result(retcode=0))
    assert result.success is True
    assert result.comment == "Done"


def test_order_check_result_success_on_done_retcode() -> None:
    """order_send()'s TRADE_RETCODE_DONE (10009) is also accepted as a
    defensive extra, in case a different MT5 build/venue ever returns it
    for order_check() too -- but 0 (tested above) is the primary, verified
    convention, not this one."""
    result = order_check_result_from_mt5(make_order_check_result(retcode=10009))
    assert result.success is True


def test_order_check_result_failure_on_reject_retcode() -> None:
    result = order_check_result_from_mt5(make_order_check_result(retcode=10006, comment="No money"))
    assert result.success is False
    assert result.comment == "No money"
