# ruff: noqa: E501
"""Lane W: SHADOW EXIT LAB - several predeclared exit policies evaluated in PARALLEL, IN SHADOW, on the SAME entry.

Why: broad exit research without live-rule competition.  Every policy is a pure, deterministic function of
(one frozen entry, the post-entry bars): it receives only causal market data, never sends an order, never touches the
real position and never reads the stack / execution client.  It reuses the Lane X harness (``demo.exit_policies``) which
steps the EXISTING ``exits.ExitEngine`` (+ E2 structure) - there is no second engine, replay or persistence system.

Policies (minimum set, exact names; ``LAB_NAMES`` maps them onto the harness ids; extras are kept for continuity)

    fixed_1_5r              P1   stop + 1.5R target (the production default; the paired baseline)
    TP1_only                P2   stop + structural TP1 (100 %)
    TP1_plus_runner         P10  structural TP1 (50 %) + runner under the production staged rules (never a TP2)
    TP1_TP2_runner          P4   structural TP1 / TP2 / runner under the production staged rules (needs a TP2 here)
    pure_structure_trail    P5   stop only + trailing behind confirmed post-entry swings
    break_even_plus_runner  P11  +1.0R -> cost-adjusted break-even floor (next bar) + structure-trailed runner, no target
    momentum_failure        P7   P1 + the production momentum-deterioration exit
    time_decay              P8   P1 + time stop (24 bars) unless the trade showed >= 0.5R
    failed_move_exit        P12  P1 + exit at the close of the first bar that closes back across the broken range edge

A policy without its required structural level is NOT_APPLICABLE (never a level invented from an R multiple).  The
failed-move rule is only semantically valid for entries that carry a failed-move level (STRUCT family: the broken
range edge); everything else is NOT_APPLICABLE.

Nothing here is searched or tuned.  Parameters are the small versioned constants of ``demo.exit_policies``
(``POLICY_SET_VERSION``); a policy change bumps that version.  Results are HYPOTHESES: forward evidence decides promotion.

Storage: ``evaluate_shadow`` returns a compact JSON-ready dict (``shadow_exit_lab``) that the forward hooks persist in the
EXISTING ``outcome_extra`` (closed real trades) / counterfactual label JSON (labelled non-traded opportunities); no schema
change.  ``safe_evaluate_shadow`` contains every exception (counted, never raised): the lab runs at outcome / label time,
off the decision path.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from demo.entry_exit_quality import (
    MIN_CLUSTERS,
    SMALL_N,
    assign_event_clusters,
    capture_ratio,
    cluster_equal_mean,
)
from demo.exit_policies import (
    BAR_SECONDS,
    LAB_POLICY_IDS,
    P_BE,
    P_BE_RUNNER,
    P_EOD,
    P_FIXED,
    P_FM,
    P_MOM,
    P_RUNNER,
    P_TIME,
    P_TP1,
    P_TP1_RUNNER,
    P_TP12,
    P_TRAIL,
    POLICY_PARAMS,
    POLICY_SET_VERSION,
    BarSeries,
    EntryInput,
    ManagementCache,
    PolicyResult,
    simulate_policy,
)

SHADOW_LAB_VERSION = "swl-1"
BASELINE_NAME = "fixed_1_5r"
LAB_NAMES: dict[str, str] = {
    "fixed_1_5r": P_FIXED,
    "TP1_only": P_TP1,
    "TP1_plus_runner": P_TP1_RUNNER,
    "TP1_TP2_runner": P_RUNNER,
    "pure_structure_trail": P_TRAIL,
    "break_even_plus_runner": P_BE_RUNNER,
    "momentum_failure": P_MOM,
    "time_decay": P_TIME,
    "failed_move_exit": P_FM,
    # continuity with Lane X (not part of the user's minimum set)
    "struct_tp1_tp2": P_TP12,
    "breakeven_lock_fixed": P_BE,
    "eod_forced_flat": P_EOD,
}
MINIMUM_POLICY_NAMES: tuple[str, ...] = tuple(list(LAB_NAMES)[:9])
assert set(LAB_NAMES.values()) == set(LAB_POLICY_IDS)
STATEMENT = "hypotheses only; forward evidence decides promotion; no policy promoted from this table"
_STOP_LABELS = frozenset({"STOP", "STOP_GAP", "TRAILING_STOP", "BREAK_EVEN_STOP"})

LAB_PARAMS: dict[str, Any] = {
    "lab_version": SHADOW_LAB_VERSION, "policy_set_version": POLICY_SET_VERSION, "names": dict(LAB_NAMES),
    "baseline": BASELINE_NAME, "policy_params": POLICY_PARAMS,
    "capture_ratio": "min(1, max(0, final_r) / mfe_r); None when mfe_r < 0.25R",
    "mfe_mae": "exit-side prices over the bars up to the policy's own exit (stop-first: the stopping bar adds only its adverse extreme)",
}


# ---- statistics of one lab run (cost + failure isolation) ------------------------------------------------
@dataclass(slots=True)
class LabStats:
    ok: int = 0
    failed: int = 0
    total_ms: float = 0.0
    last_error: str | None = None

    @property
    def mean_ms(self) -> float | None:
        return (self.total_ms / (self.ok + self.failed)) if (self.ok + self.failed) else None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "failed": self.failed, "mean_ms": self.mean_ms, "last_error": self.last_error}


# ---- per-policy record -----------------------------------------------------------------------------------
def _path_mfe_mae(e: EntryInput, bars: BarSeries, res: PolicyResult) -> tuple[float, float]:
    """MFE / MAE in R (>= 0, exit-side prices) over the bars up to the policy's own last fill."""
    long = e.direction == 1
    risk = abs(e.fill - e.stop)
    last = res.fills[-1]
    stop_like = last.reason in _STOP_LABELS
    tp_like = last.reason.startswith("TP")
    mfe = mae = 0.0
    for k in range(len(bars)):
        t = bars.ts[k]
        if t > last.ts or (t == last.ts and not (stop_like or tp_like)):
            break
        sp = bars.spread[k]
        adv = (bars.lo[k] if long else bars.h[k] + sp)
        fav = (bars.h[k] if long else bars.lo[k] + sp)
        mae = max(mae, ((e.fill - adv) if long else (adv - e.fill)) / risk)
        if t == last.ts and stop_like:
            continue  # stop-first: the stopping bar does not extend the favourable excursion
        mfe = max(mfe, ((fav - e.fill) if long else (e.fill - fav)) / risk)
    return max(mfe, 0.0), max(mae, 0.0)


