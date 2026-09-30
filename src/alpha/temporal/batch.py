# ruff: noqa: E501
"""Batch evaluation with exact prefix sharing (trie over canonical prefix hashes).

A stage result ``P_k`` depends only on (anchor, T_1..T_k, expires_after, direction), so specs that
share a prefix share ``P_k`` EXACTLY.  ``evaluate_many`` builds a trie over the cumulative prefix
hashes of ``Program.prefix_keys`` and evaluates it depth-first with an LRU (``PrefixCache``,
byte-bounded) of stage results.  Results never depend on cache state or batch order: a cache hit
returns precisely the array a recomputation would produce.  Optional multiprocessing (Windows
``spawn``) splits the batch by (anchor, T1) group; workers memory-map the frame from ``.npy`` files
and return byte-identical results.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from alpha.temporal import kernel as kn
from alpha.temporal.program import ArrayPool, Program, compile_program
from alpha.temporal.reference import MarketFrame
from alpha.temporal.spec import StateMachineStrategySpec

DEFAULT_CACHE_BYTES = 512 * 1024 * 1024
CHUNKS_PER_WORKER = 4


# ------------------------------------------------------------------------------ cache / stats
@dataclass
class BatchStats:
    n_specs: int = 0
    stage_demands: int = 0  # sum over specs of (n_transitions + 1)
    stages_computed: int = 0  # kernel stage calls actually run (anchor + advances)
    cache_hits: int = 0  # stage lookups answered from a previous call's cache
    peak_cache_bytes: int = 0
    elapsed_s: float = 0.0
    worker_peak_rss: list[int] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        """Fraction of demanded stages not recomputed (trie sharing + cache hits)."""
        return 0.0 if not self.stage_demands else 1.0 - self.stages_computed / self.stage_demands

    def merge(self, other: BatchStats) -> None:
        for f in ("n_specs", "stage_demands", "stages_computed", "cache_hits"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        self.peak_cache_bytes = max(self.peak_cache_bytes, other.peak_cache_bytes)


class PrefixCache:
    """Byte-bounded LRU of stage results keyed by cumulative prefix hash, bound to one frame."""

    def __init__(self, budget_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        self.budget = int(budget_bytes)
        self._d: OrderedDict[bytes, kn.Stage] = OrderedDict()
        self.bytes = 0
        self.peak_bytes = 0
        self._frame_ref: MarketFrame | None = None

    def bind(self, frame: MarketFrame) -> None:
        if self._frame_ref is not frame:
            self.clear()
            self._frame_ref = frame

    def clear(self) -> None:
        self._d.clear()
        self.bytes = 0

    def __len__(self) -> int:
        return len(self._d)

    def get(self, key: bytes) -> kn.Stage | None:
        st = self._d.get(key)
        if st is not None:
            self._d.move_to_end(key)
        return st

    def put(self, key: bytes, st: kn.Stage) -> None:
        if key in self._d or st.nbytes > self.budget:
            return
        self._d[key] = st
        self.bytes += st.nbytes
        self.peak_bytes = max(self.peak_bytes, self.bytes)
        while self.bytes > self.budget and self._d:
            _, old = self._d.popitem(last=False)
            self.bytes -= old.nbytes


# ------------------------------------------------------------------------------ single process
def _finish(prog: Program, frame: MarketFrame, pa, stage: kn.Stage) -> kn.TemporalResult:
    keep, stop, tgt, tr = kn.run_finalize(prog, pa, stage)
    return kn.assemble(prog, frame, stage, keep, stop, tgt, tr)


def _evaluate_independent(
    progs: Sequence[Program], frame: MarketFrame, pa, ws: kn.Workspace, stats: BatchStats,
    mode: int = 0,
) -> list[kn.TemporalResult]:
    out = []
    for p in progs:
        st = kn.run_anchor(p, pa)
        stats.stages_computed += 1
        for k in range(p.n_tr):
            st = kn.run_advance(p, pa, k, st, ws, mode)
            stats.stages_computed += 1
        out.append(_finish(p, frame, pa, st))
    return out


def _evaluate_trie(
    progs: Sequence[Program], frame: MarketFrame, pa, ws: kn.Workspace, cache: PrefixCache,
    stats: BatchStats, mode: int = 0,
) -> list[kn.TemporalResult]:
    # leaves[key] = spec indices whose LAST stage is that node; kids[key] = ordered child keys
    leaves: dict[bytes, list[int]] = {}
    kids: dict[bytes, dict[bytes, int]] = {}  # parent key -> {child key: representative spec}
    roots: dict[bytes, int] = {}
    for i, p in enumerate(progs):
        keys = p.prefix_keys
        roots.setdefault(keys[0], i)
        for d in range(p.n_tr):
            kids.setdefault(keys[d], {}).setdefault(keys[d + 1], i)
        leaves.setdefault(keys[-1], []).append(i)
    results: list[kn.TemporalResult | None] = [None] * len(progs)

    def stage_for(key: bytes, compute) -> kn.Stage:
        st = cache.get(key)
        if st is None:
            st = compute()
            stats.stages_computed += 1
            cache.put(key, st)
        else:
            stats.cache_hits += 1
        return st

    def visit(depth: int, key: bytes, stage: kn.Stage) -> None:
        for i in leaves.get(key, ()):
            results[i] = _finish(progs[i], frame, pa, stage)
        for ckey in sorted(kids.get(key, ())):
            rep = progs[kids[key][ckey]]
            child = stage_for(ckey, lambda rep=rep, d=depth, s=stage: kn.run_advance(rep, pa, d, s, ws, mode))
            visit(depth + 1, ckey, child)

    for rkey in sorted(roots):
        rep = progs[roots[rkey]]
        visit(0, rkey, stage_for(rkey, lambda rep=rep: kn.run_anchor(rep, pa)))
    return results  # type: ignore[return-value]


def evaluate_many_local(
    specs: Sequence[StateMachineStrategySpec], frame: MarketFrame, *, use_cache: bool = True,
    cache: PrefixCache | None = None, cache_bytes: int = DEFAULT_CACHE_BYTES,
    stats: BatchStats | None = None, advance_mode: int = 0,
) -> list[kn.TemporalResult]:
    """``advance_mode``: 0 auto / 1 per-bar scan / 2 next-fire jump (test hook, same results)."""
    stats = stats if stats is not None else BatchStats()
    t0 = time.perf_counter()
    pool = ArrayPool(frame)
    progs = [compile_program(s, frame, pool) for s in specs]
    pa = pool.freeze()
    ws = kn.Workspace(pa.n)
    stats.n_specs += len(progs)
    stats.stage_demands += sum(p.n_tr + 1 for p in progs)
    if use_cache:
        if cache is None:
            cache = PrefixCache(cache_bytes)
        cache.bind(frame)
        out = _evaluate_trie(progs, frame, pa, ws, cache, stats, advance_mode)
        stats.peak_cache_bytes = max(stats.peak_cache_bytes, cache.peak_bytes)
    else:
        out = _evaluate_independent(progs, frame, pa, ws, stats, advance_mode)
    stats.elapsed_s += time.perf_counter() - t0
    return out


# ------------------------------------------------------------------------------ multiprocessing
_CORE = ("o", "h", "l", "c", "atr", "run_start", "berlin_minute")


class SharedFrame:
    """Dump a MarketFrame as ``.npy`` files so spawn workers can memory-map it (context manager)."""

    def __init__(self, frame: MarketFrame, root: str | None = None) -> None:
        self._own = root is None
        self.path = tempfile.mkdtemp(prefix="temporal_frame_") if root is None else root
        os.makedirs(self.path, exist_ok=True)
        for name in _CORE:
            np.save(os.path.join(self.path, f"core_{name}.npy"), getattr(frame, name))
        names = []
        for i, (k, v) in enumerate(frame.arrays.items()):
            np.save(os.path.join(self.path, f"arr_{i}.npy"), np.asarray(v))
            names.append(k)
        meta = {"names": names,
                "thr": [[k[0], k[1], float(v)] for k, v in frame.thresholds.items()],
                "ts": frame.ts_close_ns is not None}
        if frame.ts_close_ns is not None:
            np.save(os.path.join(self.path, "ts.npy"), frame.ts_close_ns)
        with open(os.path.join(self.path, "meta.json"), "w") as f:
            json.dump(meta, f)

    def close(self) -> None:
        if self._own:
            shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> SharedFrame:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


_FRAME_CACHE: dict[str, MarketFrame] = {}


def _load_frame(path: str) -> MarketFrame:
    fr = _FRAME_CACHE.get(path)
    if fr is None:
        with open(os.path.join(path, "meta.json")) as f:
            meta = json.load(f)
        core = {k: np.load(os.path.join(path, f"core_{k}.npy"), mmap_mode="r") for k in _CORE}
        arrays = {
            n: np.load(os.path.join(path, f"arr_{i}.npy"), mmap_mode="r")
            for i, n in enumerate(meta["names"])
        }
        ts = np.load(os.path.join(path, "ts.npy"), mmap_mode="r") if meta["ts"] else None
        fr = MarketFrame(
            **core, arrays=arrays, thresholds={(a, q): v for a, q, v in meta["thr"]},
            ts_close_ns=ts,
        )
        _FRAME_CACHE.clear()
        _FRAME_CACHE[path] = fr
    return fr


def peak_rss_bytes() -> int:
    """Peak resident set of this process (0 if unavailable)."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
            c = _Counters()
            c.cb = ctypes.sizeof(c)
            psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
            return int(c.PeakWorkingSetSize)
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        return 0


