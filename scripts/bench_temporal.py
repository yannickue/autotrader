# ruff: noqa: E501
"""Synthetic frames + random valid StateMachineStrategySpecs + throughput benchmark.

Also the shared generator used by the kernel-vs-oracle parity / prefix-cache / truncation tests
(``from scripts.bench_temporal import ...``).  Everything is deterministic in its seeds.

    PYTHONPATH=src:. python scripts/bench_temporal.py --specs 2000 --workers 4
"""

from __future__ import annotations

import argparse
import hashlib
import random
import time
from collections.abc import Iterator, Mapping

import numpy as np

from alpha.events import schema as ev
from alpha.temporal.program import ArrayPool, compile_program
from alpha.temporal.reference import MarketFrame
from alpha.temporal.spec import (
    REGS,
    Capture,
    Clause,
    StateMachineStrategySpec,
    StopRule,
    TargetRule,
    Transition,
    mirror,
)


# ------------------------------------------------------------------------------ synthetic frame
class _Arrays(Mapping):
    """Arrays materialised on first access (deterministic per name); ``in`` is always true."""

    def __init__(self, base: _Base) -> None:
        self._base = base
        self._cache: dict[str, np.ndarray] = {}

    def __getitem__(self, name: str) -> np.ndarray:
        a = self._cache.get(name)
        if a is None:
            a = self._cache[name] = self._base.make(name)
        return a

    def __contains__(self, name: object) -> bool:
        return True

    def __iter__(self) -> Iterator[str]:
        return iter(self._cache)

    def __len__(self) -> int:
        return len(self._cache)


class _Thresholds(dict):
    def __init__(self, arrays: _Arrays) -> None:
        super().__init__()
        self._arrays = arrays

    def __missing__(self, key):
        name, q = key
        v = float(np.nanquantile(np.asarray(self._arrays[name], dtype=np.float64), q))
        self[key] = v
        return v


