"""C5A: instrument provider, symbol registry, retcode semantics, restart-safe state, fake broker."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters.activtrades_mt5.fake_broker import FakeMT5Broker
from nautilus_mt5.constants import (
    ORDER_CHECK_OK,
    Filling,
    Retcode,
    SendOutcome,
    TradeAction,
    classify_send_retcode,
)
from nautilus_mt5.instruments import (
    InstrumentAssumptions,
    Mt5InstrumentError,
    Mt5InstrumentProvider,
)
from nautilus_mt5.state import Mt5StateStore, StateStoreError
from nautilus_mt5.symbols import GER40, SymbolMapping, SymbolRegistry, default_registry
from tests.unit.nautilus_mt5.conftest import real_symbol_info


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# -- instrument provider -------------------------------------------------------------------


def test_provider_loads_ger40_from_broker_metadata(broker, assumptions):
    provider = Mt5InstrumentProvider(broker, assumptions=assumptions)
    run(provider.load_all_async())
    instrument = provider.find(GER40.instrument_id)
    assert str(instrument.id) == "GER40.ACTIVTRADES"
    assert str(instrument.raw_symbol) == "Ger40"  # canonical vs broker symbol kept apart
    assert (instrument.price_precision, float(instrument.price_increment)) == (2, 0.01)
    assert (instrument.size_precision, float(instrument.size_increment)) == (2, 0.25)
    assert (float(instrument.min_quantity), float(instrument.max_quantity)) == (0.25, 250.0)
    assert str(instrument.quote_currency) == "EUR"
    assert instrument.info["stop_level_price"] == "1.00"  # trade_stops_level 100 * point


def test_unprovided_values_are_tagged_as_assumed_not_broker_truth(broker, assumptions):
    provider = Mt5InstrumentProvider(broker, assumptions=assumptions)
    run(provider.load_all_async())
    info = provider.find(GER40.instrument_id).info
    assert info["margin_source"] == "ASSUMED" and info["fee_source"] == "ASSUMED"
    assert info["broker_margin_initial"] is None  # broker reported 0.0 -> not provided
    with pytest.raises(ValueError):
        InstrumentAssumptions(margin_init=Decimal("-1"), margin_maint=Decimal("0.05"))


def test_provider_fails_closed_on_bad_broker_metadata(assumptions):
    info = vars(real_symbol_info())
    cases = {
        "no symbol": {"name": "Other"},
        "not under": {"path": "Futures\\Ger40"},
        "contract size": {"trade_contract_size": 10.0},
        "not tradable": {"trade_mode": 0},
    }
    for message, patch in cases.items():
        broker = FakeMT5Broker(SimpleNamespace(**{**info, **patch}))
        provider = Mt5InstrumentProvider(broker, assumptions=assumptions)
        with pytest.raises(Mt5InstrumentError, match=message):
            run(provider.load_all_async())


def test_registry_is_extensible_without_redesign_and_rejects_duplicates():
    registry = default_registry()
    registry.register(
        SymbolMapping(canonical="NAS100", broker_symbol="Nas100", expected_path_prefix="Cash")
    )
    assert {m.canonical for m in registry.all()} == {"GER40", "NAS100"}
    assert registry.by_broker_symbol("Nas100").canonical == "NAS100"
    with pytest.raises(ValueError):
        registry.register(GER40)
    with pytest.raises(KeyError):
        SymbolRegistry().by_instrument_id(GER40.instrument_id)


def test_load_ids_only_loads_requested(broker, assumptions):
    provider = Mt5InstrumentProvider(broker, assumptions=assumptions)
    run(provider.load_ids_async([GER40.instrument_id]))
    assert provider.count == 1


# -- retcode semantics ------------------------------------------------------------------------


def test_order_check_ok_is_zero_and_order_send_success_codes_differ():
    assert ORDER_CHECK_OK == 0
    assert classify_send_retcode(Retcode.DONE) is SendOutcome.ACCEPTED_FILLED
    assert classify_send_retcode(Retcode.DONE_PARTIAL) is SendOutcome.ACCEPTED_PARTIAL
    assert classify_send_retcode(Retcode.PLACED) is SendOutcome.ACCEPTED_PLACED
    assert classify_send_retcode(0) is SendOutcome.IN_DOUBT  # 0 is NOT an order_send success


@pytest.mark.parametrize(
    "code",
    [
        Retcode.REJECT,
        Retcode.INVALID_VOLUME,
        Retcode.INVALID_STOPS,
        Retcode.NO_MONEY,
        Retcode.MARKET_CLOSED,
        Retcode.INVALID_FILL,
        Retcode.TRADE_DISABLED,
        Retcode.REQUOTE,
    ],
)
def test_definite_rejections(code):
    assert classify_send_retcode(code) is SendOutcome.REJECTED


@pytest.mark.parametrize("code", [Retcode.TIMEOUT, Retcode.CONNECTION, Retcode.ERROR, 99999])
def test_unknown_outcomes_are_in_doubt_never_assumed_rejected(code):
    assert classify_send_retcode(code) is SendOutcome.IN_DOUBT


# -- restart-safe state -------------------------------------------------------------------------


def _intent(store, cid="O-1"):
    return store.record_intent(
        client_order_id=cid,
        strategy_id="S-1",
        instrument_id="GER40.ACTIVTRADES",
        kind="MARKET",
        side="BUY",
        quantity="0.25",
    )


def test_intent_token_is_write_ahead_idempotent_and_survives_restart(tmp_path):
    path = tmp_path / "state.db"
    store = Mt5StateStore(path)
    token = _intent(store)
    assert token.startswith("NT") and len(token) <= 31  # fits the MT5 comment field
    assert _intent(store) == token  # idempotent per ClientOrderId
    store.update_order("O-1", status="ACCEPTED", order_ticket=555, position_ticket=777)
    store.close()

    reopened = Mt5StateStore(path)
    row = reopened.by_client_order_id("O-1")
    assert (row.order_ticket, row.position_ticket, row.status) == (555, 777, "ACCEPTED")
    assert reopened.by_order_ticket(555).client_order_id == "O-1"
    assert reopened.by_token(token).client_order_id == "O-1"
    assert _intent(reopened, "O-2") != token  # tokens never reused across restarts


def test_unresolved_lists_in_doubt_orders_only():
    store = Mt5StateStore(":memory:")
    for cid in ("O-1", "O-2", "O-3"):
        _intent(store, cid)
    store.update_order("O-2", status="ACCEPTED", order_ticket=2)
    store.update_order("O-3", status="IN_DOUBT")
    assert {r.client_order_id for r in store.unresolved()} == {"O-1", "O-3"}


def test_deal_dedupe_marks_once_and_persists(tmp_path):
    path = tmp_path / "s.db"
    store = Mt5StateStore(path)
    assert not store.is_ingested(42)
    assert store.mark_ingested(42, 7) is True
    assert store.mark_ingested(42, 7) is False
    store.close()
    assert Mt5StateStore(path).is_ingested(42)


def test_unknown_client_order_id_update_raises():
    with pytest.raises(StateStoreError):
        Mt5StateStore(":memory:").update_order("nope", status="DONE")


# -- fake broker semantics (the test double must itself be faithful) -----------------------------


def market(broker, *, buy=True, volume=0.25, sl=0.0, tp=0.0, **extra):
    return {
        "action": TradeAction.DEAL,
        "symbol": "Ger40",
        "volume": volume,
        "type": 0 if buy else 1,
        "price": broker.ask if buy else broker.bid,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "type_filling": Filling.IOC,
        "type_time": 0,
        "magic": 1,
        "comment": "NT1",
        **extra,
    }


def test_order_check_success_is_retcode_zero_done(broker):
    result = broker.order_check(market(broker))
    assert result.retcode == 0 and result.comment == "Done"
    assert broker.order_check(market(broker, volume=0.3)).retcode == Retcode.INVALID_VOLUME


def test_market_buy_with_attached_stop_creates_netting_position_and_deal(broker):
    result = broker.order_send(market(broker, sl=24_900.0))
    assert result.retcode == Retcode.DONE
    (pos,) = broker.positions_get()
    assert (pos.volume, pos.sl, pos.type) == (0.25, 24_900.0, 0)
    (deal,) = broker.history_deals_get()
    assert deal.entry == 0 and deal.position_id == pos.identifier and deal.order == result.order


def test_stop_too_close_and_wrong_side_rejected(broker):
    assert broker.order_check(market(broker, sl=broker.bid - 0.5)).retcode == Retcode.INVALID_STOPS
    assert broker.order_check(market(broker, sl=broker.bid + 10)).retcode == Retcode.INVALID_STOPS


def test_broker_executes_attached_stop_and_books_a_reason_sl_deal(broker):
    broker.order_send(market(broker, sl=24_900.0))
    broker.set_quote(24_890.0, 24_891.5)
    assert broker.positions_get() == ()
    deals = broker.history_deals_get()
    assert [d.entry for d in deals] == [0, 1] and deals[-1].reason == 4  # DEAL_REASON_SL


def test_partial_and_progressive_fills_hide_deals_until_released(broker):
    broker.progressive = True
    broker.fill_plan.append([(0.25, 25_001.5), (0.25, 25_001.75), (0.5, 25_002.0)])
    assert broker.order_send(market(broker, volume=1.0)).retcode == Retcode.DONE
    assert broker.history_deals_get() == ()  # nothing visible yet
    first = sorted(d.ticket for d in broker.deals)[0]
    broker.release_deals(first)
    assert len(broker.history_deals_get()) == 1
    broker.release_deals()
    assert len(broker.history_deals_get()) == 3


def test_ioc_partial_returns_done_partial(broker):
    broker.fill_plan.append([(0.25, 25_001.5)])
    assert broker.order_send(market(broker, volume=1.0)).retcode == Retcode.DONE_PARTIAL


def test_lost_response_still_executes_at_broker(broker):
    broker.lose_response_after_execute = 1
    assert broker.order_send(market(broker)) is None
    assert len(broker.positions_get()) == 1 and broker.last_error()[0] != 1


def test_disconnect_makes_calls_fail_and_reconnect_restores(broker):
    broker.disconnect()
    assert broker.account_info() is None and broker.positions_get() is None
    assert broker.last_error()[0] == -10004
    broker.reconnect()
    assert broker.account_info().login == broker.cfg.login


def test_external_fill_is_broker_truth_we_did_not_request(broker):
    deal = broker.external_market_fill(is_buy=False, volume=0.5, comment="manual")
    assert broker.positions_get()[0].type == 1 and deal.comment == "manual"
