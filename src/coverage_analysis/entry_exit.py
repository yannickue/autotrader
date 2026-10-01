# ruff: noqa: E501
"""Lane X offline study: entry quality vs exit quality, and the SAME-entry exit-policy shadow comparison.

HINDSIGHT DIAGNOSTICS on development frames (nothing after the 2026-08-31 Berlin date is ever opened: the existing
``load_dev_market_frame`` / ``dev_frame`` / ``build_family_data`` holdout guards stay in force).  No edge claim: the
numbers describe what the frozen families' entries offered (MFE / MAE / timing) and what predeclared causal exit
policies would have captured on the SAME entries.  Nothing is fitted, searched or promoted; results are hypotheses and
forward evidence decides.

Reuse (no second engine): the frozen production spec + family generators (``alpha.families``), the Lane N frame /
leader handling, the E2 structure geometry (``demo.structure``), the ExitEngine harness ``demo.exit_policies``, the
Lane P operating policy (flat deadline / entry runway), the existing cluster config ``risk_policy.cluster_of``.

Entry reconstruction (mirrors ``alpha.fast.sim``): a candidate decided at the close of bar ``i`` fills at the open of
bar ``i+1`` (long: ask = open + max(spread_i, spread_i+1); short: bid = open); candidates whose stop is not on the
loss side of the fill are dropped (``entry_gap``); the live operating policy then drops entries on non-operating days,
inside the flatten window or without the minimum runway to the effective flat instant.  Every surviving candidate is
an independent hypothetical entry (no one-position-at-a-time filter): overlap is handled by the event clusters.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from alpha.families.data import FamilyData
from alpha.families.registry import describe_candidate, generate_candidates
from demo import structure as st
from demo.entry_exit_quality import (
    ANALYSIS_VERSION,
    BAR_SECONDS,
    CLUSTER_WINDOW_S,
    CROSS_MARKET_WINDOW_S,
    DEFAULT_THRESHOLDS,
    LABEL_SPEC_VERSION,
    MIN_CLUSTERS,
    RANDOM_WALK_REFERENCE,
    SMALL_N,
    Step,
    assign_event_clusters,
    assign_market_clusters,
    capture_ratio,
    entry_exit_fields,
    summarise_entries,
)
from demo.exit_policies import (
    P_FIXED,
    POLICY_IDS,
    POLICY_PARAMS,
    POLICY_SET_VERSION,
    STRUCTURAL_POLICIES,
    BarSeries,
    EntryInput,
    simulate_all,
)
from demo.opportunity.operating_policy import OperatingPolicy

STUDY_VERSION = "eeq-study-1"
HISTORY_BARS = 120  # closed M5 bars of structure history (production ExitPlanConfig.bars)
PRE_BARS = 6  # closed bars before the entry handed to the management cache (momentum / swing context)
SPREAD_LOW, SPREAD_HIGH = 0.05, 0.15  # spread / initial risk cost-state buckets


@dataclass(frozen=True, slots=True)
class MarketInputs:
    """Everything needed to analyse one market frame (any market, including shadow)."""

    market: str
    data: FamilyData
    frame: pd.DataFrame  # ts (UTC), open, high, low, close - the SAME bars the FamilyData was built from
    specs: Sequence[Any]  # FrozenSpec-like: .spec .thr .strategy_id .family .role
    tick_size: float
    eval_from: pd.Timestamp  # first decision time counted (earlier bars are warm-up)
    tz: str


# ---- helpers ------------------------------------------------------------------------------------------
def _to_utc(ts: pd.Timestamp | datetime) -> datetime:
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.to_pydatetime()


def market_flat_utc(at: datetime, tz: str, flat_min: int) -> datetime:
    """UTC instant of the market-local flat minute on the local day of ``at``."""
    zone = ZoneInfo(tz)
    day = at.astimezone(zone).date()
    naive = datetime.combine(day, time(0, 0)) + timedelta(minutes=flat_min)
    return naive.replace(tzinfo=zone).astimezone(ZoneInfo("UTC"))


def session_bucket(minute: int, cal: Any, berlin_hour: int) -> str:
    """Generic, documented session bucket of the ENTRY bar (local minute of day)."""
    span = cal.cash_close_min - cal.cash_open_min
    if span >= 1440:  # 24/7 style calendar: four Berlin-time blocks
        return ("NIGHT_00_06", "MORNING_06_12", "AFTERNOON_12_18", "EVENING_18_24")[berlin_hour // 6]
    if cal.cash_open_min <= minute < cal.cash_open_min + 90:
        return "OPEN_90M"
    if cal.cash_close_min - 90 <= minute < cal.cash_close_min:
        return "CLOSE_90M"
    if cal.cash_open_min <= minute < cal.cash_close_min:
        return "MIDDAY"
    return "OFF_CASH"


def spread_bucket(spread_over_risk: float) -> str:
    return "LOW" if spread_over_risk < SPREAD_LOW else ("MID" if spread_over_risk < SPREAD_HIGH else "HIGH")


def _regime_edges(data: FamilyData, eval_start_idx: int) -> tuple[float, float] | None:
    rel = (data.atr / data.c)[:eval_start_idx]
    rel = rel[np.isfinite(rel)]
    if len(rel) < 200:
        return None
    return float(np.quantile(rel, 1 / 3)), float(np.quantile(rel, 2 / 3))


def _structure_levels(direction: int, entry: float, hist: pd.DataFrame) -> list[float]:
    """Confirmed swing highs (long) / lows (short) and the prior range edge beyond the entry (prices, nearest first)."""
    want = "HIGH" if direction == 1 else "LOW"
    prices = [s.price for s in st.all_swings(hist, 2) if s.kind == want]
    edges = st.prior_range_edges(hist, 24)
    if edges is not None:
        prices.append(float(edges[0].price if direction == 1 else edges[1].price))
    beyond = sorted({p for p in prices if ((p > entry) if direction == 1 else (p < entry))}, key=lambda p: abs(p - entry))
    return beyond


def _steps(d: FamilyData, ts: Sequence[datetime], lo: int, hi: int, long: bool) -> list[Step]:
    out = []
    for k in range(lo, hi):
        sp = 0.0 if long else float(d.spread[k])
        out.append(Step(ts[k], float(d.o[k]) + sp, float(d.h[k]) + sp, float(d.l[k]) + sp, float(d.c[k]) + sp))
    return out


def _series(d: FamilyData, ts: Sequence[datetime], lo: int, hi: int) -> BarSeries:
    sl = slice(lo, hi)
    return BarSeries(
        tuple(ts[lo:hi]), tuple(float(x) for x in d.o[sl]), tuple(float(x) for x in d.h[sl]),
        tuple(float(x) for x in d.l[sl]), tuple(float(x) for x in d.c[sl]), tuple(float(x) for x in d.spread[sl]),
    )


# ---- entry set ----------------------------------------------------------------------------------------
def build_entry_rows(mi: MarketInputs, op: OperatingPolicy, *, limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """All analysed entries of one market (one row per candidate), plus exclusion counters (never silent)."""
    d = mi.data
    n = len(d)
    ts_index = pd.DatetimeIndex(mi.frame["ts"])
    ts = list(ts_index.to_pydatetime())
    eval_idx = int(ts_index.searchsorted(mi.eval_from))
    edges = _regime_edges(d, eval_idx)
    hist_cols = mi.frame[["ts", "open", "high", "low", "close"]].reset_index(drop=True)
    excl: dict[str, int] = defaultdict(int)
    rows: list[dict[str, Any]] = []
    for fs in mi.specs:
        spec = fs.spec
        cands = generate_candidates(d, spec, fs.thr)
        for ci in range(len(cands.decision_idx)):
            i = int(cands.decision_idx[ci])
            j = i + 1
            if i < eval_idx:
                continue
            excl["candidates"] += 1
            if j >= n or not d.contig_next[i] or d.day[j] != d.day[i]:
                excl["no_contiguous_entry_bar"] += 1
                continue
            direction = int(cands.direction[ci])
            long = direction > 0
            stop = float(cands.stop[ci])
            if not math.isfinite(stop):
                excl["no_stop"] += 1
                continue
            e_sp = float(max(d.spread[i], d.spread[j]))
            fill = float(d.o[j]) + e_sp if long else float(d.o[j])
            risk = (fill - stop) if long else (stop - fill)
            if not risk > 0:
                excl["entry_gap_stop"] += 1
                continue
            signal = ts[j]
            if not op.is_operating_day(signal):
                excl["policy_non_operating_day"] += 1
                continue
            # A full-day calendar (flat_min >= 1440, e.g. the offline BTC / Brent research calendars) has NO market-local flat:
            # the Berlin flatten start and the broker-session buffer of the live operating policy decide alone.
            mkt_flat = signal + timedelta(days=2) if d.cal.flat_min >= 1440 else market_flat_utc(signal, mi.tz, d.cal.flat_min)
            flat = op.effective_flat_utc(mi.market, mkt_flat, signal)
            refusal = op.entry_refusal(mi.market, signal, flat)
            if refusal is not None:
                excl[f"policy_{refusal}"] += 1
                continue
            end = j
            while end < n and ts[end] < flat and d.contig_next[end]:
                end += 1
            if end < n and ts[end] >= flat:
                end += 1  # include the first bar at/after the flat instant: it carries the forced-flat fill
            end = min(end, n)
            if end - j < 1:
                excl["no_post_entry_bars"] += 1
                continue
            atr = float(d.atr[i])
            hist = hist_cols.iloc[max(0, i - HISTORY_BARS + 1): i + 1]
            try:
                geo = st.structural_geometry(direction, fill, hist, e_sp, atr, cost=e_sp, tick_size=mi.tick_size)
            except ValueError:
                geo = None
            tp1 = None if geo is None or geo.tp1 is None else float(geo.tp1.price)
            tp2 = None if geo is None or geo.tp2 is None else float(geo.tp2.price)
            levels = _structure_levels(direction, fill, hist)
            bars = _series(d, ts, j, end)
            pre = _series(d, ts, max(0, j - PRE_BARS), j) if j >= 3 else None
            ev = describe_candidate(d, spec, i, direction) if fs.family == "STRUCT" else {}
            event_id = None
            if ev and ev.get("break_bar_offset") is not None:
                event_id = f"{mi.market}:{direction}:{ts[i - int(ev['break_bar_offset'])].isoformat()}"
            entry_id = f"{mi.market}:{fs.strategy_id}:{direction}:{signal.isoformat()}"
            ei = EntryInput(
                entry_id=entry_id, market=mi.market, direction=direction, fill=fill, stop=stop, entry_ts=signal, atr=atr,
                tp1=tp1, tp2=tp2, tp1_id=geo.tp1.structure_id if geo and geo.tp1 else "tp1",
                tp2_id=geo.tp2.structure_id if geo and geo.tp2 else "tp2", flat_utc=flat, pre=pre,
            )
            pol = simulate_all(ei, bars)
            base = pol[P_FIXED]
            baseline_r = base.r
            fields = entry_exit_fields(
                direction=direction, entry=fill, stop=stop, steps=_steps(d, ts, j, end, long), entry_ts=signal,
                final_r=baseline_r if baseline_r is not None else 0.0, apply_stop=True, flat_ts=flat,
                tp1=tp1, tp2=tp2, structure_levels=levels,
            )
            mfe = fields["mfe_r"]
            rel = atr / float(d.c[i]) if d.c[i] else float("nan")
            regime = "UNKNOWN" if edges is None or not math.isfinite(rel) else ("LOW_VOL" if rel < edges[0] else ("MID_VOL" if rel < edges[1] else "HIGH_VOL"))
            berlin_hour = signal.astimezone(ZoneInfo("Europe/Berlin")).hour
            row = {
                "entry_id": entry_id, "market": mi.market, "family": fs.family, "variant": str(getattr(spec, "mode", "")),
                "strategy_id": fs.strategy_id, "role": getattr(fs, "role", "PRIMARY"), "direction": direction,
                "signal_ts": signal, "structure_event_id": event_id, "hour_berlin": berlin_hour,
                "session": session_bucket(int(d.minute[j]), d.cal, berlin_hour), "regime": regime,
                "risk_atr": risk / atr if atr > 0 else None, "spread_over_risk": e_sp / risk,
                "spread_state": spread_bucket(e_sp / risk), "signal_age_s": 0.0,
                "holding_baseline_s": base.holding_s, "flat_utc": flat, "tp1_available": tp1 is not None,
                "tp2_available": tp2 is not None, "baseline_r": baseline_r,
                "baseline_exit": base.final_reason, "baseline_censored": base.censored,
                "policy_r": {p: r.r for p, r in pol.items()},
                "policy_exit": {p: r.final_reason for p, r in pol.items()},
                "policy_na": {p: r.na_reason for p, r in pol.items() if not r.applicable},
                "policy_capture": {p: (capture_ratio(mfe, r.r)[1] if r.r is not None else None) for p, r in pol.items()},
                **fields,
            }
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                return rows, dict(excl)
    excl["analysed"] = len(rows)
    return rows, dict(excl)


# ---- aggregation --------------------------------------------------------------------------------------
def _cell(rows: Sequence[Mapping[str, Any]], clusters: Sequence[str]) -> dict[str, Any]:
    out = summarise_entries(rows, event_clusters=clusters)
    # paired subset: entries on which EVERY structural policy applies (same entries for every policy in the pair)
    paired = [(r, c) for r, c in zip(rows, clusters, strict=True) if all(r["policy_r"].get(p) is not None for p in STRUCTURAL_POLICIES)]
    out["paired_structural_subset"] = {
        "n": len(paired), "n_event_clusters": len({c for _, c in paired}),
        "mean_r": {p: (sum(r["policy_r"][p] for r, _ in paired) / len(paired)) if paired else None for p in POLICY_IDS if all(r["policy_r"].get(p) is not None for r, _ in paired)} if paired else {},
    }
    out["tp1_available_share"] = (sum(1 for r in rows if r["tp1_available"]) / len(rows)) if rows else None
    out["tp2_available_share"] = (sum(1 for r in rows if r["tp2_available"]) / len(rows)) if rows else None
    out["censored_baseline_share"] = (sum(1 for r in rows if r["baseline_censored"]) / len(rows)) if rows else None
    return out


def aggregate_market(rows: list[dict[str, Any]], *, min_cell: int = SMALL_N) -> dict[str, Any]:
    """market x family:variant x direction (x session / regime where n >= ``min_cell``)."""
    clusters = assign_event_clusters(rows)
    by_fv: dict[str, list[int]] = defaultdict(list)
    for idx, r in enumerate(rows):
        by_fv[f"{r['family']}:{r['variant']}" if r["variant"] else r["family"]].append(idx)
    out: dict[str, Any] = {"all": _cell(rows, clusters), "families": {}}
    for fv, idxs in sorted(by_fv.items()):
        sub = [rows[i] for i in idxs]
        sc = [clusters[i] for i in idxs]
        cell: dict[str, Any] = {"all": _cell(sub, sc), "by_direction": {}, "by_session": {}, "by_regime": {}, "by_spread_state": {}}
        for name, key in (("long", 1), ("short", -1)):
            m = [(r, c) for r, c in zip(sub, sc, strict=True) if r["direction"] == key]
            cell["by_direction"][name] = _cell([r for r, _ in m], [c for _, c in m])
        for field_name, bucket in (("session", "by_session"), ("regime", "by_regime"), ("spread_state", "by_spread_state")):
            groups: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
            for r, c in zip(sub, sc, strict=True):
                groups[str(r[field_name])].append((r, c))
            for g, m in sorted(groups.items()):
                if len(m) >= min_cell:
                    cell[bucket][g] = _cell([r for r, _ in m], [c for _, c in m])
                else:
                    cell[bucket][g] = {"n": len(m), "note": f"n < {min_cell}: not evaluated"}
        out["families"][fv] = cell
    return out


def market_cluster_rollup(all_rows: list[dict[str, Any]], cluster_of: Any) -> dict[str, Any]:
    """Raw entries vs event clusters vs cross-market clusters per market cluster (INDEX = GER40/NAS100/SPX500 ...)."""
    if not all_rows:
        return {}
    ev = assign_event_clusters(all_rows)
    mk = assign_market_clusters(all_rows, cluster_of)
    by: dict[str, dict[str, set[str] | int]] = defaultdict(lambda: {"raw": 0, "ev": set(), "mk": set()})
    for r, e, m in zip(all_rows, ev, mk, strict=True):
        name = str(cluster_of(r["market"]) or r["market"])
        b = by[name]
        b["raw"] += 1  # type: ignore[operator]
        b["ev"].add(e)  # type: ignore[union-attr]
        b["mk"].add(m)  # type: ignore[union-attr]
    return {k: {"raw_entries": v["raw"], "event_clusters": len(v["ev"]), "cross_market_clusters": len(v["mk"])} for k, v in sorted(by.items())}  # type: ignore[arg-type]


# ---- R2 store copy (real trades + labelled counterfactuals) -------------------------------------------
def r2_summary(db_copy: str) -> dict[str, Any]:
    """Entry-vs-exit classification of the R2 store copy, from the ALREADY STORED mfe / mae / final R (read-only)."""
    from coverage_analysis.r2 import _guard
    from demo.entry_exit_quality import classify
    from demo.store import DemoStore

    rows: list[dict[str, Any]] = []
    with DemoStore(_guard(db_copy)) as store:
        cf = {r["opportunity_id"]: r for r in store.counterfactual_rows(None)}
        for r in store.funnel_rows(None):
            if r.get("trade_type", "STRATEGY") != "STRATEGY" or r["direction"] is None:
                continue
            base = {
                "market": r["market"], "direction": int(r["direction"]), "family": str(r["family"] or ""),
                "signal_ts": datetime.fromisoformat(r["signal_ts"]),
            }
            if r["intent_id"] is not None and r["has_outcome"]:
                o = store.get_outcome(r["intent_id"])
                if o is not None:
                    c = classify(mfe_r=o.mfe_r, mae_r=o.mae_r, final_r=o.gross_r, time_to_mae_s=o.time_to_mae_s)
                    rows.append({**base, "kind": "REAL_TRADE", "mfe_r": o.mfe_r, "mae_r": o.mae_r, "time_to_mfe_s": o.time_to_mfe_s, "time_to_mae_s": o.time_to_mae_s, "baseline_r": o.gross_r, **c})
                    continue
            lab = cf.get(r["opportunity_id"])
            if lab is not None:
                full = store.get_counterfactual(r["opportunity_id"])
                c = classify(mfe_r=full.hypothetical_mfe_r, mae_r=full.hypothetical_mae_r, final_r=full.hypothetical_r)
                rows.append({**base, "kind": "COUNTERFACTUAL", "mfe_r": full.hypothetical_mfe_r, "mae_r": full.hypothetical_mae_r, "time_to_mfe_s": None, "time_to_mae_s": None, "baseline_r": full.hypothetical_r, **c})
    out: dict[str, Any] = {"n_rows": len(rows), "cells": {}, "note": "classification from stored MFE/MAE/R (fill assumptions differ: real = actual fills, counterfactual = intended entry, no costs); path-level fields exist only for trades recorded after the Lane X hook"}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[f"{r['kind']}|{r['market']}|{r['family']}|{'long' if r['direction'] > 0 else 'short'}"].append(r)
    for k, g in sorted(groups.items()):
        out["cells"][k] = summarise_entries(g, event_clusters=assign_event_clusters(g))
    return out


# ---- rendering ----------------------------------------------------------------------------------------
def _f(x: Any, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return "nan" if math.isnan(x) else f"{x:.{nd}f}"
    return str(x)


def _pol(c: Mapping[str, Any], p: str) -> str:
    s = c.get("policies", {}).get(p)
    return "-" if not s or s["mean_r"] is None else f"{s['mean_r']:+.2f}({s['n_applicable']})"


def render_market_md(market: str, res: Mapping[str, Any], data_note: str, excl: Mapping[str, int]) -> str:
    L = [f"## {market}", "", data_note, "", f"Exclusions / counters: `{json.dumps(dict(excl), sort_keys=True)}`", ""]
    if not res.get("families"):
        return "\n".join([*L, "No analysable entries.", ""])
    L += [
        "Entry quality (potential; R against the initial risk; MFE/MAE to the stop or the flat deadline):", "",
        "| family:variant | dir | n | clusters | flag | MFE mean/med | MAE mean/med | P(MFE>=.25/.5/.75/1/1.5/2) | MFE-first / MAE-first | giveback | capture (floored) | useful / failure share | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for fv, cell in res["families"].items():
        for dname, c in (("ALL", cell["all"]), ("long", cell["by_direction"]["long"]), ("short", cell["by_direction"]["short"])):
            if c["n"] == 0:
                continue
            p = c["p_mfe_ge"]
            L.append(
                f"| {fv} | {dname} | {c['n']} | {c['n_event_clusters']} | {c['flag'] if c['flag'] != 'ok' else ''} | {_f(c['mfe_mean'])}/{_f(c['mfe_median'])} | {_f(c['mae_mean'])}/{_f(c['mae_median'])} | "
                f"{'/'.join(_f(p[k], 2) for k in ('0.25', '0.5', '0.75', '1', '1.5', '2'))} | {_f(c['share_mfe_first'])} / {_f(c['share_mae_first'])} | {_f(c['mfe_giveback_mean'])} | {_f(c['capture_ratio_floored_mean'])} | {_f(c['useful_entry_share'])} / {_f(c['entry_failure_share'])} | {c['verdict']} |"
            )
    L += ["", "Exit-policy shadow comparison on the SAME entries (mean R, spread-adjusted gross; in brackets: entries the policy applies to; NA = no structural level):", "",
          "| family:variant | dir | n | " + " | ".join(p.split("_", 1)[0] + " " + p.split("_", 1)[1] for p in POLICY_IDS) + " | paired-structural n |",
          "|---|---|---|" + "---|" * (len(POLICY_IDS) + 1)]
    for fv, cell in res["families"].items():
        for dname, c in (("ALL", cell["all"]), ("long", cell["by_direction"]["long"]), ("short", cell["by_direction"]["short"])):
            if c["n"] == 0:
                continue
            L.append(f"| {fv} | {dname} | {c['n']} | " + " | ".join(_pol(c, p) for p in POLICY_IDS) + f" | {c['paired_structural_subset']['n']} |")
    L += ["", "Timing / structure / cost state (medians; times dated by bar open): ", "",
          "| family:variant | dir | n | t_MFE s | t_MAE s | t to 0.5R s | t to 1R s | bars above entry | max structure reached R | TP1 / TP2 reach | holding s | spread/1R | P1 exits |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for fv, cell in res["families"].items():
        c = cell["all"]
        if c["n"] == 0:
            continue
        tr = c["median_time_to_R_s"]
        ex = ", ".join(f"{k} {v:.2f}" for k, v in sorted(c["baseline_exit_shares"].items(), key=lambda kv: -kv[1])[:3])
        L.append(
            f"| {fv} | ALL | {c['n']} | {_f(c['median_time_to_mfe_s'], 0)} | {_f(c['median_time_to_mae_s'], 0)} | {_f(tr['0.5'], 0)} | {_f(tr['1'], 0)} | {_f(c['mean_share_bars_above_entry'])} | "
            f"{_f(c['median_max_structure_reached_r'])} | {_f(c['tp1_reach_share'])} / {_f(c['tp2_reach_share'])} | {_f(c['median_holding_s'], 0)} | {_f(c['spread_over_risk_median'], 3)} | {ex} |"
        )
    L += ["", "Profit capture per policy (mean floored capture ratio = share of the MFE kept; same entries):", "",
          "| family:variant | " + " | ".join(p.split("_", 1)[0] for p in POLICY_IDS) + " |", "|---|" + "---|" * len(POLICY_IDS)]
    for fv, cell in res["families"].items():
        c = cell["all"]
        if c["n"] == 0:
            continue
        L.append(f"| {fv} | " + " | ".join(_f((c["policies"].get(p) or {}).get("capture_ratio_floored_mean")) for p in POLICY_IDS) + " |")
    # session / regime where n permits
    extra = []
    for fv, cell in res["families"].items():
        for bucket in ("by_session", "by_regime"):
            for g, c in cell[bucket].items():
                if c.get("n", 0) >= SMALL_N and "verdict" in c:
                    extra.append(f"| {fv} | {bucket[3:]}={g} | {c['n']} | {c['n_event_clusters']} | {_f(c['mfe_median'])} | {_f(c['mae_median'])} | {_f(c['useful_entry_share'])} / {_f(c['entry_failure_share'])} | {_f(c['capture_ratio_floored_mean'])} | {_f(c['baseline_r_mean'], 2)} | {c['verdict']} |")
    if extra:
        L += ["", f"Session / regime cells with n >= {SMALL_N}:", "", "| family:variant | cell | n | clusters | MFE med | MAE med | useful / failure | capture | baseline R | verdict |", "|---|---|---|---|---|---|---|---|---|---|", *extra]
    L.append("")
    return "\n".join(L)


def render_markdown(result: Mapping[str, Any]) -> str:
    meta = result["meta"]
    th = meta["label_thresholds"]
    L = [
        "# Lane X - entry quality vs exit quality, and same-entry exit-policy shadow comparison",
        "",
        f"`{meta['analysis_version']}` / labels `{meta['label_spec_version']}` / policies `{meta['policy_set_version']}` / study `{meta['study_version']}`.",
        "",
        "**HINDSIGHT DIAGNOSTICS on development frames. No edge / expectancy claim. Nothing is fitted, searched or promoted; "
        "the exit-policy numbers are hypotheses - forward evidence decides promotion.** Dev window ends 2026-08-31 (Berlin date); "
        "no holdout bar was opened. BTCUSD / BRENT have no frozen Train/holdout split at all (all their history is development data, "
        "STRUCT constants are discovery placeholders).",
        "",
        "## Method (binding principle)",
        "",
        "Negative final R != bad entry. Entry quality (MFE / MAE / timing / structure reached) and exit / profit-capture quality "
        "(capture ratio, giveback, policy R) are classified and reported SEPARATELY; no single final-R number is used as an entry verdict.",
        "",
        f"- Labels (predeclared, not tuned): POTENTIAL_USEFUL_ENTRY = MFE >= {th['useful_mfe_r']}R; ENTRY_FAILURE = MFE < {th['failure_mfe_r']}R and MAE >= {th['failure_mae_r']}R; everything else AMBIGUOUS. "
        f"Capture (judged for useful entries only): GOOD if final_R/MFE >= {th['good_capture']}, else POOR_PROFIT_CAPTURE (= EXIT_GIVEBACK). "
        "Combined: GOOD_ENTRY_GOOD_CAPTURE | POTENTIAL_USEFUL_ENTRY_EXIT_GIVEBACK | ENTRY_FAILURE | AMBIGUOUS.",
        f"- MFE_CAPTURE_RATIO = final_R / MFE, defined only for MFE >= {th['min_mfe_for_capture']}R (else undefined: no favourable excursion), SIGNED (a +0.6R MFE that ends -1R is -1.67); the floored version clip(max(final,0)/MFE,0,1) is what the tables average. MFE_GIVEBACK = max(0, MFE - final_R).",
        "- 'final R' of the entry verdict = the production baseline (fixed 1.5R, spread-adjusted gross, stop-first, forced flat at the Berlin deadline). MFE / MAE are the POTENTIAL path: they run to the initial stop or the flat deadline and are NOT cut by the 1.5R target.",
        f"- Verdict categories (shares per cell; INCONCLUSIVE-n if n < {SMALL_N} or fewer than {MIN_CLUSTERS} independent event clusters): BAD ENTRIES = failure share >= {th['bad_entry_failure_share']}; USEFUL ENTRIES + BAD CAPTURE = useful share >= {th['useful_share_min']} and >= {th['poor_capture_share']} of the useful ones poorly captured; BOTH = both deficits (failure >= {th['bad_entry_failure_share']}, useful >= {th['both_useful_share_min']}, poor capture >= {th['poor_capture_share']}); else NO CLEAR DEFICIT.",
        f"- Dependence: event clusters = same market + same direction signals chain-linked within {int(CLUSTER_WINDOW_S)} s; STRUCT entries are keyed by their break (structure_event_id), so the four STRUCT variants of one break are ONE observation. Market clusters (existing risk-policy config: GER40/NAS100/SPX500 = INDEX, ...) chain same-direction signals within {int(CROSS_MARKET_WINDOW_S)} s across markets. Raw n is always shown next to clusters; means in JSON also exist cluster-equalised.",
        "- **NULL REFERENCE (read every share against it):** for a driftless path P(touch +x R before the -1R stop) = 1/(1+x): "
        + ", ".join(f"{k}R {v:.2f}" for k, v in meta["random_walk_reference_p_mfe_ge"].items())
        + ". With zero edge ~two thirds of entries are therefore 'useful' (MFE >= 0.5R) and ~25 % 'failures' by construction, and a 1.5R cap captures little of the MFE; the verdict categories classify cells against the PREDECLARED thresholds, they are NOT a test against this null and do not show that any entry is better than random. Compare the P(MFE>=x) columns with this line.",
        "- Timing is dated by the bar OPEN (existing convention); resolution 5 min. Entry fill = next bar open (long at ask); no latency / slippage / commission; signal age is 0 by construction offline (real trades record it).",
        "- Exit policies (same entry, same stop, same bars, stop-first, ratchets apply from the next bar; engine = existing `exits.ExitEngine` + E2 structure): " + "; ".join(f"`{k}`" for k in POLICY_IDS) + f". Parameters (versioned, not searched): `{json.dumps(POLICY_PARAMS, sort_keys=True)}`.",
        "- An entry without the required structural level is NOT_APPLICABLE for that policy (no level is invented); the `paired-structural n` column counts the entries on which P2, P3 and P4 all apply.",
        "",
    ]
    mc = result.get("market_clusters") or {}
    if mc:
        L += ["## Independence: raw entries vs event clusters vs cross-market clusters", "", "| market cluster | raw entries | event clusters | cross-market clusters |", "|---|---|---|---|"]
        L += [f"| {k} | {v['raw_entries']} | {v['event_clusters']} | {v['cross_market_clusters']} |" for k, v in mc.items()]
        L.append("")
    L += ["## Headline: verdict per market x family:variant (all directions)", "", "| market | family:variant | n (clusters) | useful / failure share | capture (floored) | baseline R | TP1+TP2 R | runner R | verdict |", "|---|---|---|---|---|---|---|---|---|"]
    for m, mres in result["markets"].items():
        for fv, cell in (mres.get("result") or {}).get("families", {}).items():
            c = cell["all"]
            L.append(f"| {m} | {fv} | {c['n']} ({c['n_event_clusters']}) | {_f(c['useful_entry_share'])} / {_f(c['entry_failure_share'])} | {_f(c['capture_ratio_floored_mean'])} | {_f(c['baseline_r_mean'])} | {_pol(c, 'P3_STRUCT_TP1_TP2')} | {_pol(c, 'P4_TP1_TP2_RUNNER')} | {c['verdict']} |")
    L.append("")
    for mres in result["markets"].values():
        L.append(mres["markdown"])
    if result.get("r2"):
        r2 = result["r2"]
        L += ["## R2 store copy (real trades / labelled counterfactuals)", "", f"{r2['n_rows']} rows. {r2['note']}", ""]
        if r2["cells"]:
            L += ["| cell | n | clusters | MFE med | MAE med | useful / failure | capture | verdict |", "|---|---|---|---|---|---|---|---|"]
            for k, c in r2["cells"].items():
                L.append(f"| {k} | {c['n']} | {c['n_event_clusters']} | {_f(c['mfe_median'])} | {_f(c['mae_median'])} | {_f(c['useful_entry_share'])} / {_f(c['entry_failure_share'])} | {_f(c['capture_ratio_floored_mean'])} | {c['verdict']} |")
        L.append("")
    L += ["## Data used and missing", ""]
    L += [f"- {x}" for x in result["meta"]["data_notes"]]
    L += ["", "## Caveats", ""]
    L += [f"- {x}" for x in result["meta"]["caveats"]]
    L.append("")
    return "\n".join(L)


def to_json(result: Mapping[str, Any]) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, (datetime, pd.Timestamp)):
            return o.isoformat()
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        if isinstance(o, Decimal):
            return str(o)
        return str(o)

    slim = {"meta": result["meta"], "market_clusters": result.get("market_clusters"), "r2": result.get("r2"), "markets": {}}
    for m, mres in result["markets"].items():
        slim["markets"][m] = {k: v for k, v in mres.items() if k != "markdown"}
    return json.dumps(slim, indent=1, sort_keys=True, default=default)


def meta_block(extra_notes: Sequence[str], caveats: Sequence[str]) -> dict[str, Any]:
    return {
        "analysis_version": ANALYSIS_VERSION, "label_spec_version": LABEL_SPEC_VERSION,
        "policy_set_version": POLICY_SET_VERSION, "study_version": STUDY_VERSION,
        "label_thresholds": DEFAULT_THRESHOLDS.as_dict(), "policy_params": POLICY_PARAMS,
        "random_walk_reference_p_mfe_ge": RANDOM_WALK_REFERENCE,
        "history_bars": HISTORY_BARS, "bar_seconds": BAR_SECONDS, "small_n": SMALL_N, "min_clusters": MIN_CLUSTERS,
        "data_notes": list(extra_notes), "caveats": list(caveats),
    }
