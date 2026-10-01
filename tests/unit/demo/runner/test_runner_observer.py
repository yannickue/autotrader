# ruff: noqa: E501
"""Runner-level GATE A: the REAL OpportunityEngine inside the DemoRunner (fake stack, replayed real bars) with the observer ON vs OFF.

With ``market_observer_enabled`` the persisted snapshots / decisions / intents / intent events / risk / TCA / pointers / seen ids and the fake stack's submits
are byte-identical to the run without it; ONLY the additive ``observer_records`` table differs. Also: default OFF, flatten-only forces it off, an exception
inside the hook or a failing observer write never changes anything, the heartbeat carries the ``market_observer`` section.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from functools import lru_cache

import pandas as pd
import pytest

from alpha.common.market_data import load_dev_market_frame
from demo.opportunity.bar_source import M5_SECONDS, ReplayBarSource
from demo.opportunity.engine import OpportunityEngine
from demo.opportunity.production_spec import DEFAULT_PATH_V1_2, load_production_spec
from demo.runner import DemoRunner, RunnerConfig, StoreSeenAdapter
from demo.store import DemoStore
from demo.testing import FakeClock, FakeStack
from markets.spec import CANONICALS, load_market_spec

GIB = 1024**3
MARKETS = ("GER40", "XAUUSD")
START_BAR = "2026-06-09T05:00"
N_CYCLES = 200


@lru_cache(maxsize=1)
def world():
    prod = load_production_spec(DEFAULT_PATH_V1_2)
    mspecs = {m: load_market_spec(m) for m in sorted(prod.market_names())}
    frames = {m: load_dev_market_frame(mspecs[m]) for m in CANONICALS}
    return prod, mspecs, frames


class ReplayStackSource:
    """The LiveBarSource surface over replayed real bars at the fake clock's time."""

    def __init__(self, clock: FakeClock, frames, point_sizes) -> None:
        self.clock = clock
        self.rs = ReplayBarSource(frames, point_sizes)
        self.frame_calls = 0

    def _sync(self) -> None:
        self.rs.set_time(self.clock())

    def m5_frame(self, market: str, n: int | None = None):
        self._sync()
        self.frame_calls += 1
        return self.rs.m5_frame(market, 1500 if n is None else n)  # the live source serves a bounded lookback, never the whole history

    def latest_quote(self, market: str):
        self._sync()
        return self.rs.latest_quote(market)

    def tick_activity(self, market: str):
        self._sync()
        return self.rs.tick_activity(market)

    def last_closed_bar_close_utc(self, market: str):
        self._sync()
        fr = self.rs.m5_frame(market, 1)
        return None if len(fr) == 0 else (pd.Timestamp(fr["ts"].iloc[-1]) + pd.Timedelta(seconds=M5_SECONDS)).to_pydatetime()


def rig(tmp_path, *, observer: bool, **cfg):
    prod, mspecs, frames = world()
    first = pd.Timestamp(START_BAR, tz="UTC")
    clock = FakeClock(first.to_pydatetime() + timedelta(seconds=M5_SECONDS + 10))
    store = DemoStore(tmp_path / "demo.sqlite", clock=lambda: clock().isoformat())
    stack = FakeStack(clock, markets=MARKETS)
    stack.bar_source = ReplayStackSource(clock, frames, {m: mspecs[m].point_size for m in frames})  # type: ignore[assignment]
    engine = OpportunityEngine(stack.bar_source, production=prod, market_specs=mspecs, window_bars=1000, min_history_bars=600, commit="runner-gate-a",
                               seen_store=StoreSeenAdapter(store))
    kw = dict(mode="demo-auto", markets=MARKETS, artifacts_dir=tmp_path / "art", poll_interval_s=1.0, min_disk_free_bytes=GIB, all_stale_grace_s=0.0,
              market_observer_enabled=observer, market_observer_budget_s=1e9)
    kw.update(cfg)
    runner = DemoRunner(stack, engine, store, config=RunnerConfig(**kw), clock=clock, sleep=lambda s: None, disk_free=lambda: 50 * GIB)
    return clock, store, stack, engine, runner


def drive(clock, runner, cycles=N_CYCLES):
    runner.start()
    assert runner.fail_reason is None, runner.fail_reason
    for _ in range(cycles):
        runner.run_cycle()
        assert runner.fail_reason is None, (runner.fail_reason, runner._last_error)
        clock.advance(minutes=5)


