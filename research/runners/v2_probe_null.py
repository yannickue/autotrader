# ruff: noqa: E501
"""V2 probe NULL CALIBRATION: the SAME probe pipeline on synthetic frames derived from the real dev frame.

Research only.  Question answered: is the best-of-N Train result of the real probe more than what the
identical search finds on data whose exploitable structure has been destroyed?  Nothing here reads a fold
TEST side (sealed lean Train evaluation, exactly like ``v2_probe``).

Null frames (timestamps, spread, volume / session structure never change; OHLC rebuilt consistently;
features + events + thresholds are rebuilt on every null frame through ``v2_probe.build_real_context``):

  A  ``sign_flip``      every Berlin day's log returns (incl. its opening-gap return) x a random +-1, high/low
                        offsets swapped on mirrored days: preserves |return| per bar, intraday shape, vol,
                        spread and time-of-day structure; destroys drift and cross-day directional persistence.
  B  ``block_shuffle``  whole Berlin days are permuted among days of the SAME layout (same bar count and
                        first-bar minute): each day's path is intact, cross-day dependence is destroyed.
                        Days with a unique layout stay in place.
  C  ``zero_drift``     per segment (search-train / rest) mean log return subtracted (zero drift), then the M5
                        (return, OHLC-offset) pairs are shuffled WITHIN each Berlin day over the day's
                        contiguous bars (anchors = day-open / gap bars stay in place): destroys intraday
                        autocorrelation and intraday time-of-day structure (V1 zero-drift null).

    python research/runners/v2_probe_null.py run --markets GER40 --seeds 6 --types A B C
    python research/runners/v2_probe_null.py report
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from alpha.common.protocol import stable_hash  # noqa: E402
from alpha.discovery.disk import assert_free_space  # noqa: E402
from alpha.discovery.folds import berlin_dates_from_ts_ns, make_folds  # noqa: E402
from alpha.discovery.v2_select import (  # noqa: E402
    SelectConfig,
    corr_matrix,
    deflated_sharpe,
    effective_independent_trials,
    select_finalists,
)
from research.runners import v2_probe  # noqa: E402

NULL_TYPES = {"A": "sign_flip", "B": "block_shuffle", "C": "zero_drift"}
NULL_SEEDS = tuple(range(20300001, 20300013))  # up to 12 seeds per (market, type)
BAR_S = 300
REPORT_DIR = REPO_ROOT / "research/reports/v2_probe_r1"
STAT_KEYS = ("n_passers", "best_e", "best_t", "n_e_pos", "n_t_gt2", "p95_e")


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- frame builders
def _tick_round(x: np.ndarray, tick: float) -> np.ndarray:
    return np.round(np.round(x / tick) * tick, 10)


def _structure(dev: pd.DataFrame) -> dict[str, np.ndarray]:
    ts = pd.DatetimeIndex(dev["ts"])
    ts_s = ts.as_unit("s").asi8
    dates = ts.tz_convert("Europe/Berlin").normalize().tz_localize(None).to_numpy().astype("datetime64[D]")
    _, day_id = np.unique(dates, return_inverse=True)
    n = len(dev)
    o, h, lo, c = (dev[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    ret = np.zeros(n)
    ret[1:] = np.log(c[1:] / c[:-1])
    gap_ok = np.zeros(n, dtype=bool)
    gap_ok[1:] = np.diff(ts_s) == BAR_S
    same_day = np.zeros(n, dtype=bool)
    same_day[1:] = day_id[1:] == day_id[:-1]
    local = ts.tz_convert("Europe/Berlin")
    return {"day": day_id.astype(np.int64), "ret": ret, "offs": np.stack([o - c, h - c, lo - c], axis=1),
            "c0": np.array([c[0]]), "cont": gap_ok & same_day, "minute": (local.hour * 60 + local.minute).to_numpy()}


def _rebuild(dev: pd.DataFrame, c0: float, new_ret: np.ndarray, offs: np.ndarray, tick: float) -> pd.DataFrame:
    new_c = _tick_round(c0 * np.exp(np.cumsum(new_ret)), tick)
    o = _tick_round(new_c + offs[:, 0], tick)
    hi = np.maximum(_tick_round(new_c + offs[:, 1], tick), np.maximum(o, new_c))
    lo = np.minimum(_tick_round(new_c + offs[:, 2], tick), np.minimum(o, new_c))
    out = dev.copy()
    out["open"], out["high"], out["low"], out["close"] = o, hi, lo, new_c
    return out


def null_sign_flip(dev: pd.DataFrame, seed: int, tick: float) -> pd.DataFrame:
    """NULL-A: random +-1 per Berlin day on all log returns of that day (gap return included)."""
    st = _structure(dev)
    rng = np.random.default_rng([seed, 1])
    day = st["day"]
    sign = rng.choice(np.array([-1.0, 1.0]), size=int(day.max()) + 1)[day]
    offs = st["offs"].copy()
    flip = sign < 0
    offs[flip, 0] = -st["offs"][flip, 0]
    offs[flip, 1] = -st["offs"][flip, 2]
    offs[flip, 2] = -st["offs"][flip, 1]
    return _rebuild(dev, float(st["c0"][0]), st["ret"] * sign, offs, tick)


def null_block_shuffle(dev: pd.DataFrame, seed: int, tick: float) -> pd.DataFrame:
    """NULL-B: permute whole Berlin days among days with the same layout (bar count, first-bar minute)."""
    st = _structure(dev)
    rng = np.random.default_rng([seed, 2])
    day, minute = st["day"], st["minute"]
    n_days = int(day.max()) + 1
    first = np.searchsorted(day, np.arange(n_days), side="left")
    last = np.searchsorted(day, np.arange(n_days), side="right")
    groups: dict[tuple[int, int], list[int]] = {}
    for d in range(1, n_days):  # day 0 holds bar 0 (no return): fixed
        groups.setdefault((int(last[d] - first[d]), int(minute[first[d]])), []).append(d)
    new_ret, new_offs = st["ret"].copy(), st["offs"].copy()
    for members in groups.values():
        if len(members) < 2:
            continue
        src = rng.permutation(members)
        for d_dst, d_src in zip(members, src, strict=True):
            new_ret[first[d_dst]:last[d_dst]] = st["ret"][first[d_src]:last[d_src]]
            new_offs[first[d_dst]:last[d_dst]] = st["offs"][first[d_src]:last[d_src]]
    return _rebuild(dev, float(st["c0"][0]), new_ret, new_offs, tick)


def null_zero_drift(dev: pd.DataFrame, seed: int, tick: float, segment: np.ndarray | None = None) -> pd.DataFrame:
    """NULL-C: zero drift per segment, then within-day shuffle of the contiguous bars' (return, offsets)."""
    st = _structure(dev)
    rng = np.random.default_rng([seed, 3])
    n = len(dev)
    seg = np.zeros(n, dtype=np.int64) if segment is None else np.asarray(segment, dtype=np.int64)
    ret = st["ret"].copy()
    idx = np.arange(n)
    for sg in np.unique(seg):
        sel = (seg == sg) & (idx > 0)
        if sel.any():
            ret[sel] -= ret[sel].mean()
    new_ret, new_offs = ret.copy(), st["offs"].copy()
    cont, day = st["cont"], st["day"]
    n_days = int(day.max()) + 1
    first = np.searchsorted(day, np.arange(n_days), side="left")
    last = np.searchsorted(day, np.arange(n_days), side="right")
    for d in range(n_days):
        pos = np.flatnonzero(cont[first[d]:last[d]]) + first[d]  # contiguous continuation bars; anchors stay
        if len(pos) < 2:
            continue
        src = rng.permutation(pos)
        new_ret[pos] = ret[src]
        new_offs[pos] = st["offs"][src]
    return _rebuild(dev, float(st["c0"][0]), new_ret, new_offs, tick)


