"""Unit tests for the SQLite durable persistence/recovery layer.

Covers: round-trip write/read for every state type, idempotent writes (same
record written twice creates no duplicate rows), the full
restart -> recover -> duplicate-fill-replay cycle (no duplicated exposure),
and fail-closed detection of ambiguous/corrupt persisted state (a
state-format-version mismatch and a row that fails a sanity check).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from persistence.models import (
    FillRecord,
    HaltStateRecord,
    OrderRecord,
    PortfolioStateRecord,
    PositionRecord,
    ReconciliationStateRecord,
    ReduceOnlyReservationRecord,
    ReservationRecord,
)
from persistence.store import STATE_FORMAT_VERSION, SQLiteStore

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
LATER = datetime(2026, 1, 1, 12, 5, tzinfo=UTC)


# -- builders ---------------------------------------------------------------


def _position(**changes: object) -> PositionRecord:
    values: dict[str, object] = {
        "instrument": "BTCUSDT-PERP",
        "quantity": Decimal("1.0"),
        "avg_entry_price": Decimal("100"),
        "mark_price": None,
        "updated_at": NOW,
    }
    values.update(changes)
    return PositionRecord(**values)


def _order(**changes: object) -> OrderRecord:
    values: dict[str, object] = {
        "client_order_id": "co-1",
        "request_id": "req-1",
        "decision_id": "dec-1",
        "instrument": "BTCUSDT-PERP",
        "side": "buy",
        "order_type": "market",
        "time_in_force": "gtc",
        "quantity": Decimal("1.0"),
        "limit_price": None,
        "reduce_only": False,
        "created_at": NOW,
        "status": "new",
        "filled_quantity": Decimal("0"),
        "avg_fill_price": Decimal("0"),
        "role": "entry",
        "parent_client_order_id": None,
        "oco_sibling_id": None,
        "replaces_client_order_id": None,
        "replaced_by_client_order_id": None,
        "trigger_price": None,
        "take_profit_price": None,
        "updated_at": NOW,
    }
    values.update(changes)
    return OrderRecord(**values)


def _fill(**changes: object) -> FillRecord:
    values: dict[str, object] = {
        "fill_id": "fill-1",
        "instrument": "BTCUSDT-PERP",
        "side": "buy",
        "quantity": Decimal("1.0"),
        "price": Decimal("100"),
        "fee": Decimal("0.1"),
        "timestamp": NOW,
        "applied_at": NOW,
    }
    values.update(changes)
    return FillRecord(**values)


def _reservation(**changes: object) -> ReservationRecord:
    values: dict[str, object] = {
        "decision_id": "dec-1",
        "side": "buy",
        "abs_notional": Decimal("100"),
        "signed_notional": Decimal("100"),
        "updated_at": NOW,
    }
    values.update(changes)
    return ReservationRecord(**values)


def _reduce_only_reservation(**changes: object) -> ReduceOnlyReservationRecord:
    values: dict[str, object] = {
        "decision_id": "dec-2",
        "instrument": "BTCUSDT-PERP",
        "quantity": Decimal("0.5"),
        "updated_at": NOW,
    }
    values.update(changes)
    return ReduceOnlyReservationRecord(**values)


def _portfolio_state(**changes: object) -> PortfolioStateRecord:
    values: dict[str, object] = {
        "starting_balance": Decimal("10000"),
        "realized_pnl": Decimal("0"),
        "fees": Decimal("0"),
        "updated_at": NOW,
    }
    values.update(changes)
    return PortfolioStateRecord(**values)


def _reconciliation_state(**changes: object) -> ReconciliationStateRecord:
    values: dict[str, object] = {
        "mode": "ready",
        "reconciled": True,
        "mismatch_reason": None,
        "last_reconciled_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return ReconciliationStateRecord(**values)


def _halt_state(**changes: object) -> HaltStateRecord:
    values: dict[str, object] = {
        "halted": False,
        "reason": None,
        "updated_at": NOW,
    }
    values.update(changes)
    return HaltStateRecord(**values)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "trader_state.db"


@pytest.fixture
def store(db_path: Path) -> SQLiteStore:
    s = SQLiteStore(db_path)
    yield s
    s.close()


# -- round-trip write/read ----------------------------------------------------


def test_position_round_trip(store: SQLiteStore) -> None:
    store.upsert_position(_position(mark_price=Decimal("101.5")))
    fetched = store.get_position("BTCUSDT-PERP")
    assert fetched is not None
    assert fetched.quantity == Decimal("1.0")
    assert fetched.avg_entry_price == Decimal("100")
    assert fetched.mark_price == Decimal("101.5")
    assert store.list_positions() == [fetched]


def test_position_upsert_is_idempotent_no_duplicate_rows(store: SQLiteStore) -> None:
    store.upsert_position(_position())
    store.upsert_position(_position(quantity=Decimal("2.0"), updated_at=LATER))
    positions = store.list_positions()
    assert len(positions) == 1
    assert positions[0].quantity == Decimal("2.0")


def test_order_round_trip(store: SQLiteStore) -> None:
    order = _order(limit_price=Decimal("99.5"), trigger_price=Decimal("95"))
    store.upsert_order(order)
    fetched = store.get_order("co-1")
    assert fetched == order
    assert store.list_orders() == [order]


def test_order_upsert_is_idempotent_no_duplicate_rows(store: SQLiteStore) -> None:
    store.upsert_order(_order())
    store.upsert_order(_order(status="accepted", updated_at=LATER))
    orders = store.list_orders()
    assert len(orders) == 1
    assert orders[0].status == "accepted"


def test_fill_round_trip(store: SQLiteStore) -> None:
    fill = _fill()
    assert store.record_fill(fill) is True
    assert store.has_fill("fill-1") is True
    assert store.list_fills() == [fill]


def test_fill_write_is_idempotent_duplicate_rejected(store: SQLiteStore) -> None:
    first = _fill()
    duplicate = _fill(price=Decimal("999"))  # same fill_id, different payload
    assert store.record_fill(first) is True
    assert store.record_fill(duplicate) is False
    fills = store.list_fills()
    assert len(fills) == 1
    assert fills[0].price == Decimal("100")  # first write wins, no overwrite


def test_reservation_round_trip_and_release(store: SQLiteStore) -> None:
    reservation = _reservation()
    store.upsert_reservation(reservation)
    assert store.list_reservations() == [reservation]
    store.release_reservation("dec-1")
    assert store.list_reservations() == []
    # releasing again is a no-op, not an error
    store.release_reservation("dec-1")


def test_reduce_only_reservation_round_trip_and_release(store: SQLiteStore) -> None:
    reservation = _reduce_only_reservation()
    store.upsert_reduce_only_reservation(reservation)
    assert store.list_reduce_only_reservations() == [reservation]
    store.release_reduce_only_reservation("dec-2")
    assert store.list_reduce_only_reservations() == []


def test_execution_id_round_trip_is_idempotent(store: SQLiteStore) -> None:
    assert store.record_execution_id("client_order_id", "co-1", recorded_at=NOW) is True
    assert store.record_execution_id("client_order_id", "co-1", recorded_at=LATER) is False
    assert store.has_execution_id("client_order_id", "co-1") is True
    assert store.has_execution_id("client_order_id", "co-unknown") is False
    assert store.list_execution_ids() == [("client_order_id", "co-1")]


def test_portfolio_state_round_trip_upsert_replaces(store: SQLiteStore) -> None:
    store.set_portfolio_state(_portfolio_state())
    store.set_portfolio_state(_portfolio_state(realized_pnl=Decimal("50"), updated_at=LATER))
    fetched = store.get_portfolio_state()
    assert fetched is not None
    assert fetched.realized_pnl == Decimal("50")


def test_reconciliation_state_round_trip(store: SQLiteStore) -> None:
    state = _reconciliation_state(
        mode="reconciling", reconciled=False, mismatch_reason="unknown order"
    )
    store.set_reconciliation_state(state)
    fetched = store.get_reconciliation_state()
    assert fetched == state


def test_halt_state_round_trip(store: SQLiteStore) -> None:
    state = _halt_state(halted=True, reason="RECONCILIATION_MISMATCH: unknown order")
    store.set_halt_state(state)
    fetched = store.get_halt_state()
    assert fetched == state


def test_empty_store_recover_returns_empty_snapshot(store: SQLiteStore) -> None:
    result = store.recover()
    assert result.ok is True
    assert result.snapshot is not None
    assert result.snapshot.positions == ()
    assert result.snapshot.orders == ()
    assert result.snapshot.fills == ()
    assert result.snapshot.reservations == ()
    assert result.snapshot.portfolio_state is None
    assert result.snapshot.halt_state is None


# -- full restart / recovery / duplicate-replay cycle -------------------------


def test_full_restart_recovery_and_duplicate_fill_replay_causes_no_double_exposure(
    db_path: Path,
) -> None:
    # 1. Open a simulated position: an order accepted and fully filled.
    store1 = SQLiteStore(db_path)
    order = _order(status="filled", filled_quantity=Decimal("1.0"), avg_fill_price=Decimal("100"))
    store1.upsert_order(order)
    fill = _fill()
    assert store1.record_fill(fill) is True  # first application, not a duplicate

    # Position/portfolio math a real execution engine would perform after
    # applying that fill exactly once.
    position = _position(quantity=Decimal("1.0"), avg_entry_price=Decimal("100"))
    store1.upsert_position(position)
    store1.set_portfolio_state(_portfolio_state(fees=Decimal("0.1")))
    reservation = _reservation()
    store1.upsert_reservation(reservation)
    store1.record_execution_id("client_order_id", order.client_order_id, recorded_at=NOW)

    # 2 & 3. Persist (every write above already committed) and terminate.
    store1.close()

    # 4. Restart: a fresh store instance over the same file.
    store2 = SQLiteStore(db_path)

    # 5. Reconstruct state.
    result = store2.recover()
    assert result.ok is True
    snapshot = result.snapshot
    assert snapshot is not None
    assert [p.instrument for p in snapshot.positions] == ["BTCUSDT-PERP"]
    assert snapshot.positions[0].quantity == Decimal("1.0")
    assert [f.fill_id for f in snapshot.fills] == ["fill-1"]
    assert [r.decision_id for r in snapshot.reservations] == ["dec-1"]

    # 6. Replay a duplicate fill event against the reconstructed state.
    assert store2.has_fill(fill.fill_id) is True  # idempotency-key check a caller must run
    replayed_is_new = store2.record_fill(fill)
    assert replayed_is_new is False  # the store rejects the replay

    # A real caller would gate position application on `replayed_is_new`; since
    # it is False here, it must NOT re-apply the fill to the position. Prove
    # that if that contract is honored, exposure is not duplicated.
    unchanged_position = store2.get_position("BTCUSDT-PERP")
    assert unchanged_position is not None
    assert unchanged_position.quantity == Decimal("1.0")  # still 1x, not 2x

    store2.close()


# -- fail-closed corruption / ambiguity detection ------------------------------


def test_recover_detects_state_format_version_mismatch(db_path: Path) -> None:
    store1 = SQLiteStore(db_path)
    store1.upsert_position(_position())
    store1.close()

    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "UPDATE meta SET value = ? WHERE key = 'state_format_version'", ("999-future",)
        )
        raw.commit()
    finally:
        raw.close()

    store2 = SQLiteStore(db_path)
    result = store2.recover()
    store2.close()

    assert result.ok is False
    assert result.snapshot is None
    assert result.error is not None
    assert "state_format_version" in result.error
    assert result.details["found_version"] == "999-future"
    assert result.details["expected_version"] == STATE_FORMAT_VERSION


def test_recover_detects_unparseable_row_value(db_path: Path) -> None:
    store1 = SQLiteStore(db_path)
    store1.upsert_position(_position())
    store1.close()

    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "UPDATE positions SET quantity = 'not-a-decimal' WHERE instrument = 'BTCUSDT-PERP'"
        )
        raw.commit()
    finally:
        raw.close()

    store2 = SQLiteStore(db_path)
    result = store2.recover()
    store2.close()

    assert result.ok is False
    assert result.snapshot is None
    assert result.error is not None


def test_recover_detects_row_that_fails_sanity_check(db_path: Path) -> None:
    # A legitimately-written order (filled_quantity <= quantity, validated by
    # OrderRecord.__post_init__ at write time), then corrupted directly at
    # the SQL layer so filled_quantity now exceeds quantity -- something the
    # Python API can never produce, but a hand-edited/corrupted DB file could.
    store1 = SQLiteStore(db_path)
    store1.upsert_order(_order(quantity=Decimal("1.0"), filled_quantity=Decimal("0")))
    store1.close()

    raw = sqlite3.connect(db_path)
    try:
        raw.execute("UPDATE orders SET filled_quantity = '2.0' WHERE client_order_id = 'co-1'")
        raw.commit()
    finally:
        raw.close()

    store2 = SQLiteStore(db_path)
    result = store2.recover()
    store2.close()

    assert result.ok is False
    assert result.snapshot is None
    assert result.error is not None


def test_recover_detects_non_utc_timestamp_row(db_path: Path) -> None:
    store1 = SQLiteStore(db_path)
    store1.upsert_position(_position())
    store1.close()

    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "UPDATE positions SET updated_at = '2026-01-01T12:00:00' "
            "WHERE instrument = 'BTCUSDT-PERP'"
        )
        raw.commit()
    finally:
        raw.close()

    store2 = SQLiteStore(db_path)
    result = store2.recover()
    store2.close()

    assert result.ok is False


def test_reopen_without_any_corruption_recovers_ok(db_path: Path) -> None:
    store1 = SQLiteStore(db_path)
    store1.upsert_position(_position())
    store1.close()

    store2 = SQLiteStore(db_path)
    result = store2.recover()
    store2.close()

    assert result.ok is True
