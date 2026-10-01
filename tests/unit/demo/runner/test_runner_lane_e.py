# ruff: noqa: E501
"""Lane E1: the runner's manage hook calls the stack's optional ``manage_exits`` between poll and clock."""

from __future__ import annotations

from demo.testing import FakeStack


class ExitStack(FakeStack):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.order: list[str] = []

    def poll_events(self):
        self.order.append("poll")
        return super().poll_events()

    def manage_exits(self, now):
        self.order.append("manage")
        return []

    def on_clock(self, now):
        self.order.append("clock")
        return super().on_clock(now)


def test_manage_exits_runs_between_poll_and_on_clock(env):
    stack = ExitStack(env.clock, markets=("GER40", "NAS100"))
    runner = env.build(stack=stack)
    runner.run_cycle()
    order = [x for x in stack.order if x in ("poll", "manage", "clock")]
    first = order.index("manage")
    assert order[first - 1] == "poll" and order[first + 1] == "clock"


def test_a_stack_without_manage_exits_is_untouched(env):
    assert not hasattr(env.stack, "manage_exits")
    env.build().run_cycle()  # FakeStack (no hook) keeps working exactly as before


class HealthStack(ExitStack):
    """Scripted exit-manager health: one row skipped for ``streak`` consecutive cycles."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.streak = 0

    def manage_exits(self, now):
        self.streak += 1
        return []

    def exit_manager_health(self):
        return {
            "rows_managed": 0, "rows_skipped": 1, "consecutive_skips_max": self.streak,
            "last_skip_reason": "skip_invalid_position:current_stop_price must be unchanged or tighter",
            "skipped_rows": {"int-x": self.streak},
        }


def test_heartbeat_shows_exit_skip_counters_and_alarms_after_three_consecutive_skips(env, capsys):
    stack = HealthStack(env.clock, markets=("GER40", "NAS100"))
    runner = env.build(stack=stack)
    for _ in range(2):
        runner.run_cycle()
    st = runner.status()
    assert st["exit_manager"]["rows_skipped"] == 1 and st["exit_manager"]["consecutive_skips_max"] == 2
    assert "EXIT_MANAGER_UNMANAGED_ROW" not in capsys.readouterr().err
    assert not [w for w in st["warnings"] if "EXIT_MANAGER_UNMANAGED_ROW" in w]
    runner.run_cycle()  # third consecutive skip -> loud
    st = runner.status()
    assert st["exit_manager"]["consecutive_skips_max"] == 3
    assert "ALARM EXIT_MANAGER_UNMANAGED_ROW int-x" in capsys.readouterr().err
    assert any("EXIT_MANAGER_UNMANAGED_ROW int-x" in w for w in st["warnings"])
    assert "EXIT_MANAGER_UNMANAGED_ROW int-x" in st["last_error"]["text"]
    runner.run_cycle()  # no repeat spam on every further cycle
    assert "ALARM" not in capsys.readouterr().err


def test_a_stack_without_health_reports_none(env):
    stack = ExitStack(env.clock, markets=("GER40", "NAS100"))
    runner = env.build(stack=stack)
    runner.run_cycle()
    assert runner.status()["exit_manager"] is None
