# ruff: noqa: E501
"""Coverage auditor (OFFLINE, READ-ONLY): how complete are the persisted research facts of a DemoStore?

Nothing is tracked or written here.  Everything is derived from facts the demo store already persists, with the
existing semantics imported (not copied): ``DemoStore`` read API, ``demo.labeling`` (``horizon_end_utc``,
``blocking_gate``, ``label_one``, the labeller's own grace / min-age defaults), ``demo.funnel`` (gate classes,
``TRADED_STATES``).  The store is opened with SQLite ``mode=ro`` + ``PRAGMA query_only`` - a write attempt fails.

OPPORTUNITY CLASSIFICATION (mutually exclusive, one per recorded snapshot)
  TRADED                  intent in FILLED / PROTECTED / CLOSED (``demo.funnel.TRADED_STATES``)
  NON_TRADED_HORIZON_OPEN not traded, and the counterfactual horizon has NOT elapsed (cause PENDING_HORIZON), or the
                          decision was accepted but no intent exists yet and it is younger than the labeller's
                          ``ACCEPTED_NO_INTENT_MIN_AGE_S`` (cause PENDING_INTENT_WINDOW).  NOT missing, NOT in the
                          eligible denominator.
  COUNTERFACTUAL_COMPLETE a counterfactual label exists.
  EXPECTED_PENDING        eligible, unlabelled, with an expected/benign cause: WAITING_FOR_BARS (the bar pointer of the
                          market has not reached the horizon end, inside the labeller grace), INCOMPLETE_BAR_COVERAGE
                          (bars observed past the horizon, inside the labeller grace: labeller lag or bar gaps).
  UNEXPECTED_MISSING      eligible, unlabelled, NOT expected: BAR_PROVIDER_ERROR (a scan error in the horizon window),
                          INVALID_GEOMETRY (the label could never be computed), LABEL_ERROR (only with a diagnostic
                          bars provider), WAITING_FOR_BARS / INCOMPLETE_BAR_COVERAGE beyond the grace period, and
                          UNKNOWN (no explanation).  UNKNOWN stays visible and drives status RED.
  (IN_FLIGHT / UNDECIDED are reported separately: an intent that is not yet resolved, a snapshot without a decision.)

``NON_TRADED_ELIGIBLE`` is the umbrella set  COUNTERFACTUAL_COMPLETE + EXPECTED_PENDING + UNEXPECTED_MISSING.

ELIGIBLE (denominator of ``eligible_coverage_pct``): a NON-TRADED opportunity (engine reject, catch-up miss, or an
engine-accepted one that ended RISK_REJECTED / SEND_FAILED / CANCELLED / never got an intent - exactly the population
``DemoStore.non_traded_unlabelled`` + ``counterfactual_rows`` / ``demo.labeling.blocking_gate`` define) whose horizon has
ELAPSED (``now >= horizon_end_utc(snapshot)``, the labeller's own precondition) and which is past the no-intent window.
Justification: before that moment the labeller is not allowed to label, so counting the opportunity would punish
coverage for time that simply has not passed; after it, every unlabelled opportunity is a real gap with a cause.
``eligible_coverage_pct = counterfactual_labelled / non_traded_eligible``; horizon-open cases never touch it.

LIMITS (NOT_AVAILABLE, never guessed)
  * A labeller error is not persisted anywhere, so LABEL_ERROR / BAR_PROVIDER_ERROR can only be inferred from
    ``scan_errors`` or by passing a diagnostic ``bars_provider``.
  * A failed shadow-lab evaluation is only counted in memory (``LabStats``): ``failed`` is NOT_AVAILABLE.
  * ``observer_definition_hash`` / ``engine_definition_hash`` are NOT stored in ``DemoStore.meta`` (they live in the
    market observer); per-row epoch = ``snapshots.versions.git_commit`` (+ ``config_hash``).  Ancestry of a commit
    relative to the 11c6aec boundary is not in the DB: epochs are reported side by side and never pooled silently.

STATUS (GREEN / AMBER / RED) is computed from amount, cause, systematicness and affected population (thresholds in
``CoverageThresholds``, no universal 100 % rule).  RED in any population => ``no_promotion_claim``.

Extension: ``register_section(name, fn)``; the placeholder ``position_thesis`` section is registered below.
"""

from __future__ import annotations

import inspect
import json
import math
import shutil
import sqlite3
import tempfile
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from demo import labeling as L
from demo.contracts import PHASES
from demo.funnel import TRADED_STATES, UNCLASSIFIED
from demo.store import DemoStore, parse_utc

COVERAGE_VERSION = "coverage-1"

# classification
TRADED = "TRADED"
NON_TRADED_HORIZON_OPEN = "NON_TRADED_HORIZON_OPEN"
NON_TRADED_ELIGIBLE = "NON_TRADED_ELIGIBLE"  # umbrella (see module docstring)
COUNTERFACTUAL_COMPLETE = "COUNTERFACTUAL_COMPLETE"
EXPECTED_PENDING = "EXPECTED_PENDING"
UNEXPECTED_MISSING = "UNEXPECTED_MISSING"
IN_FLIGHT = "IN_FLIGHT"
UNDECIDED = "UNDECIDED"
CLASSIFICATIONS = (
    TRADED,
    NON_TRADED_HORIZON_OPEN,
    COUNTERFACTUAL_COMPLETE,
    EXPECTED_PENDING,
    UNEXPECTED_MISSING,
    IN_FLIGHT,
    UNDECIDED,
)

# causes
PENDING_HORIZON = "PENDING_HORIZON"
PENDING_INTENT_WINDOW = "PENDING_INTENT_WINDOW"
WAITING_FOR_BARS = "WAITING_FOR_BARS"
INCOMPLETE_BAR_COVERAGE = "INCOMPLETE_BAR_COVERAGE"
BAR_PROVIDER_ERROR = "BAR_PROVIDER_ERROR"
INVALID_GEOMETRY = "INVALID_GEOMETRY"
LABEL_ERROR = "LABEL_ERROR"
UNKNOWN = "UNKNOWN"
UNEXPLAINED_CAUSES = frozenset({UNKNOWN, LABEL_ERROR})

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"
NOT_IMPLEMENTED_YET = "NOT_IMPLEMENTED_YET"
NOT_AVAILABLE = "NOT_AVAILABLE"
_RANK = {GREEN: 0, AMBER: 1, RED: 2}