def policy_record(e: EntryInput, bars: BarSeries, res: PolicyResult) -> dict[str, Any]:
    if not res.applicable or res.r is None or not res.fills:
        return {"applicable": False, "na_reason": res.na_reason or "NOT_APPLICABLE"}
    mfe, mae = _path_mfe_mae(e, bars, res)
    # never report an excursion smaller than what the policy itself realised on a partial/target fill
    mfe = max(mfe, max((f.r for f in res.fills), default=0.0))
    cap = capture_ratio(mfe, res.r)[1]
    return {
        "applicable": True, "r": res.r, "exit_reason": res.final_reason, "bars_held": round((res.holding_s or 0.0) / BAR_SECONDS),
        "mfe_r": mfe, "mae_r": mae, "capture_ratio": cap, "giveback_r": max(0.0, mfe - res.r),
        "censored": bool(res.censored), "stages_hit": res.stages_hit,
    }


def evaluate_shadow(
    entry: EntryInput, bars: BarSeries, *, live: Mapping[str, Any] | None = None, names: Sequence[str] = tuple(LAB_NAMES),
) -> dict[str, Any]:
    """ALL lab policies on the SAME ``entry`` and ``bars``.  Pure and deterministic; the input objects are not modified.

    ``live`` = the outcome of the ACTIVE live profile on this entry (profile name, r, exit_reason, mfe_r, ...), stored
    next to the shadow policies for the live-vs-shadow comparison."""
    mgmt = ManagementCache(entry, bars)  # the E2 management inputs, derived once and shared (causal per decision time)
    used: list[str] = []
    out: dict[str, Any] = {}
    for name in names:
        pid = LAB_NAMES[name]
        used.append(entry.entry_id)
        if pid == P_RUNNER and entry.tp2 is None:
            out[name] = {"applicable": False, "na_reason": "NO_STRUCTURAL_TP2"}
            continue
        res = simulate_policy(pid, entry, bars, mgmt)
        out[name] = policy_record(entry, bars, res)
    return {
        "version": SHADOW_LAB_VERSION, "policy_set_version": POLICY_SET_VERSION,
        "entry": {
            "entry_id": entry.entry_id, "market": entry.market, "direction": entry.direction, "fill": entry.fill,
            "stop": entry.stop, "risk": abs(entry.fill - entry.stop), "n_bars": len(bars),
            "policy_entry_ids": sorted(set(used)),
        },
        "policies": out,
        "live": None if live is None else dict(live),
    }


