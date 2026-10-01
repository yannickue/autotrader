# ruff: noqa: E501
"""Gate B report of the observer backfill (OFFLINE / RESEARCH ONLY; never touches the live trader, artifacts/, MT5 or schedulers).

    uv run python scripts/observer_gate_b_report.py --root <backfill out dir> [--out docs/evidence] [--markets GER40 ...] [--phase2-root <dir>]
        [--n-audit 300] [--n-window 60] [--n-extended 30] [--n-shallow 20] [--seed 11] [--force]

Per market (results cached in ``<root>/<MARKET>/gate_b.json``; ``--force`` recomputes): (i) feature plausibility, (ii) leakage audit on REAL data
(live-style incremental pass through physically truncated views with FULL history, truncated raw-frame windows rebuilt through the batch adapter, longer
frames with future bars present, shallow histories), (iii) warm-up accounting, (iv) label sanity, (v) matching quality, (vi) live-vs-batch parity, (vii)
coverage and caveats. Writes ``<out>/observer_backfill_report.{md,json}``. Any failed blocking check makes the GATE B verdict FAIL; the failing cases are listed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

ACTIVE = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD", "BTCUSD", "BRENT")
FP_LABELS = ((0.25, 0.25), (0.50, 0.50), (0.75, 0.50), (1.00, 0.50))
LABEL_TOL = 0.10  # |control rate - zero-drift reference| tolerance (bar-resolution, stop-first, finite sample, real drift)
SMD_TOL = 0.10
MATCH_RATE_MIN = 0.50
CAVEATS = [
    "BTCUSD and BRENT have NO frozen split, a short history (BTC from 2025-09-24, BRENT from 2025-06) with missing months (BTC lacks Oct-2025 and Mar-2026) and provisional STRUCT constants.",
    "The core thresholds (GER40 NAS100 SPX500 XAUUSD EURUSD) were partly fitted up to 2026-06-30: events before that date are in-sample for the family thresholds.",
    "The dev window 2026-07-01..2026-08-31 is NOT a clean holdout for the core (other lanes validated on it); the forward period (>= 2026-09-01) is untouched and never read.",
    "Bars are M5 bid bars: first-passage labels are bar-resolution, stop-first, cost-free; they are not P&L. Events are generator opportunities without the live operating policy filters.",
    "Controls use the partition-aware matching (observer-controls-2): inside the event's partition, partition-internal percentile ranks, +-48 bars away from every generator opportunity. BTCUSD/BRENT have no frozen split, so their partitions are generic dev-date tags only. The pre-revision controls (whole-sample ranks) are kept under _prerevision/ for comparison.",
    "Nothing here is an edge claim; OBSERVATION_ONLY_NOT_ALPHA_VALIDATED.",
]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not math.isfinite(float(o)) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


# ---------------------------------------------------------------------------------------------- per-check functions
def check_warmup(feat: pd.DataFrame) -> dict:
    """Share of rows below each group's documented history requirement (from the stored audit values) and overall ``warmup_ok = false``."""
    out: dict = {}
    for name, sub in (("events", feat[~feat["is_control"].astype(bool)]), ("controls", feat[feat["is_control"].astype(bool)])):
        n = len(sub)
        b, pdays = sub["m_bars_available"], sub["m_prev_days_available"]
        unmet = {
            "levels": b < sub["m_min_bars_levels"], "swings": b < sub["m_min_bars_swings"], "acceptance": b < sub["m_min_bars_acceptance"],
            "balance": b < sub["m_min_bars_balance"], "participation": pdays < sub["m_min_prev_days_participation"], "atr": ~sub["m_atr_ok"].astype(bool),
        }
        out[name] = {
            "n": n, "warmup_false": int((~sub["warmup_ok"].astype(bool)).sum()), "warmup_false_share": float((~sub["warmup_ok"].astype(bool)).mean()) if n else None,
            "unmet_share_by_group": {k: float(v.mean()) if n else None for k, v in unmet.items()},
        }
    by_fam = (feat[~feat["is_control"].astype(bool)].groupby("family", dropna=False)["warmup_ok"].apply(lambda s: float((~s.astype(bool)).mean())).to_dict())
    out["events_warmup_false_share_by_family"] = {str(k): v for k, v in by_fam.items()}
    return out