BOUNDARY_SHA = "11c6aec"  # baseline/fix boundary (merge fix/exit-stop-tick-rounding)
PRODUCTION_DIR_PARTS = ("artifacts", "demo_100k")
_NON_ENGINE_SOURCES = frozenset(
    {L.SRC_STACK, L.SRC_CANCELLED, L.SRC_SEND_FAILED, L.SRC_SHADOW, L.SRC_NO_INTENT}
)
_NON_TRADED_TERMINAL = frozenset(
    {"RISK_REJECTED", "SEND_FAILED", "CANCELLED"}
)  # intent states that are NON-trades
_LAB_DELAY_AFTER_FLAT = timedelta(
    minutes=5
)  # runner: the lab runs only once now >= forced_flat + one M5 bar


class ProductionPathRefused(Exception):
    """The DB path points into the production state directory without the explicit read-only override."""


@dataclass(frozen=True, slots=True)
class CoverageThresholds:
    """Status heuristics (documented, tunable; applied per population)."""

    min_n: int = 5  # a segment needs at least this many eligible rows to be called systematic
    systematic_frac: float = 0.25  # unexpected-missing share of a segment that makes it RED
    unexplained_red_frac: float = (
        0.02  # unexplained (UNKNOWN / LABEL_ERROR) share of eligible above which it is RED
    )
    unbounded_gap_frac: float = (
        0.5  # (pending + explained missing) share of eligible above which the gap is not "bounded"
    )
    analytics_red_frac: float = 0.10  # share of closed trades missing an expected analytic above which it is RED (>= 2 rows)


# ---------------------------------------------------------------------------------------------------------------
# read-only access
# ---------------------------------------------------------------------------------------------------------------
def refuse_production_path(path: str | Path, *, allow: bool = False) -> None:
    parts = tuple(p.lower() for p in Path(path).resolve().parts)
    for i in range(len(parts) - 1):
        if parts[i : i + 2] == PRODUCTION_DIR_PARTS and not allow:
            raise ProductionPathRefused(
                f"{path}: lies under {'/'.join(PRODUCTION_DIR_PARTS)} (production state); use a COPY, or pass "
                "allow_production_readonly / --allow-production-db-readonly (opened mode=ro only)"
            )


class _SnapshotStore(DemoStore):
    """Read-only store over a private temp COPY of the DB (+ its WAL): opening a WAL database, even ``mode=ro``,
    creates ``-wal`` / ``-shm`` sidecars next to it, so the original directory is never opened directly.  The
    temp copy is deleted on ``close()``."""

    _tmpdir: str | None = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._tmpdir is not None:
                shutil.rmtree(self._tmpdir, ignore_errors=True)
                self._tmpdir = None


def open_readonly(path: str | Path, *, allow_production_readonly: bool = False) -> DemoStore:
    """A ``DemoStore`` over a read-only (SQLite ``mode=ro`` + ``query_only``) connection to a private copy of the DB
    (and its ``-wal`` when present, so committed-but-uncheckpointed rows are seen): the store's own read API works,
    any write raises ``sqlite3.OperationalError``, and the source directory is never modified (no sidecar files, no
    mtime change).  The constructor is bypassed on purpose (it creates tables / sets WAL)."""
    refuse_production_path(path, allow=allow_production_readonly)
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(str(p))
    tmp = tempfile.mkdtemp(prefix="coverage_ro_")
    try:
        shutil.copyfile(p, Path(tmp) / p.name)
        wal = p.with_name(p.name + "-wal")
        if wal.is_file():
            shutil.copyfile(wal, Path(tmp) / wal.name)
        copy = Path(tmp) / p.name
        conn = sqlite3.connect(
            f"{copy.as_uri()}?mode=ro", uri=True, timeout=30.0, check_same_thread=False
        )
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    store = _SnapshotStore.__new__(_SnapshotStore)
    store.path = p
    store._tmpdir = tmp
    store._clock = lambda: datetime.now(UTC).isoformat()
    store._lock = threading.RLock()
    store._conn = conn
    return store


# ---------------------------------------------------------------------------------------------------------------
# context + section registry
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class CoverageContext:
    store: DemoStore
    phase: str | None
    now: datetime
    thresholds: CoverageThresholds = field(default_factory=CoverageThresholds)
    bars_provider: Callable[[str, str, str], Sequence[Any]] | None = (
        None  # optional diagnostic (never used to write)
    )
    bar_seconds: int = 300
    grace_s: int = field(
        default_factory=lambda: int(
            inspect.signature(L.label_counterfactuals).parameters["incomplete_grace_s"].default
        )
    )
    boundary_sha: str = BOUNDARY_SHA
    cache: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # optional offline research output for the position_thesis section (see resolve_position_thesis_input)
    position_thesis_input: Any = None

    def cached(self, key: str, fn: Callable[[], Any], default: Any) -> Any:
        if key not in self.cache:
            self.cache[key] = self.safe(key, fn, default)
        return self.cache[key]

    def safe(self, label: str, fn: Callable[[], Any], default: Any) -> Any:
        try:
            return fn()
        except sqlite3.OperationalError as exc:  # older DB without a table: visible, never fatal
            self.notes.append(f"{label}: {exc}")
            return default


SectionFn = Callable[[CoverageContext], dict[str, Any]]
SECTIONS: dict[str, SectionFn] = {}
RENDERERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {}


def register_section(
    name: str, fn: SectionFn, renderer: Callable[[dict[str, Any]], list[str]] | None = None
) -> None:
    """Add (or replace) a coverage section.  ``fn(ctx)`` returns a dict; an optional ``status`` (GREEN/AMBER/RED/
    NOT_IMPLEMENTED_YET) and ``no_promotion_reasons`` (list) take part in the overall verdict."""
    SECTIONS[name] = fn
    if renderer is not None:
        RENDERERS[name] = renderer


# ---------------------------------------------------------------------------------------------------------------
# opportunities
# ---------------------------------------------------------------------------------------------------------------
def _pct(a: int, b: int) -> float | None:
    return None if b == 0 else round(100.0 * a / b, 2)


def _geometry_invalid(snap: Any) -> bool:
    g = snap.geometry
    risk = abs(g.intended_entry - g.stop)
    if not risk > 0 or not math.isfinite(risk) or g.expected_horizon_s <= 0:
        return True
    return not (
        (snap.direction > 0 and g.stop < g.intended_entry)
        or (snap.direction < 0 and g.stop > g.intended_entry)
    )


