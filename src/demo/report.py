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


def _trade_rows(store: DemoStore, phase: str | None) -> list[dict[str, Any]]:
    rows = []
    for intent_id, opp_id, o in store.list_outcomes(phase):
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
            "labelled": len(cfs),
            "unlabelled": len(rejected) - len(cfs),
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
    return {
        "phase": phase or "ALL",
        "generated_utc": datetime.now().astimezone().isoformat(),
        "sample_size_statement": statement,
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


def render_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    L = [
        f"# DEMO report - phase {report['phase']}",
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
