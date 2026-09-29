"""Portfolio per-fill PnL ledger feeding the trading-day window (restart-safe)."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from execution.models import OrderSide
from portfolio.ledger import Portfolio
from portfolio.models import Fill
from risk.trading_day import TradingDayPolicy, realized_pnl_today

INSTRUMENT = "GER40"
DAY1 = datetime(2026, 3, 9, 10, 0, tzinfo=UTC)
DAY2 = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)


def _fill(fid: str, side: OrderSide, price: str, ts: datetime, fee: str = "1") -> Fill:
    return Fill(
        fill_id=fid,
        instrument=INSTRUMENT,
        side=side,
        quantity=Decimal("1"),
        price=Decimal(price),
        fee=Decimal(fee),
        timestamp=ts,
    )


def _two_day_portfolio() -> Portfolio:
    p = Portfolio(starting_balance=Decimal("1000"))
    p.apply_fill(_fill("a", OrderSide.BUY, "100", DAY1))
    p.apply_fill(_fill("b", OrderSide.SELL, "90", DAY1))  # -10 realized on day 1
    p.apply_fill(_fill("c", OrderSide.BUY, "100", DAY2))
    p.apply_fill(_fill("d", OrderSide.SELL, "98", DAY2))  # -2 realized on day 2
    return p


def test_all_time_realized_pnl_is_not_todays_realized_pnl() -> None:
    p = _two_day_portfolio()
    assert p.realized_pnl == Decimal("-12")  # all-time
    today = realized_pnl_today(p.pnl_entries, now=DAY2, policy=TradingDayPolicy())
    assert today == Decimal("-4")  # day 2 only: -2 realized, 2 fees
    assert today != p.realized_pnl - p.fees


def test_duplicate_fill_does_not_create_a_second_entry() -> None:
    p = _two_day_portfolio()
    assert p.apply_fill(_fill("d", OrderSide.SELL, "98", DAY2)) is False
    assert len(p.pnl_entries) == 4


def test_export_import_restart_preserves_day_windowing() -> None:
    p = _two_day_portfolio()
    restored = Portfolio(starting_balance=Decimal("0"))
    restored.import_state(p.export_state())
    assert restored.pnl_entries == p.pnl_entries
    today = realized_pnl_today(restored.pnl_entries, now=DAY2, policy=TradingDayPolicy())
    assert today == Decimal("-4")


def test_legacy_checkpoint_all_time_profit_cannot_mask_a_loss_today() -> None:
    p = Portfolio(starting_balance=Decimal("1000"))
    p.apply_fill(_fill("a", OrderSide.BUY, "100", DAY1, fee="0"))
    p.apply_fill(_fill("b", OrderSide.SELL, "150", DAY1, fee="0"))  # +50 all-time
    state = p.export_state()
    del state["pnl_entries"]
    restored = Portfolio(starting_balance=Decimal("0"))
    restored.import_state(state)
    restored.apply_fill(_fill("c", OrderSide.BUY, "100", DAY2, fee="0"))
    restored.apply_fill(_fill("d", OrderSide.SELL, "90", DAY2, fee="0"))  # -10 today
    today = realized_pnl_today(restored.pnl_entries, now=DAY2, policy=TradingDayPolicy())
    assert today == Decimal("-10")  # not +40


def test_checkpoint_whose_ledger_disagrees_with_totals_is_rejected() -> None:
    import pytest

    state = _two_day_portfolio().export_state()
    state["pnl_entries"][0]["realized_pnl"] = "999"
    with pytest.raises(ValueError):
        Portfolio(starting_balance=Decimal("0")).import_state(state)


def test_legacy_checkpoint_without_ledger_is_counted_conservatively_as_today() -> None:
    p = _two_day_portfolio()
    state = p.export_state()
    del state["pnl_entries"]  # a checkpoint written before this fix
    restored = Portfolio(starting_balance=Decimal("0"))
    restored.import_state(state)
    # Day of past fills is unknowable -> all-time totals are treated as today (fail-closed).
    today = realized_pnl_today(restored.pnl_entries, now=DAY2, policy=TradingDayPolicy())
    assert today == Decimal("-16")


def test_malformed_entries_and_fills_are_rejected_atomically() -> None:
    import pytest

    from risk.trading_day import RealizedPnlEntry

    with pytest.raises(ValueError):
        RealizedPnlEntry(timestamp=datetime(2026, 3, 10, 1, 0), realized_pnl=Decimal("1"))
    with pytest.raises(ValueError):
        RealizedPnlEntry(
            timestamp=datetime(2026, 3, 10, 1, 0, tzinfo=timezone(timedelta(hours=2))),
            realized_pnl=Decimal("1"),
        )
    with pytest.raises(ValueError):
        RealizedPnlEntry(timestamp=None, realized_pnl=Decimal("Infinity"))
    with pytest.raises(ValueError):
        RealizedPnlEntry(timestamp=None, realized_pnl=Decimal("1"), fee=Decimal("NaN"))

    p = Portfolio(starting_balance=Decimal("1000"))
    with pytest.raises(ValueError):  # naive fill timestamp: rejected before any mutation
        p.apply_fill(_fill("n", OrderSide.BUY, "100", datetime(2026, 3, 10, 1, 0)))
    assert p.positions == {} and p.pnl_entries == () and not p.has_fill("n")


def test_import_rejects_malformed_checkpoint_and_leaves_ledger_unchanged() -> None:
    import pytest

    good = _two_day_portfolio()
    before = good.export_state()

    naive = good.export_state()
    naive["pnl_entries"][0]["timestamp"] = "2026-03-09T10:00:00"
    infinite = good.export_state()
    infinite["realized_pnl"] = "Infinity"
    infinite["pnl_entries"][0]["realized_pnl"] = "Infinity"
    for bad in (naive, infinite):
        with pytest.raises(ValueError):
            good.import_state(bad)
        assert good.export_state() == before  # atomic: nothing half-applied
