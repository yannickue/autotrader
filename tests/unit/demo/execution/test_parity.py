from decimal import Decimal

import pytest

from demo.contracts import TradeIntent
from demo.execution import gates as G
from demo.execution.parity import entry_tolerance, parity_reject

D = Decimal


def intent(**over) -> TradeIntent:
    data = dict(
        opportunity_id="o", phase="DISCOVERY", intent_id="i", market="GER40", broker_symbol="Ger40",
        direction=1, entry_ref=23501.2, stop=23450.0, target=23650.0, min_space_r=1.0,
        valid_until_utc="2030-01-01T00:00:00+00:00", forced_flat_utc=None, risk_fraction=0.01,
    )
    data.update(over)
    return TradeIntent(**data)


def check(i, bid, ask, **kw):
    return parity_reject(
        i, bid=D(str(bid)), ask=D(str(ask)), max_spread=D("5"), tick_size=D("0.1"), **kw
    )


def test_audit_scenario_one_tick_adverse_drift_is_accepted():
    # decision ask 23501.2, fresh ask 23501.3 (+1 tick) used to be entry_overshoot
    assert check(intent(), 23500.5, 23501.3) is None


def test_long_default_tolerance_boundary_is_two_spreads():
    i = intent()
    # spread 1.0 -> tolerance max(2.0, 0.2) = 2.0
    assert check(i, 23502.2, 23503.2) is None  # drift exactly 2.0
    assert check(i, 23502.3, 23503.3) == G.R_ENTRY_OVERSHOOT  # drift 2.1


def test_short_default_tolerance_is_symmetric():
    i = intent(direction=-1, entry_ref=23500.0, stop=23550.0, target=23350.0)
    assert check(i, 23499.9, 23500.4) is None  # bid below entry by 0.1
    assert check(i, 23497.0, 23497.5) == G.R_ENTRY_OVERSHOOT  # adverse drift 3.0 > 2*0.5
    assert check(i, 23501.0, 23501.5) is None  # favourable move


def test_explicit_tolerance_overrides_default():
    i = intent(entry_tolerance=5.0)
    assert check(i, 23505.0, 23506.0) is None  # drift 4.8 <= 5
    assert check(i, 23506.0, 23507.0) == G.R_ENTRY_OVERSHOOT  # drift 5.8 > 5
    zero = intent(entry_tolerance=0.0)
    assert check(zero, 23500.5, 23501.3) == G.R_ENTRY_OVERSHOOT


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_malformed_tolerance_falls_back_to_default(bad):
    assert entry_tolerance(intent(entry_tolerance=bad), spread=D("1"), tick_size=D("0.1")) == D("2")


def test_tick_floor_when_spread_is_tiny():
    assert entry_tolerance(intent(), spread=D("0.05"), tick_size=D("0.1")) == D("0.2")


def test_other_rules_still_use_the_actual_fill_price():
    # drift is within tolerance but the target is crossed / stop crossed at the fill
    assert check(intent(target=23501.5), 23501.0, 23502.0) == G.R_TARGET_CROSSED
    assert check(intent(stop=23501.0), 23500.0, 23501.0) == G.R_INVALIDATION_CROSSED
    # min_space_r evaluated at the real fill (reward 148.7 / risk 51.3 < 3)
    assert check(intent(min_space_r=3.0), 23500.5, 23501.3) == G.R_MIN_SPACE_R


def test_relative_cost_is_the_primary_spread_gate_and_absolute_bound_only_a_safety_cap():
    from decimal import Decimal as D

    from demo.contracts import TradeIntent
    from demo.execution import gates as G
    from demo.execution.parity import parity_reject

    intent = TradeIntent(
        opportunity_id="o", phase="DISCOVERY", intent_id="i", market="NAS100",
        broker_symbol="UsaTec", direction=1, entry_ref=30000.0, stop=29944.34, target=None,
        min_space_r=0.0, valid_until_utc="2099-01-01T00:00:00+00:00", forced_flat_utc=None,
        risk_fraction=0.01,
    )
    common = {"max_spread": D("1.88"), "tick_size": D("0.01")}
    # audit scenario: spread 2.1 is above the old p99 bound 1.88 but only ~3.8% of a 55.7-pt stop
    assert parity_reject(intent, bid=D("29999.9"), ask=D("30002.0"), **common) is None
    # spread taking more than 20% of 1R is rejected even below the absolute bound
    tight = intent.__class__(**{**intent.to_dict(), "stop": 29999.0})
    assert parity_reject(tight, bid=D("29999.5"), ask=D("30000.0"), **common) == G.R_SPREAD_CAP
    # absolute safety cap: > 4x the bound is always rejected
    assert parity_reject(intent, bid=D("29990.0"), ask=D("30000.0"), **common) == G.R_SPREAD_CAP
    assert G.SPREAD_MAX_FRACTION_OF_RISK == 0.20 and G.SPREAD_EXTREME_MULTIPLE == 4
