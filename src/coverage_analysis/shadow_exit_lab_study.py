# ruff: noqa: E501
"""Lane W offline study: the SHADOW EXIT LAB over the Lane X development-bar entries + a random-entry control.

HINDSIGHT DIAGNOSTICS on development frames (the existing holdout guards stay in force; ORACLE is not used - the lab is
causal).  Every policy of ``demo.shadow_exit_lab`` runs on the SAME entry (same id / fill / initial stop / bars); nothing
is searched, tuned or promoted.  The RANDOM-ENTRY CONTROL runs the identical policies on entries with a random direction
at random operating-window bars (stop distance in ATR bootstrapped from the real entries of the same market, seeded) so
the reader can see what each policy yields on noise: a policy that looks the same on noise as on the family entries
shows an exit-rule effect, not family edge, and neither is an edge claim.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from coverage_analysis.entry_exit import MarketInputs, build_entry_rows
from demo.entry_exit_quality import MIN_CLUSTERS, SMALL_N
from demo.exit_policies import POLICY_PARAMS
from demo.opportunity.operating_policy import OperatingPolicy
from demo.shadow_exit_lab import (
    BASELINE_NAME,
    LAB_NAMES,
    SHADOW_LAB_VERSION,
    STATEMENT,
    summarise_groups,
    summarise_lab,
)

STUDY_VERSION = "swl-study-1"
CONTROL_OVERSAMPLE = 4  # random decision bars drawn per wanted control entry (operating-policy filters drop some)


def market_seed(market: str) -> int:
    """Fixed, documented per-market seed (no tuning knob)."""
    return 20260930 + zlib.crc32(market.encode("utf-8")) % 100_000


def _with_lab(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "lab": r["shadow_lab"]} for r in rows if r.get("shadow_lab")]


def control_candidates_fn(mi: MarketInputs, n_wanted: int, risk_atr_pool: Sequence[float], seed: int) -> Any:
    """Random-entry candidate generator for ``build_entry_rows(candidates_fn=...)``."""
    eval_idx = int(pd.DatetimeIndex(mi.frame["ts"]).searchsorted(mi.eval_from))

    def fn(d: Any, _fs: Any) -> Any:
        rng = np.random.default_rng(seed)
        n = len(d)
        idx = np.arange(max(eval_idx, 1), n - 1)
        ok = np.asarray(d.contig_next, dtype=bool)[idx] & (np.asarray(d.day)[idx + 1] == np.asarray(d.day)[idx]) & np.isfinite(np.asarray(d.atr)[idx]) & (np.asarray(d.atr)[idx] > 0)
        idx = idx[ok]
        if len(idx) == 0 or not risk_atr_pool:
            return SimpleNamespace(decision_idx=np.array([], dtype=np.int64), direction=np.array([], dtype=np.int8), stop=np.array([]))
        take = np.sort(rng.choice(idx, size=min(len(idx), max(1, n_wanted * CONTROL_OVERSAMPLE)), replace=False))
        direction = rng.choice(np.array([-1, 1], dtype=np.int8), size=len(take))
        mult = rng.choice(np.asarray(risk_atr_pool, dtype=float), size=len(take))
        stop = np.asarray(d.o)[take + 1] - direction * mult * np.asarray(d.atr)[take]
        return SimpleNamespace(decision_idx=take.astype(np.int64), direction=direction, stop=stop)

    return fn


def run_market(mi: MarketInputs, op: OperatingPolicy, *, limit: int | None = None) -> dict[str, Any]:
    real, excl = build_entry_rows(mi, op, limit=limit, shadow_lab=True)
    pool = [float(r["risk_atr"]) for r in real if r.get("risk_atr")]
    seed = market_seed(mi.market)
    ctrl: list[dict[str, Any]] = []
    excl_c: dict[str, int] = {}
    if real:
        stub = SimpleNamespace(
            spec=SimpleNamespace(mode="random"), thr=None, strategy_id="RANDOM_CONTROL", family="RANDOM_CONTROL", role="CONTROL",
        )
        mi_c = MarketInputs(market=mi.market, data=mi.data, frame=mi.frame, specs=[stub], tick_size=mi.tick_size, eval_from=mi.eval_from, tz=mi.tz)
        ctrl_all, excl_c = build_entry_rows(mi_c, op, shadow_lab=True, candidates_fn=control_candidates_fn(mi, len(real), pool, seed))
        if len(ctrl_all) > len(real):
            pick = np.sort(np.random.default_rng(seed + 1).choice(len(ctrl_all), size=len(real), replace=False))
            ctrl = [ctrl_all[int(i)] for i in pick]
        else:
            ctrl = ctrl_all
    return {"real": real, "control": ctrl, "excl": excl, "excl_control": excl_c, "seed": seed}


def aggregate_market(res: Mapping[str, Any]) -> dict[str, Any]:
    real, ctrl = _with_lab(res["real"]), _with_lab(res["control"])
    ms = sorted(float(r["shadow_lab_ms"]) for r in res["real"] if r.get("shadow_lab_ms") is not None)
    cost = {
        "n": len(ms), "mean_ms": (sum(ms) / len(ms)) if ms else None, "median_ms": ms[len(ms) // 2] if ms else None,
        "p95_ms": ms[min(len(ms) - 1, int(0.95 * len(ms)))] if ms else None, "max_ms": ms[-1] if ms else None,
        "mean_bars": (sum(r["shadow_lab_bars"] for r in res["real"]) / len(res["real"])) if res["real"] else None,
    }
    return {
        "lab_cost": cost, "n_real": len(real), "n_control": len(ctrl), "seed": res["seed"], "excl": res["excl"], "excl_control": res["excl_control"],
        "real": summarise_groups(real), "control": summarise_lab(ctrl) if ctrl else {"n": 0},
    }


# ---- rendering --------------------------------------------------------------------------------------------
def _f(x: Any, nd: int = 3) -> str:
    return "-" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def _cell_table(cell: Mapping[str, Any], ctrl: Mapping[str, Any] | None) -> list[str]:
    L = ["| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |", "|---|---|---|---|---|---|---|---|---|---|"]
    for name, p in cell["policies"].items():
        c = (ctrl or {}).get("policies", {}).get(name) if ctrl and ctrl.get("n") else None
        pr = cell["paired_vs_fixed_1_5r"].get(name)
        pair = "-" if pr is None else f"{_f(pr['mean_diff_r'])} ({pr['n_pairs']}/{pr['n_event_clusters']}{', n too small' if pr['small_n'] else ''})"
        L.append(
            f"| {name} | {p['n_applicable']} | {_f(p['share_not_applicable'], 2)} | {_f(p['mean_r'])} | {_f(p['median_r'])} | {_f(p['mean_capture_ratio'], 2)} | "
            f"{_f(p['mean_giveback_r'])} | {_f(None if c is None else c['median_r'])} | {_f(None if c is None else c['mean_r'])} | {pair} |"
        )
    return L


def render_market_md(market: str, agg: Mapping[str, Any]) -> str:
    real = agg["real"]
    ov = real["overall"]
    L = [f"## {market}", ""]
    if not ov.get("n"):
        return "\n".join([*L, "No entries (no data / no frozen spec applies)."]) + "\n"
    flag = " - **n too small** (descriptive only)" if ov["small_n"] else ""
    L += [f"Family entries: n {ov['n']}, independent event clusters {ov['n_event_clusters']}, same-entry assertion {'OK' if ov['same_entry_assertion'] else 'VIOLATED'}{flag}; "
          f"random-entry control: n {agg['n_control']} (seed {agg['seed']}). Lab cost per entry (12 policies, one process): "
          f"mean {_f(agg['lab_cost']['mean_ms'], 1)} ms, median {_f(agg['lab_cost']['median_ms'], 1)} ms, p95 {_f(agg['lab_cost']['p95_ms'], 1)} ms (mean {_f(agg['lab_cost']['mean_bars'], 0)} bars).", "", "### All family entries (real) vs random-entry control", ""]
    L += _cell_table(ov, agg["control"])
    L.append("")
    for g, c in real["groups"].items():
        fl = " - **n too small**" if c["small_n"] else ""
        L += [f"### {g} (n {c['n']}, clusters {c['n_event_clusters']}){fl}", ""]
        L += _cell_table(c, None)
        L.append("")
    return "\n".join(L) + "\n"


def render_markdown(result: Mapping[str, Any]) -> str:
    m = result["meta"]
    L = [
        "# Shadow exit lab (Lane W) - offline run", "",
        f"`{m['study_version']}` / lab `{m['lab_version']}` / policies `{m['policy_set_version']}` / baseline `{m['baseline']}`.", "",
        f"**{STATEMENT}.** Hindsight diagnostics on development bars; no edge claim.", "",
        "## Headline: median R per policy, family entries vs random-entry control", "",
        "| market | n (clusters) | " + " | ".join(LAB_NAMES) + " |", "|---|---|" + "---|" * len(LAB_NAMES),
    ]
    for mk, a in result["markets"].items():
        ov = a["real"]["overall"]
        if not ov.get("n"):
            L.append(f"| {mk} | 0 | " + " | ".join("-" for _ in LAB_NAMES) + " |")
            continue
        cells = []
        for name in LAB_NAMES:
            r = ov["policies"][name]["median_r"]
            c = a["control"]["policies"][name]["median_r"] if a["control"].get("n") else None
            cells.append(f"{_f(r, 2)} / {_f(c, 2)}")
        L.append(f"| {mk} | {ov['n']} ({ov['n_event_clusters']}){' low n' if ov['small_n'] else ''} | " + " | ".join(cells) + " |")
    L += ["", "Cell format: median R of the family entries / median R of the random-entry control (same policies, same bars source). "
          "A median is not an expectancy; compare means and the paired differences below before reading anything into a policy.", ""]
    if "cross_market" in result:
        cm = result["cross_market"]
        L += ["## All active markets pooled (event-cluster aware)", "", f"n {cm['n']}, event clusters {cm['n_event_clusters']}.", "", *_cell_table(cm, result.get("cross_market_control")), ""]
    costs = [(a["lab_cost"]["n"], a["lab_cost"]["mean_ms"], a["lab_cost"]["p95_ms"]) for a in result["markets"].values() if a.get("lab_cost", {}).get("n")]
    if costs:
        tot = sum(n for n, _, _ in costs)
        mean = sum(n * m for n, m, _ in costs) / tot
        L += ["## Forward hook cost and default", "",
              f"Measured on {tot} real development-bar entries (12 policies, one process, uncontended-per-worker): mean {mean:.1f} ms per entry, worst per-market p95 {max(p for _, _, p in costs):.0f} ms. "
              "The hook runs at outcome / label time on the runner thread (never in the order path, exception-contained, capped per cycle). "
              "Because up to 2 x the per-cycle cap of entries can be evaluated in one 15-minute label cycle on the same thread that manages open positions, the runner flag "
              "`shadow_exit_lab_enabled` (CLI `--shadow-exit-lab`) defaults to OFF; enable it deliberately once the live runner has been observed with it.", ""]
    L += ["## Notes and caveats", ""] + [f"- {c}" for c in m["caveats"]] + [""]
    for mk, a in result["markets"].items():
        L += [render_market_md(mk, a)]
    return "\n".join(L)


def to_json(result: Mapping[str, Any]) -> str:
    return json.dumps(result, indent=1, sort_keys=True, default=str)


def meta_block(extra_notes: Sequence[str], caveats: Sequence[str]) -> dict[str, Any]:
    return {
        "study_version": STUDY_VERSION, "lab_version": SHADOW_LAB_VERSION, "policy_set_version": POLICY_PARAMS["version"],
        "baseline": BASELINE_NAME, "policy_params": POLICY_PARAMS, "policy_names": list(LAB_NAMES), "small_n": SMALL_N,
        "min_clusters": MIN_CLUSTERS, "statement": STATEMENT, "notes": list(extra_notes), "caveats": list(caveats),
    }


def pooled(all_real: Sequence[Mapping[str, Any]], all_ctrl: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    real, ctrl = _with_lab(all_real), _with_lab(all_ctrl)
    return summarise_lab(real), (summarise_lab(ctrl) if ctrl else {"n": 0})
