# ruff: noqa: E501
"""Lane W CLI: SHADOW EXIT LAB over the Lane X development-bar entries + random-entry control (offline, hindsight diagnostics).

    uv run python scripts/shadow_exit_lab.py --days 0 --out docs/evidence [--jobs 4] \
        [--data-root <dir with markets/>] [--phase2-root <dir with markets/manifest_BTCUSD.json>] [--markets GER40 ...]

Reads ONLY development bars through the existing loaders (the 2026-08-31 holdout guard stays in force) and the frozen
production spec.  Never calls MT5, never opens ``artifacts/``, never places an order.  Writes
``<out>/shadow_exit_lab.{md,json}`` and ``<out>/shadow_exit_lab_<MARKET>.md``.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import entry_exit_quality as eeq  # noqa: E402

from alpha.common.market_data import DEV_END  # noqa: E402
from coverage_analysis.shadow_exit_lab_study import (  # noqa: E402
    aggregate_market,
    meta_block,
    pooled,
    render_markdown,
    render_market_md,
    run_market,
    to_json,
)
from demo.opportunity.operating_policy import load_operating_policy  # noqa: E402


def analyse(m: str, days: int, limit: int | None, data_root: str | None, p2root: str | None) -> dict:
    t0 = time.time()
    op = load_operating_policy()
    mi, cut, _eval_from, _tz = eeq.load_market_inputs(m, days, data_root, p2root)
    if mi is None:
        return {"market": m, "agg": None, "real": [], "control": [], "note": f"{m}: NO bar data available (phase-2 root not found)"}
    res = run_market(mi, op, limit=limit)
    agg = aggregate_market(res)
    ms = int((time.time() - t0) * 1000)
    n = len(res["real"]) + len(res["control"])
    print(f"{m}: {len(res['real'])} real + {len(res['control'])} control entries in {ms / 1000:.0f}s ({ms / max(n, 1):.1f} ms/entry incl. data build)", flush=True)
    span = f"{cut['ts'].iloc[0].date()}..{cut['ts'].iloc[-1].date()} ({len(cut)} M5 bars)"
    return {"market": m, "agg": agg, "real": res["real"], "control": res["control"], "note": f"{m}: {span}; real n={len(res['real'])}, control n={len(res['control'])}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--phase2-root", default=None)
    ap.add_argument("--markets", nargs="*", default=list(eeq.ACTIVE_MARKETS))
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--limit", type=int, default=None, help="debug: cap the analysed entries per market")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence"))
    ap.add_argument("--tag", default="shadow_exit_lab")
    a = ap.parse_args(argv)
    p2root = eeq._find_phase2_root(a.phase2_root)
    p2 = None if p2root is None else str(p2root)
    if a.jobs > 1 and len(a.markets) > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            done = [f.result() for f in [ex.submit(analyse, m, a.days, a.limit, a.data_root, p2) for m in a.markets]]
    else:
        done = [analyse(m, a.days, a.limit, a.data_root, p2) for m in a.markets]
    markets = {d["market"]: d["agg"] if d["agg"] is not None else {"real": {"overall": {"n": 0}}, "control": {"n": 0}, "n_real": 0, "n_control": 0, "seed": None, "excl": {}, "excl_control": {}} for d in done}
    notes = [d["note"] for d in done]
    all_real = [r for d in done for r in d["real"]]
    all_ctrl = [r for d in done for r in d["control"]]
    cm, cmc = pooled(all_real, all_ctrl)
    caveats = [
        "Hindsight diagnostics on a development window; entries of overlapping signals are not independent (event clusters; four STRUCT variants of one break are ONE observation); no edge claim, no promotion.",
        f"Development end {DEV_END} (Berlin date) is enforced by the existing loaders: no holdout bar was opened.",
        "Family generators run on Train-fitted frozen thresholds (fit_end 2026-06-30 for the five core markets): the evaluation window overlaps that fit window (in-sample for the thresholds) - another reason the numbers are descriptive only.",
        "BTCUSD M5 history lacks Oct-2025 and Mar-2026 (DST fold months); BTCUSD/BRENT have no frozen split and their STRUCT constants are discovery placeholders.",
        "The lab is CAUSAL: ORACLE / hindsight levels are not used; structural TP1/TP2/trail use only closed bars up to each decision time; absent levels -> NOT_APPLICABLE (never invented).",
        "Fills at the next bar open (long: ask), stop-first bar semantics, spread-adjusted gross R, no commission / slippage / swap; intrabar order of high/low unknown (conservative: adverse first).",
        "Policy parameters are the predeclared constants of POLICY_SET_VERSION; nothing was searched or tuned on these results; failed_move_exit applies to STRUCT entries only (the broken range edge); TP1_TP2_runner needs a structural TP2.",
        "Random-entry control: random direction, random operating-window decision bars, stop distance (ATR) bootstrapped from the real entries of the same market, fixed per-market seed; no family logic, so failed_move_exit is NOT_APPLICABLE there. It shows what a policy yields on noise given the same cost and window structure, not an edge benchmark.",
        "Rows are overlapping paths on the same bars: the pooled table adds no independent evidence beyond the event clusters shown.",
    ]
    result = {"meta": meta_block(notes, caveats), "markets": markets, "cross_market": cm, "cross_market_control": cmc}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.tag}.md").write_text(render_markdown(result), encoding="utf-8")
    (out / f"{a.tag}.json").write_text(to_json(result), encoding="utf-8")
    for m, agg in markets.items():
        (out / f"{a.tag}_{m}.md").write_text(f"# Lane W - {m}\n\nshadow exit lab, hindsight diagnostics, no edge claim.\n\n" + render_market_md(m, agg), encoding="utf-8")
    print(f"wrote {out / (a.tag + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
