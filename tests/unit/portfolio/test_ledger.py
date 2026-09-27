from decimal import Decimal

import pytest

from execution.models import OrderSide
from portfolio.ledger import Portfolio
from portfolio.models import Fill


def make_fill(
    fill_id, instrument="BTCUSDT-PERP", side=OrderSide.BUY, qty="1", price="100", fee="0"
):
    return Fill(
        fill_id=fill_id,
        instrument=instrument,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
    )


def test_apply_fill_opens_long_position():
    p = Portfolio(starting_balance=Decimal("10000"))
    applied = p.apply_fill(make_fill("f1", qty="2", price="100"))
    assert applied is True
    pos = p.positions["BTCUSDT-PERP"]
    assert pos.quantity == Decimal("2")
    assert pos.avg_entry_price == Decimal("100")


def test_duplicate_fill_id_is_noop():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", qty="2", price="100"))
    applied_again = p.apply_fill(make_fill("f1", qty="2", price="100"))
    assert applied_again is False
    assert p.positions["BTCUSDT-PERP"].quantity == Decimal("2")


def test_average_entry_price_weighted_on_add():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", qty="1", price="100"))
    p.apply_fill(make_fill("f2", qty="1", price="200"))
    pos = p.positions["BTCUSDT-PERP"]
    assert pos.quantity == Decimal("2")
    assert pos.avg_entry_price == Decimal("150")


def test_partial_close_realizes_pnl_long():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", qty="2", price="100"))
    p.apply_fill(make_fill("f2", side=OrderSide.SELL, qty="1", price="120"))
    pos = p.positions["BTCUSDT-PERP"]
    assert pos.quantity == Decimal("1")
    assert pos.avg_entry_price == Decimal("100")
    assert p.realized_pnl == Decimal("20")


def test_partial_close_realizes_pnl_short():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", side=OrderSide.SELL, qty="2", price="100"))
    p.apply_fill(make_fill("f2", side=OrderSide.BUY, qty="1", price="80"))
    pos = p.positions["BTCUSDT-PERP"]
    assert pos.quantity == Decimal("-1")
    assert p.realized_pnl == Decimal("20")


def test_position_flip_through_zero():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", side=OrderSide.BUY, qty="1", price="100"))
    p.apply_fill(make_fill("f2", side=OrderSide.SELL, qty="3", price="110"))
    pos = p.positions["BTCUSDT-PERP"]
    assert pos.quantity == Decimal("-2")
    assert pos.avg_entry_price == Decimal("110")
    assert p.realized_pnl == Decimal("10")


def test_exact_close_zeroes_avg_entry_price():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", side=OrderSide.BUY, qty="1", price="100"))
    p.apply_fill(make_fill("f2", side=OrderSide.SELL, qty="1", price="110"))
    pos = p.positions["BTCUSDT-PERP"]
    assert pos.quantity == Decimal("0")
    assert pos.avg_entry_price == Decimal("0")
    assert p.realized_pnl == Decimal("10")


def test_fees_reduce_equity():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", qty="1", price="100", fee="1.5"))
    assert p.fees == Decimal("1.5")
    assert p.equity == Decimal("10000") - Decimal("1.5")


def test_mark_and_unrealized_pnl_and_equity():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", qty="2", price="100"))
    p.mark("BTCUSDT-PERP", Decimal("110"))
    pos = p.positions["BTCUSDT-PERP"]
    assert pos.unrealized_pnl == Decimal("20")
    assert p.equity == Decimal("10000") + Decimal("20")


def test_mark_unrealized_pnl_short():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", side=OrderSide.SELL, qty="2", price="100"))
    p.mark("BTCUSDT-PERP", Decimal("90"))
    pos = p.positions["BTCUSDT-PERP"]
    assert pos.unrealized_pnl == Decimal("20")


def test_gross_net_and_instrument_notionals():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", instrument="AAA", side=OrderSide.BUY, qty="2", price="100"))
    p.apply_fill(make_fill("f2", instrument="BBB", side=OrderSide.SELL, qty="1", price="50"))
    p.mark("AAA", Decimal("100"))
    p.mark("BBB", Decimal("50"))
    assert p.gross_notional == Decimal("250")
    assert p.net_notional == Decimal("150")
    assert p.instrument_notionals == {"AAA": Decimal("200"), "BBB": Decimal("-50")}


def test_gross_and_net_notional_use_avg_entry_price_when_unmarked_long():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", qty="2", price="100"))
    # never marked -- must NOT be skipped / contribute zero exposure
    assert p.gross_notional == Decimal("200")
    assert p.net_notional == Decimal("200")
    assert p.instrument_notionals == {"BTCUSDT-PERP": Decimal("200")}


def test_gross_and_net_notional_use_avg_entry_price_when_unmarked_short():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", side=OrderSide.SELL, qty="3", price="50"))
    assert p.gross_notional == Decimal("150")
    assert p.net_notional == Decimal("-150")
    assert p.instrument_notionals == {"BTCUSDT-PERP": Decimal("-150")}


def test_notionals_switch_to_mark_price_once_marked():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", qty="2", price="100"))
    assert p.gross_notional == Decimal("200")  # avg-entry fallback
    p.mark("BTCUSDT-PERP", Decimal("110"))
    assert p.gross_notional == Decimal("220")  # now uses the mark
    assert p.net_notional == Decimal("220")
    assert p.instrument_notionals == {"BTCUSDT-PERP": Decimal("220")}


def test_flat_position_contributes_zero_notional_even_if_marked():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", qty="1", price="100"))
    p.apply_fill(make_fill("f2", side=OrderSide.SELL, qty="1", price="110"))
    p.mark("BTCUSDT-PERP", Decimal("200"))
    assert p.gross_notional == Decimal("0")
    assert p.net_notional == Decimal("0")
    assert p.instrument_notionals == {}


def test_reject_non_finite_or_non_positive_price_and_qty():
    p = Portfolio(starting_balance=Decimal("10000"))
    with pytest.raises(ValueError):
        p.apply_fill(make_fill("f1", qty="0", price="100"))
    with pytest.raises(ValueError):
        p.apply_fill(make_fill("f1", qty="-1", price="100"))
    with pytest.raises(ValueError):
        p.apply_fill(make_fill("f1", qty="1", price="0"))
    with pytest.raises(ValueError):
        p.apply_fill(make_fill("f1", qty="1", price="-5"))


def test_export_import_state_deterministic_roundtrip():
    p = Portfolio(starting_balance=Decimal("10000"))
    p.apply_fill(make_fill("f1", qty="2", price="100", fee="0.5"))
    p.apply_fill(make_fill("f2", side=OrderSide.SELL, qty="1", price="110"))
    p.mark("BTCUSDT-PERP", Decimal("105"))
    state = p.export_state()

    restored = Portfolio(starting_balance=Decimal("0"))
    restored.import_state(state)

    assert restored.export_state() == state
    assert restored.positions == p.positions
    assert restored.equity == p.equity
    # duplicate fill after restore is still recognized
    assert restored.apply_fill(make_fill("f1", qty="2", price="100", fee="0.5")) is False
