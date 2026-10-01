# ruff: noqa: E501
"""Blocking balance gate of the observer-controls-3 controls (OFFLINE / RESEARCH ONLY; reads counts, manifests, matching covariates and label AVAILABILITY only).

    uv run python scripts/observer_controls_balance.py --root <backfill out dir> [--markets GER40 ...] [--set a|b|both] [--out FILE.json]

Per market x partition (TRAIN / VALIDATION / FROZEN_OOS): match rate >= 0.90; |SMD| <= 0.10 for local_minute, atr_pct, spread_pct; max session-share difference <= 0.02;
|censored share (controls) - censored share (events)| <= 0.05. A market with any partition that has events but does not PASS (FAIL / INSUFFICIENT_N) is
``descriptive_only`` (never silently dropped). Recomputed from ``<root>/<MARKET>/controls3[_b]/controls_diag.parquet`` + ``labels`` availability (the gate never
looks at a feature-vs-label distribution). Exit code 0 when every market passes, 1 otherwise (informational; the verdict is the JSON).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ACTIVE = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD", "BTCUSD", "BRENT")
DEFAULT_ROOT = "C:/Users/yanni/AppData/Local/Temp/observer_backfill"


def run(root: Path, markets: list[str], sets: tuple[str, ...]) -> dict:
    from coverage_analysis.observer_lab.backfill_controls3 import SUBDIRS, gate_for_dir

    res: dict = {}
    for m in markets:
        mdir = root / m
        res[m] = {}
        for s in sets:
            sub = mdir / SUBDIRS[s]
            if not (sub / "controls_diag.parquet").is_file():
                res[m][s] = {"market_status": "NO_CONTROLS3", "note": f"{sub} has no controls_diag.parquet; run observer_backfill.py --step controls"}
                continue
            g = gate_for_dir(mdir, sub)
            (sub / "balance_gate.json").write_text(json.dumps(g, indent=1, default=str), encoding="utf-8")
            res[m][s] = g
    return res


def render(res: dict) -> str:
    lines = ["market | set | partition | events | matched | match | SMD min/atr/spr | sess | cens ev/ctl | verdict"]
    for m, per in res.items():
        for s, g in per.items():
            if "partitions" not in g:
                lines.append(f"{m} | {s} | {g.get('market_status')}")
                continue
            for p, r in g["partitions"].items():
                smd = r.get("smd") or {}
                cs = r.get("censored_share") or {}
                lines.append(
                    f"{m} | {s} | {p} | {r['n_events']} | {r.get('n_matched_events', 0)} (ctl {r['n_controls']}) | {r.get('match_rate', float('nan')):.3f} | "
                    f"{smd.get('local_minute', float('nan')):+.3f}/{smd.get('atr_pct', float('nan')):+.3f}/{smd.get('spread_pct', float('nan')):+.3f} | {r.get('session_share_diff_max', float('nan')):.3f} | "
                    f"{cs.get('events', float('nan')):.3f}/{cs.get('controls', float('nan')):.3f} | {r['verdict']}" + (f" ({'; '.join(r['fail_reasons'])})" if r.get("fail_reasons") and r["verdict"] != "PASS" else ""))
            lines.append(f"{m} | {s} | MARKET -> {g['market_status']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--markets", nargs="*", default=None)
    ap.add_argument("--set", choices=("a", "b", "both"), default="a")
    ap.add_argument("--out", default=None, help="write the full result JSON here (default <root>/controls3_balance.json)")
    a = ap.parse_args(argv)
    root = Path(a.root)
    res = run(root, a.markets or list(ACTIVE), ("a", "b") if a.set == "both" else (a.set,))
    out = Path(a.out) if a.out else root / "controls3_balance.json"
    out.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print(render(res))
    return 0 if all(g.get("market_status") == "analysis_eligible" for per in res.values() for g in per.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