def make_null(kind: str, dev: pd.DataFrame, seed: int, tick: float, segment: np.ndarray | None = None) -> pd.DataFrame:
    if kind == "A":
        return null_sign_flip(dev, seed, tick)
    if kind == "B":
        return null_block_shuffle(dev, seed, tick)
    if kind == "C":
        return null_zero_drift(dev, seed, tick, segment)
    raise ValueError(f"unknown null type {kind!r}")


def train_segment(dev: pd.DataFrame, cfg: dict) -> np.ndarray:
    """0 = search-train bar (fold 0 train side), 1 = rest; same fold call as ``build_real_context``."""
    dates = berlin_dates_from_ts_ns(pd.DatetimeIndex(dev["ts"]).as_unit("ns").asi8)
    f = cfg["folds"]
    folds = make_folds(dates, f["n_folds"], f["embargo_days"], f["purge_bars"],
                       initial_train_frac=f["initial_train_frac"], min_test_days=f["min_test_days"])
    return (~folds[0].train_mask).astype(np.int64)


# --------------------------------------------------------------------------- per-run summary
def _q(values: list[float], qs=(0.05, 0.5, 0.95)) -> dict[str, float] | None:
    a = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if len(a) == 0:
        return None
    out = {f"p{int(q * 100):02d}": round(float(np.quantile(a, q)), 6) for q in qs}
    out["max"] = round(float(a.max()), 6)
    return out


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def run_stats(rows: list[dict], daily: dict[str, np.ndarray], select_cfg: SelectConfig | None = None) -> dict[str, Any]:
    """Compact statistics of one probe run (real or null) from ``v2_probe.collect_stats`` rows."""
    passers = [r for r in rows if r["passer"]]
    e = [_f(r["e_adv"]) for r in passers]
    e = [v for v in e if v is not None]
    t = [_f(r["t_day"]) for r in passers]
    t = [v for v in t if v is not None]
    sel = select_finalists(rows, daily, select_cfg or SelectConfig(k=5))
    by_hash = {r["hash"]: r for r in rows}
    top5 = [{"e_adv": _f(by_hash[f["hash"]]["e_adv"]), "t_day": _f(by_hash[f["hash"]]["t_day"]),
             "n_trades": by_hash[f["hash"]]["n_trades"], "tpd": _f(by_hash[f["hash"]]["tpd"]),
             "payoff": _f(by_hash[f["hash"]]["payoff"]), "e_shock": _f(by_hash[f["hash"]]["e_shock"])}
            for f in sel["finalists"]]
    return {
        "n_specs": len(rows), "n_passers": len(passers), "best_e": max(e) if e else None,
        "best_t": max(t) if t else None, "n_e_pos": int(sum(v > 0 for v in e)), "n_t_gt2": int(sum(v > 2 for v in t)),
        "p95_e": float(np.quantile(e, 0.95)) if e else None, "median_e": float(np.median(e)) if e else None,
        "e_quantiles": _q(e), "n_eligible": sel["n_eligible"], "n_clusters_eligible": sel["n_clusters"],
        "top5_pareto": top5,
    }


