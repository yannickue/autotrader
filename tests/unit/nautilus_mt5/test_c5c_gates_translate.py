"""C5C (pure): outbound gates, reconciliation state machine, MT5 request translation."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters.activtrades_mt5.models import symbol_info_raw_from_mt5, symbol_info_to_instrument_spec
from nautilus_mt5.constants import Filling, TradeAction
from nautilus_mt5.gates import AdapterStatus, OutboundKind, admit
from nautilus_mt5.reconciliation import (
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    DiscrepancyKind,
    LocalView,
    ReconciliationTracker,
    compare,
)
from nautilus_mt5.translate import (
    Quote,
    RequestRejected,
    close_request,
    is_tightening,
    market_entry_request,
    normalize_volume,
    round_price,
    select_filling,
    sltp_request,
    validate_stops,
)
from risk.models import ReconciliationSource, ReconciliationState, RuntimeMode
from tests.unit.nautilus_mt5.conftest import real_symbol_info

VENUE = ReconciliationSource.VENUE_SNAPSHOT


def status(runtime, state, source=VENUE, unprotected=False):
    return AdapterStatus(
        runtime=runtime, reconciliation=state, source=source, unprotected_positions=unprotected
    )


RECONCILED = ReconciliationState.RECONCILED


# -- gates ---------------------------------------------------------------------------------------


def test_new_exposure_needs_ready_and_venue_reconciled():
    assert admit(OutboundKind.NEW_EXPOSURE, status(RuntimeMode.READY, RECONCILED)).ok
    for runtime in (
        RuntimeMode.HALTED,
        RuntimeMode.DEGRADED,
        RuntimeMode.RECONCILING,
        RuntimeMode.STARTING,
    ):
        assert not admit(OutboundKind.NEW_EXPOSURE, status(runtime, RECONCILED)).ok
    for state in (
        ReconciliationState.NOT_RECONCILED,
        ReconciliationState.RECONCILING,
        ReconciliationState.MISMATCH,
    ):
        assert not admit(OutboundKind.NEW_EXPOSURE, status(RuntimeMode.READY, state)).ok


def test_reconciled_without_venue_source_grants_nothing():
    for source in (None, ReconciliationSource.PAPER_SELF_CHECK):
        for kind in (OutboundKind.NEW_EXPOSURE, OutboundKind.REDUCE_ONLY):
            assert not admit(kind, status(RuntimeMode.READY, RECONCILED, source)).ok


def test_reduce_only_allowed_when_ready_or_halted_if_reconciled_only():
    assert admit(OutboundKind.REDUCE_ONLY, status(RuntimeMode.READY, RECONCILED)).ok
    assert admit(OutboundKind.REDUCE_ONLY, status(RuntimeMode.HALTED, RECONCILED)).ok
    assert not admit(OutboundKind.REDUCE_ONLY, status(RuntimeMode.DEGRADED, RECONCILED)).ok
    for state in (
        ReconciliationState.NOT_RECONCILED,
        ReconciliationState.RECONCILING,
        ReconciliationState.MISMATCH,
    ):
        assert not admit(OutboundKind.REDUCE_ONLY, status(RuntimeMode.HALTED, state)).ok


def test_verified_reduce_only_is_admitted_in_any_state_but_nothing_else_is():
    for state in (
        ReconciliationState.NOT_RECONCILED,
        ReconciliationState.RECONCILING,
        ReconciliationState.MISMATCH,
    ):
        for runtime in RuntimeMode:
            s = status(runtime, state)
            assert admit(OutboundKind.REDUCE_ONLY, s, position_verified=True).ok
            assert not admit(OutboundKind.REDUCE_ONLY, s).ok
            for kind in (OutboundKind.NEW_EXPOSURE, OutboundKind.PROTECT_LOOSEN_REMOVE):
                assert not admit(kind, s, position_verified=True).ok


def test_unprotected_position_blocks_new_exposure_but_not_reduce_only():
    s = status(RuntimeMode.READY, RECONCILED, unprotected=True)
    assert not admit(OutboundKind.NEW_EXPOSURE, s).ok
    assert admit(OutboundKind.REDUCE_ONLY, s).ok


def test_protection_tightening_needs_broker_evidence_loosening_needs_reconciliation():
    broken = status(RuntimeMode.DEGRADED, ReconciliationState.MISMATCH, None)
    assert not admit(OutboundKind.PROTECT_TIGHTEN, broken).ok
    assert admit(OutboundKind.PROTECT_TIGHTEN, broken, position_verified=True).ok
    assert not admit(OutboundKind.PROTECT_LOOSEN_REMOVE, broken, position_verified=True).ok
    assert admit(OutboundKind.PROTECT_LOOSEN_REMOVE, status(RuntimeMode.READY, RECONCILED)).ok
    assert admit(OutboundKind.CANCEL_UNFILLED, broken).ok


# -- reconciliation state machine
# ----------------------------------------------------------------------


def test_reconnect_and_restart_never_imply_reconciled():
    t = ReconciliationTracker()
    assert t.state is ReconciliationState.NOT_RECONCILED
    t.begin()
    t.complete_match(VENUE)
    assert t.state is RECONCILED and t.runtime is RuntimeMode.READY
    t.on_connect(2)  # reconnect
    assert t.state is ReconciliationState.NOT_RECONCILED and t.source is None


def test_paper_self_check_cannot_reconcile_a_broker_account():
    t = ReconciliationTracker()
    with pytest.raises(ValueError):
        t.complete_match(ReconciliationSource.PAPER_SELF_CHECK)
    assert t.state is ReconciliationState.NOT_RECONCILED


def test_mismatch_halts_and_is_stickier_than_invalidate():
    t = ReconciliationTracker()
    t.complete_mismatch(())
    assert t.state is ReconciliationState.MISMATCH and t.runtime is RuntimeMode.HALTED
    t.invalidate("connection lost")
    assert t.state is ReconciliationState.MISMATCH  # halt is not silently downgraded
    t.begin()
    t.complete_match(VENUE)
    assert t.runtime is RuntimeMode.READY  # only a fresh clean comparison clears it


def bpos(qty, symbol="Ger40", sl=24_900.0, ticket=11):
    return BrokerPosition(
        ticket=ticket,
        broker_symbol=symbol,
        signed_qty=Decimal(str(qty)),
        price_open=Decimal("25000"),
        stop_loss=None if sl is None else Decimal(str(sl)),
        take_profit=None,
        magic=1,
    )


def local(positions=None, known=()):
    return LocalView(
        positions={k: Decimal(str(v)) for k, v in (positions or {}).items()},
        protective_positions=frozenset(),
        known_order_tickets=frozenset(known),
    )


def kinds(found):
    return {d.kind for d in found}


def cmp(snapshot, view, **kw):
    return compare(
        snapshot,
        view,
        registered_symbols=frozenset({"Ger40"}),
        require_protection=kw.get("require_protection", True),
    )


def test_matching_state_has_no_discrepancies():
    found, unprotected = cmp(
        BrokerSnapshot(positions=(bpos(0.25),), open_orders=()), local({"Ger40": 0.25})
    )
    assert found == () and unprotected is False


def test_all_position_discrepancies_are_detected():
    snap = lambda *p: BrokerSnapshot(positions=tuple(p), open_orders=())  # noqa: E731
    assert kinds(cmp(snap(bpos(0.25)), local())[0]) == {DiscrepancyKind.UNEXPECTED_BROKER_POSITION}
    assert kinds(cmp(snap(), local({"Ger40": 0.25}))[0]) == {
        DiscrepancyKind.MISSING_BROKER_POSITION
    }
    assert kinds(cmp(snap(bpos(0.5)), local({"Ger40": 0.25}))[0]) == {
        DiscrepancyKind.POSITION_QTY_MISMATCH
    }
    assert kinds(cmp(snap(bpos(-0.25)), local({"Ger40": 0.25}))[0]) == {
        DiscrepancyKind.POSITION_SIDE_MISMATCH
    }
    assert kinds(cmp(snap(bpos(0.25, symbol="Other")), local())[0]) == {
        DiscrepancyKind.UNKNOWN_SYMBOL_POSITION
    }


def test_unknown_working_order_is_a_discrepancy_known_one_is_not():
    order = BrokerOrder(ticket=77, broker_symbol="Ger40", comment="x", magic=0)
    snap = BrokerSnapshot(positions=(), open_orders=(order,))
    assert kinds(cmp(snap, local())[0]) == {DiscrepancyKind.UNKNOWN_BROKER_ORDER}
    assert cmp(snap, local(known=[77]))[0] == ()


def test_missing_stop_is_flagged_as_unprotected_not_as_mismatch():
    found, unprotected = cmp(
        BrokerSnapshot(positions=(bpos(0.25, sl=None),), open_orders=()), local({"Ger40": 0.25})
    )
    assert found == () and unprotected is True
    _, ignore = cmp(
        BrokerSnapshot(positions=(bpos(0.25, sl=None),), open_orders=()),
        local({"Ger40": 0.25}),
        require_protection=False,
    )
    assert ignore is False


# -- MT5 request translation
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def spec():
    raw = symbol_info_raw_from_mt5(real_symbol_info())
    from datetime import UTC, datetime

    return symbol_info_to_instrument_spec(
        raw, canonical_symbol="GER40", retrieved_at=datetime(2026, 9, 29, tzinfo=UTC)
    )


QUOTE = Quote(bid=Decimal("25000.00"), ask=Decimal("25001.50"))


@pytest.mark.parametrize("qty", ["0", "-1", "0.1", "0.3", "0.30", "251", "NaN"])
def test_invalid_volumes_are_rejected_not_clamped(spec, qty):
    with pytest.raises(RequestRejected) as exc:
        normalize_volume(spec, Decimal(qty))
    assert exc.value.code == "INVALID_VOLUME"


@pytest.mark.parametrize("qty", ["0.25", "0.5", "1", "1.25", "250"])
def test_valid_volumes_follow_min_step_max(spec, qty):
    assert normalize_volume(spec, Decimal(qty)) == float(qty)


def test_filling_mode_comes_from_the_symbol_bitmask(spec):
    assert select_filling(spec, 3) is Filling.IOC  # observed GER40: FOK|IOC -> prefer IOC
    assert select_filling(spec, 1) is Filling.FOK
    with pytest.raises(RequestRejected, match="UNSUPPORTED_FILLING"):
        select_filling(spec, 0)
    with pytest.raises(RequestRejected):
        select_filling(spec, 4)  # RETURN-only is never used for market execution


def test_market_entry_request_shape_deviation_stops_and_precision(spec):
    req = market_entry_request(
        spec,
        symbol_filling_mask=3,
        is_buy=True,
        quantity=Decimal("0.25"),
        quote=QUOTE,
        magic=7,
        token="NT5",
        deviation_points=20,
        stop_loss=Decimal("24900.004"),
        take_profit=Decimal("25100"),
    )
    assert req == {
        "action": int(TradeAction.DEAL),
        "symbol": "Ger40",
        "volume": 0.25,
        "type": 0,
        "price": 25001.5,
        "sl": 24900.0,
        "tp": 25100.0,
        "deviation": 20,
        "magic": 7,
        "comment": "NT5",
        "type_time": 0,
        "type_filling": int(Filling.IOC),
    }
    sell = market_entry_request(
        spec,
        symbol_filling_mask=3,
        is_buy=False,
        quantity=Decimal("0.25"),
        quote=QUOTE,
        magic=7,
        token="NT6",
        deviation_points=20,
        stop_loss=Decimal("25100"),
    )
    assert sell["type"] == 1 and sell["price"] == 25000.0 and sell["tp"] == 0.0


def test_stops_must_be_on_the_correct_side_and_outside_the_broker_stop_level(spec):
    for sl in ("25000.5", "25100", "24999.5"):  # above bid / too close (level = 1.00)
        with pytest.raises(RequestRejected, match="INVALID_STOPS"):
            validate_stops(
                spec, position_is_long=True, quote=QUOTE, stop_loss=Decimal(sl), take_profit=None
            )
    with pytest.raises(RequestRejected):
        validate_stops(
            spec, position_is_long=False, quote=QUOTE, stop_loss=Decimal("24900"), take_profit=None
        )
    validate_stops(
        spec,
        position_is_long=True,
        quote=QUOTE,
        stop_loss=Decimal("24990"),
        take_profit=Decimal("25050"),
    )


def test_token_too_long_and_bad_price_rejected(spec):
    with pytest.raises(RequestRejected):
        market_entry_request(
            spec,
            symbol_filling_mask=3,
            is_buy=True,
            quantity=Decimal("0.25"),
            quote=QUOTE,
            magic=1,
            token="N" * 40,
            deviation_points=20,
        )
    with pytest.raises(RequestRejected):
        round_price(spec, Decimal("0"))


def test_close_request_binds_to_the_position_ticket_with_opposite_side(spec):
    req = close_request(
        spec,
        symbol_filling_mask=3,
        position_ticket=99,
        position_is_long=True,
        quantity=Decimal("0.25"),
        quote=QUOTE,
        magic=1,
        token="NT9",
        deviation_points=20,
    )
    assert req["position"] == 99 and req["type"] == 1 and req["price"] == 25000.0


def test_sltp_request_resends_the_unchanged_side_and_zero_removes(spec):
    req = sltp_request(
        spec,
        position_ticket=5,
        stop_loss=Decimal("24950"),
        take_profit=None,
        keep_tp=Decimal("25100"),
    )
    assert (req["sl"], req["tp"], req["action"]) == (24950.0, 25100.0, int(TradeAction.SLTP))
    assert sltp_request(spec, position_ticket=5, stop_loss=None, take_profit=None)["sl"] == 0.0


def test_tightening_definition():
    assert is_tightening(position_is_long=True, current_sl=None, new_sl=Decimal("1"))
    assert is_tightening(
        position_is_long=True, current_sl=Decimal("24900"), new_sl=Decimal("24950")
    )
    assert not is_tightening(
        position_is_long=True, current_sl=Decimal("24900"), new_sl=Decimal("24800")
    )
    assert is_tightening(
        position_is_long=False, current_sl=Decimal("25100"), new_sl=Decimal("25050")
    )


def test_symbol_namespace_is_used_only_for_fixture_construction():
    assert isinstance(real_symbol_info(), SimpleNamespace)
