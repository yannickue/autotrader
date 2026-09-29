"""Shared raw-MT5-shaped fixture builders for activtrades_mt5 tests.

Every builder returns a `types.SimpleNamespace` with real MT5 field names
(verified per `models.py`'s module docstring) -- no `MetaTrader5` import
anywhere in this test package.
"""

from types import SimpleNamespace


def make_symbol_info(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "name": "GER40.cash",
        "description": "DAX 40 Index CFD",
        "digits": 2,
        "point": 0.01,
        "trade_tick_size": 0.01,
        "trade_tick_value": 1.0,
        "trade_tick_value_profit": 1.0,
        "trade_tick_value_loss": 1.0,
        "trade_contract_size": 1.0,
        "volume_min": 0.1,
        "volume_max": 100.0,
        "volume_step": 0.1,
        "volume_limit": 0.0,
        "currency_base": "EUR",
        "currency_profit": "EUR",
        "currency_margin": "EUR",
        "margin_initial": 0.0,
        "margin_maintenance": 0.0,
        "margin_hedged": 0.0,
        "trade_calc_mode": 3,  # SYMBOL_CALC_MODE_CFDINDEX
        "trade_mode": 4,  # SYMBOL_TRADE_MODE_FULL
        "trade_exemode": 2,  # SYMBOL_TRADE_EXECUTION_MARKET
        "filling_mode": 0b11,  # FOK + IOC
        "trade_stops_level": 50,
        "trade_freeze_level": 0,
        "select": True,
        "visible": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_tick(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "time": 1_700_000_000,
        "bid": 18000.5,
        "ask": 18001.0,
        "last": 18000.7,
        "volume": 12.0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_account_info(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "login": 12345678,
        "balance": 10000.0,
        "equity": 10050.0,
        "margin": 500.0,
        "margin_free": 9550.0,
        "margin_level": 2010.0,
        "currency": "EUR",
        "leverage": 30,
        "trade_allowed": True,
        "trade_mode": 0,  # ACCOUNT_TRADE_MODE_DEMO
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_terminal_info(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "connected": True,
        "trade_allowed": True,
        "community_connection": False,
        "company": "ActivTrades",
        "name": "MetaTrader 5",
        "build": 4500,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_position(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "ticket": 1001,
        "symbol": "GER40.cash",
        "type": 0,  # POSITION_TYPE_BUY
        "volume": 1.0,
        "price_open": 18000.0,
        "price_current": 18010.0,
        "sl": 17950.0,
        "tp": 18100.0,
        "swap": -1.5,
        "profit": 10.0,
        "time": 1_700_000_000,
        "time_update": 1_700_000_100,
        "magic": 42,
        "identifier": 1001,
        "comment": "entry",
        "external_id": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_order(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "ticket": 2001,
        "symbol": "GER40.cash",
        "type": 2,  # ORDER_TYPE_BUY_LIMIT
        "state": 1,  # ORDER_STATE_PLACED
        "volume_initial": 1.0,
        "volume_current": 1.0,
        "price_open": 17990.0,
        "price_current": 18000.0,
        "sl": 0.0,
        "tp": 0.0,
        "time_setup": 1_700_000_000,
        "time_expiration": 0,
        "magic": 42,
        "position_id": 0,
        "comment": "",
        "external_id": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_deal(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "ticket": 3001,
        "order": 2001,
        "symbol": "GER40.cash",
        "type": 0,  # DEAL_TYPE_BUY
        "entry": 0,  # DEAL_ENTRY_IN
        "volume": 1.0,
        "price": 18000.0,
        "commission": -0.5,
        "swap": 0.0,
        "profit": 0.0,
        "fee": 0.0,
        "time": 1_700_000_000,
        "position_id": 1001,
        "magic": 42,
        "comment": "",
        "external_id": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_order_check_result(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        # order_check()'s own success convention (retcode=0, not
        # TRADE_RETCODE_DONE=10009) -- verified against a real ActivTrades
        # demo account, 2026-09-29. See OrderCheckResult's docstring.
        "retcode": 0,
        "comment": "Done",
        "balance": 10000.0,
        "equity": 10050.0,
        "profit": 0.0,
        "margin": 500.0,
        "margin_free": 9550.0,
        "margin_level": 2010.0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_order_send_result(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "retcode": 10009,
        "comment": "Request executed",
        "deal": 3002,
        "order": 2002,
        "volume": 1.0,
        "price": 18000.0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)