def _epochs(ctx: CoverageContext) -> dict[str, tuple[str, str]]:
    def load() -> dict[str, tuple[str, str]]:
        sql = (
            "SELECT opportunity_id, json_extract(json,'$.versions.git_commit') g, "
            "json_extract(json,'$.versions.config_hash') c FROM snapshots"
        )
        args: tuple = ()
        if ctx.phase:
            sql, args = sql + " WHERE phase=?", (ctx.phase,)
        return {
            r["opportunity_id"]: (r["g"] or "UNKNOWN", r["c"] or "UNKNOWN")
            for r in ctx.store._q(sql, args)
        }

    return ctx.cached("epochs", load, {})


def _unlabelled_cause(
    ctx: CoverageContext, row: dict[str, Any], gate: Any
) -> tuple[str, str | None, str | None]:
    """(classification, cause, note) of one non-traded, unlabelled opportunity."""
    snap, now = row["snapshot"], ctx.now
    end = L.horizon_end_utc(snap)
    if (
        gate.source == L.SRC_NO_INTENT
        and (now - parse_utc(row["decision"].decided_utc)).total_seconds()
        < L.ACCEPTED_NO_INTENT_MIN_AGE_S
    ):
        return NON_TRADED_HORIZON_OPEN, PENDING_INTENT_WINDOW, None
    if now < end:
        return NON_TRADED_HORIZON_OPEN, PENDING_HORIZON, None
    grace_over = now >= end + timedelta(seconds=ctx.grace_s)
    if _geometry_invalid(snap):
        return (
            UNEXPECTED_MISSING,
            INVALID_GEOMETRY,
            "label could never be computed (risk distance / stop side / horizon)",
        )
    start = parse_utc(snap.signal_ts_utc)
    for err in ctx.cached("scan_errors", ctx.store.scan_errors, []):
        if err["market"] == snap.market:
            t = parse_utc(err["bar_close_utc"])
            if start <= t <= end + timedelta(seconds=ctx.bar_seconds):
                return (
                    UNEXPECTED_MISSING,
                    BAR_PROVIDER_ERROR,
                    f"scan error {err['bar_close_utc']}: {err['error'][:80]}",
                )
    if ctx.bars_provider is not None:
        return _diagnose(ctx, snap, end, grace_over)
    pointers = ctx.cached("pointers", ctx.store.bar_pointers, {})
    ptr = pointers.get((snap.market, "M5"))
    if ptr is None or parse_utc(ptr) < end:
        return (
            (UNEXPECTED_MISSING if grace_over else EXPECTED_PENDING),
            WAITING_FOR_BARS,
            f"bar pointer {ptr}",
        )
    if not grace_over:
        return (
            EXPECTED_PENDING,
            INCOMPLETE_BAR_COVERAGE,
            "bars observed past the horizon; labeller lag or bar gap, inside grace",
        )
    return UNEXPECTED_MISSING, UNKNOWN, "bars observed past the horizon, grace elapsed, no label"


def _diagnose(
    ctx: CoverageContext, snap: Any, end: datetime, grace_over: bool
) -> tuple[str, str | None, str | None]:
    """Optional bars-provider diagnosis (same rules as ``label_counterfactuals``, nothing is written)."""
    start = parse_utc(snap.signal_ts_utc)
    try:
        raw = ctx.bars_provider(snap.market, snap.signal_ts_utc, end.isoformat())  # type: ignore[misc]
    except Exception as exc:
        return UNEXPECTED_MISSING, BAR_PROVIDER_ERROR, f"{type(exc).__name__}: {exc}"[:120]
    causal = [b for b in raw if start <= parse_utc(b.ts_utc) < end]
    if not causal:
        return (
            (UNEXPECTED_MISSING if grace_over else EXPECTED_PENDING),
            WAITING_FOR_BARS,
            "provider returned no bars",
        )
    covered = max(parse_utc(b.ts_utc) for b in causal) + timedelta(seconds=ctx.bar_seconds) >= end
    try:
        _label, res = L.label_one(snap, causal, labelled_utc=ctx.now.isoformat(), horizon_end=end)
    except Exception as exc:
        return UNEXPECTED_MISSING, LABEL_ERROR, f"{type(exc).__name__}: {exc}"[:120]
    if res.exit_kind == "HORIZON" and not covered:
        return (
            (UNEXPECTED_MISSING if grace_over else EXPECTED_PENDING),
            INCOMPLETE_BAR_COVERAGE,
            "bars do not cover the horizon",
        )
    return (
        UNEXPECTED_MISSING,
        UNKNOWN,
        "labellable with the provider's bars, yet unlabelled (labeller not run?)",
    )


def collect_opportunities(ctx: CoverageContext) -> list[dict[str, Any]]:
    """One classified record per recorded snapshot."""
    if "opps" in ctx.cache:
        return ctx.cache["opps"]
    st, phase = ctx.store, ctx.phase
    funnel = ctx.safe("funnel_rows", lambda: st.funnel_rows(phase), [])
    cf = {
        r["opportunity_id"]: r
        for r in ctx.safe("counterfactual_rows", lambda: st.counterfactual_rows(phase), [])
    }
    unl: dict[str, tuple[dict[str, Any], Any]] = {}
    for row in ctx.safe("non_traded_unlabelled", lambda: st.non_traded_unlabelled(phase), []):
        gate = L.blocking_gate(row)
        if gate is not None:
            unl[row["decision"].opportunity_id] = (row, gate)
    epochs = _epochs(ctx)
    out = []
    for f in funnel:
        oid = f["opportunity_id"]
        rec: dict[str, Any] = {
            "opportunity_id": oid,
            "market": f["market"],
            "family": f["family"] or "-",
            "variant": f["variant"] or "-",
            "direction": {1: "LONG", -1: "SHORT"}.get(f["direction"], "-"),
            "day": (f["signal_ts"] or "")[:10] or "-",
            "session": f["session"] or "-",
            "epoch": epochs.get(oid, ("UNKNOWN", "UNKNOWN"))[0],
            "source": "-",
            "engine_reject_reason": "-",
            "stack_reject_code": "-",
            "gate_class": "-",
            "classification": None,
            "cause": None,
            "note": None,
            "traded_and_counterfactual": False,
        }
        gate_src = None
        state = f["state"]
        if state is not None and state not in _NON_TRADED_TERMINAL:
            # precedence: an intent that is traded / in flight is NEVER a counterfactual, even when a label exists
            # (e.g. accepted-without-intent labelled after 10 min, intent + fill arrived later): flagged as contamination
            traded = state in TRADED_STATES
            rec.update(
                classification=TRADED if traded else IN_FLIGHT,
                cause=None if traded else "IN_FLIGHT_INTENT",
                source="TRADED" if traded else f"INTENT_{state}",
                traded_and_counterfactual=oid in cf,
            )
        elif oid in cf:
            c = cf[oid]
            rec.update(classification=COUNTERFACTUAL_COMPLETE, cause=None)
            gate_src = (c["source"], c["gate_code"], c["gate_class"])
        elif oid in unl:
            row, gate = unl[oid]
            cls, cause, note = _unlabelled_cause(ctx, row, gate)
            rec.update(classification=cls, cause=cause, note=note)
            gate_src = (gate.source, gate.code, gate.gate_class)
        elif f["accepted"] is None:
            rec.update(classification=UNDECIDED, cause=UNKNOWN)
        else:
            rec.update(
                classification=UNDECIDED,
                cause=UNKNOWN,
                note="decision exists but no classification applies",
            )
        if gate_src is not None:
            src, code, cls_ = gate_src
            rec["source"], rec["gate_class"] = src, cls_ or UNCLASSIFIED
            if src == L.SRC_STACK:
                rec["stack_reject_code"] = code or "-"
            elif src not in _NON_ENGINE_SOURCES:
                rec["engine_reject_reason"] = code or "-"
        out.append(rec)
    ctx.cache["opps"] = out
    return out


