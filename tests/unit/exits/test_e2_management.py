# ruff: noqa: E501
"""Lane E2: chart-based management rules of the ExitEngine (cost-adjusted break-even, structure trailing,
structural failure, MFE giveback, time-alpha decay, late-session ratchet / early loser exit)."""

from datetime import timedelta
from decimal import Decimal

from exits.engine import ExitEngine
from exits.models import ExitReason, PositionSide, StopStage
from tests.unit.exits.test_engine import NOW, _market, _policy, _position, _StubRiskGate

D = Decimal


def _eval(policy_over=None, pos_over=None, **market_over):
    engine = ExitEngine(policy=_policy(**(policy_over or {})), risk_gate=_StubRiskGate())
    pos = _position(**(pos_over or {}))
    mkt = _market(**market_over)
    return pos, engine.evaluate(position=pos, market=mkt, now=NOW)


OFF = dict(  # every legacy rule off, so a test isolates exactly one E2 rule
    breakeven_trigger_r_multiple=D("99"), breakeven_buffer_bps=D("0"), trailing_activation_r_multiple=D("99"),
    momentum_deterioration_threshold=None, max_spread_bps=None, min_liquidity_notional=None,
    max_holding_duration=None,
)


def test_cost_adjusted_break_even_covers_the_expected_exit_cost():
    _, ev = _eval(dict(OFF, breakeven_trigger_r_multiple=D("1"), breakeven_buffer_bps=D("0")), price=D("106"), bid=D("105.9"), ask=D("106.1"), expected_exit_cost=D("0.4"))
    assert ev.updated_stop_stage is StopStage.BREAK_EVEN and ev.updated_stop_price == D("100.4")
    _, short = _eval(dict(OFF, breakeven_trigger_r_multiple=D("1")), pos_over=dict(side=PositionSide.SHORT, initial_stop_price=D("105"), current_stop_price=D("105")), price=D("94"), bid=D("93.9"), ask=D("94.1"), expected_exit_cost=D("0.4"))
    assert short.updated_stop_price == D("99.6")  # entry - cost, mirrored


def test_break_even_never_lands_at_or_through_the_price():
    _, ev = _eval(dict(OFF, breakeven_trigger_r_multiple=D("0.1")), price=D("100.6"), bid=D("100.5"), ask=D("100.7"), expected_exit_cost=D("1.0"))
    assert ev.updated_stop_price == D("95") and ev.decision is None  # +1.0 cost would sit above price: no move


def test_break_even_after_first_stage_is_chart_not_r_triggered():
    _, ev = _eval(dict(OFF, breakeven_after_first_stage=True), pos_over=dict(stages_completed=1), price=D("101"), bid=D("100.9"), ask=D("101.1"), expected_exit_cost=D("0.3"))
    assert ev.updated_stop_stage is StopStage.BREAK_EVEN and ev.updated_stop_price == D("100.3")
    _, before = _eval(dict(OFF, breakeven_after_first_stage=True), price=D("101"), bid=D("100.9"), ask=D("101.1"))
    assert before.updated_stop_stage is StopStage.ORIGINAL


def test_structure_trailing_only_tightens_and_never_crosses_price():
    pol = dict(OFF, structure_trailing=True)
    _, ev = _eval(pol, price=D("108"), bid=D("107.9"), ask=D("108.1"), structure_trail_price=D("103"))
    assert ev.updated_stop_price == D("103") and ev.updated_stop_stage is StopStage.TRAILING
    # a LOWER candidate than the stop in force is ignored (E: the stop never loosens)
    pos = _position(current_stop_price=D("103"), stop_stage=StopStage.TRAILING)
    engine = ExitEngine(policy=_policy(**pol), risk_gate=_StubRiskGate())
    ev2 = engine.evaluate(position=pos, market=_market(price=D("108"), bid=D("107.9"), ask=D("108.1"), structure_trail_price=D("97")), now=NOW)
    assert ev2.updated_stop_price == D("103")
    # a candidate above the price would be an instant stop-out: ignored
    _, ev3 = _eval(pol, price=D("108"), bid=D("107.9"), ask=D("108.1"), structure_trail_price=D("109"))
    assert ev3.updated_stop_price == D("95")


def test_structure_failure_closes_the_remainder_fully():
    _, ev = _eval(dict(OFF, structure_failure_exit=True), structure_failure=True)
    assert ev.decision is not None and ev.decision.reason is ExitReason.STRUCTURE_FAILURE and not ev.decision.is_partial
    _, off = _eval(OFF, structure_failure=True)
    assert off.decision is None