# --------------------------------------------------------------------------- run one null
def run_one(market: str, kind: str, seed: int, cfg: dict, n_candidates: int, cache_root: Path,
            out_dir: Path) -> dict[str, Any]:
    """Build the null context of (market, kind, seed), run the probe search + stats, delete the cache."""
    out_path = out_dir / f"null_{market}_{kind}_{seed}.json"
    cache_dir = cache_root / f"{market}_{kind}_{seed}"
    t0 = time.perf_counter()
    try:
        from markets.spec import load_market_spec

        tick = float(load_market_spec(market).tick_size)

        def transform(dev: pd.DataFrame) -> pd.DataFrame:
            seg = train_segment(dev, cfg) if kind == "C" else None
            return make_null(kind, dev, seed, tick, seg)

        assert_free_space(cache_dir)
        ctx = v2_probe.build_real_context(market, cfg, cache_dir, dev_transform=transform)
        t_build = time.perf_counter() - t0
        ev = v2_probe.make_evaluator(ctx, cfg, None, None)  # in-memory results only (no on-disk result cache)
        timings: dict[str, float] = {}
        t1 = time.perf_counter()
        v2_probe.run_search(ev, ctx, cfg, n_candidates, cfg["seed"], timings)
        rows, daily, _ = v2_probe.collect_stats(ev, ctx)
        stats = run_stats(rows, daily)
        result = {
            "market": market, "type": kind, "type_name": NULL_TYPES[kind], "null_seed": seed,
            "search_seed": cfg["seed"], "n_candidates": n_candidates, "config_hash": stable_hash(cfg),
            "unique_specs": ev.ledger.unique, "stats": stats, "frame_build_s": round(t_build, 1),
            "search_stats_s": round(time.perf_counter() - t1, 1), "peak_rss_mb": v2_probe.peak_rss_mb(),
            "scope": "TRAIN side of search fold 0 only (null frame); no fold-test number",
        }
        out_path.write_text(json.dumps(result, indent=1, sort_keys=True, allow_nan=False), encoding="utf-8")
        return result
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


# --------------------------------------------------------------------------- calibration report
def _emp_p(real: float | None, nulls: list[float]) -> float | None:
    """One-sided empirical p = (1 + #{null >= real}) / (1 + K)."""
    if real is None or not nulls:
        return None
    return round((1 + sum(1 for v in nulls if v >= real - 1e-12)) / (1 + len(nulls)), 4)


