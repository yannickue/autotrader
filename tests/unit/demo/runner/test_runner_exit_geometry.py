# ruff: noqa: E501
"""Lane E2: the runner's plan producer (shadow geometry, exit_plan context, opt-in structure stop) and the
activation switch (``build_live_runner(exit_policy=...)`` / ``--exit-policy``)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from demo.execution.exit_manager import ExitPlanConfig
from demo.testing import FakeStack, make_pair


class PlanStack(FakeStack):
    """FakeStack that advertises the real stack's exit-plan surface."""

    exit_policy = "staged"
    exit_plan_config = ExitPlanConfig()


def _structure_bars(stack: PlanStack) -> None:
    """Flat 100.0 bars with a swing low (97.4) 10 bars back and a swing high (104.0) 8 bars back."""
    src = stack.bar_source
    last = src._last_open("GER40")
    step = timedelta(minutes=5)
    src.overrides[("GER40", last - 10 * step)] = (99.5, 100.0, 97.4, 99.5)
    src.overrides[("GER40", last - 8 * step)] = (100.0, 104.0, 99.5, 100.0)


def _run(env, stack, **pair):
    snap, dec, intent = make_pair(entry=100.0, risk=1.5, **pair)
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(stack=stack)
    r.start()
    r.run_cycle()
    return intent


def test_default_family_geometry_logs_the_structure_shadow_and_keeps_the_family_stop(env):
    stack = PlanStack(env.clock, markets=("GER40", "NAS100"))
    _structure_bars(stack)
    intent = _run(env, stack)
    sent = stack.submits[0]
    assert sent.stop == intent.stop  # family geometry stays ACTIVE: stop / sizing unchanged
    ctx = stack.contexts[intent.intent_id]
    assert ctx["geometry_source"] == "family" and ctx["exit_plan"]["stages"]  # staged: a plan is produced
    assert ctx["exit_plan"]["stages"][0]["source"] == "R"  # fixed-R family target, labelled honestly
    shadow = env.store.get_tca(intent.intent_id, "GEOMETRY")
    assert shadow["source_active"] == "family" and shadow["family"]["stop"] == str(intent.stop)
    assert shadow["structure"]["stop"] is not None and "applied" not in shadow


def test_structure_opt_in_replaces_the_stop_before_the_intent_is_recorded_and_sized(env):
    stack = PlanStack(env.clock, markets=("GER40", "NAS100"))
    stack.exit_plan_config = ExitPlanConfig(structure_markets=frozenset({"GER40"}))
    _structure_bars(stack)
    intent = _run(env, stack)
    sent = stack.submits[0]
    shadow = env.store.get_tca(intent.intent_id, "GEOMETRY")
    assert shadow["source_active"] == "structure"
    assert shadow["applied"]["family_stop"] == intent.stop
    assert sent.stop == float(shadow["structure"]["stop"]) and sent.stop < 97.4  # beyond the swing low + buffer
    assert env.store.get_intent(intent.intent_id)["stop"] == sent.stop  # the recorded intent IS what was sent
    plan = stack.contexts[intent.intent_id]["exit_plan"]
    assert plan["geometry_source"] == "structure" and plan["stages"][0]["source"].startswith("STRUCTURE:")
    assert float(plan["stages"][0]["target_price"]) > 100.0


def test_fixed_policy_stack_gets_shadow_but_no_plan(env):
    stack = PlanStack(env.clock, markets=("GER40", "NAS100"))
    stack.exit_policy = "fixed_1_5r"
    _structure_bars(stack)
    intent = _run(env, stack)
    ctx = stack.contexts[intent.intent_id]
    assert "exit_plan" not in ctx  # fixed_1_5r: nothing produced that could change an order
    assert env.store.get_tca(intent.intent_id, "GEOMETRY")["structure"] is not None


def test_a_stack_without_the_exit_plan_surface_is_untouched(env):
    snap, dec, intent = make_pair(entry=100.0, risk=1.5)
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    assert env.store.get_tca(intent.intent_id, "GEOMETRY") is None
    assert "exit_plan" not in env.stack.contexts[intent.intent_id]