class _Base:
    def __init__(self, n: int, seed: int, density: float, run_len: tuple[int, int]) -> None:
        self.n, self.seed, self.density = n, seed, density
        r = np.random.default_rng(seed)
        self.c = 1000.0 + np.cumsum(r.normal(0.0, 0.6, n))
        self.o = np.empty(n)
        self.o[0] = self.c[0]
        self.o[1:] = self.c[:-1] + r.normal(0.0, 0.1, n - 1)
        self.h = np.maximum(self.o, self.c) + np.abs(r.normal(0.0, 0.4, n))
        self.l = np.minimum(self.o, self.c) - np.abs(r.normal(0.0, 0.4, n))
        self.atr = 0.8 + 0.4 * np.abs(r.normal(0.0, 1.0, n))
        self.atr[: min(14, n)] = np.nan
        self.atr_f = np.nan_to_num(self.atr, nan=1.0)
        self.rs = np.zeros(n, dtype=np.int64)
        pos = 0
        while pos < n:
            ln = int(r.integers(run_len[0], run_len[1] + 1))
            self.rs[pos:pos + ln] = pos
            pos += ln
        self.bm = (np.arange(n) % 288) * 5
        self.arrays = _Arrays(self)

    def _rng(self, key: str) -> np.random.Generator:
        h = hashlib.blake2b(key.encode(), digest_size=8).digest()
        return np.random.default_rng(int.from_bytes(h, "little") ^ (self.seed * 2654435761))

    def _segments(self, key: str, lo: int, hi: int) -> np.ndarray:
        """Segment index per bar (piecewise-constant object identity)."""
        r = self._rng("seg:" + key)
        out = np.empty(self.n, dtype=np.int32)
        pos, sid = 0, 0
        while pos < self.n:
            ln = int(r.integers(lo, hi + 1))
            out[pos:pos + ln] = sid
            pos += ln
            sid += 1
        return out

    def _stepped_offset(self, key: str, lo: float, hi: float, seg=(20, 80)) -> np.ndarray:
        """close-at-segment-start + offset*atr, piecewise constant, NaN in ~2% of segments."""
        sid = self._segments(key, *seg)
        r = self._rng("off:" + key)
        k = int(sid.max()) + 1
        starts = np.searchsorted(sid, np.arange(k))
        off = r.uniform(lo, hi, k) * self.atr_f[starts]
        v = (self.c[starts] + off)[sid]
        v[np.isin(sid, np.flatnonzero(r.random(k) < 0.02))] = np.nan
        return v

    def make(self, name: str) -> np.ndarray:
        n, r = self.n, self._rng(name)
        if name in ev.TARGET_LEVELS:
            high = name.endswith(("high", "pdh"))
            return self._stepped_offset("tgt:" + name, 0.5, 7.0, (10, 60)) if high else \
                self._stepped_offset("tgt:" + name, -7.0, -0.5, (10, 60))
        if name in ev.FEATURE_MIRROR:
            return r.normal(0.0, 1.0, n) if name != "atr_pct" else np.exp(r.normal(0.0, 0.5, n))
        pre, _, stem = name.partition("_")
        if pre == "ev":
            p = (0.005 + 0.025 * r.random()) * self.density
            return (r.random(n) < p).astype(np.uint8)
        if pre in ("evl", "evx"):
            main = self.arrays["ev_" + stem]
            up = not any(t in stem for t in ("_dn", "high", "top"))
            mag = np.abs(r.normal(0.0, 0.8, n)) * self.atr_f
            if pre == "evx":
                mag = mag * 1.3 + 0.2
            v = self.c - mag if up else self.c + mag
            v[main == 0] = np.nan
            v[(main > 0) & (r.random(n) < 0.03)] = np.nan
            return v
        if pre == "st":
            sid = self._segments("st:" + stem, 40, 400)
            k = int(sid.max()) + 1
            return (r.random(k) < 0.6).astype(np.int8)[sid]
        if pre == "zid":
            return self._segments("lv:" + stem, 20, 80)
        if pre == "lv":
            if "zone_lo" in stem or "swing_low" in stem:
                lo, hi = (-3.0, -1.0) if "zone" in stem else (-3.0, -0.5)
            elif "zone_hi" in stem or "swing_high" in stem:
                lo, hi = (1.0, 3.0) if "zone" in stem else (0.5, 3.0)
            else:
                lo, hi = -1.5, 1.5
            return self._stepped_offset("lv:" + stem, lo, hi)
        raise KeyError(f"synthetic frame cannot make {name!r}")


def synth_frame(
    n: int = 75_837, seed: int = 0, density: float = 1.0, run_len: tuple[int, int] = (60, 300)
) -> MarketFrame:
    """Lazy frame: arrays/thresholds are generated on first use (see ``freeze_frame``)."""
    b = _Base(n, seed, density, run_len)
    return MarketFrame(b.o, b.h, b.l, b.c, b.atr, b.rs, b.bm, b.arrays, _Thresholds(b.arrays))


def freeze_frame(specs, frame: MarketFrame) -> MarketFrame:
    """Materialise every array/threshold ``specs`` touch into a plain-dict MarketFrame."""
    pool = ArrayPool(frame)
    for s in specs:
        p = compile_program(s, frame, pool)
        for group in p.stage_zids:
            for _, name in group:
                frame.arrays[name]
        if p.zone_arrays is not None:
            frame.arrays[p.zone_arrays[0]]
            frame.arrays[p.zone_arrays[1]]
    return MarketFrame(
        frame.o, frame.h, frame.l, frame.c, frame.atr, frame.run_start, frame.berlin_minute,
        dict(frame.arrays._cache), dict(frame.thresholds),  # type: ignore[attr-defined]
    )