def _dist(vals: list[float]) -> dict[str, float] | None:
    a = np.asarray([v for v in vals if v is not None], dtype=float)
    if len(a) == 0:
        return None
    return {"n": len(a), "min": round(float(a.min()), 5), "p50": round(float(np.quantile(a, 0.5)), 5),
            "p95": round(float(np.quantile(a, 0.95)), 5), "p99": round(float(np.quantile(a, 0.99)), 5),
            "max": round(float(a.max()), 5)}


def real_block(market: str, report_dir: Path, select_cfg: SelectConfig | None = None) -> dict[str, Any] | None:
    d = report_dir / market
    if not (d / "stats.csv.gz").exists():
        return None
    df, daily = v2_probe.load_stats(d)
    rows = [{k: (None if (isinstance(v, float) and math.isnan(v)) else v) for k, v in r.items()}
            for r in df.to_dict("records")]
    for r in rows:
        r["passer"] = bool(r["passer"])
        r["n_trades"] = int(r["n_trades"] or 0)
    st = run_stats(rows, daily)
    cfg = select_cfg or SelectConfig()
    sel = select_finalists(rows, daily, SelectConfig(k=cfg.k, tpd_cap=cfg.tpd_cap, corr_threshold=cfg.corr_threshold,
                                                     min_trades=cfg.min_trades))
    # effective independent trials + informational deflated Sharpe (over eligible passers' daily-R vectors)
    elig = [r["hash"] for r in rows if r["passer"] and r["hash"] in daily]
    mat = np.stack([daily[h] for h in elig]) if elig else np.zeros((0, 1))
    order = np.argsort([-(_f(r["t_day"]) or -1e9) for r in rows if r["passer"] and r["hash"] in daily], kind="stable")
    eff = effective_independent_trials(mat, order, cfg.corr_threshold) if elig else {"n_clusters": 0}
    dsr = {}
    if elig and eff["n_clusters"] >= 1:
        sharpes = []
        for li in eff["leaders"]:
            x = mat[li]
            sharpes.append(float(x.mean() / x.std(ddof=1)) if x.std() > 0 else 0.0)
        var_sr = float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else 0.0
        by_hash = {r["hash"]: r for r in rows}
        best_t_h = max(elig, key=lambda h: _f(by_hash[h]["t_day"]) if _f(by_hash[h]["t_day"]) is not None else -1e9)
        best_e_h = max(elig, key=lambda h: _f(by_hash[h]["e_adv"]) if _f(by_hash[h]["e_adv"]) is not None else -1e9)
        dsr = {"note": "INFORMATIONAL (per-day Sharpe of daily R; N = effective independent trials)",
               "n_eff": eff["n_clusters"], "sharpe_var_across_leaders": round(var_sr, 8),
               "best_t_candidate": deflated_sharpe(mat[elig.index(best_t_h)], var_sr, eff["n_clusters"]),
               "best_e_candidate": deflated_sharpe(mat[elig.index(best_e_h)], var_sr, eff["n_clusters"])}
    corr_hi = None
    if len(mat) > 1:
        c = corr_matrix(mat)
        iu = np.triu_indices(len(mat), 1)
        corr_hi = round(float((c[iu] > cfg.corr_threshold).mean()), 4)
    return {"stats": st, "effective_independent_trials": {k: v for k, v in eff.items() if k != "leaders"},
            "deflated_sharpe_informational": dsr, "share_pairs_corr_gt_thr": corr_hi,
            "selection_preview": {"n_eligible": sel["n_eligible"], "n_fronts": sel["n_fronts"],
                                  "n_clusters": sel["n_clusters"], "k": len(sel["finalists"])}}


def build_report(report_dir: Path = REPORT_DIR) -> dict[str, Any]:
    files = sorted(report_dir.glob("null_*_*_*.json"))
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in files if not p.name.startswith("null_calibration")]
    runs = [r for r in runs if "stats" in r]
    markets = sorted({r["market"] for r in runs} | {p.parent.name for p in report_dir.glob("*/stats.csv.gz")})
    out: dict[str, Any] = {"version": "v2-null-calibration-v1", "scope": "TRAIN side of search fold 0; null frames "
                           "derived from the real dev frame; same probe code path / seed / config",
                           "null_types": NULL_TYPES, "markets": {}}
    for m in markets:
        real = real_block(m, report_dir)
        mr = [r for r in runs if r["market"] == m]
        entry: dict[str, Any] = {"real": real, "null": {}}
        for kind in [*list(NULL_TYPES), "ALL"]:
            sel = [r for r in mr if kind == "ALL" or r["type"] == kind]
            if not sel:
                continue
            block: dict[str, Any] = {"n_runs": len(sel), "seeds": sorted(r["null_seed"] for r in sel), "stats": {}}
            for key in STAT_KEYS:
                vals = [r["stats"][key] for r in sel if r["stats"].get(key) is not None]
                real_v = None if real is None else real["stats"].get(key)
                block["stats"][key] = {"null": _dist(vals), "real": real_v, "empirical_p": _emp_p(real_v, vals)}
            entry["null"][kind] = block
        out["markets"][m] = entry
    return out


