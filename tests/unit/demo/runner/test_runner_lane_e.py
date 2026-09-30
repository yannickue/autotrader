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
