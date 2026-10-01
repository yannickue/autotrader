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
        for rec in hook.on_bar("GER40", ms, frame, pairs):
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
    out = hook.on_bar("GER40", ms, frame, [pair_for(frame)])
    assert out == [] and hook.stats()["errors"] == 1 and "synthetic observer failure" in hook.stats()["last_error"]
    nxt = fr.iloc[3000:3701].reset_index(drop=True)
    out = hook.on_bar("GER40", ms, nxt, [pair_for(nxt, tag="b")])  # state was rebuilt from the next frame
    assert len(out) == 1 and hook.stats()["errors"] == 1
    assert hook.on_bar("GER40", ms, None, [pair_for(nxt)]) == [] and hook.on_bar("GER40", None, nxt, [pair_for(nxt)]) == []  # garbage inputs never raise


def test_garbage_pairs_never_raise():
    ms, fr = ger40()
    frame = fr.iloc[3000:3700].reset_index(drop=True)
    hook = H.ObserverShadow(budget_s=1e9)
    assert hook.on_bar("GER40", ms, frame, [(object(), object())]) == [] and hook.stats()["errors"] >= 1


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
    out = hook.on_bar("GER40", ms, frame, [(snap, dec)])
    st = hook.stats()
    assert out == [] and st["budget_exhausted"] >= 1 and st["errors"] == 0 and st["pending"] == {"GER40": 1}
    assert st["bars_seen"]["GER40"][0] < 1200  # cut off mid-replay, state consistent
    assert (snap.to_json(), dec.to_json()) == before  # decisions untouched
    # later cycles with budget finish the job and the record equals the reference exactly
    hook.budget_s = 1e9
    hook.begin_cycle()
    recs = hook.warm_step()
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
        hook.on_bar("GER40", ms, frame, [pair_for(frame, tag=f"p{k}")])
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
    recs = hook.on_bar("GER40", ms, frame, [(snap, dec)])
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
    recs = hook.on_bar("GER40", ms, frame, [acc, rej])
    assert {r.meta["opportunity_id"] for r in recs} == {acc[0].opportunity_id, rej[0].opportunity_id}
    assert {r.family for r in recs} == {"STRUCT", "ROUND"} and all(r.labels is None for r in recs)
    assert all(not any(k.startswith("y_") for k in r.to_row()) for r in recs)


def test_wrong_bar_and_older_frames_are_skipped_not_forced():
    ms, fr = ger40()
    frame = fr.iloc[3000:3700].reset_index(drop=True)
    hook = H.ObserverShadow(budget_s=1e9)
    older = fr.iloc[3000:3600].reset_index(drop=True)
    snap_other, dec_other = pair_for(fr.iloc[3000:3650].reset_index(drop=True), tag="x")  # signal ts of another bar than the frame's last
    assert hook.on_bar("GER40", ms, frame, [(snap_other, dec_other)]) == []
    assert hook.stats()["skipped_mismatch"] == 1
    assert hook.on_bar("GER40", ms, older, [pair_for(older, tag="o")]) == [] and hook.stats()["skipped_stale"] == 1


def test_short_live_frame_is_flagged_not_complete():
    ms, fr = ger40()
    frame = fr.iloc[3000:3300].reset_index(drop=True)  # 300 closed bars: below every long requirement
    hook = H.ObserverShadow(budget_s=1e9)
    (rec,) = hook.on_bar("GER40", ms, frame, [pair_for(frame)])
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
