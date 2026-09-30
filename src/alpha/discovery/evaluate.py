"""Shared genome evaluator: canonicalize -> cache -> compile -> simulate ONCE per cost -> reduce.

Research only.  One ``GenomeEvaluator`` serves every search driver (Optuna, DEAP, random) so all
of them share one trial ledger and one on-disk result cache.

Train/Validation separation is STRUCTURAL:

* ``GenomeEval.train`` is a ``TrainView`` -- the only thing ``fitness.train_fitness`` accepts.
* Validation numbers are computed and stored (one simulation per cost yields both partitions)
  but live in a private field reachable only through ``validation_gate_view``.  Nothing in the
  search path (fitness, Optuna driver) imports or calls it; explicit gate/diagnostic code does.

Determinism: no wall-clock, no randomness.  Thresholds are resolved from TRAIN quantiles, so the
cache fingerprint contains the TRAIN-mask hash as the resolver identity.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import importlib
import json
import math
import os
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from alpha.common.protocol import SplitPlan, stable_hash
from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec
from alpha.discovery.compile import (
    SPEC_VERSION,
    ThresholdResolver,
    TrialLedger,
    behavior_key,
    canonical_hash,
    canonicalize,
    compile_genome,
)
from alpha.discovery.disk import prune_oldest_shards
from alpha.discovery.genome import Genome, GenomeError, complexity
from alpha.fast.screen import (
    PartitionScreen,
    RejectReason,
    reject_reason,
    screen_partition_trades,
)
from alpha.fast.sim import CandidateArrays, TradeArrays, simulate_fast
from alpha.fast.spec import RuleMaskCache, evaluate_spec

# v4: lazy BASE/Validation scenarios (cache entries carry a completeness flag), shard cache;
#     v3: wider source/library fingerprint; v2: MIN 60
EVALUATOR_VERSION = "ad1-genome-eval-v4"
# Stage A / fitness minimum of Train trades (COMBINED_ADVERSE).  Principled from the standard
# error: at n = 60 with sd(R) ~ 1..1.5 the mean is resolved to ~0.13-0.19 R (1 SE).
MIN_TRAIN_TRADES = 60
N_CHUNKS = 3
Z95 = 1.959963984540054
BASE_COST = "BASE"
ADVERSE_COST = "COMBINED_ADVERSE"
_SOURCE_MODULES = (
    "alpha.fast.sim", "alpha.fast.spec", "alpha.fast.screen", "alpha.discovery.compile",
    "alpha.discovery.catalog", "alpha.discovery.evaluate", "alpha.discovery.genome",
    "alpha.common.frame", "alpha.common.sim", "alpha.common.protocol",
)
_LIBRARIES = ("numba", "numpy", "pandas", "talib")


def _library_versions() -> dict[str, str]:
    out = {}
    for name in _LIBRARIES:
        try:
            out[name] = str(getattr(importlib.import_module(name), "__version__", "unknown"))
        except ImportError:
            out[name] = "absent"
    return out


# --------------------------------------------------------------------------- result types
@dataclass(frozen=True)
class SideMetrics:
    """One partition under one cost scenario."""

    screen: PartitionScreen
    se_r: float | None = None  # day-clustered standard error of the mean R
    ci_lo: float | None = None  # mean -/+ 1.96 * se_r
    ci_hi: float | None = None
    chunk_expectancy: tuple[float | None, ...] = ()  # Train only: 3 equal-calendar chunks
    chunk_trades: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen": dataclasses.asdict(self.screen), "se_r": self.se_r, "ci_lo": self.ci_lo,
            "ci_hi": self.ci_hi, "chunk_expectancy": list(self.chunk_expectancy),
            "chunk_trades": list(self.chunk_trades),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SideMetrics:
        return cls(
            PartitionScreen(**raw["screen"]), raw["se_r"], raw["ci_lo"], raw["ci_hi"],
            tuple(raw["chunk_expectancy"]), tuple(raw["chunk_trades"]),
        )


def _empty_screen(n_trades: int = 0) -> PartitionScreen:
    return PartitionScreen(n_trades, None, None, None, None, None, None, None, 0,
                           None, None, None, None, None, None)


def _empty_side(n_trades: int = 0) -> SideMetrics:
    return SideMetrics(_empty_screen(n_trades))


@dataclass(frozen=True)
class TrainView:
    """Everything ``train_fitness`` may look at.  Contains NO Validation number."""

    genome_hash: str
    complexity: int
    # ``base`` is None on a LEAN evaluation (need_base=False, valid genome): the search fitness
    # reads ``adverse`` only.  Use ``GenomeEvaluator.ensure_full`` to obtain BASE.
    base: SideMetrics | None
    adverse: SideMetrics  # COMBINED_ADVERSE cost

    def to_dict(self) -> dict[str, Any]:
        return {"genome_hash": self.genome_hash, "complexity": self.complexity,
                "base": None if self.base is None else self.base.to_dict(),
                "adverse": self.adverse.to_dict()}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TrainView:
        base = raw["base"]
        return cls(raw["genome_hash"], raw["complexity"],
                   None if base is None else SideMetrics.from_dict(base),
                   SideMetrics.from_dict(raw["adverse"]))


@dataclass(frozen=True)
class ValidationView:
    """Validation partition metrics.  Only explicit gates/diagnostics may read this."""

    base: SideMetrics
    adverse: SideMetrics

    def to_dict(self) -> dict[str, Any]:
        return {"base": self.base.to_dict(), "adverse": self.adverse.to_dict()}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ValidationView:
        return cls(SideMetrics.from_dict(raw["base"]), SideMetrics.from_dict(raw["adverse"]))


@dataclass(frozen=True)
class GenomeEval:
    """JSON-serialisable evaluation record (no per-trade arrays).

    The Validation seal (``_validation`` / ``validation_gate_view``) is a CONVENTION that keeps
    search code from reading Validation numbers by accident; it is NOT a security boundary (the
    data is in the same process and can be reached deliberately).
    """

    genome_hash: str
    lineage: str
    complexity: int
    n_candidates: int
    reject: str | None  # RejectReason value, "invalid_genome" or None
    train: TrainView
    _validation: ValidationView | None  # sealed: use validation_gate_view(); None when lean

    @property
    def rejected(self) -> bool:
        return self.reject is not None

    @property
    def is_full(self) -> bool:
        """False for a lean evaluation (BASE + Validation views not computed)."""
        return self._validation is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "genome_hash": self.genome_hash, "lineage": self.lineage,
            "complexity": self.complexity, "n_candidates": self.n_candidates,
            "reject": self.reject, "train": self.train.to_dict(),
            "validation": None if self._validation is None else self._validation.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GenomeEval:
        val = raw["validation"]
        return cls(
            raw["genome_hash"], raw["lineage"], raw["complexity"], raw["n_candidates"],
            raw["reject"], TrainView.from_dict(raw["train"]),
            None if val is None else ValidationView.from_dict(val),
        )


def validation_gate_view(ev: GenomeEval) -> ValidationView:
    """The ONLY accessor of Validation metrics.  Never call from a search-time decision."""
    if ev._validation is None:
        raise ValueError("lean evaluation carries no Validation view; call "
                         "GenomeEvaluator.ensure_full(ev, genome) first")
    return ev._validation


# --------------------------------------------------------------------------- statistics
def cluster_se(r: np.ndarray, days: np.ndarray) -> float | None:
    """Day-clustered standard error of the mean (CR1 small-cluster correction)."""
    n = len(r)
    if n < 2:
        return None
    _, inverse = np.unique(days, return_inverse=True)
    g = int(inverse.max()) + 1
    if g < 2:
        return None
    sums = np.bincount(inverse, weights=r, minlength=g)
    counts = np.bincount(inverse, minlength=g)
    resid = sums - counts * float(r.mean())
    var = float((resid**2).sum()) * g / (g - 1) / (n * n)
    return math.sqrt(var)


def _side(
    trades: TradeArrays, mask: np.ndarray, screen: PartitionScreen,
    chunk_ids: np.ndarray | None,
) -> SideMetrics:
    r = trades.r_multiple[mask]
    se = cluster_se(r, trades.entry_day[mask]) if len(r) else None
    mean = screen.expectancy_r
    lo = hi = None
    if se is not None and mean is not None:
        lo, hi = mean - Z95 * se, mean + Z95 * se
    chunk_e: tuple[float | None, ...] = ()
    chunk_n: tuple[int, ...] = ()
    if chunk_ids is not None:
        rows = []
        for k in range(N_CHUNKS):
            sel = chunk_ids[mask] == k
            rows.append((float(r[sel].mean()) if sel.any() else None, int(sel.sum())))
        chunk_e = tuple(e for e, _ in rows)
        chunk_n = tuple(n for _, n in rows)
    return SideMetrics(screen, se, lo, hi, chunk_e, chunk_n)


# --------------------------------------------------------------------------- evaluator
def _sha_arrays(features: Any, names: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for name in names:
        value = np.ascontiguousarray(features[name])
        digest.update(name.encode())
        digest.update(value.dtype.str.encode())
        digest.update(value.tobytes())
    return digest.hexdigest()


def _source_hashes() -> dict[str, str]:
    out = {}
    for name in _SOURCE_MODULES:
        path = Path(importlib.import_module(name).__file__)
        out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    kernels = Path(importlib.import_module("alpha.fast.kernels").__file__).parent
    for path in sorted(kernels.glob("*.py")):
        out[f"kernels/{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _flush_shard(root: Path, buf: list[str]) -> None:
    """Append-only shard of ``key<TAB>json`` lines (one file per flush, later shards win)."""
    if not buf or not root.is_dir():
        return
    name = f"shard_{time.time_ns():020d}_{os.getpid()}.jsonl"
    tmp = root / (name + ".tmp")
    tmp.write_text("\n".join(buf) + "\n", encoding="utf-8")
    tmp.replace(root / name)
    buf.clear()


_SHARD_FLUSH_EVERY = 2000
_RULE_MASK_ENTRIES = 768


class GenomeEvaluator:
    """Deterministic evaluator with a shared trial ledger and an on-disk result cache."""

    def __init__(
        self, store: Any, market: Any, dates: np.ndarray, split: SplitPlan, cfg: dict,
        cache_dir: Path | str, ledger: TrialLedger | None = None,
        min_train_trades: int | None = None,
        max_cache_mb: float = 500.0,
    ) -> None:
        self.store, self.market, self.split, self.cfg = store, market, split, cfg
        self.dates = np.asarray(dates).astype("datetime64[D]")
        if len(self.dates) != len(market.o):
            raise ValueError("dates must contain one Berlin date per market bar")
        self.ledger = ledger if ledger is not None else TrialLedger()
        self.sizing = SizingSpec(**cfg["sizing"])
        self.rules = SimRules(**cfg["rules"])
        # Train minimum (Stage A + fitness); part of the cache fingerprint below
        self.min_trades = int(min_train_trades if min_train_trades is not None else
                              cfg.get("sample_rules", {}).get("min_trades_flag", MIN_TRAIN_TRADES))
        self.cache_root = Path(cache_dir) / "genome_evals"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.max_cache_bytes = max(0, int(max_cache_mb * 1024 * 1024))
        prune_oldest_shards(self.cache_root, self.max_cache_bytes)
        self._memory: dict[str, dict] = {}
        self._shard_buf: list[str] = []
        self._load_shards()
        # flushed on garbage collection / interpreter exit; runners also call flush() explicitly
        self._finalizer = weakref.finalize(self, _flush_shard, self.cache_root, self._shard_buf)
        self.sim_count = 0
        self.evaluations = 0
        self._rule_masks = RuleMaskCache(store, _RULE_MASK_ENTRIES)

        train_mask = split.mask(self.dates, split.train)
        # Partition masks over bars + Train/Validation day counts are fixed: precompute once
        self._tm_bar = train_mask
        self._vm_bar = split.mask(self.dates, split.validation)
        self._n_days_train = len(np.unique(market.day[self._tm_bar]))
        self._n_days_val = len(np.unique(market.day[self._vm_bar]))
        self.resolver = ThresholdResolver(store, train_mask)
        # chunk id (0..N-1) per bar over equal-calendar thirds of the TRAIN date range
        start = np.datetime64(split.train.start)
        total = int((np.datetime64(split.train.end) - start).astype(int)) + 1
        offset = (self.dates - start).astype(int)
        self._chunk_of_bar = np.clip(offset * N_CHUNKS // total, 0, N_CHUNKS - 1)
        self._costs = {name: COST_SCENARIOS[name] for name in (BASE_COST, ADVERSE_COST)}
        self._fp_static = stable_hash({
            "evaluator": EVALUATOR_VERSION,
            "spec_version": SPEC_VERSION,
            "dataset_hash": _sha_arrays(store, ("ts_ns", "o", "h", "l", "c", "spread")),
            "feature_key": getattr(store, "metadata", {}).get("cache_key"),
            "resolver": {"kind": "train_quantiles",
                         "train_mask_sha": hashlib.sha256(np.packbits(train_mask).tobytes()
                                                          ).hexdigest()},
            "costs": {k: dataclasses.asdict(v) for k, v in self._costs.items()},
            "split": split.to_dict(),  # includes embargo_days
            "sizing": cfg["sizing"], "rules": cfg["rules"], "min_trades": self.min_trades,
            "n_chunks": N_CHUNKS,
            "sources": _source_hashes(),
            "libraries": _library_versions(),
        })

    # ------------------------------------------------------------------ cache
    def fingerprint(self, genome_hash: str) -> str:
        return stable_hash({"static": self._fp_static, "genome": genome_hash})

    def _load_shards(self) -> None:
        """Load shard files (sorted by name = time order; a complete entry is never replaced
        by a lean one).  Truncated / corrupt lines are skipped, i.e. treated as cache misses."""
        for path in sorted(self.cache_root.glob("shard_*.jsonl")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                key, _, body = line.partition("\t")
                try:
                    raw = json.loads(body)
                except ValueError:
                    continue
                cur = self._memory.get(key)
                if cur is not None and cur.get("full", True) and not raw.get("full", True):
                    continue
                self._memory[key] = raw

    def _cache_get(self, key: str) -> dict | None:
        if key in self._memory:
            return self._memory[key]
        path = self.cache_root / f"{key}.json"  # legacy one-file-per-result entries
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            self._memory[key] = raw
            return raw
        return None

    def _cache_put(self, key: str, raw: dict) -> None:
        self._memory[key] = raw
        self._shard_buf.append(f"{key}\t" + json.dumps(raw, sort_keys=True, allow_nan=False))
        if len(self._shard_buf) >= _SHARD_FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        """Persist buffered results (call at phase end; also runs on GC / exit)."""
        _flush_shard(self.cache_root, self._shard_buf)
        prune_oldest_shards(self.cache_root, self.max_cache_bytes)

    def cleanup(self) -> None:
        self.flush()

    def __enter__(self) -> GenomeEvaluator:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, genome: Genome, kind: str = "structural",
                 need_base: bool = True) -> GenomeEval:
        """Evaluate one genome; ``kind`` ('structural' | 'param') is recorded on the ledger.

        ``need_base=False`` (search drivers) skips the BASE-cost simulation and the Validation
        views: the returned ``GenomeEval`` is LEAN (``train.base is None``, no Validation view)
        but every number ``train_fitness`` reads is bit-identical.  The default returns a full
        evaluation, upgrading a lean cache entry when necessary.
        """
        self.evaluations += 1
        status = self.ledger.record(genome, kind)  # validates ``kind``
        if status == "invalid":
            digest = hashlib.sha256(genome.to_json().encode()).hexdigest()
            empty = _empty_side()
            return GenomeEval(digest, genome.lineage, 0, 0, "invalid_genome",
                              TrainView(digest, 0, empty, empty), ValidationView(empty, empty))
        canon = canonicalize(genome)
        ghash = canonical_hash(canon)
        # behaviour-level uniqueness (identical resolved spec), reported by the ledger
        spec = None
        with contextlib.suppress(GenomeError, ValueError):
            spec = compile_genome(canon, self.resolver, snap=False)
            self.ledger.behaviors.add(behavior_key(spec))
        key = self.fingerprint(ghash)
        raw = self._cache_get(key)
        if raw is not None:
            self.ledger.cache_hits += 1
        if raw is None or (need_base and not raw.get("full", True)):
            raw = self._compute_raw(canon, ghash, spec, need_base)
            self._cache_put(key, raw)
        return dataclasses.replace(GenomeEval.from_dict(raw), lineage=genome.lineage)

    def ensure_full(self, ev: GenomeEval, genome: Genome) -> GenomeEval:
        """Upgrade a lean evaluation of ``genome`` to a full one (no ledger side effects)."""
        if ev.is_full:
            return ev
        canon = canonicalize(genome)
        ghash = canonical_hash(canon)
        if ghash != ev.genome_hash:
            raise ValueError("ensure_full: genome does not match the evaluation")
        key = self.fingerprint(ghash)
        raw = self._cache_get(key)
        if raw is None or not raw.get("full", True):
            raw = self._compute_raw(canon, ghash, None, True)
            self._cache_put(key, raw)
        return dataclasses.replace(GenomeEval.from_dict(raw), lineage=ev.lineage)

    def _compute_raw(self, canon: Genome, ghash: str, spec: Any, need_full: bool) -> dict:
        result = self._compute(canon, ghash, spec, need_full)
        raw = result.to_dict()
        raw["full"] = result.is_full  # cache-only completeness flag (not part of GenomeEval)
        return raw

    def _reduce(self, trades: TradeArrays, want_val: bool
                ) -> tuple[SideMetrics, SideMetrics | None]:
        """Train (+ optionally Validation) metrics from precomputed bar-level partition masks."""
        contract = self.sizing.contract_size
        if len(trades):
            t_mask = self._tm_bar[trades.entry_idx]
            chunks = self._chunk_of_bar[trades.entry_idx]
        else:
            t_mask = np.zeros(0, bool)
            chunks = np.zeros(0, int)
        train = screen_partition_trades(trades, t_mask, self._n_days_train,
                                        contract_size=contract)
        train_side = _side(trades, t_mask, train, chunks)
        if not want_val:
            return train_side, None
        v_mask = self._vm_bar[trades.entry_idx] if len(trades) else np.zeros(0, bool)
        val = screen_partition_trades(trades, v_mask, self._n_days_val, contract_size=contract)
        return train_side, _side(trades, v_mask, val, None)

    def _compute(self, canon: Genome, ghash: str, spec: Any = None,
                 need_full: bool = True) -> GenomeEval:
        if spec is None:
            spec = compile_genome(canon, self.resolver, snap=False)
        cands = evaluate_spec(self.store, spec, self._rule_masks)
        n_cand = len(cands.decision_idx)
        cx = complexity(canon)
        empty = _empty_side()
        if n_cand:
            tm = self._tm_bar[cands.decision_idx]
            n_train_cand = int(tm.sum())
        else:
            n_train_cand = 0

        def rejected(reason: str, n: int) -> GenomeEval:
            side = _empty_side(n)
            return GenomeEval(ghash, canon.lineage, cx, n_cand, reason,
                              TrainView(ghash, cx, side, side), ValidationView(empty, empty))

        # Stage A judges TRAIN candidates only (Validation decisions must not count towards
        # the Train sample or the impossible-spec check)
        if n_cand:
            train_cands = CandidateArrays(
                cands.decision_idx[tm], cands.direction[tm], cands.stop[tm], cands.target[tm],
                cands.target_r[tm], cands.exit_kind[tm])
        else:
            train_cands = cands
        reason = reject_reason(spec, train_cands, min_trades=self.min_trades, market=self.market,
                               sizing=self.sizing, cost=self._costs[ADVERSE_COST])
        if reason is not None:
            n = n_train_cand if reason is RejectReason.TOO_FEW_TRADES else 0
            return rejected(reason.value, n)

        sides: dict[str, tuple[SideMetrics, SideMetrics | None]] = {}
        for name in (ADVERSE_COST, BASE_COST) if need_full else (ADVERSE_COST,):
            trades = simulate_fast(self.market, cands, self._costs[name], self.sizing, self.rules)
            self.sim_count += 1
            sides[name] = self._reduce(trades, want_val=need_full)
            if name == ADVERSE_COST and sides[name][0].screen.n_trades < self.min_trades:
                side = sides[name][0]
                return GenomeEval(ghash, canon.lineage, cx, n_cand,
                                  RejectReason.TOO_FEW_TRADES.value,
                                  TrainView(ghash, cx, side, side), ValidationView(empty, empty))
        if not need_full:  # lean: the fitness reads only the COMBINED_ADVERSE Train side
            return GenomeEval(ghash, canon.lineage, cx, n_cand, None,
                              TrainView(ghash, cx, None, sides[ADVERSE_COST][0]), None)
        return GenomeEval(
            ghash, canon.lineage, cx, n_cand, None,
            TrainView(ghash, cx, sides[BASE_COST][0], sides[ADVERSE_COST][0]),
            ValidationView(sides[BASE_COST][1], sides[ADVERSE_COST][1]),
        )


__all__ = (
    "ADVERSE_COST", "BASE_COST", "EVALUATOR_VERSION", "GenomeEval", "GenomeEvaluator",
    "SideMetrics", "TrainView", "ValidationView", "cluster_se",
    "validation_gate_view",
)