def opportunity_metrics(opps: Sequence[dict[str, Any]]) -> dict[str, Any]:
    n = Counter(o["classification"] for o in opps)
    causes = Counter((o["classification"], o["cause"]) for o in opps if o["cause"])
    pending_h = n[NON_TRADED_HORIZON_OPEN]
    labelled, pending, missing = (
        n[COUNTERFACTUAL_COMPLETE],
        n[EXPECTED_PENDING],
        n[UNEXPECTED_MISSING],
    )
    eligible = labelled + pending + missing
    return {
        "total_opportunities": len(opps),
        "traded": n[TRADED],
        "in_flight": n[IN_FLIGHT],
        "undecided": n[UNDECIDED],
        "traded_and_counterfactual": sum(1 for o in opps if o.get("traded_and_counterfactual")),
        "non_traded": pending_h + eligible,
        "non_traded_pending_horizon": pending_h,
        "non_traded_eligible": eligible,
        "counterfactual_labelled": labelled,
        "counterfactual_pending": pending,
        "counterfactual_unexpected_missing": missing,
        "eligible_coverage_pct": _pct(labelled, eligible),
        "pending_by_cause": {c: v for (k, c), v in sorted(causes.items()) if k == EXPECTED_PENDING},
        "unexpected_missing_by_cause": {
            c: v for (k, c), v in sorted(causes.items()) if k == UNEXPECTED_MISSING
        },
        "horizon_open_by_cause": {
            c: v for (k, c), v in sorted(causes.items()) if k == NON_TRADED_HORIZON_OPEN
        },
        "unexplained_missing": sum(
            v for (k, c), v in causes.items() if k == UNEXPECTED_MISSING and c in UNEXPLAINED_CAUSES
        ),
        "label_error": NOT_AVAILABLE
        + ": labeller errors are not persisted (only inferable with a diagnostic bars_provider)",
    }


SEGMENT_DIMS = (
    "market",
    "family",
    "variant",
    "direction",
    "day",
    "session",
    "engine_reject_reason",
    "stack_reject_code",
    "gate_class",
    "source",
)


