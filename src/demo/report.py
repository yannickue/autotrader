# ruff: noqa: E501
"""DEMO performance reports (markdown + json).

Honesty rules baked in:
  * EVERY closed trade of the selected phase is counted and listed; nothing is filtered or trimmed.
  * Small samples are stated plainly (`MIN_TRADES_FOR_INFERENCE`); the report never claims
    profitability or edge, whatever the numbers say. Discovery-phase data is flagged as non-holdout.
  * Rejected-setup EV comes from counterfactual labels that assume a fill at the intended entry with no
    slippage/fees -> an optimistic-for-fill, cost-free view, printed as such.
  * Cost convention: `ExecutionRecord.fees`/`.swap` are signed broker amounts (negative = cost);
    reported `costs_eur` = -(fees + swap) summed (positive = money paid).
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from demo.contracts import PHASES
from demo.entry_exit_quality import (
    ANALYSIS_VERSION as EEQ_VERSION,
)
from demo.entry_exit_quality import (
    LABEL_SPEC_VERSION as EEQ_LABEL_SPEC,
)
from demo.entry_exit_quality import (
    SMALL_N as EEQ_SMALL_N,
)
from demo.entry_exit_quality import (
    assign_event_clusters,
    classify,
    summarise_entries,
)
from demo.store import DemoStore, parse_utc

MILESTONES: tuple[int, ...] = (10, 25, 50, 100, 250, 500)
MIN_TRADES_FOR_INFERENCE = 100
LOW_N_GROUP = 30


# ---- milestones -------------------------------------------------------------------------------
def _ms_key(phase: str | None, m: int) -> str:
    return f"milestone_emitted:{phase or 'ALL'}:{m}"


def pending_milestone(store: DemoStore, n_trades: int, phase: str | None = None) -> int | None:
    """Highest milestone <= n_trades that has NOT been emitted yet (pure read)."""
    for m in sorted((m for m in MILESTONES if m <= n_trades), reverse=True):
        if store.get_meta(_ms_key(phase, m)) is None:
            return m
    return None


def should_emit_milestone(store: DemoStore, n_trades: int, phase: str | None = None) -> int | None:
    """Return the milestone to report now (or None) and persist the 'emitted' marker.

    If several milestones were crossed at once (e.g. 9 -> 26) only the highest is returned and the
    lower ones are marked as superseded. Lower markers are written first and the returned one last:
    a crash in between can re-emit that one milestone once, never silently lose it or emit twice.
    """
    m = pending_milestone(store, n_trades, phase)
    if m is None:
        return None
    stamp = datetime.now().astimezone().isoformat()
    for lower in (x for x in MILESTONES if x < m):
        store.set_meta_once(_ms_key(phase, lower), f"superseded@{stamp}")
    return m if store.set_meta_once(_ms_key(phase, m), f"emitted@{stamp}") else None


# ---- statistics -------------------------------------------------------------------------------
def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _stats(rs: list[float]) -> dict[str, Any]:
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    n = len(rs)
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "winrate": len(wins) / n if n else None,
        "expected_r": _mean(rs),
        "sum_r": sum(rs),
        "low_n": n < LOW_N_GROUP,
    }


def max_drawdown_r(rs: list[float]) -> float:
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def _spread_bucket(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    for cut, label in ((0.05, "<5% of risk"), (0.10, "5-10% of risk"), (0.20, "10-20% of risk")):
        if ratio < cut:
            return label
    return ">=20% of risk"


def _tercile_labeller(values: list[float | None]) -> Callable[[float | None], str]:
    nums = sorted(v for v in values if v is not None)
    if len(nums) < 3:
        return lambda v: "all" if v is not None else "unknown"
    c1, c2 = nums[len(nums) // 3], nums[2 * len(nums) // 3]

    def lab(v: float | None) -> str:
        if v is None:
            return "unknown"
        return (
            f"low(<{c1:g})" if v < c1 else (f"mid({c1:g}-{c2:g})" if v < c2 else f"high(>={c2:g})")
        )

    return lab


def _trade_rows(store: DemoStore, phase: str | None, kind: str = "strategy") -> list[dict[str, Any]]:
    rows = []
    for intent_id, opp_id, o in store.list_outcomes(phase, kind=kind):
        snap = store.get_snapshot(opp_id)
        ex = store.get_execution(intent_id)
        fees = (ex.fees or 0.0) if ex else 0.0
        swap = (ex.swap or 0.0) if ex else 0.0
        row: dict[str, Any] = {
            "intent_id": intent_id,
            "opportunity_id": opp_id,
            "closed_utc": o.closed_utc,
            "net_r": o.net_r,
            "gross_r": o.gross_r,
            "pnl_eur": o.pnl_eur,
            "mfe_r": o.mfe_r,
            "mae_r": o.mae_r,
            "holding_s": o.holding_s,
            "time_to_mfe_s": o.time_to_mfe_s,
            "exit_reason": o.exit_reason,
            "costs_eur": -(fees + swap),
            "slippage": ex.slippage if ex else None,
            "cost_status": ex.cost_status if ex else "missing_execution_record",
            "phase": snap.phase if snap else None,
        }
        if snap is not None:
            g, sig, clk = snap.geometry, snap.signal, snap.market_state.clock
            row.update(
                market=snap.market,
                direction="long" if snap.direction > 0 else "short",
                family=str(sig.get("family", "unknown")),
                hour=clk.local_minute // 60,
                session=clk.session_bucket,
                confluence=sig.get("confluence"),
                quality=sig.get("quality"),
                spread_ratio=(snap.market_state.spread / g.risk_distance)
                if g.risk_distance
                else None,
            )
        rows.append(row)
    return rows


def _group(
    rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any]
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        buckets[str(key(r))].append(r["net_r"])
    return {k: _stats(v) for k, v in sorted(buckets.items())}


def _execution_analytics(store: DemoStore, phase: str | None, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """TCA chain (decision -> arrival -> fill) and timing analytics of the STRATEGY trades."""
    ids = {r["intent_id"] for r in rows}
    tca = [t for t in store.list_tca(phase) if t["stage"] == "ENTRY" and t["intent_id"] in ids]
    extras = [e for e in (store.get_outcome_extra(i) for i in sorted(ids)) if e]

    def avg(seq: list[dict[str, Any]], k: str) -> float | None:
        return _mean([float(x[k]) for x in seq if x.get(k) is not None])

    def share(k: str) -> float | None:
        return (sum(1 for e in extras if e.get(k) is not None) / len(extras)) if extras else None

    missing: dict[str, int] = {}
    for t in tca:
        for k in t.get("tca_missing_fields", []) or []:
            missing[k] = missing.get(k, 0) + 1
    return {
        "tca": {
            "n": len(tca),
            "mean_decision_to_arrival_drift": avg(tca, "decision_to_arrival_drift"),
            "mean_decision_to_arrival_drift_r": avg(tca, "decision_to_arrival_drift_r"),
            "mean_implementation_shortfall": avg(tca, "implementation_shortfall"),
            "mean_implementation_shortfall_r": avg(tca, "implementation_shortfall_r"),
            "mean_movement_to_cost": avg(tca, "movement_to_cost"),
            "fields_missing_in_stack_events": missing,
        },
        "timing": {
            "n": len(extras),
            "mean_signal_age_at_fill_s": avg(extras, "signal_age_at_fill_s"),
            "share_reaching_0.25R": share("time_to_0.25R_s"),
            "share_reaching_0.5R": share("time_to_0.5R_s"),
            "share_reaching_1R": share("time_to_1R_s"),
            "mean_time_to_0.25R_s": avg(extras, "time_to_0.25R_s"),
            "mean_time_to_0.5R_s": avg(extras, "time_to_0.5R_s"),
            "mean_time_to_1R_s": avg(extras, "time_to_1R_s"),
            "mean_time_without_progress_s": avg(extras, "time_without_progress_s"),
            "mean_mfe_giveback_r": avg(extras, "mfe_giveback_r"),
            "resolution": "bar (5 min)",
        },
    }


def _eeq_row(base: dict[str, Any], *, mfe: float, mae: float, final_r: float, ee: dict[str, Any] | None, t_mae: float | None) -> dict[str, Any]:
    """One entry-vs-exit row: the stored path fields when present (Lane X hook), else classified from MFE / MAE / R alone."""
    cls = classify(mfe_r=mfe, mae_r=mae, final_r=final_r, time_to_mae_s=(ee or {}).get("time_to_mae_s", t_mae))
    row = {**base, "mfe_r": mfe, "mae_r": mae, "baseline_r": final_r, **cls}
    if ee:
        for k, v in ee.items():
            if k.startswith(("time_to_", "first_touch", "tp1_", "tp2_", "max_structure", "mfe_before")):
                row[k] = v
    return row


def entry_exit_section(store: DemoStore, phase: str | None, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """'Entry quality vs exit quality' (Lane X): entry and profit-capture quality measured SEPARATELY, per
    market x family x direction, for the closed STRATEGY trades and for the labelled counterfactuals.  A negative
    final R is not a bad-entry verdict (MFE +0.6R then -1R = POTENTIAL_USEFUL_ENTRY + EXIT_GIVEBACK).  Small n flagged,
    no edge claim.  Rows written before the hook (no ``entry_exit`` JSON) are classified from their stored MFE/MAE/R."""
    real: list[dict[str, Any]] = []
    for r in rows:
        snap = store.get_snapshot(r["opportunity_id"]) if r.get("opportunity_id") else None
        if snap is None:
            continue
        extra = store.get_outcome_extra(r["intent_id"]) or {}
        ee = extra.get("entry_exit")
        real.append(_eeq_row(
            {"market": snap.market, "direction": snap.direction, "family": str(snap.signal.get("family", "unknown")),
             "signal_ts": parse_utc(snap.signal_ts_utc), "kind": "REAL_TRADE"},
            mfe=float((ee or {}).get("mfe_r", r["mfe_r"])), mae=float((ee or {}).get("mae_r", r["mae_r"])),
            final_r=float(r["gross_r"]), ee=ee, t_mae=None,
        ))
    cf: list[dict[str, Any]] = []
    for c in store.list_counterfactuals(phase):
        snap = store.get_snapshot(c.opportunity_id)
        if snap is None:
            continue
        cf.append(_eeq_row(
            {"market": snap.market, "direction": snap.direction, "family": str(snap.signal.get("family", "unknown")),
             "signal_ts": parse_utc(snap.signal_ts_utc), "kind": "COUNTERFACTUAL"},
            mfe=float(c.hypothetical_mfe_r), mae=float(c.hypothetical_mae_r), final_r=float(c.hypothetical_r),
            ee=c.entry_exit, t_mae=None,
        ))

    def block(items: list[dict[str, Any]]) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for it in items:
            groups[f"{it['market']}|{it['family']}|{'long' if it['direction'] > 0 else 'short'}"].append(it)
        return {
            "overall": summarise_entries(items, event_clusters=assign_event_clusters(items)) if items else {"n": 0},
            "groups": {k: summarise_entries(g, event_clusters=assign_event_clusters(g)) for k, g in sorted(groups.items())},
            "n_with_path_fields": sum(1 for it in items if "first_touch_0.5R" in it),
        }

    return {
        "analysis_version": EEQ_VERSION, "label_spec_version": EEQ_LABEL_SPEC, "small_n": EEQ_SMALL_N,
        "real_trades": block(real), "counterfactuals": block(cf),
        "note": ("real trades: realised gross R (actual fills) vs the recorded path MFE/MAE; counterfactuals: hypothetical R "
                 "(fill at intended entry, no costs). Entry and capture are separate axes; small n flagged; descriptive only, no edge claim."),
    }


def _side_group(store: DemoStore, phase: str | None, kind: str) -> dict[str, Any]:
    """Censored (manual / external / emergency flatten) or canary trades: shown SEPARATELY, never mixed
    into the strategy metrics."""
    rows = _trade_rows(store, phase, kind)
    rs = [r["net_r"] for r in rows]
    return {
        "n": len(rows),
        "sum_net_r": sum(rs),
        "mean_net_r": _mean(rs),
        "pnl_eur": sum(r["pnl_eur"] for r in rows),
        "avg_mfe_r": _mean([r["mfe_r"] for r in rows]),
        "avg_mae_r": _mean([r["mae_r"] for r in rows]),
        "avg_holding_s": _mean([r["holding_s"] for r in rows]),
        "exit_reasons": dict(sorted({x: sum(1 for r in rows if r["exit_reason"] == x) for x in {r["exit_reason"] for r in rows}}.items())),
        "note": "excluded from strategy expectancy / winrate / cumulative R / learning labels",
        "trades": [{k: r.get(k) for k in ("closed_utc", "intent_id", "market", "direction", "net_r", "pnl_eur", "exit_reason")} for r in rows],
    }


def build_report(store: DemoStore, phase: str | None = None) -> dict[str, Any]:
    if phase is not None and phase not in PHASES:
        raise ValueError(f"bad phase {phase!r}")
    rows = _trade_rows(store, phase)
    rs = [r["net_r"] for r in rows]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_loss = -sum(losses)
    days = {parse_utc(r["closed_utc"]).date() for r in rows}
    if rows:
        span = (
            parse_utc(rows[-1]["closed_utc"]).date() - parse_utc(rows[0]["closed_utc"]).date()
        ).days + 1
    else:
        span = 0
    mean = _mean(rs)
    sd = (
        math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1))
        if n >= 2 and mean is not None
        else None
    )
    se = sd / math.sqrt(n) if sd is not None else None

    tercile_q = _tercile_labeller(
        [r.get("quality") if isinstance(r.get("quality"), (int, float)) else None for r in rows]
    )

    def quality_key(r: dict[str, Any]) -> Any:
        q = r.get("quality")
        return tercile_q(q) if isinstance(q, (int, float)) else (q if q is not None else "unknown")

    dec_all = store.list_decisions(phase)
    accepted = [d for d in dec_all if d.accepted]
    rejected = [d for d in dec_all if not d.accepted]
    cfs = store.list_counterfactuals(phase)
    rejected_ids = {d.opportunity_id for d in rejected}
    cf_on_rejected = sum(1 for c in cfs if c.opportunity_id in rejected_ids)
    cf_r = [c.hypothetical_r for c in cfs]
    resolved = [c for c in cfs if c.target_before_stop is not None]

    metrics = {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "winrate": (len(wins) / n) if n else None,
        "avg_win_r": _mean(wins),
        "avg_loss_r": _mean(losses),
        "expected_r": mean,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "profit_factor_note": None if gross_loss > 0 else "undefined (no losing trades)",
        "cumulative_r": sum(rs),
        "gross_cumulative_r": sum(r["gross_r"] for r in rows),
        "pnl_eur": sum(r["pnl_eur"] for r in rows),
        "costs_eur": sum(r["costs_eur"] for r in rows),
        "avg_slippage": _mean([r["slippage"] for r in rows if r["slippage"] is not None]),
        "max_drawdown_r": max_drawdown_r(rs),
        "avg_mfe_r": _mean([r["mfe_r"] for r in rows]),
        "avg_mae_r": _mean([r["mae_r"] for r in rows]),
        "avg_holding_s": _mean([r["holding_s"] for r in rows]),
        "trades_per_day": (n / span) if span else None,
        "active_days": len(days),
        "expected_r_se": se,
        "expected_r_approx_95ci": (
            [mean - 1.96 * se, mean + 1.96 * se] if se is not None and mean is not None else None
        ),
        "provisional_cost_trades": sum(1 for r in rows if r["cost_status"] != "verified"),
    }
    breakdowns = {
        "market": _group(rows, lambda r: r.get("market", "unknown")),
        "direction": _group(rows, lambda r: r.get("direction", "unknown")),
        "family": _group(rows, lambda r: r.get("family", "unknown")),
        "hour_local": _group(rows, lambda r: r.get("hour", "unknown")),
        "session": _group(rows, lambda r: r.get("session", "unknown")),
        "spread_bucket": _group(rows, lambda r: _spread_bucket(r.get("spread_ratio"))),
        "confluence": _group(rows, lambda r: r.get("confluence", "unknown")),
        "quality": _group(rows, quality_key),
        "exit_reason": _group(rows, lambda r: r["exit_reason"]),
    }
    accepted_vs_rejected = {
        "accepted": {
            "decisions": len(accepted),
            "traded": n,
            "expected_net_r": mean,
            "note": "realised, net of fees/swap, from actual fills",
        },
        "rejected": {
            "decisions": len(rejected),
            "labelled": len(cfs),  # every label: engine rejects AND engine-accepted non-trades (stack reject / cancel / shadow dry-run)
            "labelled_engine_rejected": cf_on_rejected,
            "labelled_accepted_non_traded": len(cfs) - cf_on_rejected,
            "unlabelled": len(rejected) - cf_on_rejected,  # never negative: rejected decisions still without a label
            "expected_hypothetical_r": _mean(cf_r),
            "target_before_stop_rate": (
                sum(1 for c in resolved if c.target_before_stop) / len(resolved)
                if resolved
                else None
            ),
            "note": "hypothetical: fill at intended entry, no slippage/fees, stop-first bars",
        },
    }
    if n == 0:
        statement = "No closed trades yet: nothing can be concluded."
    elif n < MIN_TRADES_FOR_INFERENCE:
        statement = (
            f"n={n} closed trades is far too small to draw any conclusion about expectancy "
            f"(threshold used here: {MIN_TRADES_FOR_INFERENCE}). These numbers are descriptive only."
        )
    else:
        statement = (
            f"n={n}. Even with a larger sample these are descriptive statistics of demo trades; "
            "the interval is approximate (ignores autocorrelation/regime changes)."
        )
    statement += " This report makes no claim of profitability or edge." + (
        " DISCOVERY-phase data is not a clean holdout." if phase in (None, "DISCOVERY") else ""
    )
    from demo.funnel import funnel as build_funnel

    censored = _side_group(store, phase, "censored")
    canary = _side_group(store, phase, "canary")
    account = store.account_info()
    return {
        "phase": phase or "ALL",
        "disclaimer": "DEMO_ALPHA_RESULT != LIVE_EXECUTION_PROOF",
        "account": account,
        "generated_utc": datetime.now().astimezone().isoformat(),
        "execution_analytics": _execution_analytics(store, phase, rows),
        "entry_exit_quality": entry_exit_section(store, phase, rows),
        "censored_exits": censored,
        "canary_trades": canary,
        "account_pnl_reconciliation": {
            "strategy_eur": metrics["pnl_eur"],
            "censored_eur": censored["pnl_eur"],
            "canary_eur": canary["pnl_eur"],
            "total_closed_eur": metrics["pnl_eur"] + censored["pnl_eur"] + canary["pnl_eur"],
            "note": "closed-trade P/L by population; floating P/L and deposits are not included",
        },
        "sample_size_statement": statement,
        "rejection_funnel": build_funnel(store, None, phase),
        "metrics": metrics,
        "breakdowns": breakdowns,
        "accepted_vs_rejected": accepted_vs_rejected,
        "cumulative_r_curve": _curve(rs),
        "trades": [
            {
                k: r.get(k)
                for k in (
                    "closed_utc",
                    "intent_id",
                    "opportunity_id",
                    "phase",
                    "market",
                    "direction",
                    "family",
                    "net_r",
                    "gross_r",
                    "pnl_eur",
                    "mfe_r",
                    "mae_r",
                    "holding_s",
                    "exit_reason",
                    "costs_eur",
                    "cost_status",
                )
            }
            for r in rows
        ],
    }


def _curve(rs: list[float]) -> list[float]:
    out, cum = [], 0.0
    for r in rs:
        cum += r
        out.append(cum)
    return out


# ---- rendering --------------------------------------------------------------------------------
def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def _render_entry_exit(eeq: dict[str, Any]) -> list[str]:
    L = ["", "## Entry quality vs exit quality", "",
         f"`{eeq['analysis_version']}` / labels `{eeq['label_spec_version']}`. {eeq['note']}", ""]
    for title, key in (("Closed strategy trades", "real_trades"), ("Labelled counterfactuals", "counterfactuals")):
        blk = eeq[key]
        ov = blk["overall"]
        L += [f"### {title} (n={ov.get('n', 0)}; {blk['n_with_path_fields']} with path-level fields)", ""]
        if not ov.get("n"):
            L += ["No rows.", ""]
            continue
        L += ["| market|family|dir | n | clusters | flag | MFE mean/med | MAE mean/med | useful / failure share | poor capture of useful | capture (floored) | giveback | labels (good / giveback / failure / ambiguous) | verdict |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for name, c in (("ALL", ov), *blk["groups"].items()):
            lc = c["label_counts"]
            L.append(
                f"| {name} | {c['n']} | {c['n_event_clusters']} | {'low n' if c['n'] < EEQ_SMALL_N else ''} | {_fmt(c['mfe_mean'])}/{_fmt(c['mfe_median'])} | "
                f"{_fmt(c['mae_mean'])}/{_fmt(c['mae_median'])} | {_fmt(c['useful_entry_share'])} / {_fmt(c['entry_failure_share'])} | "
                f"{_fmt(c['poor_capture_share_of_useful'])} | {_fmt(c['capture_ratio_floored_mean'])} | {_fmt(c['mfe_giveback_mean'])} | "
                f"{lc['GOOD_ENTRY_GOOD_CAPTURE']} / {lc['POTENTIAL_USEFUL_ENTRY_EXIT_GIVEBACK']} / {lc['ENTRY_FAILURE']} / {lc['AMBIGUOUS']} | {c['verdict']} |"
            )
        L.append("")
    return L


def render_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    acct = report.get("account") or {}
    L = [
        f"# DEMO report - phase {report['phase']}",
        "",
        f"**{report.get('disclaimer', 'DEMO_ALPHA_RESULT != LIVE_EXECUTION_PROOF')}**"
        f" | account_phase {acct.get('account_phase') or 'n/a'} | account {acct.get('account_id_hash') or 'n/a'}"
        + (" (legacy backfill)" if acct.get("legacy_backfill") else ""),
        "",
        f"> {report['sample_size_statement']}",
        "",
        "## Metrics",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    L += [f"| {k} | {_fmt(v)} |" for k, v in m.items() if k != "profit_factor_note"]
    if m["profit_factor_note"]:
        L.append(f"| profit_factor_note | {m['profit_factor_note']} |")
    for name, groups in report["breakdowns"].items():
        L += [
            "",
            f"## By {name}",
            "",
            "| group | n | winrate | expected R | sum R | flag |",
            "|---|---|---|---|---|---|",
        ]
        for g, s in groups.items():
            L.append(
                f"| {g} | {s['n']} | {_fmt(s['winrate'])} | {_fmt(s['expected_r'])} | {_fmt(s['sum_r'])} | "
                f"{'low n' if s['low_n'] else ''} |"
            )
    if "rejection_funnel" in report:
        from demo.funnel import render as render_funnel

        L += ["", "## Rejection funnel", "", "```", render_funnel(report["rejection_funnel"]), "```"]
    for title, key in (("Censored exits (manual / external / emergency flatten)", "censored_exits"), ("Canary / test trades", "canary_trades")):
        g = report.get(key)
        if g is not None:
            L += ["", f"## {title}", "", f"- n {g['n']} | sum net R {_fmt(g['sum_net_r'])} | EUR {_fmt(g['pnl_eur'])} | avg MFE R {_fmt(g['avg_mfe_r'])} | avg MAE R {_fmt(g['avg_mae_r'])} | avg duration s {_fmt(g['avg_holding_s'])}", f"- {g['note']}"]
    ea = report.get("execution_analytics")
    if ea:
        L += ["", "## Execution analytics (strategy trades)", "", f"- tca: {json.dumps(ea['tca'], default=str)}", f"- timing: {json.dumps(ea['timing'], default=str)}"]
    eeq = report.get("entry_exit_quality")
    if eeq:
        L += _render_entry_exit(eeq)
    rec = report.get("account_pnl_reconciliation")
    if rec:
        L += ["", "## Account P/L reconciliation (closed trades)", "", f"- strategy {_fmt(rec['strategy_eur'])} | censored {_fmt(rec['censored_eur'])} | canary {_fmt(rec['canary_eur'])} | total {_fmt(rec['total_closed_eur'])} EUR"]
    avr = report["accepted_vs_rejected"]
    L += [
        "",
        "## Accepted vs rejected",
        "",
        f"- accepted: {json.dumps(avr['accepted'])}",
        f"- rejected (counterfactual): {json.dumps(avr['rejected'])}",
        "",
        f"## All trades ({len(report['trades'])})",
        "",
        "| closed_utc | market | dir | family | net R | gross R | EUR | exit | costs EUR |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for t in report["trades"]:
        L.append(
            f"| {t['closed_utc']} | {t['market']} | {t['direction']} | {t['family']} | {_fmt(t['net_r'])} | "
            f"{_fmt(t['gross_r'])} | {_fmt(t['pnl_eur'])} | {t['exit_reason']} | {_fmt(t['costs_eur'])} |"
        )
    return "\n".join(L) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_report(
    store: DemoStore,
    out_dir: str | os.PathLike[str],
    phase: str | None = None,
    tag: str | None = None,
) -> tuple[Path, Path]:
    """Build and write `<out_dir>/report-<phase>[-<tag>].md|.json` atomically. Returns (md, json)."""
    rep = build_report(store, phase)
    stem = f"report-{rep['phase']}" + (f"-{tag}" if tag else "")
    md, js = Path(out_dir) / f"{stem}.md", Path(out_dir) / f"{stem}.json"
    _atomic_write(md, render_markdown(rep))
    _atomic_write(js, json.dumps(rep, indent=2, sort_keys=True, default=str))
    return md, js