def check_labels(lab: pd.DataFrame, feat_ts: pd.DataFrame) -> dict:
    """Label base rates vs the zero-drift reference P = fav/(fav+adv), controls, censoring and internal consistency."""
    df = lab.merge(feat_ts[["event_id", "decision_ts_ns"]], on="event_id")
    out: dict = {"rates": {}, "consistency": {}, "horizon": {}}
    for a, b in FP_LABELS:
        col = f"y_fav{round(a * 100):03d}_before_adv{round(b * 100):03d}"
        ref = a / (a + b)
        row: dict = {"zero_drift_reference_P": ref}
        for name, sub in (("events", df[~df["is_control"].astype(bool)]), ("controls", df[df["is_control"].astype(bool)])):
            s = sub[col]
            n_all = len(s)
            v = s.dropna().astype(float)
            n, k = len(v), int((v == 1).sum())
            lo, hi = wilson(k, n)
            row[name] = {"n_rows": n_all, "n_resolved": n, "censored_share": float(1 - n / n_all) if n_all else None, "rate": k / n if n else None, "wilson95": [lo, hi],
                         "diff_to_reference": (k / n - ref) if n else None}
        out["rates"][col] = row
    # internal consistency: first-passage vs excursions
    viol: dict[str, int] = {}
    def vio(name: str, mask: pd.Series) -> None:
        viol[name] = int(mask.fillna(False).sum())
    tol = 1e-9
    for a, b in FP_LABELS:
        col = f"y_fav{round(a * 100):03d}_before_adv{round(b * 100):03d}"
        vio(f"{col}=1 but mfe_r<{a}", (df[col] == 1) & (df["y_mfe_r"] < a - tol))
        vio(f"{col}=0 but mae_r<{b}", (df[col] == 0) & (df["y_mae_r"] < b - tol))
    vio("fav100=1 but fav075=0", (df["y_fav100_before_adv050"] == 1) & (df["y_fav075_before_adv050"] == 0))
    vio("fav075=1 but fav050=0", (df["y_fav075_before_adv050"] == 1) & (df["y_fav050_before_adv050"] == 0))
    vio("negative mfe/mae", (df["y_mfe_r"] < -tol) | (df["y_mae_r"] < -tol))
    out["consistency"] = {"violations": viol, "n_rows": len(df), "total_violations": int(sum(viol.values()))}
    h = (df["horizon_end_ts_ns"] - df["decision_ts_ns"]) / 1e9
    out["horizon"] = {"max_horizon_s": float(h.max()), "min_horizon_s": float(h.min()), "share_full_48_bars": float((h >= 48 * 300 - 1).mean()), "horizon_bound_ok": bool((h <= 48 * 300 + 1e-6).all() and (h >= 0).all())}
    return out


def label_verdict(lb: dict) -> tuple[str, list[str]]:
    why: list[str] = []
    if lb["consistency"]["total_violations"]:
        why.append(f"{lb['consistency']['total_violations']} internal consistency violations")
    if not lb["horizon"]["horizon_bound_ok"]:
        why.append("horizon outside [0, 48 bars]")
    for col, r in lb["rates"].items():
        d = r["controls"]["diff_to_reference"]
        if d is not None and abs(d) > LABEL_TOL:
            why.append(f"{col}: control rate deviates {d:+.3f} from the zero-drift reference (> {LABEL_TOL})")
    return ("PASS" if not why else "FAIL"), why


def check_matching(cm: dict | None) -> tuple[dict, str, list[str]]:
    if cm is None:
        return {}, "FAIL", ["no controls manifest"]
    mr = cm["match_report"]
    why = []
    if mr["match_rate"] is None or mr["match_rate"] < MATCH_RATE_MIN:
        why.append(f"match rate {mr['match_rate']:.3f} < {MATCH_RATE_MIN}")
    for k, v in mr["smd"].items():
        if v is None or abs(v) > SMD_TOL:
            why.append(f"|SMD({k})|={v} > {SMD_TOL}")
    return {**mr, "by_family": cm.get("match_by_family"), "matching_revision": cm.get("matching_revision"), "controls_in_other_partition": cm["partitions"].get("controls_in_a_different_partition_than_their_event")}, ("PASS" if not why else "WARN"), why


