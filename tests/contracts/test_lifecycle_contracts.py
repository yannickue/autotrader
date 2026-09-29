"""Lifecycle contracts 18-20, expressed through the `RuntimeUnderTest` adapter.

These behaviors currently live only in the legacy runtime (pipeline + exit
engine + paper execution). The tests talk to the adapter, never to the pipeline
directly, so a future runtime implements the same adapter and reuses them.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from exits.models import ExitReason, TakeProfitStage
from signals.models import Direction

D = Decimal

_PARTIAL_TP = {
    "target_r_multiple": D("1"),
    "partial_take_profit_fraction": D("0.5"),
    "min_remaining_quantity": D("0.001"),
}


def _below_stop_price(rt) -> Decimal:
    """A price that trades through the initial invalidation stop (long)."""
    return rt.entry_price() - rt.initial_risk() - D("1")


def _target_price(rt) -> Decimal:
    return rt.entry_price() + rt.initial_risk() * D("1") + D("0.2")


# -- 18. no implicit pyramiding -----------------------------------------------------


def test_c18_same_direction_signal_does_not_add_to_an_open_position(runtime_factory) -> None:
    rt = runtime_factory()
    opened = rt.open_long()
    assert opened > 0

    attempt = rt.submit_signal(Direction.LONG, "pyramid-1")

    assert attempt.new_exposure_admitted is False
    assert attempt.position_qty == opened


def test_c18_repeated_same_direction_signals_never_accumulate_exposure(runtime_factory) -> None:
    rt = runtime_factory()
    opened = rt.open_long()

    for i in range(3):
        attempt = rt.submit_signal(Direction.LONG, f"pyramid-repeat-{i}")
        assert attempt.new_exposure_admitted is False

    assert rt.position_qty() == opened


def test_c18_opposite_signal_never_flips_through_zero_in_one_tick(runtime_factory) -> None:
    rt = runtime_factory()
    rt.open_long()

    reversal = rt.submit_signal(Direction.SHORT, "reversal-short")

    # The reversal closes the long; it must NOT open a short in the same tick.
    assert reversal.new_exposure_admitted is False
    assert reversal.position_qty == 0

    # Once flat, the next tick may open the opposite side.
    fresh = rt.submit_signal(Direction.SHORT, "fresh-short")
    assert fresh.new_exposure_admitted is True
    assert fresh.position_qty < 0


# -- 19. emergency exit remains separate from normal exits --------------------------


def test_c19_stale_exit_data_triggers_the_emergency_exit_not_a_normal_one(
    runtime_factory,
) -> None:
    rt = runtime_factory(max_market_data_age=timedelta(seconds=1))
    rt.open_long()

    # 5s old is stale for the exit policy. Price is also far below the stop, so
    # a NORMAL exit would fire on fresh data -- the emergency reason must win.
    tick = rt.exit_tick(_below_stop_price(rt), data_age=timedelta(seconds=5))

    assert tick.reason is ExitReason.EMERGENCY_RISK_EXIT
    assert tick.is_partial is False
    assert tick.position_closed is True
    assert tick.position_qty == 0


def test_c19_normal_stop_exit_is_never_labelled_emergency(runtime_factory) -> None:
    rt = runtime_factory(max_market_data_age=timedelta(seconds=1))
    rt.open_long()

    tick = rt.exit_tick(_below_stop_price(rt))  # fresh data, price through the stop

    assert tick.reason is ExitReason.INVALIDATION_STOP
    assert tick.reason is not ExitReason.EMERGENCY_RISK_EXIT
    assert tick.position_qty == 0


def test_c19_emergency_exit_is_full_size_even_when_a_partial_target_is_configured(
    runtime_factory,
) -> None:
    rt = runtime_factory(max_market_data_age=timedelta(seconds=1), **_PARTIAL_TP)
    rt.open_long()

    tick = rt.exit_tick(_target_price(rt), data_age=timedelta(seconds=5))

    assert tick.reason is ExitReason.EMERGENCY_RISK_EXIT
    assert tick.is_partial is False
    assert tick.position_qty == 0


def test_c19_halt_and_kill_switch_alone_never_trigger_any_exit(runtime_factory) -> None:
    rt = runtime_factory()
    opened = rt.open_long()
    rt.halt_execution()

    tick = rt.exit_tick(D("100"), kill_switch=True)

    assert tick.reason is None  # no auto-flatten, and in particular no emergency exit
    assert rt.position_qty() == opened


# -- 20. partial take-profit idempotency --------------------------------------------


def test_c20_partial_take_profit_fires_once_and_leaves_a_managed_remainder(
    runtime_factory,
) -> None:
    rt = runtime_factory(**_PARTIAL_TP)
    opened = rt.open_long()

    first = rt.exit_tick(_target_price(rt))

    assert first.reason is ExitReason.TAKE_PROFIT
    assert first.is_partial is True
    assert first.position_closed is False
    assert 0 < first.position_qty < opened


def test_c20_replaying_the_same_tick_never_applies_the_partial_twice(runtime_factory) -> None:
    rt = runtime_factory(**_PARTIAL_TP)
    rt.open_long()
    target = _target_price(rt)

    first = rt.exit_tick(target)
    assert first.reason is ExitReason.TAKE_PROFIT
    remaining = first.position_qty

    for _ in range(3):
        replay = rt.exit_tick(target)
        assert replay.reason is not ExitReason.TAKE_PROFIT
        assert replay.position_qty == remaining


def test_c20_duplicate_fill_delivery_never_applies_the_partial_twice(runtime_factory) -> None:
    rt = runtime_factory(**_PARTIAL_TP)
    rt.open_long()
    target = _target_price(rt)

    first = rt.exit_tick(target)
    assert first.reason is ExitReason.TAKE_PROFIT
    remaining = first.position_qty

    rt.replay_last_exit_fill()  # the very same fill delivered a second time
    assert rt.position_qty() == remaining

    after = rt.exit_tick(target)
    assert after.reason is not ExitReason.TAKE_PROFIT
    assert after.position_qty == remaining


def test_c20_duplicate_fill_does_not_advance_a_take_profit_ladder(runtime_factory) -> None:
    stages = (
        TakeProfitStage(r_multiple=D("1"), close_fraction=D("0.3")),
        TakeProfitStage(r_multiple=D("2"), close_fraction=D("0.3")),
    )
    rt = runtime_factory(take_profit_stages=stages, min_remaining_quantity=D("0.001"))
    opened = rt.open_long()
    entry, risk = rt.entry_price(), rt.initial_risk()

    tp1 = rt.exit_tick(entry + risk * D("1") + D("0.2"))
    assert tp1.reason is ExitReason.TAKE_PROFIT
    after_tp1 = tp1.position_qty
    assert 0 < after_tp1 < opened

    rt.replay_last_exit_fill()
    assert rt.position_qty() == after_tp1

    # TP1's price again: stage 1 must not re-fire (it is already completed).
    again = rt.exit_tick(entry + risk * D("1") + D("0.2"))
    assert again.reason is not ExitReason.TAKE_PROFIT
    assert again.position_qty == after_tp1

    # TP2 fires exactly once, as its own stage.
    tp2 = rt.exit_tick(entry + risk * D("2") + D("0.2"))
    assert tp2.reason is ExitReason.TAKE_PROFIT
    assert 0 < tp2.position_qty < after_tp1
    rt.replay_last_exit_fill()
    assert rt.position_qty() == tp2.position_qty
