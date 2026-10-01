# ruff: noqa: E501
"""``demo.opportunity.observer_hook.ObserverShadow``: failure isolation, budget, immutability, live == batch features, static isolation.

Catalogue items covered: historical vs live feature parity (the hook's sliding-frame path equals the batch features at the decision bar once >= MIN_HISTORY);
exception inside the observer never raises / is counted; over-budget cut-off with deferral and exact later catch-up; read-only inputs (frame hash, frozen
snapshot/decision JSON); engine-REJECTED and catch-up opportunities are observed; warmup_ok=False on a short live frame; zero code path when disabled;
static: the hook imports nothing from execution / risk / exits, no execution / risk / exits / adapter module imports the observer, the engine never reads
observer output.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from demo.opportunity import observer_hook as H
from demo.testing import make_pair
from market_observer import bars_adapter as BA
from market_observer import observer as O
from market_observer.schema import SessionSpec

SRC = Path(__file__).resolve().parents[4] / "src"


@lru_cache(maxsize=1)
def ger40():
    from alpha.common.market_data import load_dev_market_frame
    from markets.spec import load_market_spec

    ms = load_market_spec("GER40")
    return ms, load_dev_market_frame(ms).iloc[-9000:].reset_index(drop=True)


def last_close(frame: pd.DataFrame):
    return (pd.Timestamp(frame["ts"].iloc[-1]) + pd.Timedelta(seconds=300)).to_pydatetime()


def pair_for(frame: pd.DataFrame, *, direction=1, accepted=True, tag="a", family="STRUCT", variant="breakout"):
    import dataclasses

    snap, dec, _ = make_pair(market="GER40", signal_ts=last_close(frame), direction=direction, accepted=accepted, tag=tag)
    snap = dataclasses.replace(snap, signal={**snap.signal, "family": family, "variant": variant, "structure_event_id": "se-" + tag})
    return snap, dec


def observe_now(hook, market, ms, frame, pairs):
    """The runner's two phases in one call: the in-scan O(1) stash, then the post-scan budgeted drain of ONE cycle."""
    hook.on_bar(market, ms, frame, pairs)
    hook.begin_cycle()
    return hook.drain_cycle()


# ---------------------------------------------------------------------------------------------- live == batch
def test_live_hook_features_equal_the_batch_features_at_the_decision_bar():
    """The SAME adapter feeds both: a hook fed the engine-style SLIDING 6000-bar frame produces exactly the feature columns of the batch computation over
    all 9000 bars (history >= every group's MIN_HISTORY). warmup_ok is True on both sides."""
    ms, fr = ger40()
    cfg = H.config_for(ms)
    batch = BA.bars_from_frame("GER40", fr, point_size=ms.point_size, tick_size=ms.tick_size, session=H.session_for(ms))
    ref = O.MarketStructureObserver(cfg)
    hook = H.ObserverShadow(budget_s=1e9)
    got = {}
    for end in range(8990, 9001):
        frame = fr.iloc[end - 6000: end].reset_index(drop=True)
        pairs = [pair_for(frame, direction=1, tag="l"), pair_for(frame, direction=-1, tag="s")] if end >= 8996 else []
        for rec in observe_now(hook, "GER40", ms, frame, pairs):
            got[(end - 1, rec.direction)] = rec
    assert len(got) == 2 * 5, sorted(got)
    for (i, d), rec in sorted(got.items()):
        ref.advance(batch, i)
        want = ref.observe(batch, i, O.ObservedEvent(d, float(batch.c[i]), family="STRUCT", variant="breakout"))
        assert rec.features == want.features, (i, d)
        assert rec.event_id == want.event_id and rec.meta["warmup_ok"] is True and want.meta["warmup_ok"] is True
    assert hook.stats()["errors"] == 0 and hook.stats()["skipped_mismatch"] == 0


