from decimal import Decimal

import pytest

from adapters.activtrades_mt5.orders import (
    CancelPendingRequest,
    CloseRequest,
    MarketOrderRequest,
    MT5OrderGateway,
    StopUpdateRequest,
    TakeProfitUpdateRequest,
    TradingMode,
)
from adapters.activtrades_mt5.testing import FakeMT5Client
from tests.unit.adapters.activtrades_mt5.conftest import (
    make_order_check_result,
    make_order_send_result,
)

MARKET_REQUEST = MarketOrderRequest(symbol="GER40.cash", side="BUY", volume=Decimal("1"))


def test_default_mode_is_mock() -> None:
    gateway = MT5OrderGateway(FakeMT5Client())
    assert gateway.mode is TradingMode.MOCK


@pytest.mark.parametrize("mode", [TradingMode.MOCK, TradingMode.PAPER, TradingMode.SHADOW])
def test_never_calls_client_never_sends_real_orders(mode: TradingMode) -> None:
    client = FakeMT5Client()
    client.set_order_send_result(make_order_send_result())
    client.set_order_check_result(make_order_check_result())
    gateway = MT5OrderGateway(client, mode=mode)

    submit_result = gateway.submit_market_order(MARKET_REQUEST, price=Decimal("18000"))
    preflight_result = gateway.preflight_check(MARKET_REQUEST, price=Decimal("18000"))

    assert submit_result.accepted is False
    assert preflight_result.accepted is False
    assert client.order_send_calls == []
    assert client.order_check_calls == []


def test_demo_mode_calls_client_without_confirmation() -> None:
    client = FakeMT5Client()
    client.set_order_send_result(make_order_send_result(retcode=10009))
    gateway = MT5OrderGateway(client, mode=TradingMode.DEMO)

    result = gateway.submit_market_order(MARKET_REQUEST, price=Decimal("18000"))

    assert result.accepted is True
    assert len(client.order_send_calls) == 1


@pytest.mark.parametrize("mode", [TradingMode.LIVE_SMOKE, TradingMode.LIVE])
def test_live_modes_refuse_without_confirm(mode: TradingMode) -> None:
    client = FakeMT5Client()
    client.set_order_send_result(make_order_send_result())
    gateway = MT5OrderGateway(client, mode=mode)

    result = gateway.submit_market_order(MARKET_REQUEST, price=Decimal("18000"))

    assert result.accepted is False
    assert client.order_send_calls == []


@pytest.mark.parametrize("mode", [TradingMode.LIVE_SMOKE, TradingMode.LIVE])
def test_live_modes_proceed_with_confirm(mode: TradingMode) -> None:
    client = FakeMT5Client()
    client.set_order_send_result(make_order_send_result(retcode=10009))
    gateway = MT5OrderGateway(client, mode=mode)

    result = gateway.submit_market_order(MARKET_REQUEST, price=Decimal("18000"), confirm=True)

    assert result.accepted is True
    assert len(client.order_send_calls) == 1


def test_order_send_none_result_is_not_accepted() -> None:
    client = FakeMT5Client()
    client.set_order_send_result(None)
    gateway = MT5OrderGateway(client, mode=TradingMode.DEMO)

    result = gateway.submit_market_order(MARKET_REQUEST, price=Decimal("18000"))

    assert result.accepted is False
    assert "None" in result.reason


def test_order_send_reject_retcode_is_not_accepted() -> None:
    client = FakeMT5Client()
    client.set_order_send_result(make_order_send_result(retcode=10006, comment="rejected"))
    gateway = MT5OrderGateway(client, mode=TradingMode.DEMO)

    result = gateway.submit_market_order(MARKET_REQUEST, price=Decimal("18000"))

    assert result.accepted is False
    assert result.reason == "rejected"


def test_order_send_exception_does_not_raise() -> None:
    class RaisingClient(FakeMT5Client):
        def order_send(self, request: dict) -> object:  # type: ignore[override]
            raise RuntimeError("network down")

    gateway = MT5OrderGateway(RaisingClient(), mode=TradingMode.DEMO)
    result = gateway.submit_market_order(MARKET_REQUEST, price=Decimal("18000"))

    assert result.accepted is False
    assert "network down" in result.reason


def test_preflight_check_success() -> None:
    client = FakeMT5Client()
    client.set_order_check_result(make_order_check_result(retcode=10009))
    gateway = MT5OrderGateway(client, mode=TradingMode.DEMO)

    result = gateway.preflight_check(MARKET_REQUEST, price=Decimal("18000"))

    assert result.accepted is True
    assert len(client.order_check_calls) == 1


def test_close_position_gated_like_market_order() -> None:
    client = FakeMT5Client()
    client.set_order_send_result(make_order_send_result(retcode=10009))
    gateway = MT5OrderGateway(client, mode=TradingMode.DEMO)

    result = gateway.close_position(
        CloseRequest(ticket=1001, symbol="GER40.cash", volume=Decimal("1"), side="SELL"),
        price=Decimal("18010"),
    )

    assert result.accepted is True
    sent = client.order_send_calls[0]
    assert sent["position"] == 1001


def test_update_stop_gated() -> None:
    client = FakeMT5Client()
    client.set_order_send_result(make_order_send_result(retcode=10009))
    gateway = MT5OrderGateway(client, mode=TradingMode.MOCK)

    result = gateway.update_stop(
        StopUpdateRequest(ticket=1001, symbol="GER40.cash", new_stop_loss=Decimal("17900"))
    )

    assert result.accepted is False
    assert client.order_send_calls == []


def test_update_take_profit_demo_mode() -> None:
    client = FakeMT5Client()
    client.set_order_send_result(make_order_send_result(retcode=10009))
    gateway = MT5OrderGateway(client, mode=TradingMode.DEMO)

    result = gateway.update_take_profit(
        TakeProfitUpdateRequest(ticket=1001, symbol="GER40.cash", new_take_profit=Decimal("18200"))
    )

    assert result.accepted is True


def test_cancel_pending_gated() -> None:
    client = FakeMT5Client()
    client.set_order_send_result(make_order_send_result(retcode=10009))
    gateway = MT5OrderGateway(client, mode=TradingMode.PAPER)

    result = gateway.cancel_pending(CancelPendingRequest(ticket=2001, symbol="GER40.cash"))

    assert result.accepted is False
    assert client.order_send_calls == []


def test_market_order_request_validates_side() -> None:
    with pytest.raises(ValueError, match="side"):
        MarketOrderRequest(symbol="GER40.cash", side="LONG", volume=Decimal("1"))


def test_market_order_request_validates_positive_volume() -> None:
    with pytest.raises(ValueError, match="volume"):
        MarketOrderRequest(symbol="GER40.cash", side="BUY", volume=Decimal("0"))