def test_geometry_failure_never_blocks_the_trade(env):
    stack = PlanStack(env.clock, markets=("GER40", "NAS100"))
    real = stack.bar_source.m5_frame

    def flaky(market, n=None):
        if n == stack.exit_plan_config.bars:  # only the geometry read fails; the runner's own reads work
            raise RuntimeError("feed failure while building the geometry")
        return real(market, n)

    stack.bar_source.m5_frame = flaky
    intent = _run(env, stack)
    assert stack.submits and stack.submits[0].stop == intent.stop


def test_build_live_runner_refuses_an_unknown_exit_policy(tmp_path):
    from demo import runner as rn

    with pytest.raises(rn.LiveStackRefused, match="exit_policy"):
        rn.build_live_runner("shadow", exit_policy="bogus", artifacts_dir=tmp_path)


def test_cli_exposes_the_exit_policy_switch_with_a_safe_default():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[4] / "scripts" / "demo_trader.py"
    spec = importlib.util.spec_from_file_location("demo_trader_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod._parser()
    default = parser.parse_args(["--status"])
    assert default.exit_policy == "fixed_1_5r" and default.geometry_source == "family"
    staged = parser.parse_args(["--shadow", "--exit-policy", "staged", "--geometry-source", "structure"])
    assert staged.exit_policy == "staged" and staged.geometry_source == "structure"


# -- Lane Y: per-intent exit profiles (staged_profiles) ---------------------------------------------------------------


def _profile_run(env, family, variant):
    stack = PlanStack(env.clock, markets=("GER40", "NAS100"))
    stack.exit_policy = "staged_profiles"
    _structure_bars(stack)
    snap, dec, intent = make_pair(entry=100.0, risk=1.5)
    snap.signal["family"] = family
    if variant is not None:
        snap.signal["variant"] = variant
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(stack=stack)
    r.start()
    r.run_cycle()
    return stack, intent


@pytest.mark.parametrize(
    ("family", "variant", "profile", "has_plan"),
    [("STRUCT", "breakout", "CONTINUATION", True), ("STRUCT", "fade", "FAILED_MOVE", True),
     ("ROUND", "reject", "REVERSION", True), ("LEADLAG", None, "FIXED_1_5R", False)],
)
def test_runner_routes_family_mode_to_one_frozen_profile(env, family, variant, profile, has_plan):
    stack, intent = _profile_run(env, family, variant)
    ctx = stack.contexts[intent.intent_id]
    assert ctx["exit_profile"]["profile"] == profile and ctx["exit_profile"]["mapping_version"]
    assert ("exit_plan" in ctx) is has_plan  # FIXED_1_5R-mapped: no plan -> unchanged fixed behaviour
    assert env.store.get_tca(intent.intent_id, "GEOMETRY")["exit_profile"]["profile"] == profile
    if has_plan:
        assert ctx["exit_plan"]["stages"], "the profile plan has a structural first target"


def test_legacy_staged_policy_does_not_route_profiles(env):
    stack = PlanStack(env.clock, markets=("GER40", "NAS100"))
    _structure_bars(stack)
    intent = _run(env, stack)
    assert "exit_profile" not in stack.contexts[intent.intent_id]


def test_cli_and_factory_expose_staged_profiles_with_a_safe_default(tmp_path):
    import importlib.util
    import pathlib

    from demo import runner as rn

    path = pathlib.Path(__file__).resolve().parents[4] / "scripts" / "demo_trader.py"
    spec = importlib.util.spec_from_file_location("demo_trader_y", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod._parser()
    assert parser.parse_args(["--status"]).exit_policy == "fixed_1_5r"
    assert parser.parse_args(["--shadow", "--exit-policy", "staged_profiles"]).exit_policy == "staged_profiles"
    with pytest.raises(rn.LiveStackRefused, match="exit_policy"):
        rn.build_live_runner("shadow", exit_policy="bogus", artifacts_dir=tmp_path)