# ---------------------------------------------------------------------------------------------- one market
def gate_b_market(market: str, root: Path, p2root: str | None, args) -> dict:
    import entry_exit_quality as X

    from coverage_analysis.observer_lab import backfill as BF
    from coverage_analysis.observer_lab import backfill_audit as AU
    from demo.opportunity.observer_hook import session_for
    from market_observer import bars_adapter as BA
    from markets.phase2 import load_phase2_spec
    from markets.spec import CANONICALS, load_market_spec

    t0 = time.time()
    mdir = root / market
    ev_m = json.loads((mdir / "manifest.json").read_text(encoding="utf-8"))
    cm = json.loads((mdir / "controls_manifest.json").read_text(encoding="utf-8")) if (mdir / "controls_manifest.json").is_file() else None
    ms = load_market_spec(market) if market in CANONICALS else load_phase2_spec(market)
    mi = X.build_market_inputs(market, 0, None, p2root)
    frame = mi.frame
    fp_ok = BF.frame_fingerprint(frame) == ev_m["data"]["frame_fingerprint"]
    cfg = BF.observer_config_for(ms)
    build = lambda fr: BF.build_bars(fr, ms, market)  # noqa: E731
    make_buffer = lambda: BA.BarBuffer(market, tick_size=float(ms.tick_size), session=session_for(ms), max_bars=10**9)  # noqa: E731

    feat_all = BF.load_event_table(mdir, with_labels=False, controls=True)  # features + generator fields (no labels)
    lab_all = pd.concat([pd.read_parquet(mdir / f) for f in ("labels.parquet", "controls_labels.parquet") if (mdir / f).is_file()], ignore_index=True)
    feat_only = feat_all[[c for c in feat_all.columns if not c.startswith("match_")]]
    res: dict = {"market": market, "frame_fingerprint_matches_manifest": fp_ok, "n_rows": len(feat_all), "n_events": int((~feat_all["is_control"].astype(bool)).sum()),
                 "n_controls": int(feat_all["is_control"].astype(bool).sum())}

    # (i) plausibility on decision features
    pl = AU.plausibility(feat_only)
    res["plausibility"] = {"n_feature_columns": len(pl["columns"]), "groups": pl["groups"], "impossible_value_flags": pl["impossible_value_flags"], "n_flagged": pl["n_flagged"],
                           "verdict": "PASS" if pl["n_flagged"] == 0 else "FAIL", "column_summary": {c: {k: v for k, v in i.items() if k in ("n", "missing_share", "constant", "quantiles", "distinct")} for c, i in pl["columns"].items()}}

    # (iii) warm-up
    res["warmup"] = check_warmup(feat_only)

    # (iv) labels
    lb = check_labels(lab_all, feat_all)
    verdict, why = label_verdict(lb)
    res["labels"] = {**lb, "verdict": verdict, "reasons": why}

    # (v) matching
    mq, mv, mw = check_matching(cm)
    res["matching"] = {**mq, "verdict": mv, "reasons": mw}

    pre = root / "_prerevision" / market
    if (pre / "controls_manifest.json").is_file():
        pm = json.loads((pre / "controls_manifest.json").read_text(encoding="utf-8"))
        rates = {}
        if (pre / "controls_labels.parquet").is_file():
            pl_ = pd.read_parquet(pre / "controls_labels.parquet")
            for a_, b_ in FP_LABELS:
                col = f"y_fav{round(a_ * 100):03d}_before_adv{round(b_ * 100):03d}"
                if col in pl_.columns:
                    v_ = pl_[col].dropna().astype(float)
                    rates[col] = {"n": len(v_), "rate": float(v_.mean()) if len(v_) else None}
        res["matching_prerevision"] = {"method": pm["match_report"]["method"], "n_controls": pm["n_controls"], "match_rate": pm["match_report"]["match_rate"], "smd": pm["match_report"]["smd"], "control_label_rates": rates}

    # (ii) + (vi) leakage audit and parity on real data
    rng = np.random.default_rng(args.seed)
    ev = feat_all[~feat_all["is_control"].astype(bool)]
    ct = feat_all[feat_all["is_control"].astype(bool)]
    ts_all = pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8.astype(np.int64)
    per_day = pd.Series(1, index=pd.DatetimeIndex(frame["ts"]).tz_convert("Europe/Berlin").normalize()).groupby(level=0).sum().median()
    window = int(min(12000, max(3000, math.ceil(28 * per_day))))
    n_a = min(args.n_audit, len(ev))
    samp = ev.iloc[np.sort(rng.choice(len(ev), size=n_a, replace=False))]
    samp_c = ct.iloc[np.sort(rng.choice(len(ct), size=min(args.n_audit_controls, len(ct)), replace=False))] if len(ct) else ct.iloc[:0]
    stored_a = pd.concat([samp, samp_c], ignore_index=True)
    t = time.time()
    a_res = AU.audit_buffer_pass(frame, make_buffer, cfg, float(ms.point_size), stored_a, window=6000)
    a_s = time.time() - t
    pos = {int(x): k for k, x in enumerate(ts_all)}
    deep = ev[ev["decision_ts_ns"].map(lambda x: pos.get(int(x) - 300 * 10**9, -1) >= window)]
    pool = deep if len(deep) >= args.n_window else ev
    stored_b = pool.iloc[np.sort(rng.choice(len(pool), size=min(args.n_window, len(pool)), replace=False))]
    t = time.time()
    b_res = AU.audit_window_recompute(frame, build, cfg, stored_b, depth=window, extension=0, name=f"truncated_at_decision_bar_window_{window}")
    stored_c = stored_b.iloc[: args.n_extended]
    c_res = AU.audit_window_recompute(frame, build, cfg, stored_c, depth=window, extension=500, name=f"longer_frame_plus_500_future_bars_window_{window}")
    stored_s = ev.iloc[np.sort(rng.choice(len(ev), size=min(args.n_shallow, len(ev)), replace=False))]
    shallow = AU.audit_shallow_depths(frame, build, cfg, stored_s, depths=(120, 240, 500))
    res["leakage_audit"] = {
        "history_window_bars": window, "sample_seed": args.seed,
        "live_style_incremental_full_history": {**a_res.to_dict(), "sample": {"events": len(samp), "controls": len(samp_c)}, "seconds": round(a_s, 1)},
        "truncated_raw_frame_window": {**b_res.to_dict(), "sample": len(stored_b)},
        "extended_frame_future_bars_present": {**c_res.to_dict(), "sample": len(stored_c)},
        "shallow_histories_must_be_flagged_not_warm": shallow, "window_seconds": round(time.time() - t, 1),
    }
    shallow_ok = all(v["rule_ok"] for v in shallow.values())
    res["leakage_audit"]["verdict"] = "PASS" if (a_res.passed and b_res.passed and c_res.passed and shallow_ok and fp_ok) else "FAIL"
    res["parity_live_vs_batch"] = {"n_compared": a_res.n, "pass": a_res.n_pass, "fail": a_res.n_fail, "verdict": "PASS" if a_res.passed else "FAIL", "failures": a_res.failures[:5],
                                   "note": "BarBuffer fed engine-style sliding 6000-bar frames bar by bar; the observer saw only physically truncated views; compared with the stored batch records EXACTLY"}

    # (vii) coverage
    res["coverage"] = {"data": ev_m["data"], "dev_end_guard": ev_m["dev_end_guard"], "partitions": ev_m["partitions"], "events_by_family_variant_direction": ev_m["events"]["by_family_variant_direction"],
                       "exclusions": ev_m["events"]["exclusions"], "rows": ev_m["rows"], "controls_rows": (cm or {}).get("rows"), "runtime_s": {"events_step": ev_m.get("runtime_s"), "controls_step": (cm or {}).get("runtime_s")},
                       "peak_memory_mb": {"events_step": ev_m.get("peak_memory_mb"), "controls_step": (cm or {}).get("peak_memory_mb")}, "matching_revision": (cm or {}).get("matching_revision")}
    # a readable sample record
    cand = ev[ev["warmup_ok"].astype(bool)]
    if len(cand):
        r = cand.iloc[len(cand) // 2]
        res["sample_record"] = {k: (None if (isinstance(v, float) and not math.isfinite(v)) else (v.item() if hasattr(v, "item") else v)) for k, v in r.items() if pd.notna(v) and not k.startswith("match_")}
    res["blocking"] = {"plausibility": res["plausibility"]["verdict"], "leakage_audit": res["leakage_audit"]["verdict"], "parity_live_vs_batch": res["parity_live_vs_batch"]["verdict"], "label_sanity": res["labels"]["verdict"]}
    res["informational"] = {"matching": res["matching"]["verdict"], "frame_fingerprint": "PASS" if fp_ok else "FAIL"}
    res["verdict"] = "PASS" if all(v == "PASS" for v in res["blocking"].values()) and fp_ok else "FAIL"
    res["gate_b_seconds"] = round(time.time() - t0, 1)
    return _clean(res)


# ---------------------------------------------------------------------------------------------- rendering
def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def render(results: list[dict], meta: dict) -> str:
    lines = ["# Observer backfill: Gate B report", "", f"Generated {meta['generated']} from code `{meta['git_sha']}`; backfill root `{meta['root']}` (outside git). OBSERVATION_ONLY_NOT_ALPHA_VALIDATED, offline, no edge claim.", ""]
    verdict = meta["verdict"]
    lines += [f"## GATE B VERDICT: {verdict}", ""]
    if meta["missing"]:
        lines += [f"Markets without data / incomplete: {', '.join(meta['missing'])}", ""]
    lines += ["| market | events | controls | plausibility | leakage audit | live==batch | label sanity | matching | verdict |", "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['market']} | {r['n_events']} | {r['n_controls']} | {r['blocking']['plausibility']} | {r['blocking']['leakage_audit']} | {r['blocking']['parity_live_vs_batch']} | {r['blocking']['label_sanity']} | {r['informational']['matching']} | {r['verdict']} |")
    lines += ["", "Blocking checks: plausibility, leakage audit, live-vs-batch parity, label sanity. Matching quality / warm-up / coverage are reported (WARN is non-blocking).", ""]
    fails = [(r["market"], k, v) for r in results for k, v in r["blocking"].items() if v != "PASS"]
    if fails:
        lines += ["### FAILING CHECKS (not papered over)", ""] + [f"* {m}: {k} = {v}" for m, k, v in fails] + [""]
    for r in results:
        m = r["market"]
        la = r["leakage_audit"]
        lines += [f"## {m}", ""]
        lines += [f"Rows: {r['n_events']} events + {r['n_controls']} controls; frame fingerprint matches manifest: {r['frame_fingerprint_matches_manifest']}. Gate B compute {r['gate_b_seconds']} s.", ""]
        lines += ["### (ii) Leakage audit on real data", ""]
        for key, label in (("live_style_incremental_full_history", "live-style incremental pass, full history, physically truncated views (prefix, bar 0..i)"),
                           ("truncated_raw_frame_window", f"raw frame truncated exactly at the decision bar, history window {la['history_window_bars']} bars, rebuilt through the batch adapter"),
                           ("extended_frame_future_bars_present", "same window but +500 FUTURE bars present in the frame")):
            a = la[key]
            lines.append(f"* {label}: compared {a['n_compared']}, pass {a['pass']}, fail {a['fail']}, skipped {a['skipped']} {a['skipped_reasons'] or ''} -> {a['verdict']}")
        sh = la["shallow_histories_must_be_flagged_not_warm"]
        lines.append("* shallow histories (120/240/500 bars): " + "; ".join(f"{d}: n={v['n']}, flagged not-warm {v['flagged_not_warm']}, wrongly warm {v['wrongly_flagged_warm']}, still equal {v['still_equal_to_stored']}" for d, v in sh.items()))
        for key in ("live_style_incremental_full_history", "truncated_raw_frame_window", "extended_frame_future_bars_present"):
            for f in la[key]["failures"][:3]:
                lines.append(f"  * FAILURE {key}: {json.dumps(f, default=str)[:600]}")
        lines += ["", f"### (i) Plausibility ({r['plausibility']['n_feature_columns']} feature columns, {r['plausibility']['n_flagged']} flagged) -> {r['plausibility']['verdict']}", ""]
        lines.append("| group | columns | constant | all-missing | missing >= 50% | flagged |")
        lines.append("|---|---|---|---|---|---|")
        for g, v in sorted(r["plausibility"]["groups"].items()):
            lines.append(f"| {g} | {v['n_columns']} | {len(v['constant_columns'])} | {len(v['all_missing_columns'])} | {len(v['high_missing_columns'])} | {v['flagged_columns']} |")
        for fl in r["plausibility"]["impossible_value_flags"][:15]:
            lines.append(f"* FLAG `{fl['column']}`: {fl['flags']}")
        const = sorted({c for v in r["plausibility"]["groups"].values() for c in v["constant_columns"]})
        if const:
            lines.append(f"* constant columns: {', '.join(const)}")
        w = r["warmup"]
        lines += ["", "### (iii) Warm-up accounting", "",
                  f"* events: {w['events']['warmup_false']}/{w['events']['n']} warmup_ok=false ({_pct(w['events']['warmup_false_share'])}); controls: {w['controls']['warmup_false']}/{w['controls']['n']} ({_pct(w['controls']['warmup_false_share'])})",
                  "* share of events below the documented history requirement by group: " + ", ".join(f"{k} {_pct(v)}" for k, v in w["events"]["unmet_share_by_group"].items()),
                  "* events warmup_ok=false by family: " + ", ".join(f"{k} {_pct(v)}" for k, v in w["events_warmup_false_share_by_family"].items()), ""]
        lb = r["labels"]
        lines += [f"### (iv) Label sanity -> {lb['verdict']}", "", "| label | ref P (zero drift) | events rate [Wilson95] (n) | controls rate [Wilson95] (n) | censored ev/ctl |", "|---|---|---|---|---|"]
        for col, x in lb["rates"].items():
            e, c = x["events"], x["controls"]
            fmt = lambda s: "n/a" if s["rate"] is None else f"{s['rate']:.3f} [{s['wilson95'][0]:.3f},{s['wilson95'][1]:.3f}] ({s['n_resolved']})"  # noqa: E731
            lines.append(f"| {col} | {x['zero_drift_reference_P']:.3f} | {fmt(e)} | {fmt(c)} | {_pct(e['censored_share'])} / {_pct(c['censored_share'])} |")
        lines += ["", f"* consistency violations: {lb['consistency']['total_violations']} over {lb['consistency']['n_rows']} rows; horizon max {lb['horizon']['max_horizon_s']:.0f} s (bound 14400), full-48-bar share {_pct(lb['horizon']['share_full_48_bars'])}"]
        lines += [f"* reasons: {lb['reasons']}" if lb["reasons"] else "* no label issue"]
        mq = r["matching"]
        lines += ["", f"### (v) Matching quality -> {mq['verdict']} (matching revision: {mq.get('matching_revision')})", ""]
        if mq.get("n_events") is not None:
            lines.append(f"* events {mq['n_events']}, matched {mq['n_matched']} (rate {mq['match_rate']:.3f}), unmatched {mq['n_unmatched']}, controls {mq['n_controls']}; SMD {mq['smd']}; max session-share difference {mq['session_share_diff_max']:.3f}; controls in a different partition than their event: {mq.get('controls_in_other_partition')}")
            lines.append("* match rate by family: " + ", ".join(f"{k} {v['n_matched']}/{v['n_events']}" for k, v in (mq.get("by_family") or {}).items()))
        if mq.get("reasons"):
            lines.append(f"* reasons: {mq['reasons']}")
        pr = r.get("matching_prerevision")
        if pr:
            cur = {c: x["controls"] for c, x in lb["rates"].items()}
            lines.append(f"* PRE-REVISION controls ({pr['method']}, whole-sample ranks): {pr['n_controls']} controls, match rate {pr['match_rate']:.4f}, SMD {pr['smd']}; REVISED: {r['n_controls']} controls, match rate {mq.get('match_rate')}")
            lines.append("* control label rates pre -> revised: " + ", ".join(f"{c.replace('y_', '')} {('n/a' if v['rate'] is None else format(v['rate'], '.3f'))} (n={v['n']}) -> {('n/a' if cur[c]['rate'] is None else format(cur[c]['rate'], '.3f'))} (n={cur[c]['n_resolved']})" for c, v in pr["control_label_rates"].items()))
        cov = r["coverage"]
        d = cov["data"]
        lines += ["", "### (vii) Coverage", "",
                  f"* bars {d['first_bar_utc']} .. {d['last_bar_utc']} ({d['n_bars']} bars); gaps {d['n_gaps']} (intraday > 3 bars: {d['n_intraday_gaps_gt3bars']}, largest {d['largest_gap_hours']} h); missing UTC months: {d['missing_months_utc'] or 'none'}; gaps > 3 days: {d['gaps_longer_than_3_days'] or 'none'}",
                  f"* dev-end guard: {cov['dev_end_guard']}", f"* partitions: {cov['partitions']['counts']} (plan {cov['partitions']['plan']}; frozen split exists: {cov['partitions']['has_frozen_split']}). {cov['partitions']['note']}",
                  f"* events by family|variant|direction: {cov['events_by_family_variant_direction']}", f"* candidate exclusions: {cov['exclusions']}",
                  f"* runtime s {cov['runtime_s']}, peak memory MB {cov['peak_memory_mb']}", ""]
        if "sample_record" in r:
            lines += ["### Sample event record (features at the decision bar, non-null)", "", "```json", json.dumps(r["sample_record"], indent=1, default=str), "```", ""]
    lines += ["## Honest caveats", ""] + [f"* {c}" for c in CAVEATS] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import subprocess

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence"))
    ap.add_argument("--markets", nargs="*", default=list(ACTIVE))
    ap.add_argument("--phase2-root", default=None)
    ap.add_argument("--n-audit", type=int, default=300)
    ap.add_argument("--n-audit-controls", type=int, default=30)
    ap.add_argument("--n-window", type=int, default=60)
    ap.add_argument("--n-extended", type=int, default=30)
    ap.add_argument("--n-shallow", type=int, default=20)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    import entry_exit_quality as X

    p2 = X._find_phase2_root(a.phase2_root)
    p2 = None if p2 is None else str(p2)
    root = Path(a.root)
    results, missing = [], []
    for m in a.markets:
        cache = root / m / "gate_b.json"
        if cache.is_file() and not a.force:
            results.append(json.loads(cache.read_text(encoding="utf-8")))
            continue
        if not (root / m / "manifest.json").is_file():
            missing.append(m)
            continue
        print(f"{m}: gate B ...", flush=True)
        r = gate_b_market(m, root, p2, a)
        cache.write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
        results.append(r)
        print(f"{m}: {r['verdict']} blocking={r['blocking']} in {r['gate_b_seconds']}s", flush=True)
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    verdict = "PASS" if results and not missing and all(r["verdict"] == "PASS" for r in results) and len(results) == len(ACTIVE) else "FAIL"
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M"), "git_sha": sha, "root": a.root, "missing": missing, "verdict": verdict if not (missing or len(results) < len(ACTIVE)) else f"{verdict} (INCOMPLETE: {len(results)}/{len(ACTIVE)} markets)"}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "observer_backfill_report.md").write_text(render(results, meta), encoding="utf-8")
    slim = [{k: v for k, v in r.items() if k != "plausibility"} | {"plausibility": {k: v for k, v in r["plausibility"].items() if k != "column_summary"}} for r in results]
    (out / "observer_backfill_report.json").write_text(json.dumps(_clean({"meta": meta, "caveats": CAVEATS, "markets": slim}), indent=1, default=str), encoding="utf-8")
    print(f"GATE B: {meta['verdict']}; wrote {out / 'observer_backfill_report.md'}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