# ------------------------------------------------------------------------------ random specs
_SWEEP_SRC = ev.SWEEP_LOW_SRCS
# (event, tf, variant, capture options)
_TRIGGERS: list[tuple[str, str, str, tuple[tuple[str, str], ...]]] = [
    *[("SWEEP_LOW", "M5", s, (("evl", "SWEEP_LOW"), ("evx", "SWEEP_LOW"))) for s in _SWEEP_SRC],
    ("SWEEP_LOW", "M15", "prior20", (("evl", "SWEEP_LOW"), ("evx", "SWEEP_LOW"))),
    ("BOS_UP", "M5", "", (("evl", "BOS_UP"),)),
    ("BOS_UP", "M15", "", (("evl", "BOS_UP"),)),
    ("CHOCH_UP", "M5", "", (("evl", "CHOCH_UP"),)),
    ("MOMENTUM_RESUME_UP", "M5", "", (("evl", "MOMENTUM_RESUME_UP"),)),
    ("SWING_LOW_CONF", "M5", "", (("evl", "SWING_LOW_CONF"),)),
    ("SWING_LOW_CONF", "M15", "", (("evl", "SWING_LOW_CONF"),)),
    ("PATTERN_COMPLETE", "M5", "double_bottom",
     (("evl", "PATTERN_COMPLETE"), ("evx", "PATTERN_COMPLETE"))),
    ("TRENDLINE_BREAK", "M5", "t10", (("evl", "TRENDLINE_BREAK"), ("lv", "TRENDLINE_VALUE"))),
    ("TRENDLINE_TOUCH", "M5", "t0", (("evl", "TRENDLINE_TOUCH"), ("lv", "TRENDLINE_VALUE"))),
    ("ZONE_ENTER", "M5", "prior_range", (("lv", "ZONE_LO"), ("lv", "ZONE_HI"))),
    ("ZONE_ENTER", "M15", "swing_cluster", (("lv", "ZONE_LO"), ("lv", "ZONE_HI"))),
    ("ZONE_EXIT", "M5", "prior_range", (("lv", "ZONE_LO"),)),
]
_BAR_CAPS = ("bar_low", "bar_high", "close", "min_low_since_enter", "max_high_since_enter")
_FEATURES = tuple(ev.FEATURE_MIRROR)
_QS = tuple(round(0.05 * i, 2) for i in range(1, 20))
_TOLS = ev.TOL_GRID
_WITHINS = (None, 2, 3, 5, 8, 12, 24, 48)
_SESSIONS = (None, None, (480, 1050), (540, 1200), (0, 720), (600, 1440))


def _feature(rng: random.Random) -> Clause:
    return Clause("feature", rng.choice(_FEATURES), "M5", cmp=rng.choice(("gt", "lt")),
                  q=rng.choice(_QS))


def _anchor(rng: random.Random) -> tuple[tuple[Clause, ...], tuple[Capture, ...]]:
    kind = rng.randrange(8)
    hold = rng.choice((0, 0, 3, 6))
    trend_h1 = Clause("state", "TREND_UP", "H1", op="HOLD", arg=hold) if hold else \
        Clause("state", "TREND_UP", "H1")
    zone15 = Clause("event", "ZONE_ENTER", "M15", variant="swing_cluster")
    if kind == 0:
        cl, caps = [trend_h1, zone15], [Capture("R0", "lv", "ZONE_LO")]
    elif kind == 1:
        cl = [Clause("event", "ZONE_ENTER", "M5", variant="prior_range")]
        caps = [Capture("R0", "lv", "ZONE_LO")]
    elif kind == 2:
        cl = [Clause("state", "TREND_UP", "M15"), Clause("event", "SWEEP_LOW", "M15", variant="prior20")]
        caps = [Capture("R0", "evx", "SWEEP_LOW")]
    elif kind == 3:
        cl, caps = [Clause("event", "SWING_LOW_CONF", "M5")], [Capture("R0", "evl", "SWING_LOW_CONF")]
    elif kind == 4:
        cl = [Clause("event", "PATTERN_COMPLETE", "M5", variant="double_bottom")]
        caps = [Capture("R0", "evx", "PATTERN_COMPLETE")]
    elif kind == 5:
        cl, caps = [trend_h1], [Capture("R0", "bar_low")]
    elif kind == 6:
        cl = [Clause("event", "ZONE_ENTER", "M15", variant="m15_range"), _feature(rng)]
        caps = [Capture("R0", "lv", "ZONE_LO")]
    else:
        cl = [Clause("event", "ZONE_ENTER", "M15", variant="swing_cluster", op="BEFORE",
                     arg=rng.choice((2, 4, 8))),
              Clause("state", "TREND_UP", "M15")]
        caps = [Capture("R0", "bar_low")]
    if rng.random() < 0.2 and len(cl) < 3:
        cl.append(_feature(rng))
    return tuple(cl), tuple(caps)


