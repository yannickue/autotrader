# ruff: noqa: E501
"""Lane F: OFFLINE shadow-variant measurement of the STRUCT family on BTCUSD / Brent M5 history (DEV frames only).

HINDSIGHT DIAGNOSTICS.  Nothing here is fitted, selected or promoted; there is no frozen Train/holdout split for the
Phase-2 markets and every frame is cut at the research development end (``dev_frame``: no bar after 2026-08-31 Berlin
date).  The result makes NO edge/expectancy claim; it only shows how the four entry variants of the SAME break differ in
excursion/cost terms, with n per cell and explicit 'n too small' flags, against a random-bar base rate.

Usage:  uv run python scripts/lane_f_family_variants.py [DATA_ROOT] [OUT_MD] [OUT_JSON]
  DATA_ROOT default lane_f_root (contains markets/manifest_*.json, see scripts/v2_download_market.py).
Method id: lane-f-variants-v1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from alpha.common.market_data import dev_frame, load_dev_market_frame  # noqa: E402
from alpha.families import structbrk  # noqa: E402
from alpha.families.data import build_family_data  # noqa: E402
from alpha.families.registry import generate_candidates  # noqa: E402
from alpha.families.spec import MarketCalendar  # noqa: E402
from coverage_analysis.control import control_table, outcome_arrays  # noqa: E402
from coverage_analysis.moves import MoveParams  # noqa: E402
from demo.labeling import Bar, simulate_hypothetical  # noqa: E402
from markets.phase2 import load_phase2_spec  # noqa: E402

METHOD = "lane-f-variants-v1"
HORIZON = 48  # bars (4 h) for the hypothetical walk
MIN_N = 30
TARGET_R = 1.5
# Offline measurement uses a FULL-DAY entry window (session windows belong to the operating policy, not the family).
CALS = {
    "BTCUSD": MarketCalendar("UTC", 0, 1440, 0, 1440, 1440),
    "BRENT": MarketCalendar("Europe/London", 0, 1440, 0, 1440, 1440),
}


def _bars(d, j: int, end: int) -> list[Bar]:
    return [Bar("", float(d.o[k]), float(d.h[k]), float(d.l[k]), float(d.c[k]), float(d.spread[k])) for k in range(j, end)]


def measure(d, spec) -> dict:
    cands = generate_candidates(d, spec, structbrk.fit(d, spec))
    seg = structbrk.segment_start(d)
    rows = []
    for k in range(len(cands.decision_idx)):
        i = int(cands.decision_idx[k])
        j = i + 1
        dirn = int(cands.direction[k])
        end = j
        while end < min(j + HORIZON, len(d)) and seg[end] <= j:
            end += 1
        if end - j < 12:
            continue  # look-ahead window not contiguous / not fully available: excluded (no censoring bias)
        entry = float(d.o[j] + d.spread[j]) if dirn > 0 else float(d.o[j])
        stop = float(cands.stop[k])
        risk = abs(entry - stop)
        if not (risk > 0) or (dirn > 0 and stop >= entry) or (dirn < 0 and stop <= entry):
            continue
        tgt = entry + dirn * TARGET_R * risk
        hyp = simulate_hypothetical(direction=dirn, entry=entry, stop=stop, target=tgt, bars=_bars(d, j, end))
        # unstopped excursion path over the horizon (hindsight diagnostics)
        hi = d.h[j:end]
        lo = d.l[j:end]
        fav = (hi - entry) if dirn > 0 else (entry - (lo + d.spread[j:end]))
        adv = (entry - lo) if dirn > 0 else ((hi + d.spread[j:end]) - entry)
        mfe_path, mae_path = fav.max() / risk, adv.max() / risk
        rows.append({
            "idx": i, "dir": dirn, "r": hyp.r, "kind": hyp.exit_kind, "tbs": hyp.target_before_stop,
            "mfe_r": float(mfe_path), "mae_r": float(mae_path),
            "t_mfe": int(np.argmax(fav)) + 1, "t_mae": int(np.argmax(adv)) + 1,
            "stop_atr": risk / float(d.atr[i]), "spr_price": float(d.spread[j] / d.c[i]), "spr_stop": float(d.spread[j] / risk),
            "mtc": float(fav.max() / max(d.spread[j], 1e-12)), "half": 0 if i < len(d) // 2 else 1,
        })
    return {"rows": rows, "n_candidates": len(cands.decision_idx)}


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0, "flag": "n=0"}
    a = lambda k: np.array([r[k] for r in rows], dtype=float)  # noqa: E731
    med = lambda k: float(np.median(a(k)))  # noqa: E731
    kinds = [r["kind"] for r in rows]
    out = {
        "n": n, "n_long": sum(r["dir"] > 0 for r in rows), "n_short": sum(r["dir"] < 0 for r in rows),
        "flag": "n too small (<30): no conclusion" if n < MIN_N else "ok",
        "target_before_stop_share": float(np.mean([r["tbs"] is True for r in rows])),
        "stop_share": float(np.mean([k.startswith("STOP") for k in kinds])),
        "horizon_share": float(np.mean([k == "HORIZON" for k in kinds])),
        "mean_r_1p5R_target_with_spread": float(a("r").mean()),
        "median_mfe_r": med("mfe_r"), "median_mae_r": med("mae_r"),
        "median_time_to_mfe_bars": med("t_mfe"), "median_time_to_mae_bars": med("t_mae"),
        "median_stop_atr": med("stop_atr"), "median_spread_over_price": med("spr_price"),
        "median_spread_over_stop": med("spr_stop"), "median_movement_to_cost": med("mtc"),
        "share_spread_over_20pct_of_1R": float(np.mean(a("spr_stop") > 0.20)),
        "mean_r_first_half": float(np.mean([r["r"] for r in rows if r["half"] == 0])) if any(r["half"] == 0 for r in rows) else None,
        "mean_r_second_half": float(np.mean([r["r"] for r in rows if r["half"] == 1])) if any(r["half"] == 1 for r in rows) else None,
        "n_first_half": sum(r["half"] == 0 for r in rows), "n_second_half": sum(r["half"] == 1 for r in rows),
    }
    return out


def break_stats(d, n_range: int) -> dict:
    """Per-BREAK diagnostics independent of the entry variant: false-break and reversal rates."""
    b = structbrk._break_arrays(d, n_range)
    seg = structbrk.segment_start(d)
    n = len(d)
    tot = fb = rev = follow = 0
    for k in np.flatnonzero(b["up"] | b["dn"]).tolist():
        if k + HORIZON >= n or seg[k + HORIZON] > k:
            continue
        up = bool(b["up"][k])
        edge, opp = (b["hi"][k], b["lo"][k]) if up else (b["lo"][k], b["hi"][k])
        seq = d.c[k + 1: k + 1 + structbrk.FAIL_BARS]
        back = (seq < edge).any() if up else (seq > edge).any()
        far_h, far_l = d.h[k + 1: k + 1 + HORIZON], d.l[k + 1: k + 1 + HORIZON]
        reversed_ = (far_l <= opp).any() if up else (far_h >= opp).any()
        width = abs(b["hi"][k] - b["lo"][k])
        ft = ((far_h.max() - d.c[k]) if up else (d.c[k] - far_l.min())) >= width
        tot += 1
        fb += bool(back)
        rev += bool(reversed_)
        follow += bool(ft)
    return {"n_breaks": tot, "false_break_rate_6bars": fb / tot if tot else None, "reversal_to_opposite_edge_rate_4h": rev / tot if tot else None,
            "follow_through_ge_1_range_width_4h": follow / tot if tot else None, "flag": "n too small (<30)" if tot < MIN_N else "ok"}


def run(data_root: str) -> dict:
    out: dict = {"method": METHOD, "constants": structbrk.constants(), "horizon_bars": HORIZON, "target_r": TARGET_R,
                 "caveat": "hindsight diagnostics on dev frames; NOT_ALPHA_VALIDATED; no edge claim", "markets": {}}
    for m in ("BTCUSD", "BRENT"):
        ms = load_phase2_spec(m)
        frame = dev_frame(load_dev_market_frame(ms, data_root=data_root))
        d = build_family_data(frame, CALS[m], name=m, point_size=ms.point_size, tick_size=ms.tick_size, asset_class=ms.asset_class)
        cell: dict = {"n_bars": len(d), "first_ts": str(frame["ts"].iloc[0]), "last_ts": str(frame["ts"].iloc[-1]), "variants": {}}
        trig_for_control = []
        for mode in structbrk.MODES:
            spec = structbrk.STRUCTSpec(mode=mode, target=TARGET_R)
            res = measure(d, spec)
            s = summarise(res["rows"])
            s["n_candidates_before_lookahead_filter"] = res["n_candidates"]
            cell["variants"][mode] = s
            if mode == "breakout":
                trig_for_control = [SimpleNamespace(idx=r["idx"], direction=r["dir"]) for r in res["rows"]]
        cell["break_stats_n24"] = break_stats(d, 24)
        p = MoveParams()
        outc, avail = outcome_arrays(d, p)
        eligible = avail & np.isfinite(d.atr)
        ct = control_table(d, trig_for_control, outc, eligible)
        cell["base_rate_control_breakout_vs_random_bars"] = {k: ct.get(k) for k in ("n_triggers_evaluable", "trigger", "base_all", "matched_sample", "verdict")}
        out["markets"][m] = cell
    return out


def render(res: dict) -> str:
    L = [f"# Lane F - STRUCT family variant measurement ({res['method']})", "",
         "**PHASE2_DISCOVERY / NOT_ALPHA_VALIDATED.** Hindsight diagnostics on development frames (no bar after 2026-08-31 Berlin date). "
         "Nothing is fitted, tuned or promoted; there is no frozen Train/holdout split for these markets. No expectancy/edge is claimed.", "",
         f"Constants (DISCOVERY PLACEHOLDERS, not fitted): `{json.dumps(res['constants'])}`", "",
         f"Hypothetical walk: entry next bar open (long at ask=open+spread, short at bid), structural stop, {res['target_r']}R target, stop-first pessimistic, horizon {res['horizon_bars']} bars; "
         "candidates without a fully contiguous 12-bar look-ahead are excluded. Offline window = full day (session windows are operating policy, not family logic).", ""]
    for m, c in res["markets"].items():
        L += [f"## {m}  ({c['n_bars']} M5 bars, {c['first_ts']} .. {c['last_ts']})", "",
              "| variant | n (L/S) | flag | TP-before-SL | SL share | horizon | mean R (1.5R tgt) | med MFE R | med MAE R | t_MFE/t_MAE bars | med stop ATR | spread/price | spread/stop | >20% of 1R | movement_to_cost | meanR 1st/2nd half (n) |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for v, s in c["variants"].items():
            if s["n"] == 0:
                L.append(f"| {v} | 0 | n=0 |" + " |" * 13)
                continue
            f = lambda x, nd=3: "-" if x is None else f"{x:.{nd}f}"  # noqa: E731
            L.append(f"| {v} | {s['n']} ({s['n_long']}/{s['n_short']}) | {s['flag']} | {f(s['target_before_stop_share'], 2)} | {f(s['stop_share'], 2)} | {f(s['horizon_share'], 2)} | {f(s['mean_r_1p5R_target_with_spread'])} | "
                     f"{f(s['median_mfe_r'], 2)} | {f(s['median_mae_r'], 2)} | {s['median_time_to_mfe_bars']:.0f}/{s['median_time_to_mae_bars']:.0f} | {f(s['median_stop_atr'], 2)} | {s['median_spread_over_price']:.5f} | {f(s['median_spread_over_stop'], 3)} | {f(s['share_spread_over_20pct_of_1R'], 2)} | {f(s['median_movement_to_cost'], 1)} | {f(s['mean_r_first_half'])}/{f(s['mean_r_second_half'])} ({s['n_first_half']}/{s['n_second_half']}) |")
        bs = c["break_stats_n24"]
        L += ["", f"Per-break diagnostics (n_range=24, variant-independent): n={bs['n_breaks']} ({bs['flag']}), false-break rate (close back inside within 6 bars) = {bs['false_break_rate_6bars']}, "
              f"reversal to the opposite edge within 4 h = {bs['reversal_to_opposite_edge_rate_4h']}, follow-through >= 1 range width within 4 h = {bs['follow_through_ge_1_range_width_4h']}.", ""]
        ct = c["base_rate_control_breakout_vs_random_bars"]
        if ct.get("trigger"):
            t, b = ct["trigger"], ct["base_all"]
            L += [f"Base-rate control (coverage_analysis.control, STRONG = 3 ATR favourable within 24 bars before 1 ATR adverse): breakout triggers n={t['n']} strong-share={t['rate']}, "
                  f"random-bar base rate={b['rate'] if b else None}; matched-hour sample={(ct.get('matched_sample') or {}).get('rate')}; verdict: {ct['verdict']}.", ""]
    L += ["## Caveats", "", "- Neighbouring bars and breaks cluster strongly; counts are not independent, intervals would be optimistic.",
          "- BTCUSD M5 history starts 2025-09-24 and lacks Oct-2025 and Mar-2026 (DST fold months rejected as AmbiguousServerTime for 24/7 data); Brent M5 from 2025-06.",
          "- Brent calendar is structural-proposal/probe-derived, BTC bars are recorded BID with recorded bar spread; the live tick spread is wider (see docs/V2_MARKETS.md).",
          "- Variant choice for real trading was fixed a priori (confirmed breakout) BEFORE this measurement and is not changed by it."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else str(REPO / "lane_f_root")
    res = run(root)
    md = render(res)
    (Path(sys.argv[2]) if len(sys.argv) > 2 else REPO / "docs" / "evidence" / "lane_f_family_variants.md").write_text(md, encoding="utf-8")
    (Path(sys.argv[3]) if len(sys.argv) > 3 else REPO / "docs" / "evidence" / "lane_f_family_variants.json").write_text(json.dumps(res, indent=1, default=float), encoding="utf-8")
    print(md)