def dump(store: DemoStore) -> dict[str, list]:
    db = sqlite3.connect(store.path)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    out = {t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
    db.close()
    return out


def status_without_observer(runner) -> dict:
    st = runner.status()
    st.pop("market_observer")
    return json.loads(json.dumps(st, sort_keys=True, default=str))


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    out = {}
    for name, obs in (("off", False), ("on", True)):
        tmp = tmp_path_factory.mktemp(name)
        clock, store, stack, engine, runner = rig(tmp, observer=obs)
        drive(clock, runner)
        out[name] = {"dump": dump(store), "status": status_without_observer(runner), "submits": [i.to_json() for i in stack.submits],
                     "runner": runner, "store": store, "engine": engine, "mo": runner.status()["market_observer"]}
    yield out
    for r in out.values():
        r["store"].close()


def test_runner_gate_a_persisted_state_is_byte_identical_and_only_observer_records_differ(runs):
    off, on = runs["off"], runs["on"]
    assert set(off["dump"]) == set(on["dump"]) and "observer_records" in on["dump"]
    for table in sorted(off["dump"]):
        if table == "observer_records":
            continue
        assert off["dump"][table] == on["dump"][table], f"table {table} differs with the observer on"
    assert off["dump"]["observer_records"] == []
    n_snap = len(on["dump"]["snapshots"])
    assert n_snap >= 30 and len(on["dump"]["decisions"]) == n_snap  # a meaningful population: snapshots, decisions (accepted + rejected)
    assert len(on["dump"]["intents"]) >= 1 and off["submits"] == on["submits"] and len(on["submits"]) >= 1
    assert off["status"] == on["status"]
    # exactly one observer row per persisted opportunity (accepted AND rejected), keyed by its opportunity id
    keys = {r[0] for r in on["dump"]["observer_records"]}
    snap_ids = {r[0] for r in on["dump"]["snapshots"]}
    assert keys == snap_ids and len(on["dump"]["observer_records"]) == n_snap
    print(f"\nRUNNER_GATE_A: cycles={N_CYCLES} markets={MARKETS} snapshots={n_snap} intents={len(on['dump']['intents'])} submits={len(on['submits'])} observer_rows={len(keys)}")


def test_disabled_runner_has_no_observer_state_and_never_retains_the_frame(runs):
    off = runs["off"]
    assert off["runner"]._observer is None and off["engine"].retain_frame is False and off["engine"].last_frame is None
    assert off["mo"] == {"enabled": False}


def test_heartbeat_section_of_the_enabled_observer(runs):
    mo = runs["on"]["mo"]
    for k in ("enabled", "records_written", "errors", "last_cycle_ms", "warmup_false_count", "version"):
        assert k in mo, k
    assert mo["enabled"] is True and mo["errors"] == 0 and mo["version"] == "market-structure-observer-v1"
    assert mo["records_written"] == len(runs["on"]["dump"]["observer_records"]) > 0
    assert mo["warmup_false_count"] == mo["records_written"]  # 1000-bar engine frame in this test: below the documented history
    hb = json.loads((runs["on"]["runner"].cfg.artifacts_dir / "heartbeat.json").read_text(encoding="utf-8"))
    assert hb["market_observer"]["enabled"] is True and hb["market_observer"]["records_written"] == mo["records_written"]


def test_flatten_only_forces_the_observer_off(tmp_path):
    from demo.opportunity.operating_policy import load_operating_policy

    _clock, store, _stack, engine, runner = rig(tmp_path, observer=True, flatten_only=True, operating_policy=load_operating_policy())
    try:
        assert runner._observer is None and engine.retain_frame is False
    finally:
        store.close()


def test_default_runnerconfig_is_off():
    assert RunnerConfig(markets=("GER40",)).market_observer_enabled is False


def test_a_crashing_hook_and_a_failing_observer_write_change_nothing(tmp_path, monkeypatch):
    from demo.opportunity import observer_hook as H

    for sub in ("ref", "crash", "write"):
        (tmp_path / sub).mkdir()
    ref_clock, ref_store, ref_stack, _e, ref_runner = rig(tmp_path / "ref", observer=False)
    drive(ref_clock, ref_runner, 80)
    d_ref = dump(ref_store)
    ref_submits = [i.to_json() for i in ref_stack.submits]
    ref_store.close()
    assert len(d_ref["snapshots"]) > 5

    # 1) the hook itself blows up on every call
    def boom(self, *a, **k):
        raise RuntimeError("hook exploded")

    with monkeypatch.context() as mp:
        mp.setattr(H.ObserverShadow, "on_bar", boom)
        clock, store, stack, _engine, runner = rig(tmp_path / "crash", observer=True)
        drive(clock, runner, 80)
        d = dump(store)
        assert d["observer_records"] == [] and runner._observer.stats()["errors"] > 0
        for table in d_ref:
            if table != "observer_records":
                assert d[table] == d_ref[table], table
        assert [i.to_json() for i in stack.submits] == ref_submits
        store.close()

    # 2) the observer WRITE fails (disk error): counted and contained as well
    clock2, store2, stack2, _e2, runner2 = rig(tmp_path / "write", observer=True)

    def bad_write(rec):
        raise sqlite3.OperationalError("disk I/O error")

    store2.record_observer = bad_write  # type: ignore[method-assign]
    drive(clock2, runner2, 80)
    d2 = dump(store2)
    assert d2["observer_records"] == [] and runner2._observer.stats()["errors"] > 0
    for table in d_ref:
        if table != "observer_records":
            assert d2[table] == d_ref[table], table
    assert [i.to_json() for i in stack2.submits] == ref_submits
    store2.close()


def test_observer_cycle_time_is_reported_and_bounded_by_the_configured_budget(runs):
    """The budget enforcement itself is tested at hook level (test_observer_hook.py); here the section reports the per-cycle observer time and the budget."""
    mo = runs["on"]["mo"]
    assert mo["last_cycle_ms"] >= 0 and mo["budget_s"] == 1e9