def segment_counts(opps: Sequence[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for dim in SEGMENT_DIMS:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for o in opps:
            groups[str(o[dim])].append(o)
        out[dim] = {k: _seg(v) for k, v in sorted(groups.items())}
    return out


def _seg(v: Sequence[dict[str, Any]]) -> dict[str, Any]:
    m = opportunity_metrics(v)
    keys = (
        "total_opportunities",
        "traded",
        "non_traded",
        "non_traded_pending_horizon",
        "non_traded_eligible",
        "counterfactual_labelled",
        "counterfactual_pending",
        "counterfactual_unexpected_missing",
        "eligible_coverage_pct",
    )
    return {k: m[k] for k in keys}


def assess_opportunities(
    opps: Sequence[dict[str, Any]], m: dict[str, Any], th: CoverageThresholds
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    status = GREEN
    eligible, unexplained = m["non_traded_eligible"], m["unexplained_missing"]
    gaps = m["counterfactual_pending"] + m["counterfactual_unexpected_missing"]

    def bump(level: str, why: str) -> None:
        nonlocal status
        if _RANK[level] > _RANK[status]:
            status = level
        reasons.append(f"{level}: {why}")

    if m["traded_and_counterfactual"]:
        bump(
            RED,
            f"{m['traded_and_counterfactual']} traded/in-flight opportunity(ies) also carry a counterfactual label (contamination; excluded from the CF counts)",
        )
    if m["undecided"]:
        bump(AMBER, f"{m['undecided']} snapshot(s) without a classifiable decision/intent")
    if unexplained:
        frac = unexplained / eligible if eligible else 1.0
        bump(
            RED if frac > th.unexplained_red_frac else AMBER,
            f"{unexplained} unexplained missing label(s) ({100 * frac:.1f}% of eligible)",
        )
    if eligible and gaps / eligible > th.unbounded_gap_frac and eligible >= th.min_n:
        bump(RED, f"explained gap not bounded: {gaps}/{eligible} eligible without a label")
    elif gaps:
        bump(
            AMBER,
            f"{gaps} eligible without a label, all explained ({m['pending_by_cause']} / {m['unexpected_missing_by_cause']})",
        )
    for dim in ("market", "family", "source", "gate_class"):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for o in opps:
            groups[str(o[dim])].append(o)
        for key, rows in groups.items():
            sm = opportunity_metrics(rows)
            e, miss = sm["non_traded_eligible"], sm["counterfactual_unexpected_missing"]
            if e >= th.min_n and miss / e >= th.systematic_frac:
                bump(
                    RED,
                    f"systematic missingness in {dim}={key}: {miss}/{e} eligible unexpected-missing",
                )
    return status, reasons


def _by_epoch(opps: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    g: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in opps:
        g[o["epoch"]].append(o)
    return dict(sorted(g.items()))


def section_opportunities(ctx: CoverageContext) -> dict[str, Any]:
    opps = collect_opportunities(ctx)
    metrics = opportunity_metrics(opps)
    status, reasons = assess_opportunities(opps, metrics, ctx.thresholds)
    by_epoch = {}
    for epoch, rows in _by_epoch(opps).items():
        m = opportunity_metrics(rows)
        s, r = assess_opportunities(rows, m, ctx.thresholds)
        by_epoch[epoch] = {"metrics": m, "status": s, "reasons": r}
    gap_rows = [
        {
            k: o[k]
            for k in (
                "opportunity_id",
                "market",
                "family",
                "classification",
                "cause",
                "note",
                "source",
                "epoch",
            )
        }
        for o in opps
        if o["classification"] == UNEXPECTED_MISSING
    ][:50]
    return {
        "status": status,
        "reasons": reasons,
        "metrics": metrics,
        "segments": segment_counts(opps),
        "by_epoch": by_epoch,
        "unexpected_missing_sample": gap_rows,
        "definition": {
            "eligible": "non-traded, horizon elapsed (now >= horizon_end), past the no-intent window",
            "coverage": "counterfactual_labelled / non_traded_eligible",
        },
    }


# ---------------------------------------------------------------------------------------------------------------
# trade analytics + shadow exit lab
# ---------------------------------------------------------------------------------------------------------------
def collect_trades(ctx: CoverageContext) -> list[dict[str, Any]]:
    if "trades" in ctx.cache:
        return ctx.cache["trades"]
    st, phase = ctx.store, ctx.phase
    epochs = _epochs(ctx)
    # start from the CLOSED intents (not from persisted outcomes): a CLOSED intent without an outcome row (the
    # transition -> record_outcome crash window) must stay visible
    sql = "SELECT i.intent_id AS iid, i.opportunity_id AS oid, (SELECT COUNT(*) FROM outcomes o WHERE o.intent_id=i.intent_id) AS n_out FROM intents i WHERE i.state='CLOSED'"
    closed = ctx.safe(
        "closed_intents",
        lambda: st._q(
            sql + (" AND i.phase=?" if phase else "") + " ORDER BY i.intent_id",
            (phase,) if phase else (),
        ),
        [],
    )
    strategy_ids = {
        iid
        for iid, _o, _oc in ctx.safe(
            "outcomes", lambda: st.list_outcomes(phase, kind="strategy"), []
        )
    }
    tca = {
        (t["intent_id"], t["stage"])
        for t in ctx.safe("tca_records", lambda: st.list_tca(phase), [])
    }
    out = []
    for row in closed:
        iid, oid, has_outcome = row["iid"], row["oid"], row["n_out"] > 0
        tag = st.get_trade_tag(iid) or {}
        ttype, source = tag.get("trade_type", "STRATEGY"), tag.get("source")
        expected = (
            ttype == "STRATEGY" and source != "external_import"
        )  # canary / test / external imports carry no runner analytics
        extra = st.get_outcome_extra(iid) or None
        intent = st.get_intent(iid) or {}
        snap = st.get_snapshot(oid)
        flat = intent.get("forced_flat_utc") or (  # the runner's own fallback: signal + 12 h
            None
            if snap is None
            else (parse_utc(snap.signal_ts_utc) + timedelta(hours=12)).isoformat()
        )
        out.append(
            {
                "intent_id": iid,
                "opportunity_id": oid,
                "epoch": epochs.get(oid, ("UNKNOWN", "UNKNOWN"))[0],
                "trade_type": ttype,
                "source": source,
                "expected_analytics": expected,
                "has_outcome": has_outcome,
                "lab_population": iid in strategy_ids,
                "outcome_extra": extra is not None and extra.get("final_gross_r") is not None,
                "entry_exit": bool(extra and extra.get("entry_exit")),
                "has_extra_row": extra is not None,
                "lab": bool(extra and extra.get("shadow_exit_lab") is not None),
                "entry_tca": (iid, "ENTRY") in tca,
                "exit_tca": (iid, "EXIT") in tca,
                "forced_flat_utc": flat,
            }
        )
    ctx.cache["trades"] = out
    return out


_ANALYTICS = (
    (
        "outcome_complete",
        "has_outcome",
        "outcomes row of the CLOSED intent (cause OUTCOME_ROW_MISSING)",
    ),
    ("outcome_extra_complete", "outcome_extra", "outcome_extra row with final_gross_r"),
    ("entry_tca_complete", "entry_tca", "tca_records stage ENTRY"),
    ("exit_tca_complete", "exit_tca", "tca_records stage EXIT"),
    (
        "entry_exit_diagnostic_complete",
        "entry_exit",
        "outcome_extra.entry_exit (None = unusable input or pre-Lane-X trade)",
    ),
)


def trade_metrics(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    exp = [t for t in trades if t["expected_analytics"]]
    out: dict[str, Any] = {
        "closed_trades": len(trades),
        "closed_trades_expected_analytics": len(exp),
        "closed_excluded_non_strategy_or_external": len(trades) - len(exp),
    }
    for name, key, what in _ANALYTICS:
        done = sum(1 for t in exp if t[key])
        out[name] = {
            "complete": done,
            "expected": len(exp),
            "missing": len(exp) - done,
            "pct": _pct(done, len(exp)),
            "source": what,
        }
    return out


def assess_trades(m: dict[str, Any], th: CoverageThresholds) -> tuple[str, list[str]]:
    status, reasons = GREEN, []
    for name, _k, _w in _ANALYTICS:
        a = m[name]
        if a["missing"]:
            frac = a["missing"] / a["expected"]
            level = RED if (a["missing"] >= 2 and frac > th.analytics_red_frac) else AMBER
            if (
                name == "outcome_complete"
            ):  # integrity: a CLOSED trade without an outcome is silently excluded from every alpha metric
                level = RED
            if _RANK[level] > _RANK[status]:
                status = level
            reasons.append(
                f"{level}: {name} missing for {a['missing']}/{a['expected']} closed trades (no persisted explanation)"
            )
    return status, reasons


def section_trade_analytics(ctx: CoverageContext) -> dict[str, Any]:
    trades = collect_trades(ctx)
    m = trade_metrics(trades)
    status, reasons = assess_trades(m, ctx.thresholds)
    by_epoch = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        groups[t["epoch"]].append(t)
    for epoch, rows in sorted(groups.items()):
        em = trade_metrics(rows)
        s, r = assess_trades(em, ctx.thresholds)
        by_epoch[epoch] = {"metrics": em, "status": s, "reasons": r}
    gaps = [
        {"intent_id": t["intent_id"], "missing": [n for n, k, _w in _ANALYTICS if not t[k]]}
        for t in trades
        if t["expected_analytics"] and not all(t[k] for _n, k, _w in _ANALYTICS)
    ][:50]
    return {
        "status": status,
        "reasons": reasons,
        "metrics": m,
        "by_epoch": by_epoch,
        "gap_sample": gaps,
        "population": "closed trades with trade_type STRATEGY (censored included), excluding external imports",
    }


def lab_metrics(
    ctx: CoverageContext, trades: Sequence[dict[str, Any]], cf_labels: Sequence[Any]
) -> dict[str, Any]:
    """Shadow exit lab on closed trades (eligible = strategy-kind outcome WITH an outcome_extra row, the runner's own
    precondition) and on counterfactual labels.  A missing lab result is NOT a negative policy outcome."""
    now = ctx.now
    eligible = [t for t in trades if t["lab_population"] and t["has_extra_row"]]
    complete = [t for t in eligible if t["lab"]]
    pending = unexpected = 0
    for t in eligible:
        if t["lab"]:
            continue
        flat = t["forced_flat_utc"]
        if flat is not None and now < parse_utc(flat) + _LAB_DELAY_AFTER_FLAT:
            pending += 1
        else:
            unexpected += 1
    trade_block: dict[str, Any] = {
        "eligible": len(eligible),
        "complete": len(complete),
        "pending": pending,
        "failed": NOT_AVAILABLE,
        "failed_reason": "a failed evaluation is only counted in memory (LabStats); nothing is persisted",
        "unexpected_missing": unexpected,
        "lab_absent": False,
    }
    if eligible and not complete and unexpected:
        # a completely absent required lab stays UNEXPECTED_MISSING (never rewritten to 0): it blocks promotion claims
        trade_block.update(
            lab_absent=True,
            note="no closed trade has a lab result: the lab looks disabled / never ran (cannot be proven from the DB); still unexpected_missing",
        )
    cf_complete = sum(1 for lab in cf_labels if lab.shadow_exit_lab is not None)
    cf_block: dict[str, Any] = {
        "eligible": len(cf_labels),
        "complete": cf_complete,
        "missing": len(cf_labels) - cf_complete,
        "note": "a counterfactual lab is None when the flag was off, no causal bars, or the evaluation failed (not distinguishable)",
    }
    if cf_labels and cf_complete == 0:
        cf_block["state"] = "NOT_OBSERVED (lab flag off or never ran)"
    return {
        "closed_trades": trade_block,
        "counterfactuals": cf_block,
        "missing_is_not_negative": True,
        "eligible": trade_block["eligible"],
        "complete": trade_block["complete"],
        "pending": pending,
        "failed": NOT_AVAILABLE,
        "unexpected_missing": trade_block["unexpected_missing"],
    }


def section_shadow_exit_lab(ctx: CoverageContext) -> dict[str, Any]:
    trades = collect_trades(ctx)
    labels = ctx.safe("counterfactuals", lambda: ctx.store.list_counterfactuals(ctx.phase), [])
    m = lab_metrics(ctx, trades, labels)
    tb, th = m["closed_trades"], ctx.thresholds
    status, reasons = GREEN, []
    if tb["unexpected_missing"]:
        frac = tb["unexpected_missing"] / tb["eligible"]
        status = RED if (tb["unexpected_missing"] >= 2 and frac > th.analytics_red_frac) else AMBER
        reasons.append(
            f"{status}: shadow lab missing for {tb['unexpected_missing']}/{tb['eligible']} eligible closed trades"
        )
    if tb["lab_absent"]:
        status = RED
        reasons.append(
            "RED: shadow lab produced no result for any eligible closed trade (disabled / never ran): required lab absent, blocks promotion claims"
        )
    return {"status": status, "reasons": reasons, "metrics": m}


# ---------------------------------------------------------------------------------------------------------------
# epochs / SHA visibility
# ---------------------------------------------------------------------------------------------------------------
_META_KEYS = (
    "schema_version",
    "account_phase",
    "code_sha",
    "execution_epoch",
    "observer_definition_hash",
    "engine_definition_hash",
)


def section_epochs(ctx: CoverageContext) -> dict[str, Any]:
    meta = {
        k: ctx.safe(f"meta.{k}", lambda k=k: ctx.store.get_meta(k), None)
        or NOT_AVAILABLE + " (key not stored in DemoStore.meta)"
        for k in _META_KEYS
    }
    pairs = _epochs(ctx)
    opps = collect_opportunities(ctx)
    trades = collect_trades(ctx)
    epoch_rows: dict[str, dict[str, Any]] = {}
    for opp in opps:
        e = epoch_rows.setdefault(
            opp["epoch"], {"opportunities": 0, "trades": 0, "config_hashes": set()}
        )
        e["opportunities"] += 1
    for g, c in pairs.values():
        if g in epoch_rows:
            epoch_rows[g]["config_hashes"].add(c)
    for t in trades:
        epoch_rows.setdefault(
            t["epoch"], {"opportunities": 0, "trades": 0, "config_hashes": set()}
        )["trades"] += 1
    epochs = {}
    for sha, e in sorted(epoch_rows.items()):
        at = bool(ctx.boundary_sha) and sha.startswith(ctx.boundary_sha)
        epochs[sha] = {
            "opportunities": e["opportunities"],
            "closed_trades": e["trades"],
            "config_hashes": sorted(e["config_hashes"]),
            "vs_boundary": "AT_BOUNDARY"
            if at
            else "UNKNOWN (commit ancestry is not stored in the DB; compare against git)",
        }
    mixed = len(epochs) > 1
    return {
        "meta": meta,
        "boundary_sha": ctx.boundary_sha,
        "epochs": epochs,
        "mixed_epochs": mixed,
        "pooling_warning": "multiple code epochs present: pooled numbers mix populations; judge per epoch (by_epoch blocks)"
        if mixed
        else None,
        "per_row_epoch_source": "snapshots.versions.git_commit / config_hash",
    }


# ---------------------------------------------------------------------------------------------------------------
# position thesis (fed by the offline research output of thesis/position_thesis.summary())
# ---------------------------------------------------------------------------------------------------------------
POSITION_THESIS_FIELDS = (
    "open_positions_eligible",
    "opposing_events_detected",
    "opposing_events_future_path_complete",
    "position_thesis_assessment_complete",
    "hypothetical_exit_complete",
    "pending",
    "unexpected_missing",
)
_PT_NO_PROMOTION_UNAVAILABLE = "position_thesis: no offline position-thesis output supplied - NOT_AVAILABLE (not zero); no promotion claim"


def _na_section(why: str) -> dict[str, Any]:
    return {
        "status": NOT_AVAILABLE,
        "fields": {f: NOT_AVAILABLE for f in POSITION_THESIS_FIELDS},
        "reasons": [],
        "no_promotion_claim": True,
        "no_promotion_reasons": [_PT_NO_PROMOTION_UNAVAILABLE, why],
        "note": why,
    }


def resolve_position_thesis_input(raw: Any, phase: str | None) -> tuple[Any, str]:
    """Pick the phase-matching payload of ``raw`` (never pools phases).  Returns ``(payload | None, why)``.

    ``raw`` may be a json path / json text / dict.  Accepted shapes: ``{PHASE: payload}`` or a flat payload tagged with
    ``"phase": PHASE``.  A payload is either a precomputed ``summary()`` dict (``open_positions_eligible`` ...) or
    ``{"position_theses": [...], "variants": {...}}``.  An untagged flat payload is refused (phase unknown)."""
    if raw is None:
        return None, "no position-thesis input supplied"
    if isinstance(raw, (str, Path)):
        text = str(raw)
        try:
            blob = text if text.lstrip().startswith(("{", "[")) else Path(text).read_text("utf-8")
            raw = json.loads(blob)
        except (OSError, ValueError) as exc:
            return None, f"position-thesis input unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(raw, dict):
        return None, "position-thesis input must be a mapping (phase-keyed or phase-tagged)"
    if phase is None:
        return None, "phase not given: position-thesis input is per phase and is never pooled"
    if any(k in PHASES for k in raw):
        if phase not in raw:
            return None, f"position-thesis input has no payload for phase {phase}"
        return raw[phase], f"phase {phase}"
    if "phase" in raw:
        if raw["phase"] != phase:
            return None, f"position-thesis input is tagged phase {raw['phase']}, not {phase}"
        return raw, f"phase {phase}"
    return None, "position-thesis input is not phase-keyed or phase-tagged; phases are not poolable"


def _pt_summary(payload: Any) -> tuple[dict[str, Any] | None, str]:
    if isinstance(payload, dict) and "open_positions_eligible" in payload:
        s = {k: payload[k] for k in POSITION_THESIS_FIELDS if k in payload}
        if set(s) != set(POSITION_THESIS_FIELDS):
            return None, f"summary dict lacks fields {sorted(set(POSITION_THESIS_FIELDS) - set(s))}"
        return s, "precomputed summary"
    if isinstance(payload, dict) and "position_theses" in payload:
        from research_workbench.thesis.position_thesis import summary

        variants = payload.get("variants")
        out = summary(list(payload["position_theses"]), variants)
        if variants is None:  # nothing supplied is not "0 complete / 0 missing"
            out["hypothetical_exit_complete"] = NOT_AVAILABLE
            out["pending"]["hypothetical_exit"] = NOT_AVAILABLE
            out["unexpected_missing"] = NOT_AVAILABLE
        return out, "computed from position theses"
    return None, "unrecognised position-thesis payload"


def assess_position_thesis(
    fields: dict[str, Any], th: CoverageThresholds
) -> tuple[str, list[str], list[str]]:
    """(status, reasons, no_promotion_reasons).  PENDING / NOT_OPEN never count as failure; only an eligible position
    without ANY variant result (unexpected_missing) does.  A missing variant result is not a negative outcome."""
    reasons: list[str] = []
    nop: list[str] = []
    status = GREEN
    miss = fields["unexpected_missing"]
    eligible = fields["open_positions_eligible"]
    if miss == NOT_AVAILABLE:
        nop.append(
            "position_thesis: variant results not supplied - hypothetical exits NOT_AVAILABLE"
        )
    elif miss:
        pairs = eligible * 5  # control + Variants A-D per eligible position (len(Variant))
        frac = miss / pairs if pairs else 1.0
        status = RED if frac > th.unexplained_red_frac else AMBER
        reasons.append(
            f"{status}: {miss} unexpected missing variant result(s) ({100 * frac:.1f}% of {pairs} eligible position x variant pairs)"
        )
        nop.append(f"position_thesis: {miss} unexpected missing variant result(s)")
    pending_total = sum(v for v in fields["pending"].values() if isinstance(v, int))
    if pending_total:
        nop.append(
            f"position_thesis: {pending_total} pending item(s) (not a failure; assessment incomplete)"
        )
    return status, reasons, nop


def section_position_thesis(ctx: CoverageContext) -> dict[str, Any]:
    payload, why = resolve_position_thesis_input(ctx.position_thesis_input, ctx.phase)
    if payload is None:
        return _na_section(why)
    fields, how = _pt_summary(payload)
    if fields is None:
        return {
            "status": RED,
            "fields": {f: NOT_AVAILABLE for f in POSITION_THESIS_FIELDS},
            "reasons": [f"RED: position-thesis input invalid: {how}"],
            "no_promotion_claim": True,
            "no_promotion_reasons": [f"position_thesis: input invalid: {how}"],
        }
    status, reasons, nop = assess_position_thesis(fields, ctx.thresholds)
    return {
        "status": status,
        "fields": fields,
        "reasons": reasons,
        "no_promotion_claim": bool(nop),
        "no_promotion_reasons": nop,
        "source": f"{how} ({why})",
    }


register_section("opportunities", section_opportunities)
register_section("trade_analytics", section_trade_analytics)
register_section("shadow_exit_lab", section_shadow_exit_lab)
register_section("epochs", section_epochs)
register_section("position_thesis", section_position_thesis)


# ---------------------------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------------------------
def build_coverage(store: DemoStore, *, phase: str | None = None, **kw: Any) -> dict[str, Any]:
    """Coverage report.  DISCOVERY and FROZEN are never pooled: with ``phase=None`` each phase is reported separately
    (``per_phase``), no pooled verdict is computed and ``no_promotion_claim`` is True with an explicit warning.
    Never writes."""
    if phase is not None:
        return build_phase_coverage(store, phase=phase, **kw)
    per = {p: build_phase_coverage(store, phase=p, **kw) for p in PHASES}
    worst = max((r["status"] for r in per.values()), key=lambda s: _RANK[s])
    warning = (
        "phase not given: DISCOVERY and FROZEN are NOT poolable; reported separately, no pooled verdict. "
        "Re-run with an explicit phase for a promotion decision."
    )
    return {
        "coverage_version": COVERAGE_VERSION,
        "db": str(store.path),
        "phase": "PER_PHASE",
        "now": next(iter(per.values()))["now"],
        "status": worst,
        "no_promotion_claim": True,
        "no_promotion_reasons": [warning],
        "red_populations": [f"{p}:{s}" for p, r in per.items() for s in r["red_populations"]],
        "pooled_warning": warning,
        "per_phase": per,
        "sections": {},
        "notes": [],
    }


def build_phase_coverage(
    store: DemoStore,
    *,
    phase: str | None = None,
    now: str | datetime | None = None,
    thresholds: CoverageThresholds | None = None,
    bars_provider: Callable[[str, str, str], Sequence[Any]] | None = None,
    boundary_sha: str = BOUNDARY_SHA,
    position_thesis_input: Any = None,
) -> dict[str, Any]:
    """Coverage report for an (already read-only) store.  Never writes."""
    when = now if isinstance(now, datetime) else (parse_utc(now) if now else datetime.now(UTC))
    ctx = CoverageContext(
        store=store,
        phase=phase,
        now=when,
        thresholds=thresholds or CoverageThresholds(),
        bars_provider=bars_provider,
        boundary_sha=boundary_sha,
        position_thesis_input=position_thesis_input,
    )
    sections: dict[str, Any] = {}
    for name, fn in SECTIONS.items():
        try:
            sections[name] = fn(ctx)
        except Exception as exc:  # a broken section must be visible, never silent
            sections[name] = {
                "status": RED,
                "reasons": [f"RED: section raised {type(exc).__name__}: {exc}"],
            }
    populations: list[dict[str, Any]] = []
    for name in ("opportunities", "trade_analytics"):
        sec = sections.get(name, {})
        if "status" in sec:
            populations.append({"scope": f"pooled/{name}", "status": sec["status"]})
        for epoch, e in sec.get("by_epoch", {}).items():
            populations.append({"scope": f"epoch:{epoch}/{name}", "status": e["status"]})
    for name, sec in sections.items():
        if name not in ("opportunities", "trade_analytics") and sec.get("status") in _RANK:
            populations.append({"scope": name, "status": sec["status"]})
    status = max((p["status"] for p in populations), key=lambda s: _RANK[s], default=GREEN)
    red = [p["scope"] for p in populations if p["status"] == RED]
    reasons = [
        r for sec in sections.values() for r in sec.get("reasons", []) if r.startswith("RED")
    ]
    ep = sections.get("epochs", {})
    # sections with their own no-promotion claim (position_thesis NOT_AVAILABLE / pending / missing): kept visible; they do
    # not flip the RED-population claim of the committed verdict semantics (RED sections do, via `red`)
    section_nop = {
        name: sec["no_promotion_reasons"]
        for name, sec in sections.items()
        if sec.get("no_promotion_claim") and sec.get("no_promotion_reasons")
    }
    return {
        "coverage_version": COVERAGE_VERSION,
        "db": str(store.path),
        "phase": phase,
        "now": when.isoformat(),
        "status": status,
        "populations": populations,
        "no_promotion_claim": bool(red),
        "no_promotion_reasons": reasons if red else [],
        "red_populations": red,
        "section_no_promotion": section_nop,
        "mixed_epochs": ep.get("mixed_epochs", False),
        "epoch_pooling_warning": ep.get("pooling_warning"),
        "sections": sections,
        "notes": ctx.notes,
        "thresholds": {k: getattr(ctx.thresholds, k) for k in ctx.thresholds.__slots__},
    }


def coverage_for_path(
    path: str | Path, *, allow_production_readonly: bool = False, **kw: Any
) -> dict[str, Any]:
    store = open_readonly(path, allow_production_readonly=allow_production_readonly)
    try:
        return build_coverage(store, **kw)
    finally:
        store.close()


def _f(v: Any) -> str:
    return "n/a" if v is None else (f"{v:.1f}%" if isinstance(v, float) else str(v))


def render_markdown(report: dict[str, Any]) -> str:
    if "per_phase" in report:
        head = [
            f"# Coverage audit (PER PHASE, not pooled) - {report['status']}",
            "",
            f"- NO PROMOTION CLAIM: YES - {report['pooled_warning']}",
            "",
        ]
        return "\n".join(head) + "\n".join(
            render_markdown(r).replace("# Coverage audit", "## Coverage audit", 1)
            for r in report["per_phase"].values()
        )
    lines = [
        f"# Coverage audit ({report['phase']}) - {report['status']}",
        "",
        f"- DB `{report['db']}`  NOW {report['now']}  VERSION {report['coverage_version']}",
        f"- NO PROMOTION CLAIM: {'YES - ' + ', '.join(report['red_populations']) if report['no_promotion_claim'] else 'no'}",
    ]
    if report.get("epoch_pooling_warning"):
        lines.append(f"- EPOCHS: {report['epoch_pooling_warning']}")
    for note in report.get("notes", []):
        lines.append(f"- NOTE {note}")
    lines.append("")
    for name, sec in report["sections"].items():
        lines += [f"## {name} - {sec.get('status', '')}", ""]
        lines += [f"- {r}" for r in sec.get("reasons", [])]
        if name in RENDERERS:
            lines += RENDERERS[name](sec)
        elif name == "opportunities":
            m = sec["metrics"]
            lines += [
                f"- {k}: {_f(v)}"
                for k, v in m.items()
                if not isinstance(v, dict) and k != "label_error"
            ]
            for k in ("pending_by_cause", "unexpected_missing_by_cause", "horizon_open_by_cause"):
                if m[k]:
                    lines.append(f"- {k}: {m[k]}")
            lines += [
                "",
                "| dimension | value | total | traded | eligible | labelled | pending | missing | coverage |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for dim, vals in sec["segments"].items():
                for val, s in vals.items():
                    lines.append(
                        f"| {dim} | {val} | {s['total_opportunities']} | {s['traded']} | {s['non_traded_eligible']} | {s['counterfactual_labelled']} | {s['counterfactual_pending']} | {s['counterfactual_unexpected_missing']} | {_f(s['eligible_coverage_pct'])} |"
                    )
        elif name == "trade_analytics":
            m = sec["metrics"]
            lines += [
                f"- closed_trades: {m['closed_trades']} (analytics expected for {m['closed_trades_expected_analytics']}); outcome_complete: {m['outcome_complete']['complete']}/{m['outcome_complete']['expected']}"
            ]
            lines += [
                f"- {n}: {m[n]['complete']}/{m[n]['expected']} ({_f(m[n]['pct'])})"
                for n, _k, _w in _ANALYTICS
            ]
        elif name == "shadow_exit_lab":
            m = sec["metrics"]
            lines += [
                f"- closed trades: {m['closed_trades']}",
                f"- counterfactuals: {m['counterfactuals']}",
                "- a missing lab result is NOT a negative policy outcome",
            ]
        elif name == "epochs":
            lines += [f"- meta: {sec['meta']}"] + [
                f"- epoch `{k}`: {v}" for k, v in sec["epochs"].items()
            ]
        else:
            lines.append(
                f"```\n{json.dumps({k: v for k, v in sec.items() if k not in ('status', 'reasons')}, indent=1, default=str)}\n```"
            )
        lines.append("")
    return "\n".join(lines)
