# ruff: noqa: E501
"""Temporal genome evaluator: canonicalize -> cache -> compile -> evaluate_temporal -> simulate ONCE per cost -> reduce.

Research only.  Mirrors ``alpha.discovery.evaluate.GenomeEvaluator`` for the V2 engine and reuses its
sealed view types (``TrainView``, ``ValidationView``, ``SideMetrics``) so ``fitness.train_fitness`` and the
Validation seal work unchanged:

* ``TemporalEval.train`` is a ``TrainView`` -- the only thing ``train_fitness`` accepts.  Validation numbers
  are computed and stored but live in a private field reachable only through
  ``temporal_validation_gate_view``; no search code imports or calls it.
* The MarketFrame comes from an injected ``frame_provider`` (lazy, called once) so the evaluator runs on
  synthetic frames now and on the real EventSet frame adapter later.  ``frame.thresholds`` is the
  threshold resolver (TRAIN quantiles are the frame builder's responsibility).
* ``TemporalTrialLedger`` generalises the V1 ledger through callables (validate / hash), adding
  cumulative counts, unique canonical hashes, structural vs param trials, cache hits, unique
  behaviours and behavioural twins (equal TRAIN decision streams).
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from alpha.common.protocol import SplitPlan, stable_hash
from alpha.common.sim import (
    COST_SCENARIOS,
    DEFAULT_RULES,
    DEFAULT_SIZING,
    CostScenario,
    SimRules,
    SizingSpec,
)
from alpha.discovery import temporal_compile, temporal_genome
from alpha.discovery.compile import TrialLedger
from alpha.discovery.disk import prune_oldest_shards
from alpha.discovery.evaluate import (
    ADVERSE_COST,
    BASE_COST,
    MIN_TRAIN_TRADES,
    N_CHUNKS,
    SideMetrics,
    TrainView,
    ValidationView,
    _empty_side,
    _flush_shard,
    _library_versions,
    _side,
)
from alpha.discovery.temporal_genome import (
    GenomeError,
    TemporalGenome,
    canonical_hash,
    canonicalize,
    complexity,
)
from alpha.events import schema as ev_schema
from alpha.fast import store as feature_store
from alpha.fast.screen import RejectReason, reject_reason, screen_partition_trades
from alpha.fast.sim import CandidateArrays, MarketArrays, SimWindow, TradeArrays, simulate_fast
from alpha.temporal.batch import PrefixCache
from alpha.temporal.evaluate import evaluate_temporal_many
from alpha.temporal.reference import MarketFrame

EVALUATOR_VERSION = "td1-temporal-eval-v1"
_SHARD_FLUSH_EVERY = 2000
_SOURCE_PACKAGES = ("alpha.events", "alpha.temporal")
_SOURCE_MODULES = (
    "alpha.fast.sim", "alpha.fast.screen", "alpha.fast.store", "alpha.common.sim", "alpha.common.protocol",
    "alpha.discovery.evaluate", "alpha.discovery.fitness", "alpha.discovery.temporal_genome",
    "alpha.discovery.temporal_compile", "alpha.discovery.temporal_evaluate",
)


# --------------------------------------------------------------------------- ledger
@dataclass
class TemporalTrialLedger(TrialLedger):
    """V1 ledger generalised through callables; adds behavioural-twin accounting.

    ``validate_fn(genome)`` raises ``GenomeError``/``ValueError`` for an invalid genome;
    ``hash_fn(genome)`` is the canonical hash.  Defaults are the temporal ones.
    """

    twins: dict[str, set[str]] = field(default_factory=dict)  # TRAIN decision-stream hash -> canonical hashes
    validate_fn: Callable[[Any], None] | None = field(default=None, repr=False, compare=False)
    hash_fn: Callable[[Any], str] | None = field(default=None, repr=False, compare=False)

    def record(self, genome: Any, kind: str = "structural") -> str:
        if kind not in ("param", "structural"):
            raise ValueError("kind must be 'param' or 'structural'")
        self.total_trials += 1
        if kind == "param":
            self.param_trials += 1
        else:
            self.structural_trials += 1
        try:
            (self.validate_fn or temporal_genome.validate)(genome)
        except (GenomeError, ValueError):
            self.invalid_rejects += 1
            return "invalid"
        digest = (self.hash_fn or canonical_hash)(genome)
        if digest in self.seen:
            self.duplicate_rejects += 1
            return "duplicate"
        self.seen.add(digest)
        return "new"

    def note_twin(self, twin_hash: str, chash: str) -> None:
        if twin_hash:
            self.twins.setdefault(twin_hash, set()).add(chash)

    @property
    def unique_twin_streams(self) -> int:
        return len(self.twins)

    @property
    def behavioral_twins(self) -> int:
        """Canonical genomes beyond the first that share an identical TRAIN decision stream."""
        return sum(len(s) - 1 for s in self.twins.values())

    def to_json(self) -> str:
        raw = json.loads(TrialLedger.to_json(self))
        raw["twins"] = {k: sorted(v) for k, v in sorted(self.twins.items())}
        raw["behavioral_twins"] = self.behavioral_twins
        return json.dumps(raw, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> TemporalTrialLedger:
        raw = json.loads(text)
        base = TrialLedger.from_json(text)
        led = cls(**{f.name: getattr(base, f.name) for f in dataclasses.fields(TrialLedger)})
        led.twins = {k: set(v) for k, v in raw.get("twins", {}).items()}
        return led


# --------------------------------------------------------------------------- result record
@dataclass(frozen=True)
class TemporalEval:
    """JSON-serialisable evaluation record; Validation numbers are sealed (see module docstring)."""

    genome_hash: str
    lineage: str
    complexity: int
    n_candidates: int
    reject: str | None
    train: TrainView
    _validation: ValidationView | None
    twin_hash: str = ""  # hash of the TRAIN decision_idx/direction stream ('' if none)
    behavior_key: str = ""
    trades_per_day: float | None = None  # Train, COMBINED_ADVERSE
    n_train_candidates: int = 0
    # Optional diagnostics of a FRESH computation only (None on cache hits / from_dict): never part of
    # to_dict / hashes / fitness. skip_counts: simulate_fast skip labels of the ADVERSE-cost sim
    # (Train + Validation bars, i.e. the whole simulated stream; None if no sim ran);
    # train_candidates: the Train-side CandidateArrays.
    skip_counts: dict[str, int] | None = field(default=None, compare=False, repr=False)
    train_candidates: Any = field(default=None, compare=False, repr=False)

    @property
    def rejected(self) -> bool:
        return self.reject is not None

    @property
    def is_full(self) -> bool:
        return self._validation is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "genome_hash": self.genome_hash, "lineage": self.lineage, "complexity": self.complexity,
            "n_candidates": self.n_candidates, "reject": self.reject, "train": self.train.to_dict(),
            "validation": None if self._validation is None else self._validation.to_dict(),
            "twin_hash": self.twin_hash, "behavior_key": self.behavior_key,
            "trades_per_day": self.trades_per_day, "n_train_candidates": self.n_train_candidates,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TemporalEval:
        val = raw["validation"]
        return cls(
            raw["genome_hash"], raw["lineage"], raw["complexity"], raw["n_candidates"], raw["reject"],
            TrainView.from_dict(raw["train"]), None if val is None else ValidationView.from_dict(val),
            raw.get("twin_hash", ""), raw.get("behavior_key", ""), raw.get("trades_per_day"),
            raw.get("n_train_candidates", 0),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, allow_nan=False)


def temporal_validation_gate_view(e: TemporalEval) -> ValidationView:
    """The ONLY accessor of Validation metrics.  Never call from a search-time decision."""
    if e._validation is None:
        raise ValueError("lean evaluation carries no Validation view; call ensure_full first")
    return e._validation


def twin_hash_of(candidates: CandidateArrays) -> str:
    """Behavioural identity of a candidate stream (decision bars + directions)."""
    if len(candidates.decision_idx) == 0:
        return ""
    h = hashlib.sha256(b"temporal-twin-v1:")
    h.update(np.ascontiguousarray(candidates.decision_idx, dtype=np.int64).tobytes())
    h.update(np.ascontiguousarray(candidates.direction, dtype=np.int8).tobytes())
    return h.hexdigest()


def attach_min_space(cands: CandidateArrays, spec: Any) -> CandidateArrays:
    """Carry ``spec.target.min_space_r`` into the sim path (per candidate, finite structural targets only).

    The temporal kernel checks min_space_r against the decision close; the simulator fills at
    o[i+1] + spread, so it re-checks the same requirement against the actual fill (skip label
    ``space_below_min_at_fill``).  No-op (candidates returned unchanged) for fixed_r targets or a zero
    requirement, keeping those streams bit-identical."""
    rule = spec.target
    req = float(getattr(rule, "min_space_r", 0.0))
    if rule.kind != "next_structure" or not req > 0.0 or len(cands.decision_idx) == 0:
        return cands
    ms = np.where(np.isfinite(cands.target), req, np.nan)
    return dataclasses.replace(cands, min_space_r=ms)


def market_from_frame(frame: MarketFrame, bars_per_day: int = 288, spread: float = 0.0) -> MarketArrays:
    """Simple MarketArrays for SYNTHETIC frames (tests): day = index // bars_per_day, contiguity from run_start."""
    n = len(frame)
    idx = np.arange(n)
    contig = np.zeros(n, dtype=bool)
    contig[:-1] = frame.run_start[1:] == frame.run_start[:-1]
    return MarketArrays(frame.o, frame.h, frame.l, frame.c, np.full(n, spread), frame.berlin_minute.astype(np.int64),
                        (idx // bars_per_day).astype(np.int64), contig)


def _source_hashes() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in _SOURCE_MODULES:
        out[name] = hashlib.sha256(Path(importlib.import_module(name).__file__).read_bytes()).hexdigest()
    for pkg in _SOURCE_PACKAGES:
        root = Path(importlib.import_module(pkg).__file__).parent
        for path in sorted(root.glob("*.py")):
            out[f"{pkg}/{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _frame_fingerprint(frame: MarketFrame) -> str:
    h = hashlib.sha256()
    for name in ("o", "h", "l", "c", "atr", "run_start", "berlin_minute"):
        a = np.ascontiguousarray(getattr(frame, name))
        h.update(name.encode() + a.dtype.str.encode() + a.tobytes())
    return h.hexdigest()


# --------------------------------------------------------------------------- evaluator
class TemporalEvaluator:
    """Deterministic evaluator with a shared trial ledger, prefix cache and an optional on-disk result cache."""

    def __init__(
        self, frame_provider: Callable[[], MarketFrame], market: MarketArrays, dates: np.ndarray,
        split: SplitPlan, *, sizing: SizingSpec = DEFAULT_SIZING, rules: SimRules = DEFAULT_RULES,
        cost_scenarios: Mapping[str, CostScenario] | None = None,
        min_train_trades: int = MIN_TRAIN_TRADES, ledger: TemporalTrialLedger | None = None,
        cache_dir: Path | str | None = None, data_fingerprint: str | None = None,
        max_cache_mb: float = 500.0, prefix_cache_mb: float = 256.0,
        window: SimWindow | None = None, events_cache_key: str | None = None,
        features_cache_key: str | None = None,
    ) -> None:
        # Provenance of the frame's event/feature arrays.  The frame itself carries no cache key, so the
        # caller passes the EventSet cache key (``events.metadata['cache_key']`` /
        # ``alpha.events.store.cache_key(features, params)``) and the FeatureSet one.  With an on-disk
        # result cache the event key is MANDATORY (fail closed): OHLC alone cannot identify the event
        # arrays a cached result was computed from.  In-memory-only evaluators (synthetic tests) may
        # rely on ``data_fingerprint`` / the frame OHLC fingerprint instead.
        if cache_dir is not None and not events_cache_key:
            raise ValueError("TemporalEvaluator with an on-disk cache requires events_cache_key "
                             "(the EventSet cache key); refusing to key results by OHLC alone")
        self.events_cache_key, self.features_cache_key = events_cache_key, features_cache_key
        self.frame_provider = frame_provider
        self._frame: MarketFrame | None = None
        self.market, self.split, self.sizing, self.rules = market, split, sizing, rules
        # None = V1 GER40 constants (bit-identical); else the market's local-minute entry/flat window.
        self.window = window
        self._fresh: tuple[dict[str, int] | None, Any] | None = None
        self.dates = np.asarray(dates).astype("datetime64[D]")
        if len(self.dates) != len(market.o):
            raise ValueError("dates must contain one date per market bar")
        self.ledger = ledger if ledger is not None else TemporalTrialLedger()
        self.min_trades = int(min_train_trades)
        scen = dict(cost_scenarios) if cost_scenarios is not None else {n: COST_SCENARIOS[n] for n in (BASE_COST, ADVERSE_COST)}
        self._costs = {n: scen[n] for n in (BASE_COST, ADVERSE_COST)}
        self._data_fp = data_fingerprint
        self._fp_static: str | None = None
        self._memory: dict[str, dict] = {}
        self._shard_buf: list[str] = []
        self.cache_root: Path | None = None
        self.max_cache_bytes = max(0, int(max_cache_mb * 1024 * 1024))
        if cache_dir is not None:
            self.cache_root = Path(cache_dir) / "temporal_evals"
            self.cache_root.mkdir(parents=True, exist_ok=True)
            prune_oldest_shards(self.cache_root, self.max_cache_bytes)
            self._load_shards()
        self._prefix_cache = PrefixCache(int(prefix_cache_mb * 1024 * 1024))
        self.sim_count = 0
        self.evaluations = 0
        self._tm_bar = split.mask(self.dates, split.train)
        self._vm_bar = split.mask(self.dates, split.validation)
        self._n_days_train = len(np.unique(market.day[self._tm_bar]))
        self._n_days_val = len(np.unique(market.day[self._vm_bar]))
        start = np.datetime64(split.train.start)
        total = int((np.datetime64(split.train.end) - start).astype(int)) + 1
        offset = (self.dates - start).astype(int)
        self._chunk_of_bar = np.clip(offset * N_CHUNKS // total, 0, N_CHUNKS - 1)

    # ------------------------------------------------------------------ frame / fingerprint
    @property
    def frame(self) -> MarketFrame:
        if self._frame is None:
            self._frame = self.frame_provider()
        return self._frame

    def resolver(self, name: str, q: float) -> float:
        return float(self.frame.thresholds[(name, q)])

    def _fp(self) -> str:
        if self._fp_static is None:
            self._fp_static = stable_hash({
                "evaluator": EVALUATOR_VERSION, "events": ev_schema.EVENT_SET_VERSION,
                "registry": ev_schema.registry_fingerprint(),
                "data": self._data_fp or _frame_fingerprint(self.frame),
                "events_cache_key": self.events_cache_key, "features_cache_key": self.features_cache_key,
                "feature_set_version": feature_store.FEATURE_SET_VERSION,
                "market": hashlib.sha256(b"".join(np.ascontiguousarray(a).tobytes() for a in (
                    self.market.o, self.market.h, self.market.l, self.market.c, self.market.spread,
                    self.market.day, self.market.contig_next))).hexdigest(),
                "train_mask_sha": hashlib.sha256(np.packbits(self._tm_bar).tobytes()).hexdigest(),
                "costs": {k: dataclasses.asdict(v) for k, v in self._costs.items()},
                "split": self.split.to_dict(), "sizing": dataclasses.asdict(self.sizing),
                "rules": dataclasses.asdict(self.rules), "min_trades": self.min_trades,
                "n_chunks": N_CHUNKS, "sources": _source_hashes(), "libraries": _library_versions(),
                **({} if self.window is None else {"window": dataclasses.asdict(self.window)}),
            })
        return self._fp_static

    def fingerprint(self, genome_hash: str, behavior_key: str = "") -> str:
        """Per-genome result key = static provenance + canonical hash + behaviour key (the latter folds the
        thresholds the frame actually resolved for this genome's feature clauses)."""
        return stable_hash({"static": self._fp(), "genome": genome_hash, "behavior": behavior_key})

    # ------------------------------------------------------------------ cache
    def _load_shards(self) -> None:
        assert self.cache_root is not None
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

    def _cache_put(self, key: str, raw: dict) -> None:
        self._memory[key] = raw
        if self.cache_root is not None:
            self._shard_buf.append(f"{key}\t" + json.dumps(raw, sort_keys=True, allow_nan=False))
            if len(self._shard_buf) >= _SHARD_FLUSH_EVERY:
                self.flush()

    def flush(self) -> None:
        if self.cache_root is not None:
            _flush_shard(self.cache_root, self._shard_buf)
            prune_oldest_shards(self.cache_root, self.max_cache_bytes)

    def __enter__(self) -> TemporalEvaluator:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.flush()

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, genome: TemporalGenome, kind: str = "structural", need_base: bool = True) -> TemporalEval:
        """Evaluate one genome; ``need_base=False`` (search) yields a LEAN record (no BASE/Validation)."""
        self.evaluations += 1
        status = self.ledger.record(genome, kind)  # validates ``kind``
        if status == "invalid":
            return self._invalid(genome)
        canon = canonicalize(genome)
        ghash = canonical_hash(canon)
        try:
            spec = temporal_compile.compile_temporal(canon, self.resolver, canonical=True)
        except GenomeError:
            self.ledger.invalid_rejects += 1
            return self._invalid(genome)
        bkey = temporal_compile.behavior_key(spec)
        self.ledger.behaviors.add(bkey)
        key = self.fingerprint(ghash, bkey)
        raw = self._memory.get(key)
        if raw is not None:
            self.ledger.cache_hits += 1
        fresh = None
        if raw is None or (need_base and not raw.get("full", True)):
            raw = self._compute_raw(canon, ghash, spec, bkey, need_base)
            fresh, self._fresh = self._fresh, None
            self._cache_put(key, raw)
        out = dataclasses.replace(TemporalEval.from_dict(raw), lineage=genome.lineage)
        if fresh is not None:
            out = dataclasses.replace(out, skip_counts=fresh[0], train_candidates=fresh[1])
        self.ledger.note_twin(out.twin_hash, out.genome_hash)
        return out

    def ensure_full(self, e: TemporalEval, genome: TemporalGenome) -> TemporalEval:
        """Upgrade a lean evaluation to a full one (no ledger side effects)."""
        if e.is_full:
            return e
        canon = canonicalize(genome)
        ghash = canonical_hash(canon)
        if ghash != e.genome_hash:
            raise ValueError("ensure_full: genome does not match the evaluation")
        spec = temporal_compile.compile_temporal(canon, self.resolver, canonical=True)
        bkey = temporal_compile.behavior_key(spec)
        key = self.fingerprint(ghash, bkey)
        raw = self._memory.get(key)
        if raw is None or not raw.get("full", True):
            raw = self._compute_raw(canon, ghash, spec, bkey, True)
            self._fresh = None  # diagnostics are only surfaced by evaluate()
            self._cache_put(key, raw)
        return dataclasses.replace(TemporalEval.from_dict(raw), lineage=e.lineage)

    def _invalid(self, genome: TemporalGenome) -> TemporalEval:
        digest = hashlib.sha256(genome.to_json().encode()).hexdigest()
        empty = _empty_side()
        return TemporalEval(digest, genome.lineage, 0, 0, "invalid_genome",
                            TrainView(digest, 0, empty, empty), ValidationView(empty, empty))

    def _compute_raw(self, canon, ghash, spec, bkey, need_full) -> dict:
        result = self._compute(canon, ghash, spec, bkey, need_full)
        self._fresh = (result.skip_counts, result.train_candidates)
        raw = result.to_dict()
        raw["full"] = result.is_full
        return raw

    def _reduce(self, trades: TradeArrays, want_val: bool) -> tuple[SideMetrics, SideMetrics | None]:
        contract = self.sizing.contract_size
        if len(trades):
            t_mask = self._tm_bar[trades.entry_idx]
            chunks = self._chunk_of_bar[trades.entry_idx]
        else:
            t_mask, chunks = np.zeros(0, bool), np.zeros(0, int)
        train = screen_partition_trades(trades, t_mask, self._n_days_train, contract_size=contract)
        train_side = _side(trades, t_mask, train, chunks)
        if not want_val:
            return train_side, None
        v_mask = self._vm_bar[trades.entry_idx] if len(trades) else np.zeros(0, bool)
        val = screen_partition_trades(trades, v_mask, self._n_days_val, contract_size=contract)
        return train_side, _side(trades, v_mask, val, None)

    def _compute(self, canon: TemporalGenome, ghash: str, spec: Any, bkey: str, need_full: bool) -> TemporalEval:
        cands = evaluate_temporal_many([spec], self.frame, use_cache=True, cache=self._prefix_cache)[0].candidates
        cands = attach_min_space(cands, spec)
        n_cand = len(cands.decision_idx)
        cx = complexity(canon)
        empty = _empty_side()
        if n_cand:
            tm = self._tm_bar[cands.decision_idx]
            train_cands = cands.subset(tm)
        else:
            tm, train_cands = np.zeros(0, bool), cands
        n_train_cand = len(train_cands.decision_idx)
        twin = twin_hash_of(train_cands)

        def rejected(reason: str, n: int) -> TemporalEval:
            side = _empty_side(n)
            return TemporalEval(ghash, canon.lineage, cx, n_cand, reason, TrainView(ghash, cx, side, side),
                                ValidationView(empty, empty), twin, bkey, None, n_train_cand,
                                train_candidates=train_cands)

        reason = reject_reason(spec, train_cands, min_trades=self.min_trades, market=self.market,
                               sizing=self.sizing, cost=self._costs[ADVERSE_COST])
        if reason is not None:
            return rejected(reason.value, n_train_cand if reason is RejectReason.TOO_FEW_TRADES else 0)

        sides: dict[str, tuple[SideMetrics, SideMetrics | None]] = {}
        skips: dict[str, int] | None = None
        for name in (ADVERSE_COST, BASE_COST) if need_full else (ADVERSE_COST,):
            trades = simulate_fast(self.market, cands, self._costs[name], self.sizing, self.rules, self.window)
            self.sim_count += 1
            if name == ADVERSE_COST:
                skips = trades.skips
            sides[name] = self._reduce(trades, want_val=need_full)
            if name == ADVERSE_COST and sides[name][0].screen.n_trades < self.min_trades:
                side = sides[name][0]
                return TemporalEval(ghash, canon.lineage, cx, n_cand, RejectReason.TOO_FEW_TRADES.value,
                                    TrainView(ghash, cx, side, side), ValidationView(empty, empty), twin,
                                    bkey, None, n_train_cand, skip_counts=skips, train_candidates=train_cands)
        tpd = sides[ADVERSE_COST][0].screen.trades_per_day
        if not need_full:
            return TemporalEval(ghash, canon.lineage, cx, n_cand, None,
                                TrainView(ghash, cx, None, sides[ADVERSE_COST][0]), None, twin, bkey, tpd,
                                n_train_cand, skip_counts=skips, train_candidates=train_cands)
        return TemporalEval(
            ghash, canon.lineage, cx, n_cand, None,
            TrainView(ghash, cx, sides[BASE_COST][0], sides[ADVERSE_COST][0]),
            ValidationView(sides[BASE_COST][1], sides[ADVERSE_COST][1]), twin, bkey, tpd, n_train_cand,
            skip_counts=skips, train_candidates=train_cands,
        )


__all__ = (
    "EVALUATOR_VERSION", "TemporalEval", "TemporalEvaluator", "TemporalTrialLedger",
    "attach_min_space", "market_from_frame", "temporal_validation_gate_view", "twin_hash_of",
)
