# ruff: noqa: E501
"""Per-market orchestration + report rendering (hindsight diagnostics, read-only)."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd

from alpha.families.data import FamilyData, build_family_data
from alpha.families.spec import MarketCalendar
from coverage_analysis import HINDSIGHT_LABEL
from coverage_analysis.classify import CLASSES, classify_moves, no_setup_reasons
from coverage_analysis.control import control_table, eligible_mask, outcome_arrays
from coverage_analysis.moves import MoveParams, find_moves
from coverage_analysis.r2 import R2Row
from coverage_analysis.replay import DEFAULT_TOLERANCE, replay_market


def analyse_data(
    market: str, data: FamilyData, specs: Sequence[Any], *, lo: int = 0, params: MoveParams | None = None,
    tolerance: float = DEFAULT_TOLERANCE, r2_rows: Sequence[R2Row] = (), seed: int = 20260930,
) -> dict[str, Any]:
    p = params or MoveParams()
    rep = replay_market(market, data, list(specs), tolerance=tolerance)
    moves = find_moves(data, p, lo=lo)
    rows = classify_moves(data, moves, rep, specs, market, r2_rows, p)
    outcomes, avail = outcome_arrays(data, p)
    elig = eligible_mask(data, specs, avail)
    elig_eval = elig.copy()
    elig_eval[:lo] = False
    groups: dict[str, list] = {}
    for kind, trs in (("SIGNAL", rep.signals), ("NEAR", rep.near)):
        trs = [t for t in trs if t.idx >= lo]
        groups[f"ALL/{kind}"] = trs
        for fam in sorted({t.family for t in trs}):
            groups[f"{fam}/{kind}"] = [t for t in trs if t.family == fam]
    control = {k: control_table(data, v, outcomes, elig_eval, seed=seed) for k, v in groups.items()}
    counts = Counter(r["klass"] for r in rows)
    n_days = len({int(d) for d in data.day[lo:][elig[lo:]]})
    return {
        "market": market, "n_bars": len(data) - lo, "eligible_bars": int(elig_eval.sum()), "eligible_days": n_days,
        "moves": len(rows), "counts": {k: counts.get(k, 0) for k in CLASSES},
        "no_setup_top_reasons": no_setup_reasons(rows), "move_rows": rows,
        "signals": len([t for t in rep.signals if t.idx >= lo]), "near_misses": len([t for t in rep.near if t.idx >= lo]),
        "control": control,
        "base_rate_all_bars": {str(d): float((outcomes[d][elig_eval] == 1).mean()) if elig_eval.any() else None for d in (1, -1)},
    }


def analyse_market(
    market: str, frame: pd.DataFrame, mspec: Any, specs: Sequence[Any], *, eval_from: pd.Timestamp | None = None,
    cross: Mapping[str, Any] | None = None, **kw: Any,
) -> dict[str, Any]:
    cal = MarketCalendar.from_market_spec(mspec)
    data = build_family_data(
        frame, cal, name=market, point_size=mspec.point_size, tick_size=mspec.tick_size,
        asset_class=mspec.asset_class, cross=cross,
    )
    lo = 0
    if eval_from is not None:
        ts = pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8
        lo = int(np.searchsorted(ts, pd.Timestamp(eval_from).as_unit("ns").value, side="left"))
    return analyse_data(market, data, specs, lo=lo, **kw)


# ---------------------------------------------------------------------------------------- render
def _f(v: Any, nd: int = 3) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4g}" if 0 < abs(v) < 0.01 else f"{v:.{nd}f}"
    return str(v)


def _cell(c: Mapping[str, Any], k: str) -> str:
    return "-" if c.get(k) is None else _f(c[k]["rate"])


def render_markdown(results: Sequence[dict[str, Any]], meta: Mapping[str, Any]) -> str:
    pm = meta["params"]
    L = [
        "# Lane N - retrospective coverage analysis (market move -> EXECUTED / REJECTED / NEAR_MISS / OUT_OF_WINDOW / NO_SETUP)",
        "",
        f"**{HINDSIGHT_LABEL}.**",
        "",
        f"- move definition: pivot -> >= {pm['n_atr']} ATR favourable excursion within {pm['m_bars']} bars, "
        f"max adverse {pm['max_adverse_atr']} ATR, swing k={pm['swing_k']}; cover window = [start-{pm['cover_pre']}, start+{pm['cover_post']}] bars",
        f"- near-miss: one family condition relaxed at a time by <= {meta['tolerance']} of its scale, the family's own `generate` re-run (no re-implementation); "
        "actual = value effectively reached (<= 1.9% of scale resolution), required = frozen threshold, normalized gap = |required - actual| / scale "
        "(scale = |required|, ATR-distance parameters floored at 0.25 ATR); gap_abs in the condition's unit",
        f"- data: {meta['data']}",
        f"- R2 data reused (read-only, copy): {meta['r2']}",
        f"- sample: {meta['sample']}",
        "",
        "## Coverage per market",
        "",
        "| market | bars | moves | EXECUTED | REJECTED | NEAR_MISS | OUT_OF_WINDOW | NO_SETUP | SIGNAL_NOT_IN_R2 | replay signals | near-misses |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    tot: Counter[str] = Counter()
    for r in results:
        c = r["counts"]
        tot.update(c)
        L.append(f"| {r['market']} | {r['n_bars']} | {r['moves']} | {c['EXECUTED']} | {c['REJECTED']} | {c['NEAR_MISS']} | {c['OUT_OF_WINDOW']} | {c['NO_SETUP']} | {c['SIGNAL_NOT_IN_R2']} | {r['signals']} | {r['near_misses']} |")
    L.append(f"| **all** | {sum(r['n_bars'] for r in results)} | {sum(r['moves'] for r in results)} | {tot['EXECUTED']} | {tot['REJECTED']} | {tot['NEAR_MISS']} | {tot['OUT_OF_WINDOW']} | {tot['NO_SETUP']} | {tot['SIGNAL_NOT_IN_R2']} | {sum(r['signals'] for r in results)} | {sum(r['near_misses'] for r in results)} |")
    L += [
        "",
        "EXECUTED / REJECTED need an R2 record around the move start. SIGNAL_NOT_IN_R2 is a data-availability bucket, not an outcome: the frozen generators re-run offline would have signalled, but R2 has no record for the period (bars pre-date the live runner).",
        "",
        "## Top reasons for NO_SETUP (closest failing family condition at the move start)",
        "",
    ]
    for r in results:
        L.append(f"- {r['market']}: " + ("; ".join(f"{k} x{v}" for k, v in r["no_setup_top_reasons"]) or "-"))
    L += ["", "## NEAR_MISS moves (first 15 per market)", "",
          "| market | dir | start | family/mode | failed condition | actual | required | normalized gap | gap (abs, unit) |", "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        for m in [x for x in r["move_rows"] if x["klass"] == "NEAR_MISS"][:15]:
            d = m["detail"]
            L.append(f"| {r['market']} | {m['direction']:+d} | {m['start_utc']} | {d['family']}/{d['mode']} | {d['failed_condition']} | {_f(d['actual'])} | {_f(d['required'])} | {_f(d['normalized_gap'])} | {_f(d['gap_abs'])} {d['unit']} |")
    L += ["", "## R2 records attached to moves (EXECUTED / REJECTED)", ""]
    any_r2 = False
    for r in results:
        for m in [x for x in r["move_rows"] if x["klass"] in ("EXECUTED", "REJECTED")]:
            any_r2 = True
            L.append(f"- {r['market']} {m['direction']:+d} {m['start_utc']} [{m['klass']}]: {json.dumps(m['detail'], default=str)}")
    if not any_r2:
        L.append("- none (no R2 record overlaps the analysed bars)")
    L += [
        "",
        "## MANDATORY CONTROL: outcome mix after the trigger bar vs random / matched / time-shifted bars",
        "",
        "For every trigger (real signal or NEAR_MISS) the entry is the bar close, same direction. STRONG = >= N ATR favourable within M bars before an adverse move of "
        f"{pm['max_adverse_atr']} ATR; ADVERSE = the adverse move comes first; NOISE = neither within M bars. Controls: `base_all` = exact share over all eligible bars (trigger direction mix); "
        "`uniform` / `matched` (same local hour + direction) = seeded random bars, K=20 per trigger; `shifted` = trigger bars moved +-1/2 days. "
        "Neighbouring bars are autocorrelated: 95% Wilson intervals are optimistic. n < 30 triggers = no conclusion.",
        "",
        "| market | group | n | STRONG [95% CI] | ADVERSE | NOISE | base STRONG / ADV / NOISE | uniform STRONG | matched STRONG | shifted STRONG | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        for g, c in r["control"].items():
            t = c["trigger"]
            ci = t["ci95"]
            b = c.get("base_all")
            bs = "-" if b is None else f"{_f(b['rate'])} / {_f(b['adverse_share'])} / {_f(b['noise_share'])}"
            L.append(f"| {r['market']} | {g} | {c['n_triggers_evaluable']} | {_f(t['rate'])} [{_f(ci[0])}, {_f(ci[1])}] | {_f(t['adverse_share'])} | {_f(t['noise_share'])} | {bs} | {_cell(c, 'uniform_sample')} | {_cell(c, 'matched_sample')} | {_cell(c, 'shifted')} | {c['verdict']} |")
    L += ["", "Base rate of STRONG moves on all eligible bars (per direction): " + "; ".join(f"{r['market']} long {_f(r['base_rate_all_bars']['1'])} / short {_f(r['base_rate_all_bars']['-1'])}" for r in results), ""]
    return "\n".join(L) + "\n"


def to_json(results: Sequence[dict[str, Any]], meta: Mapping[str, Any]) -> str:
    return json.dumps({"label": HINDSIGHT_LABEL, "meta": dict(meta), "results": list(results)}, indent=1, default=str)


def meta_params(p: MoveParams) -> dict[str, Any]:
    return asdict(p)
