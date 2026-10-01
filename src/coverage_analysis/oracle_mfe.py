# ruff: noqa: E501
"""Lane X2: ORACLE available-MFE diagnostic.  ORACLE_RETROSPECTIVE - NEVER_IN_LIVE_DECISION_PATH.

WHAT THIS IS.  For each historical / replayable entry it computes the MAXIMUM FAVOURABLE EXCURSION the market offered
between the entry and the HORIZON, in R of the entry's initial risk (``available_mfe_r``), INDEPENDENT of the stop-out
(the excursion keeps being measured after the stop would have been hit), and sets it against the realised R of the
fixed_1_5r baseline and of the Lane X structural-exit shadow policies on the SAME entries (capture ratio, giveback).
It uses FUTURE bars by construction: it is a hindsight yardstick (what was on the table), never foresight and never
a signal, a filter, a threshold source or a live input.  It must not be imported by anything under ``src/demo``,
``src/exits``, ``src/nautilus_mt5`` or ``scripts/demo_trader.py`` (isolation test), and it lives in
``coverage_analysis`` so the existing offline-only import rules (no execution / risk / adapter coupling) hold.

NOT AN EDGE PROOF.  Lane X's label counts (useful entry + poor capture) are easy to satisfy: a driftless path reaches
+0.5R before -1R about two thirds of the time.  Read every ``P(available_mfe >= x)`` next to the ZERO-DRIFT NULL
column computed for the same entries (same per-entry risk, same horizon length, volatility measured on the bars
BEFORE the entry).  Large available MFE with a poor realised result describes a profit-capture hypothesis only.

HORIZON (documented per family; never a universal invention).  The frozen family specs carry NO thesis horizon of
their own (no max-hold / time-stop field in ``alpha.families.spec`` / the production spec), so for EVERY family the
horizon is the live operating policy's effective forced-flat instant of the entry: the earliest of the market-local
session flat, the Berlin flatten start (21:55 Berlin) and the broker-session close minus buffer
(``OperatingPolicy.effective_flat_utc``) - the same instant Lane X's paths use.  Bars opening at or after that instant
are NOT used.  An entry whose path ends before the horizon (data gap / end of data) is flagged ``horizon_complete=False``.

Bar semantics = Lane X (``demo.entry_exit_quality``): EXIT-SIDE prices (long: bid highs; short: ask lows), bar dated by
its open, the entry bar counts, fills at the next bar open.  Unlike Lane X's path, nothing is cut at the stop.

Versioned and predeclared (no threshold is tuned on results): ``ORACLE_VERSION``, ``LEVELS_R``, ``SMALL_AVAILABLE_R``,
``LARGE_AVAILABLE_R``, ``NULL_SEED`` / ``NULL_N_PATHS``.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from coverage_analysis.entry_exit import SMALL_N, _steps
from demo.entry_exit_quality import (
    DEFAULT_THRESHOLDS,
    Step,
    assign_event_clusters,
    cluster_equal_mean,
)

ORACLE_VERSION = "oracle-mfe-1"
ORACLE_LABEL = "ORACLE_RETROSPECTIVE"
ORACLE_LIVE_USE = "NEVER_IN_LIVE_DECISION_PATH"

LEVELS_R: tuple[float, ...] = (0.25, 0.5, 1.0, 1.5, 2.0, 3.0)
SMALL_AVAILABLE_R = 0.25  # available MFE below this: "the entry had little favourable opportunity"
LARGE_AVAILABLE_R = 1.5  # available MFE at/above this: "a large favourable excursion was on the table"
MIN_AVAILABLE_FOR_CAPTURE = DEFAULT_THRESHOLDS.min_mfe_for_capture  # 0.10R, as Lane X
NULL_SEED = 20260930
NULL_N_PATHS = 2000
SIGMA_LOOKBACK_BARS = 120  # causal volatility window (closed bars BEFORE the entry bar) for the null reference
SIGMA_MIN_DIFFS = 20
BAR_SECONDS = 300
REPORT_POLICIES: tuple[str, ...] = ("P1_FIXED_1_5R", "P2_STRUCT_TP1", "P3_STRUCT_TP1_TP2", "P4_TP1_TP2_RUNNER")


def _key(x: float) -> str:
    return f"{x:g}"


# ---- the oracle measurement -----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OracleExcursion:
    available_mfe_r: float
    time_to_available_mfe_s: float | None
    bars: int
    horizon_complete: bool  # a bar at/after the horizon was reached (the path was not cut short by a gap / data end)
    stop_touched: bool  # the initial stop would have been hit inside the horizon (the measurement continues anyway)
    time_to_stop_s: float | None


def available_mfe(
    *, direction: int, entry: float, stop: float, steps: Iterable[Step], entry_ts: datetime, horizon_ts: datetime,
) -> OracleExcursion:
    """Maximum favourable excursion in R of ``|entry - stop|`` over the bars from the entry up to (excluding) ``horizon_ts``.

    ORACLE_RETROSPECTIVE / NEVER_IN_LIVE_DECISION_PATH.  EXIT-SIDE prices (see module doc).  The stop is only REPORTED
    (``stop_touched``), it never ends the measurement."""
    risk = abs(entry - stop)
    if not risk > 0:
        raise ValueError("initial risk distance must be > 0")
    if direction not in (1, -1):
        raise ValueError("direction must be +1/-1")
    if (direction > 0 and stop >= entry) or (direction < 0 and stop <= entry):
        raise ValueError("stop must be on the adverse side of entry")
    long = direction > 0
    best = 0.0
    t_best: float | None = None
    bars = 0
    complete = False
    stopped = False
    t_stop: float | None = None
    for s in steps:
        if s.ts >= horizon_ts:
            complete = True
            break
        t = max((s.ts - entry_ts).total_seconds(), 0.0)
        bars += 1
        fav = ((s.hi - entry) if long else (entry - s.lo)) / risk
        if fav > best:
            best, t_best = fav, t
        if not stopped and ((s.lo <= stop) if long else (s.hi >= stop)):
            stopped, t_stop = True, t
    return OracleExcursion(best, t_best, bars, complete, stopped, t_stop)


def capture_fields(available_mfe_r: float, realised_r: float | None) -> dict[str, Any]:
    """realised vs available: ``realized_capture_r`` (the realised R), signed ``capture_ratio`` = realised / available
    (undefined below ``MIN_AVAILABLE_FOR_CAPTURE``), ``giveback_from_available_mfe`` = max(0, available - realised)."""
    if realised_r is None:
        return {"realized_capture_r": None, "capture_ratio": None, "capture_ratio_floored": None, "giveback_from_available_mfe": None}
    ok = available_mfe_r >= MIN_AVAILABLE_FOR_CAPTURE
    return {
        "realized_capture_r": realised_r,
        "capture_ratio": (realised_r / available_mfe_r) if ok else None,
        "capture_ratio_floored": min(1.0, max(0.0, realised_r) / available_mfe_r) if ok else None,
        "giveback_from_available_mfe": max(0.0, available_mfe_r - realised_r),
    }


def opportunity_bucket(available_mfe_r: float) -> str:
    if available_mfe_r < SMALL_AVAILABLE_R:
        return "LITTLE_OPPORTUNITY"
    if available_mfe_r >= LARGE_AVAILABLE_R:
        return "LARGE_OPPORTUNITY"
    return "MODERATE_OPPORTUNITY"


# ---- zero-drift reference -------------------------------------------------------------------------
def null_reach_probability(level_r: float, *, risk: float, sigma_bar: float, n_bars: int) -> float:
    """P(running maximum of a driftless Brownian path >= level_r * risk within ``n_bars`` bars of per-bar std ``sigma_bar``)
    = 2 * (1 - Phi(a / (sigma sqrt(n)))) = erfc(a / (sigma sqrt(2 n))).  The stop does NOT truncate it (available MFE is
    stop-independent).  Continuous-path reflection principle; bar-high monitoring is close to continuous, discrete
    close-only monitoring would sit slightly lower."""
    if level_r <= 0:
        return 1.0
    if n_bars <= 0 or not sigma_bar > 0:
        return 0.0
    return math.erfc(level_r * risk / (sigma_bar * math.sqrt(2.0 * n_bars)))


def simulate_null_reach(
    levels: Sequence[float], *, risk: float, sigma_bar: float, n_bars: int, n_paths: int = NULL_N_PATHS, seed: int = NULL_SEED,
) -> dict[float, float]:
    """Seeded Monte-Carlo cross-check of ``null_reach_probability`` (Gaussian random walk, close-to-close steps)."""
    rng = np.random.default_rng(seed)
    if n_bars <= 0:
        return {x: 0.0 for x in levels}
    paths = np.cumsum(rng.normal(0.0, sigma_bar, size=(n_paths, n_bars)), axis=1)
    peak = np.maximum(paths.max(axis=1), 0.0) / risk
    return {x: float(np.mean(peak >= x)) for x in levels}


def causal_sigma_bar(close: np.ndarray, day: np.ndarray, contig_next: np.ndarray, entry_idx: int) -> float | None:
    """Std of one-bar close changes over the ``SIGMA_LOOKBACK_BARS`` closed bars BEFORE the entry bar (same-day contiguous
    pairs only, so overnight gaps do not inflate it); None when fewer than ``SIGMA_MIN_DIFFS`` pairs exist."""
    lo = max(0, entry_idx - SIGMA_LOOKBACK_BARS)
    k = np.arange(lo, entry_idx - 1)
    if len(k) == 0:
        return None
    ok = contig_next[k] & (day[k] == day[k + 1])
    diffs = (close[k + 1] - close[k])[ok]
    diffs = diffs[np.isfinite(diffs)]
    if len(diffs) < SIGMA_MIN_DIFFS:
        return None
    sd = float(np.std(diffs, ddof=1))
    return sd if sd > 0 else None


# ---- entry rows -----------------------------------------------------------------------------------
def build_oracle_rows(mi: Any, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Oracle fields for the Lane X entry rows of one market (``coverage_analysis.entry_exit.build_entry_rows``)."""
    d = mi.data
    ts = list(pd.DatetimeIndex(mi.frame["ts"]).to_pydatetime())
    out: list[dict[str, Any]] = []
    for r in rows:
        direction = int(r["direction"])
        j, end = int(r["entry_idx"]), int(r["end_idx"])
        steps = _steps(d, ts, j, end, direction > 0)
        o = available_mfe(
            direction=direction, entry=float(r["fill"]), stop=float(r["stop"]), steps=steps, entry_ts=r["signal_ts"], horizon_ts=r["flat_utc"],
        )
        risk = abs(float(r["fill"]) - float(r["stop"]))
        sigma = causal_sigma_bar(d.c, d.day, d.contig_next, j)
        null_p = None if sigma is None else {_key(x): null_reach_probability(x, risk=risk, sigma_bar=sigma, n_bars=o.bars) for x in LEVELS_R}
        realised = {p: r["policy_r"].get(p) for p in REPORT_POLICIES}
        new = {k: r[k] for k in ("entry_id", "market", "family", "variant", "strategy_id", "direction", "signal_ts", "structure_event_id", "flat_utc", "baseline_r") if k in r}
        new.update(
            available_mfe_r=o.available_mfe_r, time_to_available_mfe_s=o.time_to_available_mfe_s, horizon_bars=o.bars,
            horizon_complete=o.horizon_complete, stop_touched_before_horizon_end=o.stop_touched, time_to_stop_s=o.time_to_stop_s,
            sigma_bar=sigma, risk_px=risk, null_p=null_p, realised=realised, lane_x_mfe_r=r.get("mfe_r"),
        )
        out.append(new)
    return out