def safe_evaluate_shadow(
    entry: EntryInput, bars: BarSeries, *, live: Mapping[str, Any] | None = None, stats: LabStats | None = None,
) -> dict[str, Any] | None:
    """``evaluate_shadow`` that NEVER raises: a failure returns None and is counted (diagnostic only)."""
    t0 = time.perf_counter()
    try:
        out = evaluate_shadow(entry, bars, live=live)
        ok = True
    except Exception as exc:
        out, ok = None, False
        if stats is not None:
            stats.last_error = f"{type(exc).__name__}: {exc}"
    if stats is not None:
        stats.total_ms += (time.perf_counter() - t0) * 1000.0
        if ok:
            stats.ok += 1
        else:
            stats.failed += 1
    return out


# ---- entry builders (forward hooks) ----------------------------------------------------------------------
def failed_move_level(direction: int, signal: Mapping[str, Any] | None) -> float | None:
    """The broken range edge of a STRUCT signal (``signal['structure_levels']`` + ``signal['variant']``), else None.
    Continuation variants: long -> range_high, short -> range_low; ``fade`` (trade against the failed break): long ->
    range_low, short -> range_high.  A close back across it against the trade = the failed move / failed reclaim."""
    if not signal:
        return None
    lv = signal.get("structure_levels") or {}
    hi, lo = lv.get("range_high"), lv.get("range_low")
    if hi is None or lo is None:
        return None
    fade = str(signal.get("variant") or "") == "fade"
    use_high = (direction == -1) if fade else (direction == 1)
    return float(hi if use_high else lo)


def to_series(rows: Sequence[Any]) -> BarSeries:
    """``demo.labeling.Bar`` rows (ts_utc ISO / open / high / low / close / spread) -> ``BarSeries`` (time-ordered)."""
    from demo.store import parse_utc

    rows = sorted(rows, key=lambda b: parse_utc(b.ts_utc))
    return BarSeries(
        tuple(parse_utc(b.ts_utc) for b in rows), tuple(float(b.open) for b in rows), tuple(float(b.high) for b in rows),
        tuple(float(b.low) for b in rows), tuple(float(b.close) for b in rows), tuple(float(b.spread) for b in rows),
    )


# ---- aggregation (report + offline study) ---------------------------------------------------------------
def _mean(xs: Sequence[float]) -> float | None:
    return (sum(xs) / len(xs)) if xs else None


def _median(xs: Sequence[float]) -> float | None:
    return statistics.median(xs) if xs else None


