# ruff: noqa: E501
"""GATE A - LIVE BEHAVIOUR PARITY: the observer must never change an opportunity, direction, decision, reason code, intent, entry, stop, target, size,
exit profile, arbitration result or snapshot byte.

The REAL ``OpportunityEngine`` (frozen production spec v1.2 = the committed production universe incl. BTCUSD / BRENT STRUCT variants) is driven bar by bar
over the SAME historical inputs TWICE: observer OFF (engine untouched) vs observer ON (``retain_frame`` + ``ObserverShadow.on_bar`` after every decision + ``drain_cycle``),
for every market of the production universe, several hundred bars each. Everything the engine returns is compared byte for byte, and the engine's full
state fingerprint is compared before / after every hook call. Data: real DEV bars of the five core markets (data/markets); BTCUSD / BRENT have no history in
the repo, so their frames are price-rescaled copies of real XAUUSD bars (real timestamps, ranges and tick volumes; synthetic price level only) - the engine
runs the production STRUCT variants on them under their real MarketSpecs / calendars.

Catalogue: baseline parity (engine ON == OFF); no effect on Risk/Execution at the engine boundary (intents, stops, targets, risk fractions identical);
arbitration results identical (snapshot.signal['arbitration']); snapshot immutability (JSON bytes identical, no observer data inside); zero code path when
the flag is off (frame never retained).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from functools import lru_cache

import pandas as pd
import pytest

from alpha.common.market_data import load_dev_market_frame
from demo.opportunity.bar_source import M5_SECONDS, ReplayBarSource
from demo.opportunity.engine import CatchupInfo, InMemorySeenStore, OpportunityEngine
from demo.opportunity.observer_hook import ObserverShadow
from demo.opportunity.production_spec import DEFAULT_PATH_V1_2, load_production_spec
from markets.spec import CANONICALS, load_market_spec

SYNTH = {"BTCUSD": ("XAUUSD", 40.0), "BRENT": ("XAUUSD", 0.04)}

# (market, first bar OPEN, number of bars): several hundred real bars each; chosen so the windows contain opportunities
WINDOWS = [
    ("GER40", "2026-06-09T05:00", 300),
    ("NAS100", "2026-06-09T12:00", 300),
    ("SPX500", "2026-06-09T12:00", 300),
    ("XAUUSD", "2026-06-09T05:00", 300),
    ("EURUSD", "2026-06-09T05:00", 300),
    ("BTCUSD", "2026-06-09T05:00", 300),
    ("BRENT", "2026-06-09T05:00", 300),
]
CATCHUP_WINDOWS = [
    ("GER40", "2026-06-09T05:00", 250),
    ("XAUUSD", "2026-06-09T05:00", 250),
    ("NAS100", "2026-06-09T12:00", 250),
    ("BTCUSD", "2026-06-09T05:00", 250),
]
TOTALS: Counter = Counter()


@lru_cache(maxsize=1)
def world():
    prod = load_production_spec(DEFAULT_PATH_V1_2)
    names = sorted(prod.market_names())
    mspecs = {m: load_market_spec(m) for m in names}
    frames = {m: load_dev_market_frame(mspecs[m]) for m in CANONICALS}
    for m, (src, scale) in SYNTH.items():
        d = frames[src].copy()
        for c in ("open", "high", "low", "close"):
            d[c] = d[c] * scale
        frames[m] = d
    return prod, mspecs, frames


def fingerprint(engine: OpportunityEngine) -> str:
    """Hash of every piece of engine state that influences or records a decision."""
    state = {
        "last_bar": {k: str(v) for k, v in sorted(engine._last_bar.items())},
        "seen": sorted(engine._seen._seen) if isinstance(engine._seen, InMemorySeenStore) else None,
        "intents": {k: v.to_json() for k, v in sorted(engine._intents.items())},
        "last_intents": [i.to_json() for i in engine.last_intents],
        "last_candidates": [repr(c) for c in engine.last_candidates],
        "health": dict(sorted(engine.health.items())), "dups": engine.suppressed_duplicates, "skipped": engine.shadow_skipped_in_window,
        "config_hash": engine.config_hash, "strategy_hash": engine.strategy_hash,
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def run(market: str, start: str, n_bars: int, *, observer: bool, catchup: bool = False):
    prod, mspecs, frames = world()
    src = ReplayBarSource(frames, {m: mspecs[m].point_size for m in frames})
    engine = OpportunityEngine(src, production=prod, market_specs=mspecs, window_bars=1000, min_history_bars=600, commit="gate-a", seen_store=InMemorySeenStore())
    hook = None
    if observer:
        engine.retain_frame = True
        hook = ObserverShadow(budget_s=1e9)
    ts = pd.DatetimeIndex(frames[market]["ts"])
    t0 = pd.Timestamp(start, tz="UTC")
    first = int(ts.searchsorted(t0))
    out = []
    n_records = 0
    for t in ts[first: first + n_bars]:
        now = (t + pd.Timedelta(seconds=M5_SECONDS)).to_pydatetime()
        src.set_time(now)
        if catchup:  # the CATCH-UP path: every opportunity the policy would accept is recorded as a rejected CATCHUP_MISSED non-trade
            pairs = engine.on_m5_close(market, now, catchup=CatchupInfo(live_now=now + pd.Timedelta(hours=1)))
        else:
            pairs = engine.on_m5_close(market, now)
        intents = engine.intents_for(list(pairs))
        out.append({
            "ts": now.isoformat(),
            "pairs": [(s.to_json(), d.to_json()) for s, d in pairs],
            "intents": [i.to_json() for i in intents],
            "cands": [repr(c) for c in engine.last_candidates],
            "fp": fingerprint(engine),
        })
        if hook is not None:
            before = fingerprint(engine)
            snap_json = [(s.to_json(), d.to_json()) for s, d in pairs]
            hook.on_bar(market, mspecs[market], engine.last_frame, pairs)  # the O(1) in-scan stash ...
            hook.begin_cycle()
            recs = hook.drain_cycle()  # ... and the post-scan budgeted work
            n_records += len(recs)
            assert fingerprint(engine) == before, "the hook changed engine state"
            assert [(s.to_json(), d.to_json()) for s, d in pairs] == snap_json, "the hook changed a snapshot / decision"
        else:
            assert engine.last_frame is None, "zero code path when disabled: the frame must never be retained"
    return out, engine, hook, n_records


def _compare(market, start, n_bars, *, catchup, label):
    off, eng_off, _h, _n = run(market, start, n_bars, observer=False, catchup=catchup)
    on, eng_on, hook, n_records = run(market, start, n_bars, observer=True, catchup=catchup)
    assert len(off) == len(on) == n_bars
    for a, b in zip(off, on, strict=True):
        assert a == b, f"{market} {a['ts']}: engine output differs with the observer on"
    n_pairs = sum(len(x["pairs"]) for x in off)
    n_acc = sum(1 for x in off for _s, d in x["pairs"] if json.loads(d)["accepted"])
    n_rej = n_pairs - n_acc
    n_int = sum(len(x["intents"]) for x in off)
    reasons = Counter(r for x in off for _s, d in x["pairs"] for r in json.loads(d)["reasons"])
    arb = sum(1 for x in off for s, _d in x["pairs"] if "arbitration" in json.loads(s)["signal"])
    families = Counter(json.loads(s)["signal"].get("family") for x in off for s, _d in x["pairs"])
    assert fingerprint(eng_off) == fingerprint(eng_on)
    assert n_pairs > 0, f"{market}: the window must contain opportunities to be meaningful"
    stats = hook.stats()
    assert stats["errors"] == 0 and stats["skipped_mismatch"] == 0
    assert n_records == n_pairs and stats["pending"] == {}, (n_records, n_pairs, stats)
    TOTALS.update({"cases": 1, "bars": n_bars, "opportunities": n_pairs, "accepted": n_acc, "rejected": n_rej, "intents": n_int, "arbitrated": arb, "records": n_records})
    print(f"\nGATE_A {label} {market}: bars={n_bars} opportunities={n_pairs} accepted={n_acc} rejected={n_rej} intents={n_int} arbitrated={arb} records={n_records} families={dict(families)} top_reasons={dict(reasons.most_common(4))}")


@pytest.mark.parametrize(("market", "start", "n_bars"), WINDOWS)
def test_gate_a_engine_output_is_byte_identical_with_the_observer_on(market, start, n_bars):
    _compare(market, start, n_bars, catchup=False, label="live")


@pytest.mark.parametrize(("market", "start", "n_bars"), CATCHUP_WINDOWS)
def test_gate_a_catchup_path_rejections_are_byte_identical_with_the_observer_on(market, start, n_bars):
    """Engine-REJECTED / counterfactual opportunities (CATCHUP_MISSED & co.) are observed exactly like accepted ones and change nothing."""
    _compare(market, start, n_bars, catchup=True, label="catchup")


def test_gate_a_totals_are_not_vacuous():
    """Runs after the parametrised cases in file order: the replay really compared a meaningful population."""
    if TOTALS["cases"] < len(WINDOWS) + len(CATCHUP_WINDOWS):
        pytest.skip("parity cases did not all run in this session")
    print(f"\nGATE_A TOTAL: {dict(TOTALS)}")
    assert TOTALS["opportunities"] >= 50 and TOTALS["intents"] >= 1 and TOTALS["rejected"] >= 50