def render_md(rep: dict[str, Any]) -> str:
    lines = ["# V2 probe null calibration (Train side of search fold 0)", "",
             "Null types: A sign_flip, B block_shuffle, C zero_drift. Empirical p = (1 + #null>=real)/(1 + K), one-sided;",
             "resolution is 1/(K+1): K = number of null runs.", ""]
    for m, e in rep["markets"].items():
        real = e["real"]
        lines.append(f"## {m}")
        if real is None:
            lines += ["(no real stats)", ""]
            continue
        st, eff = real["stats"], real["effective_independent_trials"]
        lines.append(f"real: passers {st['n_passers']}, best E {st['best_e']:.4f}, best t {st['best_t']:.2f}, "
                     f"n(E>0) {st['n_e_pos']}, n(t>2) {st['n_t_gt2']}, p95 E {st['p95_e']:.4f}, median E {st['median_e']:.4f}; "
                     f"distinct behaviours (corr>0.8) {eff.get('n_clusters')} of {eff.get('n_strategies')}")
        d = real["deflated_sharpe_informational"]
        if d:
            lines.append(f"deflated Sharpe (informational, N_eff {d['n_eff']}): best-t candidate {d['best_t_candidate']}; "
                         f"best-E candidate {d['best_e_candidate']}")
        for kind, blk in e["null"].items():
            lines.append(f"- null {kind} ({blk['n_runs']} runs):")
            for key in STAT_KEYS:
                x = blk["stats"][key]
                nd = x["null"]
                if nd is None:
                    continue
                lines.append(f"  - {key}: real {x['real']:.4g} | null min {nd['min']} p50 {nd['p50']} p95 {nd['p95']} "
                             f"p99 {nd['p99']} max {nd['max']} | p={x['empirical_p']}")
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI
def cmd_run(args: argparse.Namespace) -> int:
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    n = args.n_candidates or cfg["n_candidates"]
    cache_root = Path(args.cache_dir or REPO_ROOT / "data/feature_store/v2n")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.perf_counter()
    for market in args.markets:
        for seed in NULL_SEEDS[: args.seeds]:
            for kind in args.types:
                out = out_dir / f"null_{market}_{kind}_{seed}.json"
                if out.exists() and not args.force:
                    log(f"[skip] {out.name}")
                    continue
                if args.max_minutes and (time.perf_counter() - t_start) / 60 > args.max_minutes:
                    log("[stop] time budget reached")
                    return 0
                r = run_one(market, kind, seed, cfg, n, cache_root, out_dir)
                s = r["stats"]
                log(f"[{market} {kind} {seed}] passers={s['n_passers']} best_e={s['best_e']} best_t={s['best_t']} "
                    f"build={r['frame_build_s']}s search+stats={r['search_stats_s']}s rss={r['peak_rss_mb']}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    rep = build_report(Path(args.out_dir))
    out = Path(args.out_dir)
    (out / "null_calibration.json").write_text(json.dumps(rep, indent=1, sort_keys=True, allow_nan=False), encoding="utf-8")
    (out / "null_calibration.md").write_text(render_md(rep), encoding="utf-8")
    log(f"wrote {out / 'null_calibration.json'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", default=str(v2_probe.DEFAULT_CONFIG))
    r.add_argument("--markets", nargs="+", required=True)
    r.add_argument("--types", nargs="+", default=list(NULL_TYPES), choices=list(NULL_TYPES))
    r.add_argument("--seeds", type=int, default=3)
    r.add_argument("--n-candidates", type=int, default=None)
    r.add_argument("--out-dir", default=str(REPORT_DIR))
    r.add_argument("--cache-dir", default=None)
    r.add_argument("--max-minutes", type=float, default=0.0)
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_run)
    q = sub.add_parser("report")
    q.add_argument("--out-dir", default=str(REPORT_DIR))
    q.set_defaults(fn=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