def summarise_lab(rows: Sequence[Mapping[str, Any]], *, small_n: int = SMALL_N, min_clusters: int = MIN_CLUSTERS) -> dict[str, Any]:
    """One cell.  Rows need ``market``, ``direction``, ``signal_ts`` (datetime), optional ``structure_event_id`` and ``lab``
    (an ``evaluate_shadow`` dict).  Paired differences vs ``fixed_1_5r`` use only entries where BOTH policies apply; the
    independent unit is the event cluster (four STRUCT variants of one break are ONE observation)."""
    labs = [r["lab"] for r in rows]
    clusters = assign_event_clusters(rows) if rows else []
    names = [n for n in LAB_NAMES]
    pol: dict[str, Any] = {}
    paired: dict[str, Any] = {}
    base = [(lb["policies"].get(BASELINE_NAME) or {}) for lb in labs]
    for name in names:
        recs = [lb["policies"].get(name) or {"applicable": False} for lb in labs]
        app = [x for x in recs if x.get("applicable")]
        rs = [x["r"] for x in app]
        caps = [x["capture_ratio"] for x in app if x.get("capture_ratio") is not None]
        pol[name] = {
            "n_applicable": len(app), "share_not_applicable": ((len(recs) - len(app)) / len(recs)) if recs else None,
            "mean_r": _mean(rs), "median_r": _median(rs), "mean_capture_ratio": _mean(caps),
            "n_capture": len(caps), "mean_giveback_r": _mean([x["giveback_r"] for x in app]),
            "mean_mfe_r": _mean([x["mfe_r"] for x in app]), "mean_mae_r": _mean([x["mae_r"] for x in app]),
            "censored_share": (sum(1 for x in app if x.get("censored")) / len(app)) if app else None,
            "cluster_equal_mean_r": cluster_equal_mean([x["r"] if x.get("applicable") else None for x in recs], clusters),
            "n_event_clusters": len({c for c, x in zip(clusters, recs, strict=True) if x.get("applicable")}),
            "exit_reasons": dict(sorted(_count(x["exit_reason"] for x in app).items())),
        }
        if name == BASELINE_NAME:
            continue
        idx = [i for i, (a, b) in enumerate(zip(recs, base, strict=True)) if a.get("applicable") and b.get("applicable")]
        d = [recs[i]["r"] - base[i]["r"] for i in idx]
        cl = [clusters[i] for i in idx]
        paired[name] = {
            "n_pairs": len(idx), "n_event_clusters": len(set(cl)), "mean_diff_r": _mean(d),
            "cluster_equal_mean_diff_r": cluster_equal_mean(d, cl),
            "small_n": len(idx) < small_n or len(set(cl)) < min_clusters,
        }
    same_entry = all(
        lb["entry"]["policy_entry_ids"] == [lb["entry"]["entry_id"]] and (r.get("entry_id") in (None, lb["entry"]["entry_id"]))
        for r, lb in zip(rows, labs, strict=True)
    )
    live_rs = [lb["live"]["r"] for lb in labs if lb.get("live") and lb["live"].get("r") is not None]
    return {
        "n": len(rows), "n_event_clusters": len(set(clusters)), "small_n": len(rows) < small_n or len(set(clusters)) < min_clusters,
        "same_entry_assertion": bool(same_entry),
        "policies": pol, "paired_vs_fixed_1_5r": paired,
        "live": {"n": len(live_rs), "mean_r": _mean(live_rs), "median_r": _median(live_rs)},
    }


def _count(it: Any) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for x in it:
        out[str(x)] += 1
    return out


def summarise_groups(rows: Sequence[Mapping[str, Any]], *, small_n: int = SMALL_N) -> dict[str, Any]:
    """market x family x variant cells (+ per live profile when present)."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    prof: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        key = f"{r['market']}|{r.get('family', '?')}|{r.get('variant') or '-'}"
        groups[key].append(r)
        lp = (r["lab"].get("live") or {}).get("profile")
        if lp:
            prof[str(lp)].append(r)
    return {
        "lab_version": SHADOW_LAB_VERSION, "policy_set_version": POLICY_SET_VERSION, "small_n": small_n, "statement": STATEMENT,
        "overall": summarise_lab(rows, small_n=small_n) if rows else {"n": 0},
        "groups": {k: summarise_lab(v, small_n=small_n) for k, v in sorted(groups.items())},
        "by_live_profile": {k: summarise_lab(v, small_n=small_n) for k, v in sorted(prof.items())},
    }


def flat_deadline_bars(bars: BarSeries, flat_utc: datetime | None) -> BarSeries:
    """Trim ``bars`` to the first bar at / after ``flat_utc`` inclusive (the one that carries the forced-flat fill)."""
    if flat_utc is None:
        return bars
    keep = 0
    for i, t in enumerate(bars.ts):
        keep = i + 1
        if t >= flat_utc:
            break
    return BarSeries(*(col[:keep] for col in (bars.ts, bars.o, bars.h, bars.lo, bars.c, bars.spread)))


__all__ = (
    "BASELINE_NAME", "LAB_NAMES", "LAB_PARAMS", "MINIMUM_POLICY_NAMES", "SHADOW_LAB_VERSION", "STATEMENT", "LabStats",
    "evaluate_shadow", "failed_move_level", "flat_deadline_bars", "policy_record", "safe_evaluate_shadow",
    "summarise_groups", "summarise_lab", "to_series",
)
