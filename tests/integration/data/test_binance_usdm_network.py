import pytest

from data.binance_usdm import BinanceUsdMDiscovery


@pytest.mark.skip(reason="real Binance network integration test; opt in by removing this skip")
def test_real_binance_usdm_exchange_info_contains_tradable_perpetuals() -> None:
    instruments = BinanceUsdMDiscovery().discover_instruments()

    assert any(item.status == "TRADING" for item in instruments)
    assert all(item.venue_symbol and item.price_tick > 0 for item in instruments)
