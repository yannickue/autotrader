# ruff: noqa: E501
"""Lane W hook in the runner: the shadow exit lab of a CLOSED real trade is written (additively) after the flat deadline,
off the decision path, and a failure inside it never touches the recorded outcome."""

from __future__ import annotations

from datetime import timedelta

import pytest

from demo import report
from demo.runner import DEFAULT_SHADOW_EXIT_LAB
from demo.testing import make_pair


def _closed_trade(env, **cfg):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(**cfg)
    r.start()
    r.run_cycle()
    env.stack.close_position(intent.intent_id, reason="TARGET", exit_price=102.6)
    env.clock.advance(minutes=35)
    r.run_cycle()
    return r, snap, intent


def test_default_flag_is_off_and_nothing_is_written(env):
    r, _snap, intent = _closed_trade(env)
    assert r.cfg.shadow_exit_lab_enabled is False and DEFAULT_SHADOW_EXIT_LAB is False  # measured 25-60 ms/entry on the runner thread
    r.label_now(env.clock() + timedelta(hours=7))
    assert "shadow_exit_lab" not in (env.store.get_outcome_extra(intent.intent_id) or {})
    assert r._shadow_stats.ok == 0 and r._shadow_stats.failed == 0


def test_cli_flag_exists_and_defaults_to_none():
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("demo_trader_cli", Path(__file__).resolve().parents[4] / "scripts" / "demo_trader.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["demo_trader_cli"] = mod
    spec.loader.exec_module(mod)
    parser = mod._parser()
    assert parser.parse_args(["--shadow"]).shadow_exit_lab is None
    assert parser.parse_args(["--shadow", "--shadow-exit-lab"]).shadow_exit_lab is True
    assert parser.parse_args(["--shadow", "--no-shadow-exit-lab"]).shadow_exit_lab is False


def test_closed_trade_gets_the_lab_after_the_flat_deadline_with_the_live_outcome_alongside(env):
    r, _snap, intent = _closed_trade(env, shadow_exit_lab_enabled=True)
    before = env.store.get_outcome(intent.intent_id)
    extra0 = env.store.get_outcome_extra(intent.intent_id)
    assert "shadow_exit_lab" not in extra0
    r.label_now(env.clock())  # before the forced-flat deadline (signal + 5 h): not yet
    assert "shadow_exit_lab" not in env.store.get_outcome_extra(intent.intent_id)
    env.clock.advance(hours=6)
    r.label_now(env.clock())
    extra = env.store.get_outcome_extra(intent.intent_id)
    lab = extra["shadow_exit_lab"]
    ex = env.store.get_execution(intent.intent_id)
    assert lab["entry"]["entry_id"] == intent.intent_id and lab["entry"]["policy_entry_ids"] == [intent.intent_id]
    assert lab["entry"]["fill"] == pytest.approx(ex.fill_price) and lab["entry"]["stop"] == pytest.approx(intent.stop)
    assert lab["live"]["profile"] == "fixed_1_5r" and lab["live"]["r"] == pytest.approx(before.gross_r)
    assert set(lab["policies"]) >= {"fixed_1_5r", "TP1_only", "failed_move_exit", "time_decay", "break_even_plus_runner"}
    # every pre-existing outcome field is untouched, the outcome record itself is unchanged
    for k, v in extra0.items():
        assert extra[k] == v
    assert env.store.get_outcome(intent.intent_id) == before
    again = env.store.get_outcome_extra(intent.intent_id)
    r.label_now(env.clock())
    assert env.store.get_outcome_extra(intent.intent_id) == again  # insert-once
    rep = report.build_report(env.store, "DISCOVERY")
    assert rep["shadow_exit_lab"]["real_trades"]["overall"]["n"] == 1
    assert rep["shadow_exit_lab"]["real_trades"]["overall"]["same_entry_assertion"] is True


def test_failure_inside_the_lab_never_touches_outcome_or_trading(env, monkeypatch):
    import demo.shadow_exit_lab as lab_mod

    def boom(*a, **k):
        raise RuntimeError("lab exploded")

    r, _snap, intent = _closed_trade(env, shadow_exit_lab_enabled=True)
    before = (env.store.get_outcome(intent.intent_id), env.store.get_outcome_extra(intent.intent_id))
    monkeypatch.setattr(lab_mod, "evaluate_shadow", boom)
    env.clock.advance(hours=6)
    r.label_now(env.clock())  # must not raise
    assert (env.store.get_outcome(intent.intent_id), env.store.get_outcome_extra(intent.intent_id)) == before
    assert r._shadow_stats.failed >= 1 and "lab exploded" in (r._shadow_stats.last_error or "")
    monkeypatch.setattr(r, "_frame_rows", boom)  # even a failure OUTSIDE the lab guard is contained and only noted
    r._shadow_lab_seen.clear()
    r.label_now(env.clock())
    assert env.store.get_outcome(intent.intent_id) == before[0]
