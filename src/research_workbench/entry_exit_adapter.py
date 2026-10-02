# ruff: noqa: E501
"""ENTRY / EXIT quality ADAPTER (OFFLINE ONLY): maps fast trades onto the EXISTING analytics, nothing is re-implemented.

Wired (called, not copied):

* ``demo.entry_exit_quality``  ``entry_exit_fields`` (MFE/MAE/timing/first-touch/classification per entry),
  ``summarise_entries`` (cell summary), ``assign_event_clusters``, ``capture_ratio``, ``MFE_LEVELS_R``.
* ``demo.shadow_exit_lab``     ``evaluate_shadow`` (same-entry exit-policy lab, via ``safe_evaluate_shadow``) and
  ``summarise_lab`` (paired comparison on identical entries).

NOT_AVAILABLE here (reported as such, never invented): structural TP1/TP2 and failed-move levels
(need ``demo.structure`` on a pandas history and the STRUCT family data -> the policies that require them are
``NOT_APPLICABLE`` through the lab's own rule), the ``OperatingPolicy`` flat deadline (the market window's forced-flat
minute is used instead), and the random-entry control (``coverage_analysis.shadow_exit_lab_study.control_candidates_fn``
needs ``FamilyData``/``MarketInputs``): ``NOT_RUN``.

CAVEAT (verbatim in every report): DIAGNOSTIC/RESEARCH ONLY, spread-adjusted gross, NOT commission/slippage/swap
complete, never the sole promotion gate.

Right-tail reporting (``right_tail``) is MEASUREMENT ONLY: P(realized_R >= 2R/3R/5R) and mean/median realized_R
conditional on MFE >= 2R/3R/5R with N per bucket. No exit policy is changed or tuned here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

CAVEAT = (
    "DIAGNOSTIC/RESEARCH ONLY, spread-adjusted gross, NOT commission/slippage/swap complete, "
    "never the sole promotion gate."
)
RIGHT_TAIL_LEVELS = (2.0, 3.0, 5.0)
DEFAULT_CONFIG: dict[str, Any] = {
    "max_entries": 2000,
    "max_lab_entries": 100,
    "run_exit_lab": True,
    "flat_min": 1290,
}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_PRE_BARS = 6


def right_tail(
    realized_r: np.ndarray | list[float],
    mfe_r: np.ndarray | list[float],
    levels: tuple[float, ...] = RIGHT_TAIL_LEVELS,
) -> dict[str, Any]:
    """MEASUREMENT ONLY. For each level L: P(realized_R >= L) over all N, and mean/median realized_R given MFE >= L."""
    r = np.asarray(realized_r, dtype=float)
    mfe = np.asarray(mfe_r, dtype=float)
    if len(r) != len(mfe):
        raise ValueError("realized_r and mfe_r must be aligned")
    out: dict[str, Any] = {"n": len(r), "levels": {}}
    for level in levels:
        sub = r[mfe >= level]
        out["levels"][f"{level:g}R"] = {
            "p_realized_ge": float((r >= level).mean()) if len(r) else None,
            "n_realized_ge": int((r >= level).sum()),
            "n_mfe_ge": len(sub),
            "mean_realized_given_mfe_ge": float(sub.mean()) if len(sub) else None,
            "median_realized_given_mfe_ge": float(np.median(sub)) if len(sub) else None,
        }
    return out


def _dt(ns: int) -> datetime:
    return _EPOCH + timedelta(microseconds=int(ns) // 1000)


def _sample(n: int, cap: int) -> np.ndarray:
    if n <= cap:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, cap).astype(int))


def _entry_context(arrays: dict[str, np.ndarray], j: int, flat_min: int) -> tuple[int, int | None]:
    """(end index exclusive incl. the first flat bar, index of the flat bar or None)."""
    n = len(arrays["o"])
    day, minute, contig_next = arrays["day"], arrays["minute"], arrays["contig_next"]
    end = j
    flat_idx: int | None = None
    while end < n and day[end] == day[j]:
        if minute[end] >= flat_min:
            flat_idx = end
            end += 1  # the first bar at/after flat carries the forced-flat fill
            break
        end += 1
        if end < n and not contig_next[end - 1]:
            break
    return min(end, n), flat_idx


def run_entry_exit_diagnostic(
    market: str,
    arrays: dict[str, np.ndarray],
    candidates: Any,
    trades: Any,
    *,
    max_entries: int = DEFAULT_CONFIG["max_entries"],
    max_lab_entries: int = DEFAULT_CONFIG["max_lab_entries"],
    run_exit_lab: bool = DEFAULT_CONFIG["run_exit_lab"],
    flat_min: int = DEFAULT_CONFIG["flat_min"],
) -> dict[str, Any]:
    """ENTRY_EXIT_DIAGNOSTIC over the trades of one market (see module docstring for scope and caveat)."""
    result: dict[str, Any] = {
        "status": "NOT_AVAILABLE",
        "caveat": CAVEAT,
        "market": market,
        "not_available": [],
        "random_entry_control": {
            "status": "NOT_RUN",
            "reason": "control_candidates_fn needs FamilyData/MarketInputs (alpha.families); not constructible from fast arrays",
        },
    }
    n_trades = len(trades)
    if n_trades == 0:
        result["not_available"].append("no trades")
        return result
    try:
        from demo.entry_exit_quality import (
            MFE_LEVELS_R,
            Step,
            assign_event_clusters,
            entry_exit_fields,
            summarise_entries,
        )
    except Exception as exc:  # pragma: no cover - import environment problem
        result["not_available"].append(f"demo.entry_exit_quality import failed: {exc!r}")
        return result

    stops = candidates.stop[np.searchsorted(candidates.decision_idx, trades.decision_idx)]
    picked = _sample(n_trades, max_entries)
    labels = trades.exit_reason_labels
    ts_ns = arrays["ts_ns"]
    rows: list[dict[str, Any]] = []
    sources: list[int] = []
    excluded = 0
    for ti in picked:
        j = int(trades.entry_idx[ti])
        direction = int(trades.side[ti])
        end, _ = _entry_context(arrays, j, flat_min)
        sp = np.zeros(end - j) if direction > 0 else arrays["spread"][j:end]
        steps = [
            Step(
                _dt(ts_ns[k]), float(arrays["o"][k] + s), float(arrays["h"][k] + s),
                float(arrays["l"][k] + s), float(arrays["c"][k] + s),
            )
            for k, s in zip(range(j, end), sp, strict=True)
        ]  # fmt: skip
        try:
            fields = entry_exit_fields(
                direction=direction, entry=float(trades.entry_price[ti]), stop=float(stops[ti]),
                steps=steps, entry_ts=_dt(ts_ns[j]), final_r=float(trades.r_multiple[ti]),
                apply_stop=True,
            )  # fmt: skip
        except ValueError:
            excluded += 1
            continue
        rows.append(
            {
                **fields,
                "market": market,
                "direction": direction,
                "signal_ts": _dt(ts_ns[int(trades.decision_idx[ti])] + 300 * 10**9),
                "entry_id": f"{market}:{int(trades.decision_idx[ti])}",
                "baseline_r": float(trades.r_multiple[ti]),
                "baseline_exit": str(labels[ti]),
            }
        )
        sources.append(int(ti))
    if not rows:
        result["not_available"].append("no entry could be mapped (all excluded)")
        return result
    clusters = assign_event_clusters(rows)
    result["entry_quality"] = {
        "summary": summarise_entries(rows, event_clusters=clusters),
        "mfe_levels_r": list(MFE_LEVELS_R),
        "n_mapped": len(rows),
        "n_excluded": excluded,
        "n_sampled": len(picked),
        "n_trades": int(n_trades),
        "path_definition": "potential path: entry bar to initial stop / market flat minute / data gap; independent of the real exit",
    }
    result["status"] = "COMPLETE"
    result["exit_quality"] = _exit_quality(
        market, arrays, candidates, trades, stops, rows, sources, run_exit_lab,
        max_lab_entries, flat_min,
    )  # fmt: skip
    result["not_available"].append(
        "structural TP1/TP2 + failed-move level (need demo.structure history / STRUCT family data): "
        "policies requiring them are NOT_APPLICABLE"
    )
    result["not_available"].append(
        f"OperatingPolicy flat deadline: market window flat minute {flat_min} used instead"
    )
    return result


def _exit_quality(
    market: str,
    arrays: dict[str, np.ndarray],
    candidates: Any,
    trades: Any,
    stops: np.ndarray,
    rows: list[dict[str, Any]],
    sources: list[int],
    run_lab: bool,
    max_lab: int,
    flat_min: int,
) -> dict[str, Any]:
    own = right_tail(
        [r["baseline_r"] for r in rows], [r["mfe_r"] for r in rows]
    )  # strategy's own exit profile on the potential-MFE basis
    out: dict[str, Any] = {
        "strategy_exit_right_tail": own,
        "same_entry_comparison": {"status": "NOT_RUN"},
        "note": "same-entry comparison: every policy is evaluated on the identical entry set; right-tail is measurement only",
    }
    if not run_lab:
        return out
    try:
        from demo.exit_policies import BarSeries, EntryInput
        from demo.shadow_exit_lab import LabStats, safe_evaluate_shadow, summarise_lab
    except Exception as exc:
        out["same_entry_comparison"] = {
            "status": "NOT_AVAILABLE",
            "reason": f"import failed: {exc!r}",
        }
        return out
    ts_ns = arrays["ts_ns"]
    pick = _sample(len(rows), max_lab)
    lab_rows: list[dict[str, Any]] = []
    potential_mfe: list[float] = []
    stats = LabStats()
    for ri in pick:
        ti = sources[int(ri)]
        j = int(trades.entry_idx[ti])
        direction = int(trades.side[ti])
        end, flat_idx = _entry_context(arrays, j, flat_min)
        sl = slice(j, end)
        bars = BarSeries(
            tuple(_dt(x) for x in ts_ns[sl]), tuple(map(float, arrays["o"][sl])),
            tuple(map(float, arrays["h"][sl])), tuple(map(float, arrays["l"][sl])),
            tuple(map(float, arrays["c"][sl])), tuple(map(float, arrays["spread"][sl])),
        )  # fmt: skip
        pre = None
        if j >= _PRE_BARS:
            ps = slice(j - _PRE_BARS, j)
            pre = BarSeries(
                tuple(_dt(x) for x in ts_ns[ps]), tuple(map(float, arrays["o"][ps])),
                tuple(map(float, arrays["h"][ps])), tuple(map(float, arrays["l"][ps])),
                tuple(map(float, arrays["c"][ps])), tuple(map(float, arrays["spread"][ps])),
            )  # fmt: skip
        decision = int(trades.decision_idx[ti])
        atr = float(arrays["atr"][decision]) if np.isfinite(arrays["atr"][decision]) else None
        entry = EntryInput(
            entry_id=rows[int(ri)]["entry_id"], market=market, direction=direction,
            fill=float(trades.entry_price[ti]), stop=float(stops[ti]), entry_ts=_dt(ts_ns[j]),
            atr=atr, flat_utc=_dt(ts_ns[flat_idx]) if flat_idx is not None else None, pre=pre,
        )  # fmt: skip
        lab = safe_evaluate_shadow(
            entry, bars, live={"profile": "fast_sim", "r": float(trades.r_multiple[ti]), "mfe_r": rows[int(ri)]["mfe_r"]},
            stats=stats,
        )  # fmt: skip
        if lab is None:
            continue
        lab_rows.append(
            {
                "market": market, "direction": direction, "signal_ts": rows[int(ri)]["signal_ts"],
                "entry_id": rows[int(ri)]["entry_id"], "lab": lab,
            }
        )  # fmt: skip
        potential_mfe.append(float(rows[int(ri)]["mfe_r"]))
    if not lab_rows:
        out["same_entry_comparison"] = {
            "status": "NOT_AVAILABLE",
            "reason": f"lab produced no result ({stats.as_dict()})",
        }
        return out
    summary = summarise_lab(lab_rows)
    out["same_entry_comparison"] = {
        "status": "COMPLETE",
        "n_entries": len(lab_rows),
        "lab_stats": stats.as_dict(),
        "summary": summary,
        "right_tail_by_policy": _policy_right_tails(lab_rows, potential_mfe),
        "basis": "right tail conditions on the entry's POTENTIAL MFE (identical entry set for every policy)",
    }
    return out


def _policy_right_tails(
    lab_rows: list[dict[str, Any]], potential_mfe: list[float]
) -> dict[str, Any]:
    names = list(lab_rows[0]["lab"]["policies"])
    baseline = "fixed_1_5r"
    mfe = np.asarray(potential_mfe)

    def records(name: str) -> list[dict[str, Any] | None]:
        return [
            (
                lb["lab"]["policies"].get(name)
                if (lb["lab"]["policies"].get(name) or {}).get("applicable")
                else None
            )
            for lb in lab_rows
        ]

    base = records(baseline)
    out: dict[str, Any] = {}
    for name in names:
        recs = records(name)
        own_idx = [i for i, x in enumerate(recs) if x is not None]
        entry: dict[str, Any] = {
            "n_applicable": len(own_idx),
            "right_tail": right_tail([recs[i]["r"] for i in own_idx], mfe[own_idx])
            if own_idx
            else None,
            "mean_capture_ratio": (
                float(
                    np.mean(
                        [
                            recs[i]["capture_ratio"]
                            for i in own_idx
                            if recs[i].get("capture_ratio") is not None
                        ]
                    )
                )
                if any(recs[i].get("capture_ratio") is not None for i in own_idx)
                else None
            ),
        }
        if name != baseline:
            # PAIRED set: both policies applicable AND capture defined for both (no independent subsets)
            both_applicable = [i for i in own_idx if base[i] is not None]
            both = [
                i
                for i in both_applicable
                if recs[i].get("capture_ratio") is not None
                and base[i].get("capture_ratio") is not None
            ]
            n_excl_na = len(lab_rows) - len(both_applicable)
            n_excl_cap = len(both_applicable) - len(both)
            paired: dict[str, Any] = {
                "basis": "paired entries (both policies applicable and capture defined)",
                "n_pairs": len(both),
                "n_excluded_not_both_applicable": n_excl_na,
                "n_excluded_capture_undefined": n_excl_cap,
            }
            if both:
                tail_p = right_tail([recs[i]["r"] for i in both], mfe[both])
                tail_b = right_tail([base[i]["r"] for i in both], mfe[both])
                cap_p = float(np.mean([recs[i]["capture_ratio"] for i in both]))
                cap_b = float(np.mean([base[i]["capture_ratio"] for i in both]))
                lower_levels = [
                    lvl
                    for lvl, v in tail_p["levels"].items()
                    if (v["p_realized_ge"] or 0.0) < (tail_b["levels"][lvl]["p_realized_ge"] or 0.0)
                ]
                paired.update(
                    right_tail=tail_p,
                    baseline_right_tail=tail_b,
                    capture_mean=cap_p,
                    baseline_capture_mean=cap_b,
                    FLAG_higher_capture_lower_right_tail=bool(cap_p > cap_b and lower_levels),
                    lower_tail_levels=lower_levels,
                )
            else:
                paired.update(FLAG_higher_capture_lower_right_tail=False, lower_tail_levels=[])
            entry["paired_vs_baseline"] = paired
        out[name] = entry
    return out


__all__ = (
    "CAVEAT",
    "DEFAULT_CONFIG",
    "RIGHT_TAIL_LEVELS",
    "right_tail",
    "run_entry_exit_diagnostic",
)
