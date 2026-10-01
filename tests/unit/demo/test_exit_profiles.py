# ruff: noqa: E501
"""Lane Y: the family/mode -> exit PROFILE router (pure) and the engine policies it derives (no execution)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from demo import exit_profiles as xp
from demo.execution.exit_manager import (
    ExitPlanConfig,
    build_exit_plan,
    default_staged_exit_policy,
    produce_exit_context,
)
from demo.opportunity import production_spec as ps
from exits.engine import ExitEngine
from exits.models import (
    ExitMarketState,
    ExitPosition,
    ExitReason,
    PositionSide,
    StopStage,
    TakeProfitStage,
)

D = Decimal


# -- mapping determinism / coverage ---------------------------------------------------------------------------------


def _active_pairs() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for path in (ps.DEFAULT_PATH, ps.DEFAULT_PATH_V1_1, ps.DEFAULT_PATH_V1_2):
        for _market, specs in ps.load_production_spec(path).markets:
            for fs in specs:
                mode = getattr(fs.spec, "mode", None)
                out.add((fs.family, str(mode) if mode not in (None, "") else xp.NO_MODE))
    return out


def test_every_active_family_mode_maps_to_exactly_one_profile():
    pairs = _active_pairs()
    assert len(pairs) >= 17  # v1 + v1.2 incl. STRUCT variants (sanity: the spec files were really read)
    keys = [(r.family, r.mode) for r in xp.ROUTES]
    assert len(keys) == len(set(keys)), "a (family, mode) is mapped twice"
    unmapped = sorted(p for p in pairs if not xp.is_mapped(*p))
    assert not unmapped, f"unmapped (family, mode) - add them to demo.exit_profiles.ROUTES: {unmapped}"
    for fam, mode in pairs:
        route = xp.route_for(fam, None if mode == xp.NO_MODE else mode)
        assert route.profile in xp.PROFILES and (route.family, route.mode) == (fam, mode)
        assert route.rationale and route.thesis
        if route.profile == xp.PROFILE_FIXED:
            assert route.temporary and route.stop_basis is None  # FIXED is only ever an explicit TEMPORARY benchmark
        else:
            assert not route.temporary and route.stop_basis in (xp.STOP_BASIS_FAMILY, xp.STOP_BASIS_CHART)


def test_unmapped_pair_is_never_guessed_and_falls_to_the_flagged_fixed_baseline():
    route = xp.route_for("MADEUP", "x")
    assert route.profile == xp.PROFILE_FIXED and route.temporary and route.thesis == "UNMAPPED family/mode"
    assert not xp.is_mapped("MADEUP", "x")


@pytest.mark.parametrize(
    ("family", "mode", "profile"),
    [
        ("STRUCT", "breakout", xp.PROFILE_CONTINUATION), ("STRUCT", "confirmed", xp.PROFILE_CONTINUATION),
        ("STRUCT", "retest", xp.PROFILE_CONTINUATION), ("STRUCT", "fade", xp.PROFILE_FAILED_MOVE),
        ("ORB", "breakout", xp.PROFILE_CONTINUATION), ("ORB", "fade", xp.PROFILE_FAILED_MOVE),
        ("ROUND", "reject", xp.PROFILE_REVERSION), ("ROUND", "break", xp.PROFILE_CONTINUATION),
        ("VOLREV", "fade", xp.PROFILE_REVERSION), ("VOLREV", "expand", xp.PROFILE_CONTINUATION),
        ("GAP", "fade", xp.PROFILE_REVERSION), ("GAP", "go", xp.PROFILE_CONTINUATION),
        ("OVERNIGHT", "continue", xp.PROFILE_CONTINUATION), ("OVERNIGHT", "reverse", xp.PROFILE_FIXED),
        ("EOD", "continue", xp.PROFILE_CONTINUATION), ("EOD", "reverse", xp.PROFILE_FIXED),
        ("LEADLAG", None, xp.PROFILE_FIXED),
    ],
)
def test_documented_thesis_classification(family, mode, profile):
    assert xp.route_for(family, mode).profile == profile


def test_attribution_carries_profile_version_and_fractions():
    a = xp.attribution(xp.route_for("STRUCT", "breakout"))
    assert a["profile"] == "CONTINUATION" and a["mapping_version"] == xp.MAPPING_VERSION
    assert (a["tp1_fraction"], a["runner_fraction"], a["tp2"]) == ("0.5", "0.5", "DORMANT_SHADOW")
    r = xp.attribution(xp.route_for("ROUND", "reject"))
    assert (r["tp1_fraction"], r["runner_fraction"]) == ("1", "0")
    f = xp.attribution(xp.route_for("LEADLAG", None))
    assert f["profile"] == "FIXED_1_5R" and f["tp1_fraction"] is None and f["temporary"] is True


# -- policies: rule competition removed -----------------------------------------------------------------------------


@pytest.mark.parametrize("profile", xp.ENGINE_PROFILES)
def test_no_competing_rule_is_active_in_any_profile(profile):
    p = xp.profile_policy(profile, default_staged_exit_policy())
    assert p.breakeven_trigger_r_multiple == 99 and not p.breakeven_after_first_stage  # break-even is no authority
    assert p.trailing_activation_r_multiple == 99  # no ATR / percentage trail
    assert p.momentum_deterioration_threshold is None and p.late_loser_momentum_threshold is None and p.late_window is None
    assert p.max_giveback_fraction is None and p.take_profit_stages == ()
    assert p.structure_failure_exit is True  # thesis failure
    assert p.max_holding_duration is None  # no global max_holding
    assert p.structure_trailing == (profile == xp.PROFILE_CONTINUATION)  # ONE trailing authority, CONTINUATION only


def test_profile_policy_refuses_the_fixed_profile_and_time_stop_is_reversion_only():
    base = default_staged_exit_policy()
    with pytest.raises(ValueError):
        xp.profile_policy(xp.PROFILE_FIXED, base)
    assert xp.profile_policy(xp.PROFILE_REVERSION, base, time_stop_minutes=45).max_holding_duration == timedelta(minutes=45)
    assert xp.profile_policy(xp.PROFILE_CONTINUATION, base, time_stop_minutes=45).max_holding_duration is None
    assert xp.profile_policy(xp.PROFILE_FAILED_MOVE, base, time_stop_minutes=45).max_holding_duration is None
    # real mappings: no family documents a duration horizon (only clock exits) -> the time stop is OFF everywhere
    assert all(r.time_stop_minutes is None for r in xp.ROUTES)


# -- engine behaviour under the profile policies (single ExitEngine) ------------------------------------------------

NOW = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


class _Gate:
    def release(self, decision_id: str) -> None:
        pass


def _position(*, stop="90", stages=(), stop_stage=StopStage.ORIGINAL, completed=0, qty="1", realized="0", hw=None):
    return ExitPosition(
        position_id="p", instrument="X", side=PositionSide.LONG, entry_price=D("100"), quantity=D(qty),
        initial_stop_price=D("90"), current_stop_price=D(stop), stop_stage=stop_stage, opened_at=NOW - timedelta(hours=1),
        realized_partial_quantity=D(realized), stages_completed=completed, target_stages=tuple(stages),
        high_water_mark=None if hw is None else D(hw),
    )


def _market(price, *, trail=None, failure=False, momentum=None, cost="0.5", mfe="0"):
    return ExitMarketState(
        instrument="X", timestamp=NOW, price=D(price), bid=D(price), ask=D(price) + D("0.2"), volatility=D("0.01"),
        mfe_r=D(mfe), giveback_r=D("0"), holding_seconds=D("3600"), expected_exit_cost=D(cost),
        structure_trail_price=None if trail is None else D(trail), structure_failure=failure,
        momentum_score=None if momentum is None else D(momentum), time_to_forced_flat=timedelta(minutes=10),
    )


def _engine(profile, **kw):
    return ExitEngine(policy=xp.profile_policy(profile, default_staged_exit_policy(), **kw), risk_gate=_Gate())


def test_break_even_is_not_triggered_by_an_r_threshold_even_late_in_the_session():
    for profile in xp.ENGINE_PROFILES:
        ev = _engine(profile).evaluate(position=_position(), market=_market("135"), now=NOW)  # +3.5R, 10 min before the flat
        assert ev.updated_stop_price == D("90") and ev.updated_stop_stage is StopStage.ORIGINAL and not ev.break_even_activated
        assert ev.decision is None  # and no late-session / momentum / giveback exit either


def test_momentum_deterioration_never_closes_a_profile_row():
    for profile in xp.ENGINE_PROFILES:
        ev = _engine(profile).evaluate(position=_position(), market=_market("95", momentum="-5"), now=NOW)
        assert ev.decision is None


def test_continuation_structure_trail_tightens_only_with_a_cost_floor_never_loosens():
    eng = _engine(xp.PROFILE_CONTINUATION)
    ev = eng.evaluate(position=_position(), market=_market("120", trail="105"), now=NOW)  # new higher low beyond entry
    assert ev.updated_stop_price == D("105") and ev.updated_stop_stage is StopStage.TRAILING
    pos2 = _position(stop="105", stop_stage=StopStage.TRAILING)
    ev2 = eng.evaluate(position=pos2, market=_market("118", trail="101"), now=NOW)  # a lower candidate: never loosened
    assert ev2.updated_stop_price == D("105")
    # structure only barely beyond entry: floored at the cost-adjusted break-even (entry + exit cost), only WITH the structure
    ev3 = eng.evaluate(position=_position(), market=_market("110", trail="100.2", cost="0.5"), now=NOW)
    assert ev3.updated_stop_price == D("100.5")
    ev4 = eng.evaluate(position=_position(), market=_market("110", cost="0.5"), now=NOW)  # no structure -> no floor, no BE
    assert ev4.updated_stop_price == D("90")
    # a structure below entry is not lifted to the floor (loser side stays structural)
    ev5 = eng.evaluate(position=_position(), market=_market("110", trail="95"), now=NOW)
    assert ev5.updated_stop_price == D("95")


def test_reversion_and_failed_move_have_no_trail_and_close_fully_at_their_single_target():
    stage = TakeProfitStage(close_fraction=D("1"), target_price=D("110"), stage_id="tp1", source="STRUCTURE:mean")
    for profile in (xp.PROFILE_REVERSION, xp.PROFILE_FAILED_MOVE):
        eng = _engine(profile)
        ev = eng.evaluate(position=_position(stages=[stage]), market=_market("108", trail="104"), now=NOW)
        assert ev.updated_stop_price == D("90") and ev.decision is None  # no trail
        ev = eng.evaluate(position=_position(stages=[stage]), market=_market("110.5"), now=NOW)
        assert ev.decision is not None and not ev.decision.is_partial and ev.decision.quantity == D("1")
        assert ev.decision.reason is ExitReason.TAKE_PROFIT


def test_continuation_tp1_is_a_partial_and_the_runner_is_not_capped():
    stage = TakeProfitStage(close_fraction=D("0.5"), target_price=D("110"), stage_id="tp1", source="STRUCTURE:x")
    eng = _engine(xp.PROFILE_CONTINUATION)
    ev = eng.evaluate(position=_position(stages=[stage], qty="2"), market=_market("111"), now=NOW)
    assert ev.decision is not None and ev.decision.is_partial and ev.decision.quantity == D("1")
    after = _position(stages=[stage], qty="1", realized="1", completed=1)
    ev = eng.evaluate(position=after, market=_market("150"), now=NOW)  # runner: no further target
    assert ev.decision is None and ev.updated_stop_price == D("90")  # no BE after TP1 either


def test_thesis_failure_closes_every_profile_row():
    for profile in xp.ENGINE_PROFILES:
        ev = _engine(profile).evaluate(position=_position(), market=_market("97", failure=True), now=NOW)
        assert ev.decision is not None and ev.decision.reason is ExitReason.STRUCTURE_FAILURE


def test_reversion_time_decay_only_when_a_horizon_is_configured_and_the_trade_never_worked():
    eng_off = _engine(xp.PROFILE_REVERSION)
    eng_on = _engine(xp.PROFILE_REVERSION, time_stop_minutes=30)
    pos = _position()
    assert eng_off.evaluate(position=pos, market=_market("100.1"), now=NOW).decision is None
    ev = eng_on.evaluate(position=pos, market=_market("100.1"), now=NOW)
    assert ev.decision is not None and ev.decision.reason is ExitReason.TIME_STOP
    assert eng_on.evaluate(position=pos, market=_market("100.1", mfe="0.8"), now=NOW).decision is None  # it worked


def test_default_staged_policy_is_unchanged_golden():
    p = default_staged_exit_policy()
    assert p.policy_id == "staged-e2-v1" and p.structure_cost_floor is False  # the legacy `staged` policy is not touched
    assert p.max_holding_duration == timedelta(hours=6) and p.momentum_deterioration_threshold == D("-1.0")


# -- plan producer --------------------------------------------------------------------------------------------------


def test_reversion_prefers_the_family_structural_target_and_tp2_stays_shadow():
    plan = build_exit_plan(
        direction=1, entry_ref=D("100"), stop=D("95"), fractions=xp.PROFILE_FRACTIONS[xp.PROFILE_REVERSION], geometry=None,
        source="structure", family_target=D("108"), target_is_structural=True, prefer_family_target=True, record_tp2_shadow=True,
    )
    assert [s["target_price"] for s in plan["stages"]] == ["108"] and plan["stages"][0]["close_fraction"] == "1"
    assert plan["fractions"]["runner"] == "0" and plan["tp2_shadow"] is None and plan["target_origin"] == "family_target"


def test_produce_exit_context_attributes_the_profile_and_keeps_fixed_rows_planless():
    import numpy as np
    import pandas as pd

    ts = pd.date_range("2026-10-01 06:00", periods=60, freq="5min", tz="UTC")
    base = 25000 + np.sin(np.arange(60) / 4.0) * 30
    frame = pd.DataFrame({"ts": ts, "open": base, "high": base + 5, "low": base - 5, "close": base})
    common = dict(
        direction=1, entry_ref=25000.0, stop=24950.0, target=25100.0, family="STRUCT", market="BTCUSD", atr=20.0, frame=frame,
        spread=1.0, tick_size=0.01, structure_levels=None, target_is_structural=False, cfg=ExitPlanConfig(),
    )
    cont = produce_exit_context(**common, staged=True, route=xp.route_for("STRUCT", "breakout"))
    assert cont["exit_profile"]["profile"] == "CONTINUATION" and cont["shadow"]["exit_profile"]["profile"] == "CONTINUATION"
    assert cont["structure_stop"] is None  # STRUCT: the family stop already IS the thesis invalidation (kept)
    assert cont["exit_plan"]["fractions"]["runner"] == "0.5"
    fixed = produce_exit_context(**common, staged=False, route=xp.route_for("LEADLAG", None))
    assert fixed["exit_plan"] is None and fixed["exit_profile"]["profile"] == "FIXED_1_5R"
    legacy = produce_exit_context(**common, staged=True)
    assert legacy["exit_profile"] is None  # legacy `staged`: no routing at all