# ---- aggregation ----------------------------------------------------------------------------------
def _mean(xs: Sequence[float]) -> float | None:
    return (sum(xs) / len(xs)) if xs else None


def _median(xs: Sequence[float]) -> float | None:
    return float(np.median(xs)) if xs else None


def _cell(rows: Sequence[Mapping[str, Any]], clusters: Sequence[str]) -> dict[str, Any]:
    n = len(rows)
    out: dict[str, Any] = {"n": n, "n_event_clusters": len(set(clusters)), "small_n": n < SMALL_N, "oracle_version": ORACLE_VERSION}
    if not n:
        return out
    av = [float(r["available_mfe_r"]) for r in rows]
    out["available_mfe_mean"] = _mean(av)
    out["available_mfe_median"] = _median(av)
    out["available_mfe_mean_cluster_equal"] = cluster_equal_mean(av, clusters)
    out["p_available_ge"] = {_key(x): sum(1 for a in av if a >= x) / n for x in LEVELS_R}
    with_null = [r["null_p"] for r in rows if r.get("null_p")]
    out["n_null"] = len(with_null)
    if with_null:
        nm = {_key(x): sum(p[_key(x)] for p in with_null) / len(with_null) for x in LEVELS_R}
        out["null_p_available_ge"] = nm
        out["enrichment_vs_null"] = {k: out["p_available_ge"][k] - v for k, v in nm.items()}
    else:
        out["null_p_available_ge"], out["enrichment_vs_null"] = None, None
    tt = [float(r["time_to_available_mfe_s"]) for r in rows if r.get("time_to_available_mfe_s") is not None]
    out["time_to_available_mfe_median_s"] = _median(tt)
    out["horizon_complete_share"] = sum(1 for r in rows if r.get("horizon_complete")) / n
    out["stop_touched_share"] = sum(1 for r in rows if r.get("stop_touched_before_horizon_end")) / n
    out["horizon_bars_median"] = _median([float(r["horizon_bars"]) for r in rows])
    pols: dict[str, Any] = {}
    contrast: dict[str, Any] = {}
    for p in REPORT_POLICIES:
        pairs = [(float(r["available_mfe_r"]), float(r["realised"][p])) for r in rows if r["realised"].get(p) is not None]
        if not pairs:
            continue
        caps = [capture_fields(a, x) for a, x in pairs]
        floored = [c["capture_ratio_floored"] for c in caps if c["capture_ratio_floored"] is not None]
        signed = [c["capture_ratio"] for c in caps if c["capture_ratio"] is not None]
        pols[p] = {
            "n": len(pairs), "realized_r_mean": _mean([x for _, x in pairs]),
            "capture_ratio_median": _median(signed), "capture_ratio_floored_mean": _mean(floored),
            "giveback_mean": _mean([c["giveback_from_available_mfe"] for c in caps]),
        }
        tab: dict[str, dict[str, Any]] = {b: {"n": 0, "loss": 0, "win": 0, "realized_r_mean": None} for b in ("LITTLE_OPPORTUNITY", "MODERATE_OPPORTUNITY", "LARGE_OPPORTUNITY")}
        acc: dict[str, list[float]] = defaultdict(list)
        for a, x in pairs:
            b = opportunity_bucket(a)
            tab[b]["n"] += 1
            tab[b]["loss" if x <= 0 else "win"] += 1
            acc[b].append(x)
        for b, v in acc.items():
            tab[b]["realized_r_mean"] = _mean(v)
        tab["LITTLE_OPPORTUNITY"]["reading"] = "entry had little favourable opportunity (loss here is not a capture problem)"
        tab["LARGE_OPPORTUNITY"]["reading"] = "large favourable excursion on the table; a loss here is a profit-capture problem (hypothesis, hindsight)"
        contrast[p] = tab
    out["policies"] = pols
    out["contrast"] = contrast
    return out