# ---------------------------------------------------------------------------------------------- failure isolation / budget
def test_exception_inside_the_observer_is_contained_counted_and_recovers(monkeypatch):
    ms, fr = ger40()
    frame = fr.iloc[3000:3700].reset_index(drop=True)
    hook = H.ObserverShadow(budget_s=1e9)
    calls = {"n": 0}
    real = O.MarketStructureObserver.observe

    def boom(self, bars, i, event, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("synthetic observer failure")
        return real(self, bars, i, event, **kw)

    monkeypatch.setattr(O.MarketStructureObserver, "observe", boom)
    out = observe_now(hook, "GER40", ms, frame, [pair_for(frame)])
    assert out == [] and hook.stats()["errors"] == 1 and "synthetic observer failure" in hook.stats()["last_error"]
    nxt = fr.iloc[3000:3701].reset_index(drop=True)
    out = observe_now(hook, "GER40", ms, nxt, [pair_for(nxt, tag="b")])  # the failing event was dropped alone; the next one is observed
    assert len(out) == 1 and hook.stats()["errors"] == 1
    assert observe_now(hook, "GER40", ms, None, [pair_for(nxt)]) == [] and observe_now(hook, "GER40", None, nxt, [pair_for(nxt)]) == []  # garbage inputs never raise


def test_garbage_pairs_never_raise():
    ms, fr = ger40()
    frame = fr.iloc[3000:3700].reset_index(drop=True)
    hook = H.ObserverShadow(budget_s=1e9)
    assert observe_now(hook, "GER40", ms, frame, [(object(), object())]) == [] and hook.stats()["errors"] >= 1


class StepClock:
    """Every reading advances ``step`` seconds: a deterministic 'slow observer'."""

    def __init__(self, step: float) -> None:
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def test_observer_slower_than_its_budget_is_cut_off_deferred_and_caught_up_exactly():
    ms, fr = ger40()
    frame = fr.iloc[2000:3200].reset_index(drop=True)  # 1200 bars: the registry needs ~1200 steps
    snap, dec = pair_for(frame)
    before = (snap.to_json(), dec.to_json())
    hook = H.ObserverShadow(budget_s=0.5, clock=StepClock(0.3))  # a few clock readings exhaust the cycle budget
    hook.begin_cycle()
    hook.on_bar("GER40", ms, frame, [(snap, dec)])
    out = hook.drain_cycle()
    st = hook.stats()
    assert out == [] and st["budget_exhausted"] >= 1 and st["errors"] == 0 and st["pending"] == {"GER40": 1}
    assert st["bars_seen"]["GER40"][0] < 1200  # cut off mid-replay, state consistent
    assert (snap.to_json(), dec.to_json()) == before  # decisions untouched
    # later cycles with budget finish the job and the record equals the reference exactly
    hook.budget_s = 1e9
    hook.begin_cycle()
    recs = hook.drain_cycle()
    assert len(recs) == 1 and hook.stats()["pending"] == {}
    b = BA.bars_from_frame("GER40", frame, point_size=ms.point_size, tick_size=ms.tick_size, session=H.session_for(ms))
    want = O.observe_event(b, 1199, O.ObservedEvent(1, float(b.c[1199]), "STRUCT", "breakout"), H.config_for(ms))
    assert recs[0].features == want.features and recs[0].meta["warmup_ok"] is False  # 1200 bars < 21 previous days


def test_pending_queue_is_bounded_and_overflow_is_counted():
    ms, fr = ger40()
    hook = H.ObserverShadow(budget_s=0.0, max_pending=2)
    for k in range(5):
        frame = fr.iloc[2000: 2700 + k].reset_index(drop=True)
        hook.begin_cycle()
        hook.on_bar("GER40", ms, frame, [pair_for(frame, tag=f"p{k}")])  # never drained: the queue is bounded, the overflow counted
    st = hook.stats()
    assert st["pending"] == {"GER40": 2} and st["skipped_budget"] == 3


# ---------------------------------------------------------------------------------------------- immutability
def test_inputs_are_never_mutated():
    ms, fr = ger40()
    frame = fr.iloc[3000:3700].reset_index(drop=True)
    h0 = int(pd.util.hash_pandas_object(frame, index=True).sum())
    snap, dec = pair_for(frame)
    j0 = (snap.to_json(), dec.to_json(), dict(snap.signal))
    hook = H.ObserverShadow(budget_s=1e9)
    recs = observe_now(hook, "GER40", ms, frame, [(snap, dec)])
    assert len(recs) == 1
    assert int(pd.util.hash_pandas_object(frame, index=True).sum()) == h0
    assert (snap.to_json(), dec.to_json(), dict(snap.signal)) == j0
    assert "observer" not in " ".join(snap.signal) and "f_levels" not in snap.to_json()
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass / slots
        snap.direction = -1  # type: ignore[misc]


def test_rejected_and_catchup_opportunities_are_observed_and_marked_by_their_ids():
    ms, fr = ger40()
    frame = fr.iloc[3000:3700].reset_index(drop=True)
    hook = H.ObserverShadow(budget_s=1e9)
    acc = pair_for(frame, accepted=True, tag="acc")
    rej = pair_for(frame, accepted=False, tag="rej", family="ROUND", variant="reject")
    recs = observe_now(hook, "GER40", ms, frame, [acc, rej])
    assert {r.meta["opportunity_id"] for r in recs} == {acc[0].opportunity_id, rej[0].opportunity_id}
    assert {r.family for r in recs} == {"STRUCT", "ROUND"} and all(r.labels is None for r in recs)
    assert all(not any(k.startswith("y_") for k in r.to_row()) for r in recs)


def test_wrong_bar_and_older_frames_are_skipped_not_forced():
    ms, fr = ger40()
    frame = fr.iloc[3000:3700].reset_index(drop=True)
    hook = H.ObserverShadow(budget_s=1e9)
    older = fr.iloc[3000:3600].reset_index(drop=True)
    snap_other, dec_other = pair_for(fr.iloc[3000:3650].reset_index(drop=True), tag="x")  # signal ts of another bar than the frame's last
    assert observe_now(hook, "GER40", ms, frame, [(snap_other, dec_other)]) == []
    assert hook.stats()["skipped_mismatch"] == 1
    assert observe_now(hook, "GER40", ms, older, [pair_for(older, tag="o")]) == [] and hook.stats()["skipped_stale"] == 1


def test_short_live_frame_is_flagged_not_complete():
    ms, fr = ger40()
    frame = fr.iloc[3000:3300].reset_index(drop=True)  # 300 closed bars: below every long requirement
    hook = H.ObserverShadow(budget_s=1e9)
    (rec,) = observe_now(hook, "GER40", ms, frame, [pair_for(frame)])
    assert rec.meta["warmup_ok"] is False and hook.stats()["warmup_false_count"] == 1 and rec.meta["bars_available"] == 300


def test_crypto_has_no_session_and_round_steps_come_from_the_market_spec():
    from markets.spec import load_market_spec

    btc = load_market_spec("BTCUSD")
    assert H.session_for(btc) == SessionSpec("UTC", None, None)
    cfg = H.config_for(btc)
    assert cfg.levels.round_minor_step == pytest.approx(500.0) and cfg.levels.round_major_step == pytest.approx(1000.0)  # crypto_cfd 50000/100000 ticks of 0.01
    ger = H.config_for(load_market_spec("GER40"))
    assert ger.levels.round_minor_step == pytest.approx(50.0) and ger.levels.round_major_step == pytest.approx(100.0)


# ---------------------------------------------------------------------------------------------- static isolation
def _py(root: Path):
    return [p for p in root.rglob("*.py")]


def test_observer_hook_imports_nothing_from_execution_risk_or_exits():
    text = (SRC / "demo" / "opportunity" / "observer_hook.py").read_text(encoding="utf-8")
    bad = re.findall(r"^\s*(?:from|import)\s+((?:demo\.execution|execution|risk|exits|demo\.exit_\w+|demo\.exit_policies|nautilus_\w+|adapters|demo\.runner|demo\.store)[\w.]*)", text, re.M)
    assert bad == [], bad


def test_no_execution_risk_exit_or_adapter_module_imports_the_observer():
    pat = re.compile(r"^\s*(?:from|import)\s+(?:market_observer|demo\.opportunity\.observer_hook)\b", re.M)
    roots = [SRC / d for d in ("execution", "risk", "exits", "nautilus_mt5", "nautilus_kernel", "adapters", "persistence", "portfolio", "margin", "signals")] + [SRC / "demo" / "execution"]
    files = [p for r in roots if r.exists() for p in _py(r)] + [SRC / "demo" / n for n in ("exit_policies.py", "exit_profiles.py", "contracts.py", "labeling.py")]
    offenders = [str(p) for p in files if pat.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_the_engine_and_decision_modules_never_read_observer_output():
    opp = SRC / "demo" / "opportunity"
    for name in ("engine.py", "policy.py", "arbitration.py", "snapshot.py", "operating_policy.py", "production_spec.py"):
        text = (opp / name).read_text(encoding="utf-8")
        assert not re.search(r"market_observer|observer_hook|ObserverShadow|ObserverRecord|observer_records", text), name
    # the only observer-related engine state is the write-only frame reference gated by ``retain_frame``
    eng = (opp / "engine.py").read_text(encoding="utf-8")
    assert eng.count("last_frame") == 3 and eng.count("retain_frame") == 3


def test_runner_default_has_no_observer_code_path():
    import inspect

    from demo import runner as R

    assert R.RunnerConfig.__dataclass_fields__["market_observer_enabled"].default is False
    src = inspect.getsource(R.DemoRunner._observer_after_bar)
    assert "if obs is None:" in src and "return" in src
    # the factory default is OFF and the CLI default is OFF
    assert inspect.signature(R.build_live_runner).parameters["market_observer"].default is None
    cli = (Path(__file__).resolve().parents[4] / "scripts" / "demo_trader.py").read_text(encoding="utf-8")
    assert '"--market-observer"' in cli and '"--no-market-observer"' in cli and "market_observer=bool(args.market_observer)" in cli
    sup = (Path(__file__).resolve().parents[4] / "scripts" / "autostart" / "supervisor.py").read_text(encoding="utf-8")
    assert "market-observer" not in sup  # production runner flags are NOT changed by this lane



# ---------------------------------------------------------------------------------------------- Lane H: two-phase timing contract
class Spy:
    """Counts calls of the expensive steps and optionally advances a manual clock per call (a deterministic 'slow machine')."""

    def __init__(self, monkeypatch, clock=None, cost=0.0):
        self.calls = {"frame_arrays": 0, "sync": 0, "advance": 0, "observe": 0}
        self.sync_max_append: list = []
        self.clock, self.cost = clock, cost
        real_fa, real_sync, real_adv, real_obs = BA.frame_arrays, BA.BarBuffer.sync, O.MarketStructureObserver.advance, O.MarketStructureObserver.observe
        spy = self

        def tick():
            if spy.clock is not None:
                spy.clock.t += spy.cost

        def fa(*a, **k):
            spy.calls["frame_arrays"] += 1
            tick()
            return real_fa(*a, **k)

        def sync(self, *a, **k):
            spy.calls["sync"] += 1
            spy.sync_max_append.append(k.get("max_append"))
            tick()
            return real_sync(self, *a, **k)

        def adv(self, *a, **k):
            spy.calls["advance"] += 1
            return real_adv(self, *a, **k)

        def obs(self, *a, **k):
            spy.calls["observe"] += 1
            return real_obs(self, *a, **k)

        monkeypatch.setattr(BA, "frame_arrays", fa)
        monkeypatch.setattr(BA.BarBuffer, "sync", sync)
        monkeypatch.setattr(O.MarketStructureObserver, "advance", adv)
        monkeypatch.setattr(O.MarketStructureObserver, "observe", obs)


class ManualClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_on_bar_only_stashes_no_array_conversion_no_sync_no_registry_no_features(monkeypatch):
    """HIGH 1: the in-scan call is O(1): nothing expensive may run in it; everything happens in the post-scan drain."""
    ms, fr = ger40()
    frame = fr.iloc[3000:9000].reset_index(drop=True)  # 6000 bars: the production window / a cold start
    spy = Spy(monkeypatch)
    hook = H.ObserverShadow(budget_s=1e9)
    hook.on_bar("GER40", ms, frame, [pair_for(frame, tag="a"), pair_for(frame, direction=-1, tag="b")])
    assert spy.calls == {"frame_arrays": 0, "sync": 0, "advance": 0, "observe": 0}
    assert hook.stats()["pending"] == {"GER40": 2} and hook.stats()["bars_seen"] == {} and hook.stats()["records_built"] == 0
    assert hook.stats()["enqueue_ms_max"] < 10.0
    hook.begin_cycle()
    recs = hook.drain_cycle()
    assert len(recs) == 2 and spy.calls["frame_arrays"] == 1 and spy.calls["sync"] >= 12 and spy.calls["observe"] == 2


def test_the_cycle_budget_covers_frame_arrays_and_the_chunked_buffer_sync(monkeypatch):
    """HIGH 1: frame_arrays and BarBuffer.sync are under the SAME budget as the registry; a 6000-bar cold start never runs in one block and ends
    bit-identical to the unbounded batch conversion."""
    ms, fr = ger40()
    frame = fr.iloc[3000:9000].reset_index(drop=True)
    clock = ManualClock()
    spy = Spy(monkeypatch, clock, cost=0.1)  # every sync chunk 'costs' 0.1 s of the injected clock
    hook = H.ObserverShadow(budget_s=0.35, clock=clock, persist_reserve_s=0.0)
    hook.on_bar("GER40", ms, frame, [pair_for(frame)])
    # a cycle whose budget is already used up starts NOTHING (not even the array conversion)
    hook.begin_cycle()
    hook._used = 10.0
    assert hook.drain_cycle() == [] and spy.calls["frame_arrays"] == 0 and spy.calls["sync"] == 0
    cycles, per_cycle_syncs, recs = 0, [], []
    while not recs and cycles < 60:
        hook.begin_cycle()
        before = spy.calls["sync"]
        recs = hook.drain_cycle()
        per_cycle_syncs.append(spy.calls["sync"] - before)
        cycles += 1
    assert len(recs) == 1 and hook.stats()["pending"] == {}
    assert max(per_cycle_syncs) <= 4 and cycles >= 3, per_cycle_syncs  # spread over cycles, never one block
    assert all(m == H.SYNC_CHUNK_BARS for m in spy.sync_max_append) and spy.calls["frame_arrays"] == 1
    assert hook.stats()["budget_exhausted"] >= 1 and hook.stats()["errors"] == 0
    batch = BA.bars_from_frame("GER40", frame, point_size=ms.point_size, tick_size=ms.tick_size, session=H.session_for(ms))
    got = hook._m["GER40"].buf.bars()
    for name in ("ts_ns", "o", "h", "l", "c", "tick_volume", "spread", "atr", "segment_id", "local_minute", "local_day"):
        assert np.array_equal(getattr(got, name), getattr(batch, name), equal_nan=True), name
    want = O.observe_event(batch, 5999, O.ObservedEvent(1, float(batch.c[5999]), "STRUCT", "breakout"), H.config_for(ms))
    assert recs[0].features == want.features  # the chunked cold start produced exactly the reference record


def test_buffer_sync_in_chunks_equals_the_unbounded_sync():
    ms, fr = ger40()
    frame = fr.iloc[3000:5500].reset_index(drop=True)
    arrs = BA.frame_arrays(frame, ms.point_size)
    full = BA.BarBuffer("GER40", tick_size=ms.tick_size, session=H.session_for(ms))
    full.sync(**arrs)
    part = BA.BarBuffer("GER40", tick_size=ms.tick_size, session=H.session_for(ms))
    n = 0
    while part.last_ts_ns != int(arrs["ts_ns"][-1]):
        part.sync(**arrs, max_append=300)
        n += 1
        assert n < 50
    assert n == 9 and len(part) == len(full)
    a, b = part.bars(), full.bars()
    for name in ("ts_ns", "o", "h", "l", "c", "tick_volume", "spread", "atr", "segment_id", "local_minute", "local_day"):
        assert np.array_equal(getattr(a, name), getattr(b, name), equal_nan=True), name


def test_deferral_is_bounded_counted_and_continues_next_cycle():
    ms, fr = ger40()
    clock = ManualClock()
    hook = H.ObserverShadow(budget_s=0.5, clock=clock, max_pending=3, persist_reserve_s=0.0)
    for k in range(6):  # six bars with an event each, nothing drained in between (a long scan / a busy cycle)
        frame = fr.iloc[2000: 2700 + k].reset_index(drop=True)
        hook.on_bar("GER40", ms, frame, [pair_for(frame, tag=f"d{k}")])
    st = hook.stats()
    assert st["pending"] == {"GER40": 3} and st["skipped_budget"] == 3 and st["deferred"] == 6 and st["records_built"] == 0
    hook.begin_cycle()
    hook._used = 99.0  # budget exhausted: the drain defers (counted), nothing is forced
    assert hook.drain_cycle() == [] and hook.stats()["pending"] == {"GER40": 3} and hook.stats()["budget_exhausted"] >= 1
    hook.begin_cycle()
    hook.budget_s = 1e9
    recs = hook.drain_cycle()  # next cycle: the queued events are observed at THEIR OWN bars (oldest first, so none is overtaken)
    assert len(recs) == 3 and hook.stats()["skipped_stale"] == 0 and hook.stats()["pending"] == {}


def test_a_bad_snapshot_is_skipped_and_counted_without_resetting_the_market():
    ms, fr = ger40()
    frame = fr.iloc[3000:3700].reset_index(drop=True)
    hook = H.ObserverShadow(budget_s=1e9)
    good = pair_for(frame, tag="g1")
    recs = observe_now(hook, "GER40", ms, frame, [good, (object(), object()), pair_for(frame, direction=-1, tag="g2")])
    st = hook.stats()
    assert len(recs) == 2 and st["errors"] == 1 and st["dropped_pending"] == 0 and st["resets"] == 0
    assert st["bars_seen"]["GER40"] == [700, 700]  # the market state (buffer + registry) was never dropped for the bad snapshot


def test_a_state_error_drops_the_market_counts_the_pending_events_and_rebuilds(monkeypatch):
    ms, fr = ger40()
    frame = fr.iloc[3000:3700].reset_index(drop=True)
    hook = H.ObserverShadow(budget_s=1e9)

    def boom(self, *a, **k):
        raise RuntimeError("registry exploded")

    with monkeypatch.context() as mp:
        mp.setattr(O.MarketStructureObserver, "advance", boom)
        out = observe_now(hook, "GER40", ms, frame, [pair_for(frame, tag="x"), pair_for(frame, direction=-1, tag="y")])
    st = hook.stats()
    assert out == [] and st["errors"] == 1 and st["dropped_pending"] == 2 and st["pending"] == {} and "GER40" not in st["bars_seen"]
    nxt = fr.iloc[3000:3701].reset_index(drop=True)
    assert len(observe_now(hook, "GER40", ms, nxt, [pair_for(nxt, tag="z")])) == 1  # rebuilt from the next frame


def test_a_buffer_reset_keeps_the_queued_events_and_restarts_the_registry_budgeted():
    """MEDIUM 8: a reset (buffer cap exceeded) rebuilds the buffer in chunks and replays the registry under the budget; events are keyed by their bar
    time, so they are still observed exactly at their own bar afterwards."""
    ms, fr = ger40()
    hook = H.ObserverShadow(budget_s=1e9, max_buffer_bars=800)
    f1 = fr.iloc[2000:2700].reset_index(drop=True)
    assert len(observe_now(hook, "GER40", ms, f1, [pair_for(f1, tag="r1")])) == 1
    f2 = fr.iloc[2150:2850].reset_index(drop=True)  # 150 new bars: 700 + 150 > 800 -> reset
    (rec,) = observe_now(hook, "GER40", ms, f2, [pair_for(f2, tag="r2")])
    st = hook.stats()
    assert st["resets"] == 1 and st["errors"] == 0 and st["bars_seen"]["GER40"] == [700, 700]
    b = BA.bars_from_frame("GER40", f2, point_size=ms.point_size, tick_size=ms.tick_size, session=H.session_for(ms))
    want = O.observe_event(b, 699, O.ObservedEvent(1, float(b.c[699]), "STRUCT", "breakout"), H.config_for(ms))
    assert rec.features == want.features


def test_persist_time_is_charged_against_the_same_cycle_budget():
    hook = H.ObserverShadow(budget_s=0.4)
    hook.begin_cycle()
    hook.charge(0.25)
    st = hook.stats()
    assert st["last_cycle_ms"] >= 250.0 and st["persist_ms_total"] >= 250.0
    assert hook._left(hook._clock()) < 0.4 - 0.25 + 1e-6  # less is left for the next piece of drain work in the same cycle


def test_swing_replay_is_test_only_and_no_src_module_imports_it():
    import market_observer
    from market_observer import swings

    assert not hasattr(market_observer, "SwingReplay")
    assert "TEST-ONLY" in (swings.SwingReplay.__doc__ or "")
    offenders = [str(p) for p in _py(SRC) if "SwingReplay" in p.read_text(encoding="utf-8") and p.name != "swings.py"]
    assert offenders == []


def test_no_execution_risk_exit_or_adapter_module_imports_observer_store():
    pat = re.compile(r"^\s*(?:from|import)\s+(?:demo\.observer_store|demo\.opportunity\.observer_hook|market_observer)\b", re.M)
    roots = [SRC / d for d in ("execution", "risk", "exits", "nautilus_mt5", "nautilus_kernel", "adapters", "persistence", "portfolio", "margin", "signals")] + [SRC / "demo" / "execution"]
    files = [p for r in roots if r.exists() for p in _py(r)] + [SRC / "demo" / n for n in ("exit_policies.py", "exit_profiles.py", "contracts.py", "labeling.py", "store.py", "export.py")]
    assert [str(p) for p in files if pat.search(p.read_text(encoding="utf-8"))] == []
    # the only importer of the observer store is the runner (lazily, flag on)
    importers = sorted(p.name for p in _py(SRC) if re.search(r"^\s*(?:from|import)\s+demo\.observer_store\b", p.read_text(encoding="utf-8"), re.M))
    assert importers == ["runner.py"]
