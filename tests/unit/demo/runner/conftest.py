# ruff: noqa: E501
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.runner import DemoRunner, RunnerConfig
from demo.store import DemoStore
from demo.testing import T0, FakeClock, FakeStack, ScriptedEngine

GIB = 1024**3


class Env(SimpleNamespace):
    def build(self, *, mode: str = "demo-auto", phase: str = "DISCOVERY", stack=None, store=None,
              engine=None, markets=("GER40",), **kw) -> DemoRunner:
        cfg_kw = dict(
            mode=mode, phase=phase, markets=tuple(markets), artifacts_dir=self.tmp / "art",
            poll_interval_s=1.0, min_disk_free_bytes=GIB, all_stale_grace_s=0.0,
        )
        runner_kw = {k: kw.pop(k) for k in ("predictor", "trainer", "spec_loader", "disk_free", "learning_error") if k in kw}
        cfg_kw.update(kw)
        r = DemoRunner(
            stack or self.stack, engine or self.engine, store or self.store,
            config=RunnerConfig(**cfg_kw), clock=self.clock, sleep=lambda s: None,
            disk_free=runner_kw.pop("disk_free", lambda: 50 * GIB), **runner_kw,
        )
        return r


@pytest.fixture
def env(tmp_path) -> Env:
    clock = FakeClock(T0 + timedelta(seconds=10))
    store = DemoStore(tmp_path / "demo.sqlite", clock=lambda: clock().isoformat())
    stack = FakeStack(clock, markets=("GER40", "NAS100"))
    e = Env(tmp=tmp_path, clock=clock, store=store, stack=stack, engine=ScriptedEngine())
    yield e
    store.close()