def _worker(path: str, spec_jsons: list[str], cache_bytes: int, use_cache: bool):
    frame = _load_frame(path)
    specs = [StateMachineStrategySpec.from_dict(json.loads(j)) for j in spec_jsons]
    stats = BatchStats()
    res = evaluate_many_local(
        specs, frame, use_cache=use_cache, cache_bytes=cache_bytes, stats=stats
    )
    return res, stats, peak_rss_bytes()


def _group_key(spec: StateMachineStrategySpec) -> str:
    d = spec.to_dict()
    head = {"dir": d["direction"], "exp": d["expires_after"], "a": sorted(map(json.dumps, d["anchor"])),
            "ac": d["anchor_capture"], "t1": d["states"][0]}
    return json.dumps(head, sort_keys=True)


def _partition(specs: Sequence[StateMachineStrategySpec], workers: int) -> list[list[int]]:
    groups: dict[str, list[int]] = {}
    for i, s in enumerate(specs):
        groups.setdefault(_group_key(s), []).append(i)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    bins: list[list[int]] = [[] for _ in range(workers)]
    for _, idx in ordered:  # LPT: biggest group to the currently lightest bin (deterministic)
        min(bins, key=len).extend(idx)
    return [sorted(b) for b in bins if b]


def evaluate_many(
    specs: Sequence[StateMachineStrategySpec], frame: MarketFrame, *, workers: int = 1,
    use_cache: bool = True, cache: PrefixCache | None = None,
    cache_bytes: int = DEFAULT_CACHE_BYTES, stats: BatchStats | None = None,
    executor=None, shared: SharedFrame | None = None,
) -> list[kn.TemporalResult]:
    """Evaluate many specs on one frame; results are in input order and independent of
    ``workers``, ``use_cache`` and the order of ``specs``."""
    if workers <= 1 or len(specs) <= 1:
        return evaluate_many_local(
            specs, frame, use_cache=use_cache, cache=cache, cache_bytes=cache_bytes, stats=stats
        )
    stats = stats if stats is not None else BatchStats()
    t0 = time.perf_counter()
    parts = _partition(specs, workers * CHUNKS_PER_WORKER)  # dynamic dispatch balances heavy specs
    own_frame = shared is None
    sh = shared if shared is not None else SharedFrame(frame)
    own_exec = executor is None
    if own_exec:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        executor = ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"))
    try:
        futs = [
            executor.submit(
                _worker, sh.path,
                [json.dumps(specs[i].to_dict()) for i in idx],
                cache_bytes // workers, use_cache,
            )
            for idx in parts
        ]
        out: list[kn.TemporalResult | None] = [None] * len(specs)
        for idx, fut in zip(parts, futs, strict=True):
            res, wstats, rss = fut.result()
            for i, r in zip(idx, res, strict=True):
                out[i] = r
            stats.merge(wstats)
            stats.worker_peak_rss.append(rss)
    finally:
        if own_exec:
            executor.shutdown()
        if own_frame:
            sh.close()
    stats.elapsed_s += time.perf_counter() - t0
    return out  # type: ignore[return-value]