def _bound(rng: random.Random, reg: str, *, trigger: bool) -> Clause:
    name = rng.choice(("BREAK_UP", "RECLAIM_UP", "TOUCH", "RETEST_HOLD_UP", "HOLD_ABOVE")
                      if trigger else ("BREAK_DN", "HOLD_ABOVE", "RECLAIM_UP", "TOUCH"))
    variant = rng.choice(("k3", "k6")) if name in ("RECLAIM_UP", "HOLD_ABOVE") else ""
    tol = rng.choice(_TOLS) if name in ("BREAK_UP", "BREAK_DN", "TOUCH", "RETEST_HOLD_UP") else 0.0
    return Clause("bound", name, "M5", reg=reg, tol_atr=tol, variant=variant)


def _guard(rng: random.Random, regs: list[str]) -> Clause:
    r = rng.random()
    if r < 0.30:
        return _feature(rng)
    if r < 0.50:
        return Clause("state", "TREND_UP", rng.choice(("M15", "H1")), op="HOLD",
                      arg=rng.choice((2, 3, 5)))
    if r < 0.60:
        return Clause("state", "TREND_UP", "D1")
    if r < 0.80 and regs:
        return _bound(rng, rng.choice(regs), trigger=False)
    if r < 0.90:
        return Clause("event", rng.choice(("BOS_UP", "MOMENTUM_RESUME_UP")), "M5", op="BEFORE",
                      arg=rng.choice((3, 5, 8)))
    return Clause("event", rng.choice(("BOS_UP", "SWING_LOW_CONF")), "M5", op="SINCE_ENTER")


def _invalidate(rng: random.Random, regs: list[str]) -> Clause:
    r = rng.random()
    if r < 0.6 and regs:
        return Clause("bound", "BREAK_DN", "M5", reg=rng.choice(regs), tol_atr=rng.choice(_TOLS))
    if r < 0.8:
        return Clause("event", "CHOCH_DN", "M5")
    return Clause("state", "TREND_UP", "M15", op="NOT")