def test_mfe_giveback_exits_only_after_a_meaningful_excursion():
    pol = dict(OFF, max_giveback_fraction=D("0.5"), giveback_min_mfe_r=D("1"))
    _, ev = _eval(pol, price=D("102"), bid=D("101.9"), ask=D("102.1"), mfe_r=D("2"), giveback_r=D("1.2"))
    assert ev.decision is not None and ev.decision.reason is ExitReason.MFE_GIVEBACK
    _, small = _eval(pol, price=D("100.5"), bid=D("100.4"), ask=D("100.6"), mfe_r=D("0.4"), giveback_r=D("0.4"))
    assert small.decision is None


def test_time_stop_respects_alpha_only_when_configured():
    pos_over = dict(opened_at=NOW - timedelta(hours=5))
    pol = dict(OFF, max_holding_duration=timedelta(hours=4))
    _, legacy = _eval(pol, pos_over)
    assert legacy.decision is not None and legacy.decision.reason is ExitReason.TIME_STOP  # unchanged default
    _, decayed = _eval(dict(pol, time_stop_min_mfe_r=D("1")), pos_over, mfe_r=D("0.2"))
    assert decayed.decision is not None and decayed.decision.reason is ExitReason.TIME_STOP
    _, worked = _eval(dict(pol, time_stop_min_mfe_r=D("1")), pos_over, mfe_r=D("1.5"))
    assert worked.decision is None  # it worked: not aged out


# -- D / F: late session ---------------------------------------------------------------------------------------


LATE = dict(late_window=timedelta(minutes=45), late_loser_momentum_threshold=D("-0.3"))


def test_D_profitable_late_position_can_tighten_protection_to_cost_adjusted_break_even():
    _, ev = _eval(dict(OFF, **LATE), price=D("100.8"), bid=D("100.7"), ask=D("100.9"), expected_exit_cost=D("0.3"), time_to_forced_flat=timedelta(minutes=20))
    assert ev.updated_stop_stage is StopStage.BREAK_EVEN and ev.updated_stop_price == D("100.3") > D("95")
    assert ev.decision is None  # tightened, not closed: it may still run until the forced flat
    # outside the window nothing moves
    _, early = _eval(dict(OFF, **LATE), price=D("100.8"), bid=D("100.7"), ask=D("100.9"), expected_exit_cost=D("0.3"), time_to_forced_flat=timedelta(hours=3))
    assert early.updated_stop_price == D("95")


def test_E_a_stop_never_loosens_even_when_every_rule_proposes_a_lower_one():
    pos = _position(current_stop_price=D("103"), stop_stage=StopStage.TRAILING)
    engine = ExitEngine(policy=_policy(**dict(OFF, structure_trailing=True, breakeven_after_first_stage=True, **LATE)), risk_gate=_StubRiskGate())
    ev = engine.evaluate(position=pos, market=_market(price=D("108"), bid=D("107.9"), ask=D("108.1"), expected_exit_cost=D("0.3"), structure_trail_price=D("96"), time_to_forced_flat=timedelta(minutes=10)), now=NOW)
    assert ev.updated_stop_price >= D("103")


def test_F_a_losing_late_trade_with_adverse_momentum_exits_before_the_deadline():
    _, ev = _eval(dict(OFF, **LATE), price=D("98"), bid=D("97.9"), ask=D("98.1"), momentum_score=D("-0.8"), time_to_forced_flat=timedelta(minutes=20))
    assert ev.decision is not None and ev.decision.reason is ExitReason.LATE_SESSION_DETERIORATION and not ev.decision.is_partial
    # not late yet -> held (the original stop still protects it)
    _, early = _eval(dict(OFF, **LATE), price=D("98"), bid=D("97.9"), ask=D("98.1"), momentum_score=D("-0.8"), time_to_forced_flat=timedelta(hours=3))
    assert early.decision is None
    # a late WINNER is never cut by the loser rule
    _, winner = _eval(dict(OFF, **LATE), price=D("102"), bid=D("101.9"), ask=D("102.1"), momentum_score=D("-0.8"), time_to_forced_flat=timedelta(minutes=20))
    assert winner.decision is None
    # late loser without adverse momentum is not exited on this rule
    _, calm = _eval(dict(OFF, **LATE), price=D("98"), bid=D("97.9"), ask=D("98.1"), momentum_score=D("0.2"), time_to_forced_flat=timedelta(minutes=20))
    assert calm.decision is None