def aggregate_oracle(rows: Sequence[Mapping[str, Any]], *, min_cell: int = SMALL_N) -> dict[str, Any]:
    """market x family:variant x direction."""
    rows = list(rows)
    clusters = assign_event_clusters(rows)
    by_fv: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        by_fv[f"{r['family']}:{r['variant']}" if r.get("variant") else str(r["family"])].append(i)
    out: dict[str, Any] = {"all": _cell(rows, clusters), "families": {}, "min_cell": min_cell}
    for fv, idxs in sorted(by_fv.items()):
        sub = [rows[i] for i in idxs]
        sc = [clusters[i] for i in idxs]
        cell: dict[str, Any] = {"all": _cell(sub, sc), "by_direction": {}}
        for name, key in (("long", 1), ("short", -1)):
            m = [(r, c) for r, c in zip(sub, sc, strict=True) if int(r["direction"]) == key]
            cell["by_direction"][name] = _cell([r for r, _ in m], [c for _, c in m])
        out["families"][fv] = cell
    return out


# ---- rendering ------------------------------------------------------------------------------------
def _f(x: Any, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return "nan" if math.isnan(x) else f"{x:.{nd}f}"
    return str(x)


def oracle_meta(notes: Sequence[str], caveats: Sequence[str]) -> dict[str, Any]:
    return {
        "oracle_version": ORACLE_VERSION, "label": ORACLE_LABEL, "live_use": ORACLE_LIVE_USE,
        "levels_r": list(LEVELS_R), "small_available_r": SMALL_AVAILABLE_R, "large_available_r": LARGE_AVAILABLE_R,
        "min_available_for_capture_r": MIN_AVAILABLE_FOR_CAPTURE, "null_reference": "zero-drift Brownian reflection, per-entry risk / horizon bars / causal pre-entry sigma",
        "null_seed": NULL_SEED, "sigma_lookback_bars": SIGMA_LOOKBACK_BARS, "small_n": SMALL_N,
        "horizon": "live operating-policy effective forced-flat instant (earliest of market flat, Berlin 21:55 flatten start, broker close - buffer); frozen family specs carry no own thesis horizon",
        "report_policies": list(REPORT_POLICIES), "data_notes": list(notes), "caveats": list(caveats),
    }


def _row_md(label: str, c: Mapping[str, Any]) -> str:
    p = c.get("p_available_ge") or {}
    nl = c.get("null_p_available_ge") or {}
    cells = " | ".join(f"{_f(p.get(_key(x)))} / {_f(nl.get(_key(x)))}" for x in LEVELS_R)
    flag = " (n small)" if c.get("small_n") else ""
    return f"| {label} | {c['n']} ({c['n_event_clusters']}){flag} | {_f(c.get('available_mfe_median'))} / {_f(c.get('available_mfe_mean'))} | {cells} |"


def render_oracle_market_md(market: str, res: Mapping[str, Any], data_note: str, excl: Mapping[str, int]) -> str:
    L = [f"## {market}", "", f"`{ORACLE_LABEL}` / `{ORACLE_LIVE_USE}` / `{ORACLE_VERSION}`", "", data_note, "", f"Exclusions / counters: `{json.dumps(dict(excl), sort_keys=True)}`", ""]
    if not res.get("families"):
        return "\n".join([*L, "No analysable entries.", ""])
    head = "| cell | n (clusters) | available MFE R median / mean | " + " | ".join(f"P>={_key(x)}R emp / null" for x in LEVELS_R) + " |"
    sep = "|---|---|---|" + "---|" * len(LEVELS_R)
    L += ["### Distribution of available_mfe_r (empirical / zero-drift null)", "", head, sep, _row_md("ALL", res["all"])]
    for fv, cell in res["families"].items():
        L.append(_row_md(fv, cell["all"]))
        for name in ("long", "short"):
            if cell["by_direction"][name]["n"]:
                L.append(_row_md(f"{fv} {name}", cell["by_direction"][name]))
    L += ["", "### Capture vs available MFE and the contrast table (fixed_1_5r baseline; structural shadow policies where applicable)", "",
          "| cell | policy | n | realised R | capture (floored) | giveback R | little-opp: loss/n | large-opp: loss/win (n) |", "|---|---|---|---|---|---|---|---|"]
    for fv, cell in res["families"].items():
        c = cell["all"]
        for p, s in c.get("policies", {}).items():
            tab = c["contrast"][p]
            lo, la = tab["LITTLE_OPPORTUNITY"], tab["LARGE_OPPORTUNITY"]
            L.append(f"| {fv} | {p} | {s['n']} | {_f(s['realized_r_mean'])} | {_f(s['capture_ratio_floored_mean'])} | {_f(s['giveback_mean'])} | {lo['loss']}/{lo['n']} | {la['loss']}/{la['win']} ({la['n']}) |")
    L.append("")
    return "\n".join(L)


def render_oracle_markdown(result: Mapping[str, Any]) -> str:
    meta = result["meta"]
    L = [
        "# Lane X2 - Oracle available_mfe_r diagnostic",
        "",
        f"`{meta['label']}` / `{meta['live_use']}` / `{meta['oracle_version']}`",
        "",
        "**HINDSIGHT yardstick, not tradable foresight, no edge claim, no promotion.** It uses the bars AFTER each entry (up to the horizon) and must never enter the live decision path. "
        "Lane X's headline (most cells = useful entry + poor capture) is NOT an edge proof: a driftless path reaches +0.5R before -1R about two thirds of the time. "
        "Use the raw diagnostics, and read every empirical P(available_mfe >= x) NEXT TO the zero-drift null column of the same entries; label counts must not be used to promote strategies.",
        "",
        "## Definition",
        "",
        "- `available_mfe_r` = maximum favourable excursion (exit-side bar highs for longs / lows for shorts) from the entry to the horizon, in R of the entry's initial risk |fill - stop|; INDEPENDENT of the stop-out (keeps counting after the stop would have been hit).",
        f"- Horizon: {meta['horizon']}. Documented identically for every family (no family carries a thesis horizon).",
        "- Realised side: fixed_1_5r baseline (`P1_FIXED_1_5R`) and the Lane X structural shadow policies (`P2_STRUCT_TP1`, `P3_STRUCT_TP1_TP2`, `P4_TP1_TP2_RUNNER`) on the SAME entries; `capture_ratio` = realised / available (signed; undefined below "
        f"{meta['min_available_for_capture_r']}R), `giveback_from_available_mfe` = max(0, available - realised).",
        f"- Null reference: P(max of a driftless Brownian path >= x R within the entry's own horizon bars) = erfc(x*risk / (sigma*sqrt(2 n))), sigma = std of one-bar close changes over the {meta['sigma_lookback_bars']} closed bars BEFORE the entry (same-day pairs); averaged over the cell's entries. A seeded simulation (seed {meta['null_seed']}) cross-checks the analytic form in the tests. Real markets have fat tails / volatility clustering, so the null is a yardstick, not an exact law.",
        f"- Contrast buckets (predeclared): LITTLE_OPPORTUNITY = available < {meta['small_available_r']}R; LARGE_OPPORTUNITY = available >= {meta['large_available_r']}R; loss = realised <= 0.",
        "",
    ]
    heads = ["| market | n | median available R | P(avail>=1R) emp | P(avail>=1R) null | enrichment |", "|---|---|---|---|---|---|"]
    rows_md = []
    for m, mres in result["markets"].items():
        c = (mres.get("result") or {}).get("all") or {}
        if not c.get("n"):
            rows_md.append(f"| {m} | 0 | - | - | - | - |")
            continue
        e = (c.get("p_available_ge") or {}).get("1")
        nl = (c.get("null_p_available_ge") or {}).get("1")
        rows_md.append(f"| {m} | {c['n']} | {_f(c['available_mfe_median'])} | {_f(e)} | {_f(nl)} | {_f(None if e is None or nl is None else e - nl)} |")
    L += ["## Headline per market", "", *heads, *rows_md, ""]
    for mres in result["markets"].values():
        L.append(mres.get("markdown", ""))
    L += ["## Data used and missing", ""] + [f"- {x}" for x in meta["data_notes"]]
    L += ["", "## Caveats", ""] + [f"- {x}" for x in meta["caveats"]] + [""]
    return "\n".join(L)


def to_oracle_json(result: Mapping[str, Any]) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, (datetime, pd.Timestamp)):
            return o.isoformat()
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        return str(o)

    slim = {"meta": result["meta"], "markets": {m: {k: v for k, v in mres.items() if k != "markdown"} for m, mres in result["markets"].items()}}
    return json.dumps(slim, indent=1, sort_keys=True, default=default)