def _build(rng: random.Random, sid: int) -> StateMachineStrategySpec:
    anchor, anchor_caps = _anchor(rng)
    regs = [c.reg for c in anchor_caps]
    lv_zone = [c.reg for c in anchor_caps if c.source == "lv" and c.of == "ZONE_LO"]
    n_tr = rng.choice((1, 2, 2, 3, 3, 4, 5))
    states = []
    for _ in range(n_tr):
        free = [r for r in REGS if r not in regs]
        if rng.random() < 0.4 and regs:
            trig = _bound(rng, rng.choice(regs), trigger=True)
            opts: tuple[tuple[str, str], ...] = ()
        else:
            ev_name, tf, variant, opts = rng.choice(_TRIGGERS)
            trig = Clause("event", ev_name, tf, variant=variant)
        guards = tuple(_guard(rng, regs) for _ in range(rng.choice((0, 0, 1, 2))))
        invs = tuple(_invalidate(rng, regs) for _ in range(rng.choice((0, 1, 1, 2))))
        caps: list[Capture] = []
        for _ in range(rng.choice((0, 1, 1, 2))):
            if not free:
                break
            if opts and rng.random() < 0.6:
                src, of = rng.choice(opts)
                cap = Capture(free[0], src, of)
            else:
                cap = Capture(free[0], rng.choice(_BAR_CAPS))
            if any(c.source == cap.source and c.of == cap.of for c in caps):
                continue
            caps.append(cap)
            if cap.source == "lv" and cap.of == "ZONE_LO":
                lv_zone.append(cap.reg)
            free = free[1:]
        regs += [c.reg for c in caps]
        states.append(Transition(trig, within=rng.choice(_WITHINS), guards=guards,
                                 invalidate=invs, capture=tuple(caps)))
    if not regs:
        raise ValueError("no register")
    sk = rng.choice(("register", "register", "swing", "atr", "zone_edge"))
    buf = round(rng.choice(range(0, 21)) * 0.05, 2)
    max_risk = rng.choice([x * 0.25 for x in range(4, 25)])
    if sk == "zone_edge" and lv_zone:
        stop = StopRule("zone_edge", reg=lv_zone[0], buffer_atr=buf, max_risk_atr=max_risk)
    elif sk == "swing":
        stop = StopRule("swing", of="SWING_LOW_LVL", tf=rng.choice(("M5", "M15")),
                        buffer_atr=buf, max_risk_atr=max_risk)
    elif sk == "atr":
        stop = StopRule("atr", atr_mult=rng.choice([x * 0.25 for x in range(1, 17)]),
                        max_risk_atr=max_risk)
    else:
        stop = StopRule("register", reg=rng.choice(regs), buffer_atr=buf, max_risk_atr=max_risk)
    if rng.random() < 0.5:
        target = TargetRule("fixed_r", r=rng.choice([1.0 + 0.25 * i for i in range(13)]))
    else:
        pool = ("h1_swing_high", "m15_swing_high", "m5_swing_high", "pdh", "session_high")
        lv = tuple(sorted(rng.sample(pool, rng.randint(1, 4))))
        target = TargetRule("next_structure", levels=lv,
                            fallback_r=rng.choice([1.0 + 0.25 * i for i in range(13)]),
                            min_space_r=rng.choice([0.5 + 0.25 * i for i in range(11)]))
    ctx_pool = [Clause("state", "TREND_UP", "D1"), Clause("state", "TREND_UP", "H1", op="HOLD", arg=3),
                _feature(rng), Clause("event", "ZONE_ENTER", "M15", variant="swing_cluster",
                                      op="BEFORE", arg=12)]
    context = tuple(rng.sample(ctx_pool, rng.choice((0, 0, 1, 2))))
    return StateMachineStrategySpec(
        strategy_id=f"rnd{sid}", version=1, direction="LONG", anchor=anchor,
        anchor_capture=anchor_caps, states=tuple(states), context=context,
        expires_after=rng.randint(n_tr + 1, 96), session_window=rng.choice(_SESSIONS),
        stop=stop, target=target,
    )


def golden_like(rng: random.Random, sid: int) -> StateMachineStrategySpec:
    """The section-2 example structure with randomised windows / stop / target."""
    w = lambda: rng.choice((3, 5, 8, 12, 24))  # noqa: E731
    return StateMachineStrategySpec(
        strategy_id=f"gold{sid}", version=1, direction="LONG",
        anchor=(Clause("state", "TREND_UP", "H1"),
                Clause("event", "ZONE_ENTER", "M15", variant="swing_cluster")),
        anchor_capture=(Capture("R0", "lv", "ZONE_LO"),),
        states=(
            Transition(Clause("event", "SWEEP_LOW", "M5", variant=rng.choice(_SWEEP_SRC)),
                       within=w(), invalidate=(Clause("bound", "BREAK_DN", "M5", reg="R0"),),
                       capture=(Capture("R1", "evx", "SWEEP_LOW"),)),
            Transition(Clause("bound", "RECLAIM_UP", "M5", reg="R0", variant=rng.choice(("k3", "k6"))),
                       within=w(), invalidate=(Clause("bound", "BREAK_DN", "M5", reg="R1"),)),
            Transition(Clause("event", "BOS_UP", "M5"), within=w(),
                       capture=(Capture("R2", "evl", "BOS_UP"),)),
            Transition(Clause("bound", "RETEST_HOLD_UP", "M5", reg="R2",
                              tol_atr=rng.choice(_TOLS)), within=w()),
        )[: rng.choice((2, 3, 4))],
        context=(), expires_after=rng.randint(12, 60), session_window=rng.choice(_SESSIONS),
        stop=StopRule("register", reg="R1", buffer_atr=rng.choice((0.0, 0.1, 0.25)),
                      max_risk_atr=rng.choice((2.0, 3.0, 6.0))),
        target=rng.choice((
            TargetRule("fixed_r", r=2.0),
            TargetRule("next_structure", levels=("h1_swing_high", "pdh", "session_high"),
                       fallback_r=2.0, min_space_r=1.5),
        )),
    )


