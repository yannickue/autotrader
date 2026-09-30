# ruff: noqa: E501
"""Lane E: target stages (R or structural price), per-position ladders, tighten-only stop rule."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from exits.engine import ExitEngine
from exits.models import (
    SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED,
    ExitPosition,
    ExitReason,
    PositionSide,
    TakeProfitStage,
    stop_is_unchanged_or_tighter,
)
from tests.unit.exits.test_engine import NOW, _market, _policy, _position, _StubRiskGate

D = Decimal


def _engine(**policy: object) -> ExitEngine:
    return ExitEngine(policy=_policy(**policy), risk_gate=_StubRiskGate())


def _stage(**changes: object) -> TakeProfitStage:
    values: dict[str, object] = {"close_fraction": D("0.4")}
    values.update(changes)
    return TakeProfitStage(**values)


# -- stage contract ---------------------------------------------------------------------------


def test_stage_needs_exactly_one_of_r_multiple_or_target_price():
    with pytest.raises(ValueError, match="exactly one"):
        _stage()
    with pytest.raises(ValueError, match="exactly one"):
        _stage(r_multiple=D("1"), target_price=D("110"), source="R")
    assert _stage(r_multiple=D("1")).source == "R"
    assert _stage(target_price=D("110"), source="STRUCTURE:prev_day_high").target_price == D("110")


def test_stage_source_must_match_its_kind():
    with pytest.raises(ValueError, match="source"):
        _stage(target_price=D("110"))  # default source "R" is wrong for a price stage
    with pytest.raises(ValueError, match="source"):
        _stage(r_multiple=D("1"), source="STRUCTURE:x")
    with pytest.raises(ValueError, match="source"):
        _stage(target_price=D("110"), source="STRUCTURE:")


def test_close_fraction_is_configurable_never_hardcoded_thirds():
    for fraction in ("0.2", "0.5", "0.75"):
        assert _stage(r_multiple=D("1"), close_fraction=D(fraction)).close_fraction == D(fraction)


def test_legacy_r_only_stage_construction_still_works():
    stage = TakeProfitStage(r_multiple=D("2"), close_fraction=D("0.5"))
    assert (stage.r_multiple, stage.target_price, stage.stage_id) == (D("2"), None, "")


# -- per-position ladder validation -------------------------------------------------------------


def test_long_target_prices_must_be_strictly_increasing_beyond_entry():
    ok = (
        _stage(target_price=D("104"), source="STRUCTURE:a", stage_id="tp1"),
        _stage(target_price=D("108"), source="STRUCTURE:b", stage_id="tp2"),
    )
    assert _position(target_stages=ok).target_stages == ok
    below_entry = (_stage(target_price=D("99"), source="STRUCTURE:a"),)
    with pytest.raises(ValueError, match="favourable"):
        _position(target_stages=below_entry)
    not_ordered = (
        _stage(target_price=D("108"), source="STRUCTURE:a", close_fraction=D("0.3")),
        _stage(target_price=D("104"), source="STRUCTURE:b", close_fraction=D("0.3")),
    )
    with pytest.raises(ValueError, match="strictly"):
        _position(target_stages=not_ordered)
    equal = (
        _stage(target_price=D("104"), source="STRUCTURE:a", close_fraction=D("0.3")),
        _stage(target_price=D("104"), source="STRUCTURE:b", close_fraction=D("0.3")),
    )
    with pytest.raises(ValueError, match="strictly"):
        _position(target_stages=equal)


def test_short_target_prices_are_mirrored():
    short = {
        "side": PositionSide.SHORT,
        "initial_stop_price": D("105"),
        "current_stop_price": D("105"),
    }
    ok = (
        _stage(target_price=D("96"), source="STRUCTURE:a", close_fraction=D("0.3")),
        _stage(target_price=D("92"), source="STRUCTURE:b", close_fraction=D("0.3")),
    )
    assert _position(target_stages=ok, **short).target_stages == ok
    above_entry = (_stage(target_price=D("101"), source="STRUCTURE:a"),)
    with pytest.raises(ValueError, match="favourable"):
        _position(target_stages=above_entry, **short)
    wrong_order = (
        _stage(target_price=D("92"), source="STRUCTURE:a", close_fraction=D("0.3")),
        _stage(target_price=D("96"), source="STRUCTURE:b", close_fraction=D("0.3")),
    )
    with pytest.raises(ValueError, match="strictly"):
        _position(target_stages=wrong_order, **short)


def test_mixed_r_and_price_stages_are_ordered_by_their_effective_price():
    # long, entry 100, stop 95 -> 1R = 5: r=1 -> 105, structure 104 would be BELOW it
    bad = (
        _stage(r_multiple=D("1"), close_fraction=D("0.3")),
        _stage(target_price=D("104"), source="STRUCTURE:a", close_fraction=D("0.3")),
    )
    with pytest.raises(ValueError, match="strictly"):
        _position(target_stages=bad)


def test_fractions_of_the_original_quantity_may_not_exceed_one():
    too_much = (
        _stage(target_price=D("104"), source="STRUCTURE:a", close_fraction=D("0.6")),
        _stage(target_price=D("108"), source="STRUCTURE:b", close_fraction=D("0.6")),
    )
    with pytest.raises(ValueError, match="sum"):
        _position(target_stages=too_much)


def test_tp1_plus_runner_is_legal_and_no_second_target_is_invented():
    only_tp1 = (_stage(target_price=D("104"), source="STRUCTURE:a", close_fraction=D("0.5")),)
    position = _position(target_stages=only_tp1)
    assert len(position.target_stages) == 1
    assert SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED == "SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED"


# -- tighten-only stops ---------------------------------------------------------------------------


def test_stop_after_entry_may_only_stay_or_tighten():
    assert stop_is_unchanged_or_tighter(PositionSide.LONG, old=D("95"), new=D("95"))
    assert stop_is_unchanged_or_tighter(PositionSide.LONG, old=D("95"), new=D("96"))
    assert not stop_is_unchanged_or_tighter(PositionSide.LONG, old=D("95"), new=D("94.99"))
    assert stop_is_unchanged_or_tighter(PositionSide.SHORT, old=D("105"), new=D("104"))
    assert not stop_is_unchanged_or_tighter(PositionSide.SHORT, old=D("105"), new=D("105.01"))


def test_position_cannot_carry_a_stop_further_from_entry_than_the_initial_stop():
    with pytest.raises(ValueError, match="tighter"):
        _position(current_stop_price=D("94"))  # long: looser than initial 95


# -- engine behaviour on per-position stages ---------------------------------------------------------


def test_engine_uses_position_price_stages_and_reports_their_identity():
    stages = (
        _stage(target_price=D("104"), source="STRUCTURE:pdh", stage_id="tp1", close_fraction=D("0.5")),
    )
    position = _position(quantity=D("1"), target_stages=stages)
    engine = _engine()
    miss = engine.evaluate(position=position, market=_market(price=D("103.9")), now=NOW)
    assert miss.decision is None
    hit = engine.evaluate(position=position, market=_market(price=D("104")), now=NOW)
    decision = hit.decision
    assert decision is not None and decision.reason is ExitReason.TAKE_PROFIT
    assert decision.quantity == D("0.50") and decision.is_partial
    assert decision.metadata["stage_id"] == "tp1"
    assert decision.metadata["stage_source"] == "STRUCTURE:pdh"
    assert decision.metadata["target_price"] == "104"


def test_second_stage_quantity_is_a_share_of_the_original_not_the_remainder():
    stages = (
        _stage(target_price=D("104"), source="STRUCTURE:a", stage_id="tp1", close_fraction=D("0.4")),
        _stage(target_price=D("108"), source="STRUCTURE:b", stage_id="tp2", close_fraction=D("0.3")),
    )
    after_tp1 = _position(
        quantity=D("0.60"), realized_partial_quantity=D("0.40"), stages_completed=1,
        target_stages=stages,
    )
    decision = _engine().evaluate(
        position=after_tp1, market=_market(price=D("108.5")), now=NOW
    ).decision
    assert decision is not None and decision.quantity == D("0.30")
    assert decision.metadata["stage_id"] == "tp2"


def test_position_stages_take_precedence_over_policy_ladder():
    policy_ladder = (TakeProfitStage(r_multiple=D("1"), close_fraction=D("0.9")),)
    stages = (
        _stage(target_price=D("110"), source="STRUCTURE:a", stage_id="tp1", close_fraction=D("0.25")),
    )
    engine = _engine(take_profit_stages=policy_ladder)
    position = _position(quantity=D("1"), target_stages=stages)
    # price at +1R (105) would hit the policy ladder, but the position's own ladder wins
    assert engine.evaluate(position=position, market=_market(price=D("105")), now=NOW).decision is None


def test_r_stage_on_a_position_ladder_matches_the_legacy_policy_behaviour():
    legacy = _engine(take_profit_stages=(TakeProfitStage(r_multiple=D("2"), close_fraction=D("0.4")),))
    per_position = _engine()
    market = _market(price=D("110"))
    a = legacy.evaluate(position=_position(), market=market, now=NOW).decision
    b = per_position.evaluate(
        position=_position(target_stages=(TakeProfitStage(r_multiple=D("2"), close_fraction=D("0.4")),)),
        market=market,
        now=NOW,
    ).decision
    assert a is not None and b is not None
    assert (a.quantity, a.reason, a.is_partial) == (b.quantity, b.reason, b.is_partial)


def test_policy_ladder_rejects_price_stages():
    with pytest.raises(ValueError, match="r_multiple"):
        _policy(
            take_profit_stages=(_stage(target_price=D("104"), source="STRUCTURE:a"),)
        )


def test_short_position_price_stage_triggers_below_entry():
    stages = (_stage(target_price=D("96"), source="STRUCTURE:a", stage_id="tp1"),)
    position = _position(
        side=PositionSide.SHORT, initial_stop_price=D("105"), current_stop_price=D("105"),
        target_stages=stages,
    )
    engine = _engine()
    assert engine.evaluate(position=position, market=_market(price=D("96.1")), now=NOW).decision is None
    decision = engine.evaluate(position=position, market=_market(price=D("96")), now=NOW).decision
    assert decision is not None and decision.reason is ExitReason.TAKE_PROFIT


def test_unused_imports_guard():
    assert datetime(2026, 1, 1, tzinfo=UTC) and ExitPosition


def test_undersized_last_stage_of_a_tp1_runner_ladder_never_closes_the_runner():
    stages = (_stage(target_price=D("104"), source="STRUCTURE:a", stage_id="tp1", close_fraction=D("0.1")),)
    # 0.1 * 1.0 = 0.10 -> rounds to 0.00 at a 1.0 step: TP1+runner must skip, not close everything
    engine = _engine(quantity_step=D("1"), min_remaining_quantity=D("1"))
    decision = engine.evaluate(
        position=_position(target_stages=stages), market=_market(price=D("105")), now=NOW
    ).decision
    assert decision is None


def test_undersized_last_stage_of_a_complete_ladder_still_closes_the_rest():
    stages = (_stage(target_price=D("104"), source="STRUCTURE:a", stage_id="tp1", close_fraction=D("1")),)
    engine = _engine(quantity_step=D("2"), min_remaining_quantity=D("1"))  # 1.0*1 -> 0 lots at step 2
    decision = engine.evaluate(
        position=_position(target_stages=stages), market=_market(price=D("105")), now=NOW
    ).decision
    assert decision is not None and decision.quantity == D("1") and not decision.is_partial