def random_spec(rng: random.Random, sid: int = 0, *, short: bool | None = None,
                golden: float = 0.15) -> StateMachineStrategySpec:
    """A random valid spec (LONG, or its mirror for SHORT); ``short=None`` flips a coin."""
    for _ in range(200):
        try:
            spec = golden_like(rng, sid) if rng.random() < golden else _build(rng, sid)
            if (rng.random() < 0.5) if short is None else short:
                spec = mirror(spec)
            return spec
        except ValueError:
            continue
    raise RuntimeError("could not generate a valid spec")  # pragma: no cover


def random_specs(n: int, seed: int, **kw) -> list[StateMachineStrategySpec]:
    rng = random.Random(seed)
    return [random_spec(rng, i, **kw) for i in range(n)]


# ------------------------------------------------------------------------------ benchmark
def _mb(b: int) -> str:
    return f"{b / 2**20:.0f} MB"


def main() -> None:
    from alpha.temporal import batch
    from alpha.temporal.evaluate import evaluate_temporal_many

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=75_837)
    ap.add_argument("--specs", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--density", type=float, default=1.0)
    ap.add_argument("--cache-mb", type=int, default=512)
    a = ap.parse_args()

    specs = random_specs(a.specs, a.seed)
    t0 = time.perf_counter()
    frame = freeze_frame(specs, synth_frame(a.n, a.seed, a.density))
    print(f"frame: {a.n} bars, {len(frame.arrays)} arrays, {len(specs)} specs "
          f"(setup {time.perf_counter() - t0:.1f}s)")
    evaluate_temporal_many(specs[:8], frame)  # numba warm-up / cache load

    st = batch.BatchStats()
    t0 = time.perf_counter()
    res = evaluate_temporal_many(specs, frame, cache_bytes=a.cache_mb << 20, stats=st)
    dt = time.perf_counter() - t0
    with_c = sum(len(r.candidates.decision_idx) > 0 for r in res)
    total_c = sum(len(r.candidates.decision_idx) for r in res)
    print(f"1 worker, prefix cache: {len(specs) / dt:.0f} specs/s ({dt:.2f}s)  "
          f"hit_rate={st.hit_rate:.2f}  stages {st.stages_computed}/{st.stage_demands}  "
          f"peak cache {_mb(st.peak_cache_bytes)}  peak RSS {_mb(batch.peak_rss_bytes())}")
    print(f"specs with >=1 candidate: {with_c}/{len(specs)} = {with_c / len(specs):.1%}  "
          f"(total candidates {total_c})")

    st2 = batch.BatchStats()
    t0 = time.perf_counter()
    res2 = evaluate_temporal_many(specs, frame, use_cache=False, stats=st2)
    dt2 = time.perf_counter() - t0
    same = all(x.to_bytes() == y.to_bytes() for x, y in zip(res, res2, strict=True))
    print(f"1 worker, no cache:     {len(specs) / dt2:.0f} specs/s ({dt2:.2f}s)  identical={same}")

    if a.workers > 1:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        with batch.SharedFrame(frame) as sh, ProcessPoolExecutor(
            max_workers=a.workers, mp_context=mp.get_context("spawn")
        ) as ex:
            evaluate_temporal_many(specs[: a.workers * 2], frame, workers=a.workers,
                                   executor=ex, shared=sh)  # spawn + numba cache warm-up
            st3 = batch.BatchStats()
            t0 = time.perf_counter()
            res3 = evaluate_temporal_many(specs, frame, workers=a.workers, executor=ex,
                                          shared=sh, cache_bytes=a.cache_mb << 20, stats=st3)
            dt3 = time.perf_counter() - t0
        same3 = all(x.to_bytes() == y.to_bytes() for x, y in zip(res, res3, strict=True))
        print(f"{a.workers} workers:          {len(specs) / dt3:.0f} specs/s ({dt3:.2f}s)  "
              f"hit_rate={st3.hit_rate:.2f}  identical={same3}  "
              f"worker peak RSS {[_mb(x) for x in st3.worker_peak_rss]}")


if __name__ == "__main__":
    main()
